# 064: Remediate the Reviewer's Overflow Findings

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro_review/review.py`, `maestro/evolution.py`, `maestro/agent.py`, `maestro/vcs.py`, `.claude/commands/process-comments.md`, tests |
| **Depends on** | 063 _(builds on the parity remediation prompt landed in its Phase 1)_ |
| **Derived from** | `maestro_review/review.py` (`_build_review_payload`) — the overflow path that no consumer reads |
| **What landed** | All four steps. Step 1 — `maestro_review/review.py` (`_build_review_payload`, `_overflow_fingerprint`) wraps the overflow list in the stable sentinel pair `_OVERFLOW_SENTINEL_OPEN` / `_OVERFLOW_SENTINEL_CLOSE` (`<!-- maestro:overflow -->` … `<!-- /maestro:overflow -->`) and prefixes each line with a deterministic `of-<hash8>` handle derived from `severity\|file\|line\|normalized-title`. Step 2 — the shared core-side parser `_eligible_overflow_bug_findings` (with `OverflowBugFinding`, the legacy-header fallback, and the `_OVERFLOW_ACK_SENTINEL` acknowledgement format) in `maestro/evolution.py`, kept in sync with `review.py` by the cross-package tests in `tests/test_tend_bug_comments.py`. Step 3 — `_route_overflow_findings` (called from `_route_bug_comments`) records onto `TendPRsResult.bug_overflow_routed` via the new best-effort `CodeHost.get_pr_review_bodies` / `get_pr_comment_bodies` accessors (`maestro/vcs.py`); `_format_bug_comments_problem` gained the overflow section and `_route_pr_feedback_to_pending_requests` folds both channels into one `Remediate PR #N` entry (`maestro/agent.py`). Step 4 — `.claude/commands/process-comments.md` parses the overflow block, validates/fixes by fingerprint, and records verdicts on the roll-up acknowledgement comment; guarded by `tests/test_process_comments_command_doc.py`. |

## Description

> Status updated to shipped on 2026-06-11 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

The AI reviewer places each finding either as an inline review comment on a
valid diff line or, when it cannot, in an *overflow* list appended to the review
**body**. Both remediation surfaces — the autonomous tend phase and the
`/process-comments` slash command — act exclusively on inline review *threads*.
An overflow finding has no thread, no `databaseId`, and no resolved/outdated
state, so a 🐛 bug that overflows is silently dropped: never fixed, never
resolved, and re-emitted verbatim by every later review run (the reviewer
dedupes inline findings against existing threads, but overflow is body text
re-derived from the diff each pass). The defect the reviewer found is real; the
only thing missing is a path for either consumer to see and address it.

This proposal closes that path. The reviewer keeps emitting overflow into the
body but in a stable, machine-parseable block with a compact per-finding
fingerprint. A shared core-side parser extracts the bug-severity overflow
findings at zero SDK cost — the same shape and discipline the existing
`_eligible_bug_comments` path already uses for threads. The autonomous tend
phase routes them into the same `Remediate PR #N` remediation task, and the
`/process-comments` command picks them up alongside its thread work list.
Because an overflow finding has no thread to reply to or resolve, terminal
state is tracked on a single roll-up acknowledgement comment keyed by
fingerprint — the body-text analogue of the per-thread terminal markers in
`_BUG_COMMENT_TERMINAL_MARKERS`.

End state: a bug the reviewer cannot place inline is handled by the same
validate→fix→acknowledge discipline as an inline bug thread, on both the
unattended and human-paced surfaces, and stops recurring once addressed.

## Relevant Findings

- `maestro_review/review.py` (`PRReviewer._build_review_payload`) — splits
  findings into inline `comments` (when `finding.line > 0` and the line is in
  `valid_lines[finding.file]`) and an `overflow` list for the rest, rendered as
  `- **{file}:{line}** [{severity}] — {title}: {description}` under the literal
  header `**Additional findings** (could not be placed inline):`. The block
  carries severity (including `[bug]`) but is plain body text with no stable
  delimiter separating it from the sibling `**Findings already covered by open
  issues**` block or the `*AI-generated review — advisory only*` footer.
- `maestro_review/review.py` (the `valid_lines` construction, ~`diff is None`
  fallback) — overflow arises from two distinct causes: a finding line of `0`
  (or otherwise outside the diff hunks), and a finding whose **file is absent
  from the diff entirely** (so `valid_lines.get(finding.file)` is empty). When a
  diff was provided but parsed empty (e.g. binary), every finding overflows.
  This matters because the second class cannot be turned into a GitHub review
  comment at all (the path is not in the diff), so a body-block remedy is the
  only general one.
