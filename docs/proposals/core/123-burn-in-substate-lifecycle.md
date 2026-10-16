# 123: Burn-In Substates — Make the Rollout Position a Typed, Enforced Field Instead of Hand-Maintained Prose

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `docs/proposals/README.md` (Status Key, the Open Proposals table, the "Burn-in — pending graduation & decommission" section), `docs/proposals/TEMPLATE.md` (metadata table), the seven `burn-in` proposal docs under `docs/proposals/core/` (084, 090, 094, 111, 113, 116, 120), new `tests/test_proposal_status_vocabulary.py`, the three proposal-status command docs `.claude/commands/{impl,plan-proposal,update-proposals}.md` and their guards |
| **Depends on** | 111 _(burn-in — the code-side config-flag lifecycle this mirrors on the doc-status side)_, 059 _(partial — the structured-proposal-metadata convention this extends)_ |
| **Derived from** | proposal-doc corpus, README tables, `maestro.production.toml` canary state, and git history via /propose, 2026-09-01 |

## Description

> Status updated to shipped on 2026-09-03 by /polish — see [README.md](../README.md#shipped) for the landing record.

The single `burn-in` proposal status is doing the work of three. A proposal is
labelled `burn-in` from the instant its last behaviour-code step merges until the
day its superseded logic is deleted — a span that covers three structurally
distinct rollout positions: **code landed but the observation window never
started** (no canary deployed), **the canary is live and the window is running**,
and **the flag has cut over to its target default and only decommission remains**.
The status column shows the same word for all three. The finer position is
recoverable only by reading a hand-maintained prose bullet in the "Burn-in —
pending graduation & decommission" section — and those bullets drift from reality,
which is why the git history carries repeated manual re-sync commits.

This is the *same class* the taxonomy already tried to fix once. Commit
`038caefa` ("introduce burn-in proposal status, separate from partial") split
`burn-in` out of `partial` precisely because one label was conflating "still
building" with "done, now burning in." That split solved the front half and
recreated the identical problem one notch to the right: `burn-in` now conflates
the three positions *inside* the burn-in window. The remedy so far has been more
prose (a dedicated section, #3557) plus periodic hand reconciliation
(`36759320`, `0ff7f2b7`, dedicated status-only PRs #3607) — a catcher that
re-syncs the drift after the fact rather than a structure that prevents it.

This proposal makes the rollout position a **typed, closed-vocabulary field**
carried by the `Status` token itself — `burn-in:ready`, `burn-in:observing`,
`burn-in:graduated` — and binds it with one deterministic, zero-LLM-cost doc-lint
that (a) rejects any status outside the closed vocabulary, (b) forbids a bare
`burn-in` with no substate, and (c) requires the README Open Proposals column to
equal each proposal doc's own `Status` metadata. The end state: the Open
Proposals table is self-describing (a reader sees `burn-in:ready` vs
`burn-in:observing` at a glance, without parsing prose); the substate lives in
exactly one canonical place; and status drift fails CI loudly instead of being
reconciled by hand every few weeks.

## What landed

- **Step 1 — the typed burn-in vocabulary.** `docs/proposals/README.md` (Status
  Key) defines the three substates (`burn-in:ready`, `burn-in:observing`,
  `burn-in:graduated`) with their one-line entry criteria and the rule that a
  `burn-in` status must carry a substate suffix; `docs/proposals/TEMPLATE.md`
  points the metadata `Status` row at the closed vocabulary and shows the
  `burn-in:<substate>` token shape.
- **Step 2 — the seven existing burn-in proposals reconciled** (084, 090, 094, 111,
  113, 116, 120) to a typed substate in both their doc metadata and the README Open
  Proposals column, grounded in each proposal's `maestro.production.toml` canary
  state (084/090/113/116 → `burn-in:observing`, 094/111 → `burn-in:graduated`,
  120 → `burn-in:ready`).
- **Step 3 — the status-vocabulary and README↔doc binding lint**
  (`tests/test_proposal_status_vocabulary.py`) — a read-only doc-lint asserting the
  closed status vocabulary, the no-bare-`burn-in` rule, and the number-keyed
  README↔doc status binding, each with a seeded negative control.

## What is missing

- **Step 4 — relieve the burn-in prose section** of its position-carrying duty.

## Relevant Findings

- `docs/proposals/README.md` (Status Key) — defines `burn-in` as "Partially
  completed but **pending burn-in** for full completion. All behaviour-code has
  landed and is running behind its flag; what remains is the burn-in observation
  window, then graduating the flag … and decommissioning." The definition itself
  spans start-to-finish of the window, so the label cannot distinguish a proposal
  that has not started observing from one that has finished and is decommissioning.
- `docs/proposals/README.md` (the "Burn-in — pending graduation & decommission"
  section) — carries the *actual* position of each burn-in proposal in free prose,
  one hand-written bullet apiece (e.g. proposal 120's bullet says "every flag
  default-off" — the tell that its window has not started; proposal 094's says
  "the default-on flip has landed" — the tell it has graduated). The position is
  real and load-bearing but lives only here, in a form no lint checks.
- `docs/proposals/README.md` (Open Proposals table) — the machine-shaped surface
  (one status token per row) shows a bare `burn-in` for all seven of 084, 090,
  094, 111, 113, 116, 120, discarding the distinction the prose section preserves.
- `docs/proposals/TEMPLATE.md` (metadata table) — the `| **Status** |` row is the
  single authored source of a proposal's status; it enumerates no vocabulary and
  offers no burn-in substate shape, so an author has nothing to copy and the
  README column is filled independently — the seam where drift enters.
- `tests/test_proposals_readme_shipped_order.py` (`_shipped_numbers`,
  `test_shipped_table_is_strictly_ascending`) — the house pattern for a
  deterministic, zero-LLM-cost lint over the README tables: slice a section by its
  headings, parse rows with an anchored regex, assert an invariant, and pin the
  detector with a negative control. The status-binding lint reuses this exact
  shape.
- `tests/test_proposal_no_stale_missing_section.py` (`_find_stale_missing_sections`)
  — the sibling checked-in-docs meta-test that reads `docs/proposals/**/*.md`
  directly (no `mem_fs`, no network, no subprocess) and fails on a
  self-contradictory section. Confirms the "read the doc tree, assert a property,
  seed a negative control" pattern is already the established way to enforce a
  corpus invariant.
- `tests/test_no_line_citations_in_docs.py` — the canonical corpus-wide doc-lint;
  proves CI already gates on properties of every proposal file, so adding a
  status-vocabulary gate is additive, not a new category of check.
- `maestro/flag_lifecycle.py` (`ExperimentalFlag`, `EXPERIMENTAL_FLAGS`,
  `audit_flags_report`) — proposal 111's **code-side** rollout lifecycle: each
  flag carries a typed `review_by` horizon and a machine-readable manifest, and
  `maestro audit-flags` reports position against it. The doc-status side has no
  equivalent typing; this proposal is its doc-side complement, reusing the same
  "typed field + deterministic report" philosophy rather than inventing a rival.
- `maestro.production.toml` (the proposal-090/113/116 canary sections and the
  absence of any proposal-120 canary) — the ground truth that the substates
  describe: 090/113/116 have their canary flags flipped on (observing), 084 has
  Context7 on with a recorded hold (observing), and 120's two flags
  (`pre_mint_generation_gate_enabled`, `self_model_external_anchor_enabled`) are
  absent, inheriting the base default-off (ready). The substate of a flag-gated
  proposal is corroborated by this file.
- No runtime module reads a proposal doc's `Status` field — the `maestro
  list-proposals` surface reads `EvolutionProposal` slice-plan state, not the
  markdown metadata. The blast radius of a status-token change is therefore
  bounded to `docs/proposals/` plus the one new test; no `Config` field, no
  loop behaviour, no trust-chain surface is touched.

## Production Evidence

The "production data" for a taxonomy concern is the live proposal corpus, the
committed README tables, the production canary overlay, and the git history — not
the runtime `data/` bind-mount, which holds no proposal-doc status. All reads
below are of the checked-in tree at the current HEAD; windows are stated per
datum.

**One label, three positions, seven proposals (Open Proposals table + the
burn-in prose section, current HEAD):**

| Proposal | Status column shows | Actual position (from the prose bullet + `maestro.production.toml`) | True substate |
|---|---|---|---|
| 084 | `burn-in` | Context7 canary on, Serena off, **hold recorded** 2026-07-11 | observing |
| 090 | `burn-in` | forced-arm canary **live in production** | observing |
| 094 | `burn-in` | **default-on flip landed**; "or close" decommission remains | graduated |
| 111 | `burn-in` | first collapse landed; remaining collapses aging to a `review_by` floor | graduated |
| 113 | `burn-in` | Phases A–C with the **production canary live**; base-default graduation pending | observing |
| 116 | `burn-in` | nine of ten steps landed, **canary live**; Step-10 deletion pending | observing |
| 120 | `burn-in` | **every flag default-off**, no canary; window not started | ready |

Seven proposals, one status token, three genuinely distinct rollout positions —
1 `ready`, 4 `observing`, 2 `graduated`. The column that a reader (or the
autonomous loop) scans first collapses all three to the same word.

**The distinction is real enough to have already caused a misread (this
session, 2026-09-01):** answering "is proposal 120 running in production?"
required reading `maestro.production.toml`, the proposal's own status prose, and
the flag-lifecycle manifest across three question-and-answer rounds — because the
status token alone (`burn-in`) could not distinguish 120 (`ready`, never
canaried) from 116 (`observing`, canary live), even though both wore the same
badge.

**The prose-carried position drifts and is reconciled by hand (git history):**

- `038caefa` — "introduce burn-in proposal status, separate from partial": the
  taxonomy was split once already for this exact conflation class.
- `697709f0` (PR #3557) "separate-burn-in-proposals": a whole prose section was
  hand-added to carry the position the status column could not.
- `36759320` — "reconcile proposal statuses with the shipped tree": a **bulk
  manual** re-sync of statuses against reality.
- `0ff7f2b7` — "fix stale (partial) status on proposal 120 Depends-on row": a
  point stale-status fix.
- `1b5ff311` (PR #3607) — "reconcile status to burn-in after Step 8 landed": a
  dedicated, status-only PR to move one proposal's label.

Five commits over the recent history whose entire job was to hand-correct a
status/position that no test pins.

## Prior Point Fixes (whack-a-mole ledger)

Every control on proposal-status accuracy is either a free-text carrier or a
manual re-sync. None types the field or binds the README column to the doc, which
is why the position keeps drifting and the label keeps under-describing.

| Identifier | What it patched | Why the class survived |
|---|---|---|
| `038caefa` (introduce `burn-in`, split from `partial`) | Broke one conflated status into two | Recreated the identical conflation one notch right: `burn-in` now spans three positions |
| `697709f0` / #3557 (separate burn-in section) | Added prose to carry the position the token could not | Prose is unchecked and drifts; the token still shows a bare word |
| `36759320` (bulk "reconcile proposal statuses") | Re-synced statuses to reality by hand | A periodic manual pass, not a structure; drift returns after it |
| `0ff7f2b7` (stale-status point fix) | Fixed one stale status row | One row; the next status change re-opens the gap |
| `1b5ff311` / #3607 (status-only reconcile PR) | Moved one proposal's label after a step landed | Every step landing needs another hand PR; nothing enforces the token↔reality tie |

**Retired / subsumed by this design:** the periodic manual "reconcile proposal
statuses" pass and the stale-status point fixes are retired by the README↔doc
binding lint (Step 3) — a drift now fails CI instead of waiting for a human
sweep. The burn-in prose section is *relieved* (not deleted): it keeps its
remaining-work / tripwire narrative but stops carrying the rollout *position*,
which moves to the typed token (Step 4).

## Implementation Plan

Each step is docs-or-test only and independently mergeable; the vocabulary is
defined first, the corpus is made conformant next, the enforcing lint lands on
the already-green tree, and the subsumed prose is relieved last. This proposal
changes no runtime behaviour, so there is no `Config` flag to gate — rollback is
a plain revert of the docs and the one test.

### Phase A — Define the typed vocabulary

1. [x] **Define the three burn-in substates in the Status Key and TEMPLATE.**
   `docs/proposals/README.md` (Status Key), `docs/proposals/TEMPLATE.md`
   (metadata table) — add `burn-in:ready` (behaviour-code landed and gated;
   observation window **not started** — no canary deployed / flag default-off
   everywhere), `burn-in:observing` (canary/flag **live**, bounded window running,
   evidence accruing against tripwires), and `burn-in:graduated` (flag cut over to
   its target default or evidence in; only **decommission** of superseded logic
   remains), each with its one-line entry criterion, and state the rule that a
   `burn-in` status MUST carry a substate suffix. Pure docs; the tree stays green
   because no lint exists yet.
   - *Depends on:* none.
   - *Acceptance:* the Status Key defines exactly the three substates with entry
     criteria and the no-bare-`burn-in` rule; TEMPLATE.md shows the
     `burn-in:<substate>` token shape in its metadata-table guidance.

### Phase B — Make the corpus conformant

2. [x] **Reconcile the seven existing burn-in proposals to a typed substate.**
   the `Status` metadata row of `docs/proposals/core/{084,090,094,111,113,116,120}.md`
   and their rows in the `docs/proposals/README.md` Open Proposals table — set each
   to the substate its canary state dictates (084/090/113/116 → `burn-in:observing`,
   094/111 → `burn-in:graduated`, 120 → `burn-in:ready`), keeping the doc metadata
   and the README column identical. Documentation-only reconciliation.
   - *Depends on:* 1.
   - *Acceptance:* every one of the seven proposals shows the same
     `burn-in:<substate>` token in both its doc metadata and the README column,
     matching the position recorded in its burn-in prose bullet.

### Phase C — Enforce the tie deterministically

3. [x] **Add the status-vocabulary and README↔doc binding lint.** new
   `tests/test_proposal_status_vocabulary.py` — a checked-in-docs meta-test (same
   family as `tests/test_no_line_citations_in_docs.py`: no `mem_fs`, no network,
   no subprocess) that parses every proposal doc's `| **Status** |` token and the
   README Open Proposals status column and asserts: (a) every status is in the
   closed vocabulary (the base statuses plus the three burn-in substates); (b) no
   status is a bare `burn-in`; (c) for every numbered Open-Proposals row the
   README status equals the linked doc's own `Status` metadata (bound on the
   proposal **number**, not the title). Each assertion carries a seeded negative
   control (a typo status, a bare `burn-in`, a README/doc mismatch) proving the
   detector bites, plus a non-vacuity guard so an empty parse fails loudly.
   - *Depends on:* 1, 2.
   - *Acceptance:* the lint passes on the reconciled tree; a seeded bad-vocabulary
     token, a seeded bare `burn-in`, and a seeded README-vs-doc status mismatch
     each fail it.

### Phase D — Relieve the subsumed prose

4. [x] **Retire the position-carrying duty of the burn-in prose section.**
   `docs/proposals/README.md` (the "Burn-in — pending graduation & decommission"
   section) — trim each bullet to its remaining-work / tripwire narrative and drop
   the sentence fragments that restate the rollout *position* (now carried by the
   typed token), and record in the section preamble that the README↔doc binding
   lint — not a periodic hand pass — keeps the column and the docs in sync.
   - *Depends on:* 3.
   - *Acceptance:* no burn-in bullet restates its proposal's rollout position as
     free prose (the token is authoritative); the section names the binding lint as
     the sync mechanism; the Step-3 lint and the full doc-lint suite stay green.

## Testing Strategy

All tests are read-only over the checked-in `docs/proposals/` tree, matching the
existing `tests/test_no_line_citations_in_docs.py` /
`tests/test_proposals_readme_shipped_order.py` /
`tests/test_proposal_no_stale_missing_section.py` meta-tests — no `mem_fs`, no
real network, no subprocess, no `sleep`, per CLAUDE.md.

- **Step 3 — the deterministic enforcement anchor (`tests/test_proposal_status_vocabulary.py`):**
  - `test_status_vocabulary_is_closed` — every proposal doc and every Open
    Proposals row uses a status from the closed set; a seeded `burn-im` typo fails.
  - `test_every_burn_in_carries_a_substate` — no bare `burn-in`; a seeded bare
    token fails.
  - `test_readme_status_matches_doc_status` — the README status column equals the
    linked proposal doc's `Status` metadata, keyed by proposal number; a seeded
    mismatch (README `burn-in:observing` vs doc `burn-in:ready`) fails.
  - A non-vacuity guard asserting the parse found the expected order-of-magnitude
    row count, so a regex that silently matches nothing fails instead of passing
    vacuously (the guard shape `test_shipped_table_is_strictly_ascending` uses).
- **Steps 1, 2, 4** are documentation edits with no independent test; their
  correctness is pinned transitively by the Step-3 lint (a wrong reconciliation or
  an undefined substate token fails it) and by the pre-existing
  `tests/test_no_line_citations_in_docs.py` and
  `tests/test_proposal_no_stale_missing_section.py`.

The proposal is falsified if, after Step 3 lands, status/position drift still
requires a manual reconciliation commit (the binding did not actually pin the
tie), or if a burn-in proposal legitimately fits none of the three substates (the
vocabulary is not exhaustive over the window).

## Risk Assessment

- **A burn-in proposal that fits none of the three substates.** The three
  positions are exhaustive over a bounded window — before it starts (`ready`),
  during it (`observing`), after cut-over (`graduated`) — so a proposal that fits
  none is a signal the window model itself is unclear, which the typed field
  surfaces rather than hides. Mitigation: the substate is author-asserted and
  human-reviewed at PR time exactly as the base status is today; the lint checks
  vocabulary and README↔doc agreement, not a semantic judgement it cannot make.
- **The binding lint is brittle if it keys on titles.** A README row cites a
  proposal by linked title, which can differ cosmetically from the doc's H1.
  Mitigation: Step 3 binds on the proposal **number** parsed from the row link and
  the filename, never on title text.
- **Reconciliation churn on the seven existing proposals.** Step 2 rewrites seven
  status tokens in fourteen places. Mitigation: it is documentation-only, lands in
  one reviewable PR, and is immediately pinned by Step 3 so it cannot silently
  regress; every non-doc environment is unaffected.
- **Trust chain.** Untouched — no approval, sandbox, tool-gating, public-API, or
  `Config` surface changes; no runtime module reads the doc `Status` field, so the
  change cannot alter loop behaviour. The only writes are to `docs/proposals/` and
  one new test.
- **Rollback posture.** Docs plus one deterministic test; rollback is a plain
  revert with no schema, no state file, and no flag. Because the change is
  behaviour-neutral, the flag-lifecycle dark-launch discipline does not apply —
  there is nothing to run behind a switch.

## Out of scope

- **Auto-deriving the substate from `maestro.production.toml`.** A test that
  cross-checks an asserted `burn-in:observing` against a named canary flag being
  present would over-fit: the graduated proposals (094, 111) reach their target
  via a base-default flip with no production-only canary, so no single flag maps
  cleanly. The substate stays author-asserted and human-reviewed; a
  flag-presence cross-check is a possible later enhancement, not this increment.
- **Substating any status other than `burn-in`.** `partial`, `proposed`,
  `approved`, and `in-progress` are single positions and need no refinement; this
  proposal types only the status that demonstrably conflates.
- **Auto-advancing a substate over time.** Moving a proposal `ready → observing →
  graduated` remains a human edit tied to the real rollout action (deploying the
  canary, cutting over the flag); this proposal makes the field legible and
  enforced, not self-updating.
- **The income-track service-proposal status shape.** `income/` proposals may use
  a service-flavoured layout; their statuses stay in the same closed vocabulary but
  their burn-in lifecycle (rare for a docs-only offering) is not specially modelled
  here.
- **Retiring the burn-in prose section entirely.** It keeps its genuine
  remaining-work and tripwire narrative; only its duty to carry the rollout
  *position* is retired (Step 4).
