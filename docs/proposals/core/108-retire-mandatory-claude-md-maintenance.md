# 108: Retire Mandatory CLAUDE.md Maintenance and Its Conflict Machinery

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/prompts/defaults.py`, `maestro/evolution.py`, `maestro/planner.py`, `maestro/brevity_conflict.py`, `maestro/mechanical_reconcile.py`, `maestro/vcs.py`, `maestro/session_landing.py`, `maestro.toml`, `CLAUDE.md`, tests |
| **Depends on** | _(none)_ |
| **Derived from** | code + git history via /propose, 2026-07-30 (no live production data — see Production Evidence) |

## Description

> Status updated to shipped on 2026-08-01 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Maestro's own runtime never reads `CLAUDE.md`: the SDK options builder
(`maestro/sdk.py`, `_build_sdk_options`) never sets `setting_sources`, and the
identity/system prompt on every call comes from `Consciousness.get_identity_prompt()`,
not the file. Yet every evolution is *required* to edit it, and the file is
*separately* bounded by a hard size cap. Those two forces are permanently
opposed — the mandate forces bullets to be added, the cap forces them to be
trimmed — and neither is grounded in a runtime consumer. The friction between
them has spawned a growing pile of machinery whose only job is to absorb the
collision: a per-turn "trim one word under the cap" treadmill and two
deterministic merge-conflict reconcilers wired into the tend path.

This proposal decouples documentation maintenance from the evolution gate.
The mandatory "every evolution MUST update `CLAUDE.md`" requirement is removed
(reverting to `CLAUDE.md`'s own already-stated policy — "update only if an
invariant changed"), the hard brevity-cap regression test is removed, and the
merge-conflict machinery that cap-collisions justified is retired as dead code
once its only trigger is gone.

The end state: doc updates are discretionary and driven by real invariant
changes; there is no forced-growth pressure fighting a hard ceiling; and the
`brevity_conflict` / `mechanical_reconcile` / `_try_mechanical_cap_reconcile`
subsystem — built to dissolve a conflict magnet that only exists because of the
mandate — is deleted rather than maintained.

## Production Evidence

No live `data/` directory was reachable in this environment (no Docker host),
so the evidence below is git history and code only; the class is quantified
from commit messages rather than runtime stores.

- **The trim treadmill.** 61 commits in repo history carry a `CLAUDE.md`
  message *and* a brevity/cap/trim/compress token (window: full `main`
  history). Representative spans:

  ```
  40e4aecd docs: trim one redundant word in CLAUDE.md to hold word-count cap
  a4793061 maestro: trim one word to clear CLAUDE.md brevity word-count cap
  ef3c180d Fix quality gate: trim CLAUDE.md to satisfy word-count cap
  6aed4e36 maestro: trim CLAUDE.md one word under brevity cap to clear failing quality gate
  ab10f5fd Compress CLAUDE.md back to its stated contract and re-tighten the caps
  ```

  Every one of these is pure friction: a commit whose entire content is moving
  the file back under a ceiling it was forced over by an added bullet.

- **The conflict magnet.** The resolver's own introducing commit names the
  problem: `479e5522 "Deterministic brevity-cap conflict auto-resolver —
  dissolve the tend-path conflict magnet"`. Two concurrent evolution branches
  each add-and-trim, then collide on the single integer cap constant in the
  brevity test on merge.

## Prior Point Fixes (whack-a-mole ledger)

Each entry patched a symptom of the mandate/cap tension without removing the
source. The design retires all of them.

| Identifier | What it patched | Why the class survived |
|---|---|---|
| `62fa5f0b` | Built the mechanical reconcile-and-land actuator so cap collisions land without an LLM turn | Absorbed the conflict; the mandate that produces it stayed |
| `3873aa15` | Handled the lockstep two-cap brevity collision in the reconciler | A new collision shape, not a new root |
| `af5242b9` | Fixed the reconciler: union resolution + fire on real merges | Reconciler correctness fix on top of the reconciler |
| `1c988c3a` | Gated the mechanical cap-reconcile before pushing | Added a guard to the actuator pile |
| `479e5522` | Added the deterministic `brevity_conflict` resolver to "dissolve the conflict magnet" | Treated the magnet as permanent instead of removing it |
| `ab10f5fd` | Whole compression pass to reclaim cap headroom | Bought headroom the next N evolutions spend again |
| 61× trim commits (`40e4aecd`, `a4793061`, `ef3c180d`, …) | Trimmed a word/line back under the cap | The mandate re-fills the headroom every cycle |

Retired by this proposal: the two reconciler modules, the tend-path actuator,
the cap regression test, and the mandate prose that drives the trim treadmill.

## Relevant Findings