- `maestro/evolution.py` (`_eligible_bug_comments`, `_AI_BUG_COMMENT_RE`,
  `_BUG_COMMENT_TERMINAL_MARKERS`, `TendPRsResult.bug_comments_routed`,
  `_route_bug_comments`) — the autonomous detector parses only
  `reviewThreads` nodes (`isResolved`/`isOutdated`/root-body regex/terminal
  markers). It never reads the review body, so overflow findings are invisible
  to it. `_route_bug_comments` already fetches review threads via one GraphQL
  read and records `(comment_id, path, title)` tuples; the review **body** is
  not fetched there.
- `maestro/agent.py` (`_format_bug_comments_problem`,
  `_route_pr_feedback_to_pending_requests`, `_PRRemediationAccumulator`,
  `_compose_pr_remediation_prompt`) — renders the per-thread remediation
  directive (validate-with-a-failing-test, minimal fix, disagreement reply,
  `Tracked in #N` on unfixable, commit-don't-push, resolve the thread). The
  directive assumes every finding is a thread it can reply to and
  `resolveReviewThread`; an overflow finding has neither.
- `.claude/commands/process-comments.md` (*Select the comments*, *Commit, push,
  and reply*) — fetches `reviewThreads(first:100)` and builds its work list from
  thread roots only; the only acknowledgement mechanism it knows is an
  `in_reply_to` thread reply carrying `_FIXED_LOCALLY_MARKER` /
  `_DISAGREEMENT_MARKER`. It does not read the review body or handle
  thread-less findings.
- `maestro/evolution.py` (the `_AI_BUG_COMMENT_RE` "kept in sync by
  `tests/test_tend_bug_comments.py`" comment) — the established precedent for
  duplicating a small reviewer-side string constant on the core side under
  strict package separation (`maestro` must not import `maestro_review`),
  guarded by a cross-package sync test. The overflow block's sentinel and
  fingerprint format follow the same pattern.
- `docs/proposals/core/063-retire-standalone-comment-processor.md` (Phase 1,
  landed) — `_format_bug_comments_problem` already folds in the full
  validate/fix/disagree/track discipline. Step 3 below extends that same
  renderer rather than introducing a parallel one.
- `maestro_review/markers.py` (`_DISAGREEMENT_MARKER`, `_FIXED_LOCALLY_MARKER`)
  — the shared, import-free marker module. The overflow acknowledgement reuses
  these exact phrases so a fixed/disputed overflow finding reads consistently
  with a fixed/disputed thread.

## Implementation Plan

Order matters: harden the reviewer's block first (enabling, additive), then the
shared parser, then the two consumers. Each consumer parser tolerates both the
hardened block and the legacy header so an interrupted run never loses
detection. All steps are gated by the existing `[loop] tend_bug_comments_enabled`
flag on the autonomous side — disabling it reverts to thread-only handling.

1. [x] **Harden the overflow block into a parseable section.**
   `maestro_review/review.py` (`_build_review_payload`) — wrap the overflow list
   in a stable delimiter pair (an HTML-comment sentinel, e.g.
   `<!-- maestro:overflow -->` … `<!-- /maestro:overflow -->`) and prefix each
   line with a compact, stable fingerprint `of-<hash8>` derived from
   `severity|file|line|normalized-title`. The human-readable
   `**file:line** [severity] — title: description` text is preserved; only the
   delimiter and handle are added. This disambiguates the block from the
   issue-tracked and footer blocks and gives consumers a token to reference
   without echoing whole lines.
   - *Depends on:* none.
   - *Acceptance:* a unit test over `_build_review_payload` asserts the rendered
     body wraps overflow findings in the sentinel pair and that each line begins
     with a deterministic `of-<hash>` handle stable across two renders of the
     same finding.

2. [x] **Add the shared core-side overflow parser and acknowledgement format.**
   `maestro/evolution.py` — add `_eligible_overflow_bug_findings(review_body)`
   mirroring `_eligible_bug_comments`: parse the sentinel-delimited block (with
   a legacy fallback to the `**Additional findings**` header), keep only
   `[bug]`-severity lines, and drop any whose fingerprint already appears under
   a terminal marker in the roll-up acknowledgement comment. Define the
   acknowledgement comment's header sentinel and per-line
   `of-<hash> — <marker>` format here, alongside `_BUG_COMMENT_TERMINAL_MARKERS`,
   and keep the block sentinel + fingerprint constants in sync with
   `review.py` via the existing cross-package sync test.
   - *Depends on:* 1.
   - *Acceptance:* a table-driven unit test feeds a synthetic review body plus
     acknowledgement comment and asserts the parser returns the unaddressed
     `[bug]` overflow findings, excludes `[suggestion]`/`[nit]` lines, and
     excludes any fingerprint already marked `Fixed`/`Could not prove`/`Tracked
     in #`.

3. [x] **Route overflow findings through the autonomous tend phase.**
   `maestro/evolution.py` (`_route_bug_comments` → also fetch the latest AI
   review body and call `_eligible_overflow_bug_findings`; record on a new
   `TendPRsResult.bug_overflow_routed` channel) and `maestro/agent.py`
   (`_format_bug_comments_problem` gains an overflow section;
   `_route_pr_feedback_to_pending_requests` collapses it into the same
   `Remediate PR #N` queue entry). The overflow directive carries the same
   validate→fix discipline but, lacking a thread, records each verdict by
   upserting the roll-up acknowledgement comment (`of-<hash> — <marker>`) instead
   of an `in_reply_to` reply or `resolveReviewThread`; commit-not-push still
   holds.
   - *Depends on:* 2.
   - *Acceptance:* a unit test asserts the rendered directive references the
     fetched overflow findings by fingerprint and instructs upserting the
     acknowledgement comment (not a thread reply); a fixture whose findings are
     all already acknowledged routes nothing.

4. [x] **Teach `/process-comments` to address overflow findings.**
   `.claude/commands/process-comments.md` — after building the thread work list,
   also fetch the most recent AI review body, parse the overflow block (same
   sentinel + fingerprint), filter to unaddressed 🐛 bugs, and validate+fix each
   with plan context exactly as for a thread. Because there is no thread, record
   verdicts by upserting the roll-up acknowledgement comment after the push
   (push-first still gates), reusing the `_FIXED_LOCALLY_MARKER` /
   `_DISAGREEMENT_MARKER` wording per fingerprint. Add an *Overflow findings*
   line to *Report*.
   - *Depends on:* 1.
   - *Acceptance:* the command file documents the body-fetch, the
     fingerprint-keyed acknowledgement comment, and the report line; the
     existing doc-drift / line-citation tests pass.

## Testing Strategy

- **Reviewer block (Step 1).** Pure-rendering unit tests over
  `_build_review_payload`: the sentinel pair wraps overflow, fingerprints are
  deterministic, and inline findings are unaffected. Mocked `CodeHost`, no I/O.
- **Parser (Step 2).** Table-driven tests over `_eligible_overflow_bug_findings`
  covering: hardened block, legacy-header fallback, severity filtering,
  fingerprint dedup against each terminal marker, empty/malformed body. A
  cross-package sync test (mirroring `tests/test_tend_bug_comments.py`) asserts
  the block sentinel and fingerprint scheme match between `review.py` and
  `evolution.py`.
- **Autonomous routing (Step 3).** Unit tests over `_route_bug_comments` (mocked
  GraphQL body fetch) and `_format_bug_comments_problem` assert overflow
  findings are routed and rendered with the acknowledgement-comment instruction,
  and that an all-acknowledged body routes nothing.
- **Doc drift (Step 4).** The proposal/line-citation drift checks and a grep
  assertion confirm the command documents the overflow path and cites only
  surviving symbols.
- All tests honour CLAUDE.md: `mem_fs` for file I/O, mocked `CodeHost`/GraphQL,
  no real network, no real `sleep`.

## Risk Assessment

- **Mis-parsing the review body.** A brittle parser could route phantom
  findings or miss real ones. Mitigation: the hardened sentinel + fingerprint
  block is the primary path; the legacy-header fallback is best-effort and
  table-tested against malformed input, and the parser is zero-SDK so a bad
  parse costs nothing but a skipped route (the next review re-emits the block).
- **Acknowledgement-comment churn.** If the fingerprint is unstable across
  reviews (e.g. the reviewer rewords a title), an addressed finding could
  re-route. Mitigation: the fingerprint normalizes the title and is unit-tested
  for stability; worst case is one redundant remediation, the same failure mode
  the thread path already tolerates.
- **Cross-package drift.** `maestro` must not import `maestro_review`, so the
  block sentinel and fingerprint scheme are duplicated. Mitigation: the
  cross-package sync test is the explicit control, following the established
  `_AI_BUG_COMMENT_RE` precedent.
- **Rollback.** Steps are independently revertible; the autonomous side stays
  behind `[loop] tend_bug_comments_enabled`. Step 1 is additive prose in the
  review body and harmless if the consumers are not yet updated.

## Out of scope

- **Reviewer-side overflow suppression.** Teaching the reviewer to read the
  acknowledgement comment and stop re-emitting disputed/false-positive overflow
  findings is a follow-up; this proposal does not worsen today's churn (overflow
  already re-emits every run) and the acknowledgement comment makes that
  enhancement straightforward later.
- **Promoting overflow to file-level review comments.** Posting findings whose
  file *is* in the diff as `subject_type: file` comments (turning them into
  threads the existing pipeline handles) is an alternative considered and
  deferred — it does not cover the file-not-in-diff class, and the body-block
  path is the single mechanism that handles both.
- Changing the emoji-severity convention, the GraphQL thread shape, or the
  reviewer's finding-generation prompt.
