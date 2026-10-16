# 063: Retire the standalone comment processor

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro_review/processor.py`, `maestro_review/processor_prompt.py`, `maestro_review/cli.py`, `maestro_review/pyproject.toml`, `maestro_review/markers.py`, `maestro_review/CLAUDE.md`, `maestro/evolution.py`, `maestro/agent.py`, `.claude/commands/process-comments.md`, tests |
| **Derived from** | `maestro_review/CLAUDE.md` (the processor "no longer runs in CI") |
| **What landed** | Steps 1–4 — `_format_bug_comments_problem` (`maestro/agent.py`) folds the retired comment-processor's failing-test-first validation and broken-test-repair discipline into the rendered remediation directive (`VALIDATION_PROMPT` / `FIX_PROMPT` / `FINAL_GATE_FIX_PROMPT` substance), and now also renders the disagreement-reply path (`_DISAGREEMENT_MARKER` wording, "Could not prove this defect") and the create-a-tracked-issue-on-failed-fix instruction ("Tracked in #N"); covered by `tests/test_pr_feedback_routing.py::test_bug_comments_problem_carries_validation_and_repair_discipline` and `::test_bug_comments_problem_carries_disagreement_and_tracked_issue_paths`. Step 3 makes `_BUG_COMMENT_TERMINAL_MARKERS` (`maestro/evolution.py`) the authoritative core-side list — the "keep in sync with `CommentProcessor._TERMINAL_MARKERS`" comment is gone, replaced by prose declaring the list canonical for the autonomous path, and `tests/test_tend_bug_comments.py::test_historical_terminal_markers_excluded` pins every historical terminal marker to a literal so dropping one fails the test. Step 4 repoints `.claude/commands/process-comments.md` off processor internals — it no longer cites `processor.py`, `processor_prompt.py`, `maestro-process-comments`, the "CI agent" framing, or any processor-only symbol, instead anchoring on surviving symbols (`maestro_review/markers.py` for `_DISAGREEMENT_MARKER` / `_FIXED_LOCALLY_MARKER`, and `maestro/evolution.py::_BUG_COMMENT_TERMINAL_MARKERS` + `_eligible_bug_comments`); the doc-coverage test moved out of the `maestro_review/` suite to the core suite at `tests/test_process_comments_command_doc.py`, where it now asserts the command lists every `_BUG_COMMENT_TERMINAL_MARKERS` entry (sourced from `maestro.evolution`, not `CommentProcessor`) and adds `test_command_doc_cites_no_retired_processor_internals`, a guard that fails if any retired processor reference resurfaces (PR #2546). Steps 5–8 — the deletions landed (PR #2603): the `process_comments` CLI / `maestro-process-comments` entry point removed from `maestro_review/cli.py` and `maestro_review/pyproject.toml` (Step 5); `maestro_review/processor.py` + `processor_prompt.py` deleted (Step 6); the four processor-only test files removed and the two remaining importers (`test_bug_hunt_review.py`, `test_tend_bug_comments.py`) repointed off `processor.py` (Step 7); and `maestro_review/CLAUDE.md` updated to record that comment handling now lives in the core loop (`tend_evolution_prs`) and the `/process-comments` command, with `markers.py` shared by the reviewer's escalation path (Step 8). |
| **What is missing** | Nothing — all eight steps shipped (Steps 1–4 in PR #2546, Steps 5–8 in PR #2603). |

## Description

> Status updated to shipped on 2026-06-17 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

The AI-bug-review-comment fixing workflow exists in three places today, two of
which are live and one of which is vestigial. The vestigial one is the
standalone `CommentProcessor` agent in `maestro_review/processor.py` — a ~63 KB
module that claims a single unresolved finding, validates it with a failing
test, fixes it with retry, pushes with rebase, and replies. It once ran in CI;
that job has been removed, and the module now survives only as the engine behind
the local `/process-comments` slash command and as a direct CLI
(`maestro-process-comments`). Meanwhile the two surfaces that actually drive the
project's day-to-day comment handling — Maestro's autonomous loop and the
plan-aware `/process-comments` command — each already re-implement the same
validate→fix→reply discipline against the same GraphQL thread shape and the same
terminal markers. The standalone agent is a third copy of logic nobody invokes
on its own, kept in lockstep by hand-maintained "keep in sync" comments.

This proposal removes the standalone agent and makes its two consumers
self-sufficient. The autonomous path (`tend_evolution_prs` → a remediation task)
becomes the canonical *unattended* comment handler, and the `/process-comments`
slash command becomes the canonical *human-paced* one. Anything the agent did
that those two paths do not yet do — chiefly the failing-test-first validation
discipline, the "could not prove this defect" disagreement reply that lets the
reviewer escalate, and the create-a-tracked-issue-on-failed-fix path — is folded
into the remediation-task prompt so the autonomous path reaches full parity
before the agent is deleted.

The end state: `maestro_review/` keeps only the reviewer (`maestro-review`) plus
the genuinely shared pieces the reviewer already imports (`markers.py`,
`PRReviewer._find_matching_issue`); `processor.py`, `processor_prompt.py`, the
`process_comments` CLI, the `maestro-process-comments` entry point, and the
processor-only test files are gone; and the two remaining surfaces no longer cite
processor internals that have ceased to exist.

## Relevant Findings

- `maestro_review/processor.py` (`CommentProcessor.process`) — the standalone
  agent: picks one unresolved finding, claims it with a reply, validates
  (`_validate_comment`), fixes with retry (`_apply_fix`), pushes
  (`_push_with_rebase`), and replies. This is the logic being retired.
- `maestro_review/CLAUDE.md` — records that the processor "no longer
  runs in CI — the `process-comments` job and the standalone
  `comment-processor.yml` workflow have been removed"; it is "retained for direct
  use and is the engine behind the local `/process-comments` slash command."
  Confirms the module has no autonomous caller of its own.
- `maestro_review/cli.py` (`process_comments`) — the only entry point that
  constructs `CommentProcessor`; lazily imports it to avoid a cycle.
  `maestro_review/pyproject.toml` (`[project.scripts]`) exposes it as
  `maestro-process-comments`.
- `maestro/evolution.py` (`tend_evolution_prs`, `_route_bug_comments`,
  `_eligible_bug_comments`, `_AI_BUG_COMMENT_RE`, `_BUG_COMMENT_TERMINAL_MARKERS`)
  — the autonomous path already detects unresolved 🐛 threads mechanically and at
  zero SDK cost, skipping resolved/outdated/terminally-marked threads. A comment
  in this file explicitly keeps `_BUG_COMMENT_TERMINAL_MARKERS` "in sync" with
  `CommentProcessor._TERMINAL_MARKERS` — the hand-maintained duplication this
  proposal eliminates one side of.
- `maestro/agent.py` (`_route_pr_feedback_to_pending_requests`,
  `_format_bug_comments_problem`, `_PRRemediationAccumulator`) — routes detected
  bug comments into a `PendingPRRequest` and renders the remediation prompt. The
  prompt tells the task to fetch threads, commit (not push — tend is the sole
  pusher), and resolve threads via `resolveReviewThread`. It does **not** yet
  mandate the failing-test-first validation, the disagreement reply, or the
  create-an-issue-on-failure path that `processor.py` performs.
- `maestro/consciousness.py` (`PendingPRRequest`, `SelfModel.pending_pr_requests`)
  — the FIFO queue the routed remediation task lands in; consumed by the
  `_run_task_phase` priority order.
- `maestro/vcs.py` (`SubprocessCodeHost.get_pr_review_threads`,
  `reply_to_review_comment`) — the core-side GraphQL fetch and reply helpers the
  remediation task already has available; parity does not require new VCS code.
- `maestro_review/review.py` (`PRReviewer._find_matching_issue`,
  `_DISAGREEMENT_MARKER`) — the reviewer imports `_DISAGREEMENT_MARKER` from
  `markers.py` to detect a processor's disagreement and escalate to an issue, and
  exposes `_find_matching_issue` for issue dedup. Both are shared with the
  reviewer and **must survive** the processor's removal.
- `maestro_review/processor_prompt.py` (`VALIDATION_PROMPT`, `FIX_PROMPT`,
  `REVISION_PROMPT`, `FINAL_GATE_FIX_PROMPT`, `FINAL_GATE_REVISION_PROMPT`,
  `CONFLICT_RESOLUTION_PROMPT`) — the durable prompt text whose *substance*
  (validate-with-a-failing-test, fix minimally, repair tests the fix broke) is
  the thing of value; it should be lifted into the remediation prompt, not lost.
- `.claude/commands/process-comments.md` — already a full, plan-aware
  re-implementation of the agent's workflow (validate → fix → push-first →
  reply, with the `_FIXED_LOCALLY_MARKER` and `_DISAGREEMENT_MARKER` wording). It
  cites processor-only symbols (`_AI_COMMENT_RE`, `_EMOJI_SEVERITY`,
  `_TERMINAL_MARKERS`, `VALIDATION_PROMPT`, `FIX_PROMPT`, `_push_with_rebase`,
  "the CI agent") that will no longer exist and must be repointed.
- `tests/test_maestro_review/test_processor.py`,
  `tests/test_maestro_review/test_processor_single_finding.py`,
  `tests/test_maestro_review/test_bug_hunt_processor_deep.py`,
  `tests/test_maestro_review/test_bug_hunt_processor_git.py` — the
  processor-only test surface (~4500 lines) deleted alongside the module.
- `tests/test_process_comments_command_doc.py`
  (already repointed in Step 4 — imports `_BUG_COMMENT_TERMINAL_MARKERS` from
  `maestro.evolution`, not `CommentProcessor`; lives in the core suite),
  `tests/test_maestro_review/test_bug_hunt_review.py`
  (`TestIsAiReviewCommentNoneBody.test_none_body`, a lazy
  `from maestro_review.processor import CommentProcessor`), and
  `tests/test_tend_bug_comments.py` (imports
  `processor._EMOJI_SEVERITY` and asserts
  `_BUG_COMMENT_TERMINAL_MARKERS == CommentProcessor._TERMINAL_MARKERS`) — the
  test files touching `processor.py`. The first already repointed in Step 4 (per
  its note above); the remaining two still import from `processor.py` and are
  **not** processor-only, so they cannot simply be deleted — each must be
  repointed or trimmed in Step 7 so collection does not raise `ModuleNotFoundError`
  once the module is gone. `test_tend_bug_comments.py` lives in the **core** suite, so
  leaving it unhandled breaks the mandatory `pytest tests/ -v` gate, not merely
  the `maestro_review/` sub-suite.

## Implementation Plan

Order matters: bring the autonomous path to parity **first** (additive, behind
the existing `[loop] tend_bug_comments_enabled` flag), then repoint the slash
command, then delete the agent. An interrupted run after any step leaves a
working tree — the agent keeps functioning until the final deletion steps.

### Phase 1 — Bring the autonomous path to parity

1. [x] **Fold the processor's validation/fix discipline into the remediation
   prompt.** `maestro/agent.py` (`_format_bug_comments_problem`) — lift the
   substance of `processor_prompt.VALIDATION_PROMPT`/`FIX_PROMPT`/
   `FINAL_GATE_FIX_PROMPT` into the rendered remediation directive: for each
   thread, prefer a failing test that confirms the defect (static-analysis
   fallback only when a test is impractical), apply a minimal fix, and repair any
   pre-existing tests the fix breaks rather than weakening the fix.
   - *Depends on:* none.
   - *Acceptance:* a unit test asserts the rendered directive from
     `_format_bug_comments_problem` contains the failing-test-first instruction
     and the test-repair instruction for a sample bug-comment tuple.

2. [x] **Add the disagreement-reply and tracked-issue-on-failure instructions to
   the remediation prompt.** `maestro/agent.py` (`_format_bug_comments_problem`)
   — instruct the task to (a) reply with the `_DISAGREEMENT_MARKER` wording
   ("Could not prove this defect …") on a thread it cannot prove, so the next
   reviewer run escalates it to an issue, and (b) on a fix it cannot land, open a
   deduplicated tracked issue and reply "Tracked in #N" (already a recognized
   terminal marker). This closes the two behaviours the autonomous path lacks
   versus the agent.
   - *Depends on:* 1.
   - *Acceptance:* a unit test asserts the rendered directive references the
     disagreement-marker wording and the "Tracked in #" failure path; an
     existing-thread fixture carrying that marker is still filtered out by
     `_eligible_bug_comments`.

3. [x] **Make `_BUG_COMMENT_TERMINAL_MARKERS` the canonical core-side list.**
   `maestro/evolution.py` — drop the "keep in sync with
   `CommentProcessor._TERMINAL_MARKERS`" comment and document that this list is
   now authoritative for the autonomous path (maestro core does not import
   maestro_review). Confirm it still recognizes every marker the agent emitted
   ("Fixed automatically", "Fixed locally with implementation-plan context.",
   "Could not prove this defect/bug", "Tracked in #") so threads resolved under
   the old agent are not re-routed.
   - *Depends on:* none.
   - *Acceptance:* a unit test feeds a thread carrying each historical terminal
     marker and asserts `_eligible_bug_comments` excludes all of them.

### Phase 2 — Make the slash command self-describing

4. [x] **Repoint `/process-comments` off processor internals.**
   `.claude/commands/process-comments.md` — replace every citation of
   processor-only symbols (`_AI_COMMENT_RE`, `_EMOJI_SEVERITY`,
   `_TERMINAL_MARKERS`, `VALIDATION_PROMPT`, `FIX_PROMPT`, `FINAL_GATE_FIX_PROMPT`,
   `_push_with_rebase`) and the "sibling of the CI agent" framing with references
   to surviving symbols (`maestro_review/markers.py`, the GraphQL shape inlined
   in the command, the evolution.py classifiers) and reframe it as the canonical
   human-paced surface. The command's behaviour does not change — it is already a
   standalone re-implementation; only its prose references do.
   - *Depends on:* none.
   - *Acceptance:* the command file contains no reference to `processor.py`,
     `processor_prompt.py`, `maestro-process-comments`, or "the CI agent"; a
     grep-based doc test (or the existing line-citation drift check) passes.

### Phase 3 — Delete the standalone agent

5. [x] **Remove the CLI entry point and its wiring.** `maestro_review/cli.py`
   (delete `process_comments`), `maestro_review/pyproject.toml` (remove the
   `maestro-process-comments` script). This severs the only constructor of
   `CommentProcessor`.
   - *Depends on:* 1, 2, 4.
   - *Acceptance:* `maestro_review/pyproject.toml` has no
     `maestro-process-comments` script and `cli.py` defines no `process_comments`;
     the review CLI (`maestro-review`) still resolves and runs.

6. [x] **Delete the processor module and its prompts.** Remove
   `maestro_review/processor.py` and `maestro_review/processor_prompt.py`. Verify
   no remaining import references them (`review.py` must not regress — it imports
   only `markers.py`, never `processor.py`).
   - *Depends on:* 5.
   - *Acceptance:* `ruff` + `mypy` over `maestro_review/` pass with both files
     gone; `grep` finds no surviving `from maestro_review.processor import`.

7. [x] **Delete the processor test suite and repoint the remaining importers.**
   Remove the four processor-only files —
   `tests/test_maestro_review/test_processor.py`,
   `test_processor_single_finding.py`, `test_bug_hunt_processor_deep.py`, and
   `test_bug_hunt_processor_git.py` — then handle the two further test files
   that still import from `processor.py` but are *not* processor-only (see
   Relevant Findings; the third such importer,
   `tests/test_process_comments_command_doc.py`, already repointed its assertion
   at `_BUG_COMMENT_TERMINAL_MARKERS` from `maestro.evolution` and moved to the
   core suite when Step 4 landed), so collection does not raise
   `ModuleNotFoundError` once the module is gone:
   - `tests/test_maestro_review/test_bug_hunt_review.py` — delete
     `TestIsAiReviewCommentNoneBody.test_none_body`; its only assertion targets
     `CommentProcessor._is_ai_review_comment`, which ceases to exist with the
     module, so there is nothing left to cover. Keep the rest of the file, which
     exercises `PRReviewer`.
   - `tests/test_tend_bug_comments.py` (core suite) — delete
     `test_terminal_markers_match_processor` (the cross-package sync it guarded
     no longer exists) and rewrite `test_bug_regex_matches_review_emoji` to
     inline the bug emoji (🐛) instead of deriving it from
     `processor._EMOJI_SEVERITY`.
   Keep `markers.py` and the `PRReviewer._find_matching_issue` reviewer tests
   intact.
   - *Depends on:* 6.
   - *Acceptance:* `pytest tests/test_maestro_review/ --no-cov` is green with the
     four files removed and the remaining `test_maestro_review/` importer
     (`test_bug_hunt_review.py`) repointed;
     `pytest tests/ -v` (the core suite) is also green with
     `test_tend_bug_comments.py` updated and its cross-sync test removed;
     `markers.py` is still covered by reviewer tests.

8. [x] **Update package docs.** `maestro_review/CLAUDE.md` — remove the
   `processor.py` row and the line describing the retained
   `CommentProcessor`/`maestro-process-comments`; note that comment handling now
   lives in the core loop (`tend_evolution_prs`) and the `/process-comments`
   command, with `markers.py` shared by the reviewer's escalation path.
   - *Depends on:* 6.
   - *Acceptance:* `maestro_review/CLAUDE.md` no longer mentions
     `processor.py`/`CommentProcessor`; its module table matches the files that
     remain on disk.

## Testing Strategy

- **Remediation-prompt parity (Phase 1).** New unit tests over
  `_format_bug_comments_problem` assert the rendered directive carries the
  failing-test-first validation instruction, the test-repair instruction, the
  `_DISAGREEMENT_MARKER` reply path, and the tracked-issue-on-failure path. These
  are pure string-rendering tests — no I/O, no SDK.
- **Terminal-marker regression (Phase 1).** A table-driven unit test feeds
  `_eligible_bug_comments` one synthetic thread per historical terminal marker
  and asserts each is excluded, guarding against re-routing threads the old agent
  already closed. Resolved/outdated/non-🐛 threads remain covered by the existing
  `_eligible_bug_comments` tests.
- **Doc drift (Phase 2 & 3).** A grep-based assertion (or the existing
  proposal/line-citation drift check) confirms neither `/process-comments.md` nor
  `maestro_review/CLAUDE.md` references the deleted symbols.
- **Survivors stay green (Phase 3).** Run the full `maestro_review/` gate suite
  (`ruff`, `mypy`, `pytest --no-cov`) after deletion to prove the reviewer,
  `markers.py`, and `PRReviewer._find_matching_issue` are unaffected. Re-run the
  **core** `pytest tests/ -v` gate as well — Step 7 trims
  `test_tend_bug_comments.py` (core suite) when it drops the cross-package sync
  test, so core must be confirmed green after deletion, not only after Phase 1's
  autonomous-path changes.
- All tests honour CLAUDE.md rules: `mem_fs` for any file I/O, mocked
  `CodeHost`/GraphQL, no real network, no real `sleep`.

## Risk Assessment

- **Capability regression — autonomous path lacks something the agent did.**
  Mitigation: Phase 1 lands and is tested *before* any deletion, and the agent
  remains fully functional through Phase 2. The autonomous path is gated by
  `[loop] tend_bug_comments_enabled`, so disabling the flag reverts to no
  automated comment handling without touching the slash command.
- **Loss of the human-paced direct CLI.** `maestro-process-comments` disappears,
  but its only documented consumer is the `/process-comments` slash command,
  which is a self-contained re-implementation and does not shell out to the CLI.
  Anyone scripting the CLI directly must switch to the slash command; called out
  here because it is the one externally visible removal.
- **Breaking the reviewer's escalation chain.** `review.py` depends on
  `markers.py::_DISAGREEMENT_MARKER` and `PRReviewer._find_matching_issue`; both
  are explicitly preserved (Steps 6–8) and exercised by reviewer tests, which run
  green as the acceptance gate for the deletion steps.
- **Re-routing already-closed threads.** If the canonical
  `_BUG_COMMENT_TERMINAL_MARKERS` list dropped a marker the old agent emitted,
  the loop would re-flag a resolved thread. Step 3's table-driven test over every
  historical marker is the specific control that prevents this.
- **Rollback.** Each phase is an independent PR. Phases 1–2 are additive prose
  and prompt changes — revert in place. Phase 3 deletions are recoverable by
  reverting the deletion commit; nothing in the deleted module is depended on by
  code that ships after it, so a revert is clean.

## Out of scope

- Changing the GraphQL thread shape, the emoji-severity convention, or the
  reviewer (`maestro-review`) itself.
- The push-with-rebase / conflict-resolution machinery in `processor.py`: the
  autonomous path never pushes (tend is the sole pusher) and the slash command
  already has its own push-first logic, so this code is simply retired, not
  relocated.