- `maestro/sdk.py` (`_build_sdk_options`) — never sets `setting_sources`, so
  the Claude Agent SDK does not auto-load `CLAUDE.md` into Maestro's own agent
  context; the file is not a runtime input to the loop.
- `maestro/prompts/defaults.py` (`EVOLUTION_PROMPT`) — "REQUIREMENT: Every
  evolution MUST update CLAUDE.md" plus the doc-only verification-language
  exemption.
- `maestro/evolution.py` (`propose_plan` prompt) — `DOCUMENTATION: Include
  "CLAUDE.md" in files_affected`; (implement prompt) — `DOCUMENTATION
  REQUIREMENT: You MUST update CLAUDE.md`; (`self_review` prompt) — the
  `CLAUDE.md` checklist item. Three separate restatements of one mandate.
- `maestro/planner.py` — instructs the planner to make the final step a doc
  update "(CLAUDE.md, README.md, or docstrings)".
- `CLAUDE.md` (Evolution Protocol) — states the *opposite* of the prompts:
  "Update CLAUDE.md *only* if an invariant changed." The mandate and the file's
  own stated policy contradict each other.
- `tests/test_claude_md_brevity.py` (`_MAX_LINES`, `_MAX_WORDS`,
  `_MAX_BULLET_CHARS`) — hard caps; its own docstring states the caps sit "just
  above the file's post-compression size, so the headroom is deliberately
  tight" — i.e. calibrated to fight the mandate.
- `maestro/brevity_conflict.py` (`resolve_brevity_cap_conflict`) and
  `maestro/mechanical_reconcile.py` (`classify_reconcilable_conflict`) — exist
  solely to reconcile cap collisions in `tests/test_claude_md_brevity.py`;
  their only trigger is a single-integer cap collision in that one file.
- `maestro/evolution.py` (`EvolutionEngine._try_mechanical_cap_reconcile`) — the
  tend-path actuator called from `_tend_single_pr`; short-circuits unless the
  sole conflicted path is the brevity test file.
- `maestro/vcs.py` (`classify_reconcilable_conflict` reference) and
  `maestro/session_landing.py` (module docstring) — carry references to the
  reconciler machinery that become stale once it is deleted.
- `tests/test_prompts.py` (`test_evolution_prompt_mentions_claude_md`,
  and the surviving-guidance assertion) and `tests/test_evolution.py`
  (the `Include "CLAUDE.md" in files_affected` assertion) — pin the mandate
  in place; they must change with it.

## Implementation Plan

1. [x] **Gate the mandatory-update prose behind a default-off flag.**
   `maestro/prompts/defaults.py`, `maestro/evolution.py`, `maestro/planner.py`,
   `maestro.toml` — introduce `[loop] require_claude_md_update` (default
   `false`). When false, the proposal/implement/self-review/planner prompts omit
   the "MUST update CLAUDE.md" mandate and instead carry the soft, conditional
   guidance already stated in the Evolution Protocol ("update only if an
   invariant changed"). The doc-only scope-gate exemption in
   `maestro/planner.py` (`check_plan_scope`) is unchanged.
   - *Depends on:* none.
   - *Acceptance:* with the flag false, none of the four prompt strings contain
     an unconditional "MUST update CLAUDE.md"; with it true, the legacy mandate
     text is present — asserted in `tests/test_prompts.py` and
     `tests/test_evolution.py`.

2. [x] **Remove the hard brevity-cap regression test.**
   `tests/test_claude_md_brevity.py` — delete the file. With the mandate off,
   `CLAUDE.md` no longer faces forced-growth pressure, so the tight-headroom cap
   loses its purpose; prompt-budget discipline reverts to the soft
   document-discipline norm and the read-only `maestro lint` drift diagnostic.
   - *Depends on:* 1.
   - *Acceptance:* the file is gone and the full suite passes with no reference
     to `_MAX_LINES` / `_MAX_WORDS` / `_MAX_BULLET_CHARS` remaining.

3. [x] **Retire the cap-collision reconciler subsystem.**
   `maestro/brevity_conflict.py`, `maestro/mechanical_reconcile.py`,
   `maestro/evolution.py` (`_try_mechanical_cap_reconcile` and its
   `_tend_single_pr` call site), `tests/test_brevity_conflict.py`,
   `tests/test_mechanical_reconcile.py` — delete. Their sole trigger is a
   single-integer cap collision in the file removed in step 2, so they are dead
   code once it is gone; `_tend_single_pr` falls through to its existing
   LLM-routing path for any conflict, exactly as it does today when the
   reconciler declines.
   - *Depends on:* 2.
   - *Acceptance:* `grep` finds no import or call of `resolve_brevity_cap_conflict`,
     `classify_reconcilable_conflict`, or `_try_mechanical_cap_reconcile` in
     `maestro/`; the suite passes.

4. [x] **Prune the stale references and invariants.**
   `maestro/vcs.py`, `maestro/session_landing.py`, `CLAUDE.md` — remove the
   docstring/invariant references to the retired machinery (the
   "Mechanical cap-reconcile actuator" bullet and the brevity-cap-test pointer
   in the document-discipline note), and delete the now-obsolete
   `test_claude_md_brevity.py` reference. The document-discipline *norm* prose
   stays as soft guidance.
   - *Depends on:* 3.
   - *Acceptance:* no source or doc file names `brevity_conflict`,
     `mechanical_reconcile`, or `test_claude_md_brevity`; `mypy` and `ruff`
     are clean.

## Testing Strategy

- **Step 1** — `tests/test_prompts.py` and `tests/test_evolution.py` are
  rewritten to assert the flag semantics: flag-false omits the unconditional
  mandate and keeps the soft conditional guidance; flag-true restores the legacy
  text. Config default is asserted `false`. All prompt-string assertions use the
  in-memory constants — no I/O.
- **Step 2** — deletion is verified by suite green with the file absent; a
  targeted check confirms no surviving symbol imports the removed cap constants.
- **Step 3** — the existing `_tend_single_pr` conflict-routing tests
  (`tests/test_evolution.py` / tend coverage) must still pass with the
  reconciler removed, proving a brevity-cap-shaped conflict now routes through
  the ordinary LLM path with no regression. Any test that mocked the reconciler
  is updated to the fall-through expectation. All git/subprocess interaction
  stays mocked per the project's no-real-subprocess rule; file I/O uses
  `mem_fs`.
- **Step 4** — `tests/test_no_line_citations_in_docs.py` continues to pass with
  this proposal present (citation discipline), and a doc-drift check confirms
  the retired symbols are unreferenced.

## Risk Assessment

- **CLAUDE.md drifts stale or bloats.** The mandate and cap were the two forces
  keeping the file current *and* small. Removing both risks drift. Mitigations:
  the conditional norm ("update only if an invariant changed") remains in the
  Evolution Protocol; the read-only `maestro lint` drift diagnostic
  (`maestro/cli.py`) stays; human PR review still catches architecture changes
  that warrant a doc update. Crucially, the *forced-growth* pressure is what the
  cap was mostly fighting — with the mandate gone, organic bloat from evolution
  churn largely disappears, so the cap's removal is low-risk in practice.
- **Prompt-budget cost in human sessions.** `CLAUDE.md` is loaded by human
  Claude Code sessions (not Maestro's loop), so unbounded growth would cost
  those sessions. If the operator wants to retain a ceiling, a *loose* cap with
  no reconciler (a soft regression test only) is a viable middle ground — but
  the proposal as scoped removes the cap entirely; retaining a loose cap is a
  one-line reviewer choice at PR time.
- **Rollback posture.** Step 1 is fully flag-gated (`[loop]
  require_claude_md_update`, default `false`); flipping it `true` restores the
  legacy mandate without a revert. Steps 2–4 are deletions of code whose only
  trigger is removed first, so an interrupted rollout (e.g. steps 1–2 landed,
  3–4 not) leaves the loop working — the reconciler simply never fires because
  no cap collision can occur. No state-file or schema migration is involved.
- **Trust chain.** Untouched — none of the removed code participates in
  approvals, sandboxing, action-gating, or the merge-truth / CI-truth
  chokepoints.

## Falsifiability

If the proposal is right, two measurable classes go to ~zero after it lands:
future commits matching the "trim CLAUDE.md under cap" pattern (61 historical),
and tend-path brevity-cap-collision reconciler firings. The design fails if
`CLAUDE.md` grows materially (e.g. >20% in word count over the next N
evolutions) and that growth measurably degrades human-session prompt budget —
which would show the cap was doing real organic-drift work the mandate was not
responsible for, and argue for retaining a loose cap.

## Out of scope

- **The coined-term lexicon** (`maestro/lexicon.py`, `tests/test_lexicon.py`,
  `tests/test_lexicon_coverage.py`, proposal 101). It has a `CLAUDE.md`
  coverage guard but is a broader maintained-vocabulary system with
  `docs/lexicon.md` as its source of truth and a `maestro lint` CLI; retiring it
  is a distinct decision. Left intact.
- **`tests/test_no_line_citations_in_docs.py`.** It governs `docs/proposals`
  citation discipline (and this proposal's own CI), not `CLAUDE.md`. Retained.
- **The doc-only scope-gate exemption** (`check_plan_scope`). Independent of the
  mandate; keeps multi-file doc-only plans from tripping the verification-language
  gate. Retained.
