# 083: Fail the Review When Any Posted Comment Lacks a Reply — Inline Threads and Overflow Findings

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro_review/cli.py`, `maestro_review/github.py`, `maestro_review/review.py`, `maestro_review/CLAUDE.md`, `docs/review-process.md`, `maestro.toml`, tests |
| **Depends on** | none |
| **What landed** | Steps 1–5. Step 1 — the review-body and comment-body accessors on the reviewer's GitHub client. `GitHubClient.get_pr_review_bodies` / `get_pr_comment_bodies` (`maestro_review/github.py`), each running `gh pr view <pr> --json reviews\|comments` and returning `[]` on `gh` failure (consistent with `get_pr_review_threads`), with matching `get_pr_review_bodies` / `get_pr_comment_bodies` entries on the `CodeHost` protocol — mirroring the same-named accessors in `maestro/vcs.py`. Tests in `tests/test_maestro_review/test_github.py`. Step 2 — the `OverflowFinding` dataclass plus `_parse_overflow_findings` / `_parse_overflow_ack` / `_AI_FINDING_RE` in `maestro_review/review.py` (all severities, with the legacy `**Additional findings**` header as fallback). Tests in `tests/test_maestro_review/test_review.py`. Step 3 — `find_unreplied_comments` returning an `UnrepliedComments(inline, overflow)` report on `PRReviewer`, backed by the `_is_unreplied_ai_thread` predicate and the `_latest_overflow_findings` helper, in `maestro_review/review.py`; re-uses `get_pr_review_threads` / `_parse_review_threads` and makes no Claude call. Tests in `tests/test_maestro_review/test_review.py`. Step 4 — the `--require-comment-replies` check-only CLI mode and the `[review] require_comment_replies` default (`main` / `_load_review_defaults`) in `maestro_review/cli.py`; the mode skips the diff review and posting, calls `find_unreplied_comments`, and exits `1` on any unreplied finding (infrastructure failure exits `1`). Tests in `tests/test_maestro_review/test_cli.py`. Step 5 — the gate's documentation: the CLI Usage example, the `review.py` / `cli.py` module rows, and the `[review]` config block in `maestro_review/CLAUDE.md`; the `require_comment_replies` default in the `[review]` block of `maestro.toml`; and the `### Reply-Coverage Gate` section in `docs/review-process.md`. Tests in `tests/test_review_process_doc.py`. |
| **What is missing** | Nothing — Steps 1–5 have all landed. |
| **Derived from** | The overflow-findings scheme and `<!-- maestro:overflow-ack -->` roll-up shipped in proposal 064, and the validate → fix → **reply** discipline documented in `docs/review-process.md` and `.claude/commands/process-comments.md` |

## Description

> Status updated to shipped on 2026-06-21 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

The reviewer raises two kinds of comment on a PR: **inline** findings posted as
review threads, and thread-less **overflow** findings it lists in the review
body (for lines it cannot place inline). The comment-remediation surfaces —
Maestro's autonomous loop (`tend_evolution_prs`) and the human-paced
`/process-comments` command — are then meant to close each one by posting a
reply: an inline thread reply, or an acknowledgement line on the overflow
roll-up comment. Today nothing verifies that this closure actually happened. The
CLI's only failure signal (`maestro_review/cli.py::main`) is "this diff
introduced new bug-severity findings"; a PR can carry a pile of the reviewer's
own findings with **zero** replies and the review still exits `0`.

This proposal adds a check-only gate to the reviewer: when invoked with a new
`--require-comment-replies` flag, `maestro-review` inspects the current comment
state of the PR and exits `1` if **any** finding it is responsible for — inline
or overflow — is still open and unanswered. "Answered" means the inline thread
carries at least one reply (or is resolved / outdated), or the overflow finding's
`of-<hash8>` fingerprint appears on the `<!-- maestro:overflow-ack -->` roll-up.
The check makes no Claude call and posts nothing — it is a deterministic,
zero-SDK-cost audit of reply state built from GraphQL plus two `gh` reads.

The flag is opt-in and default-off, so the existing diff-review CI job
(`.github/workflows/code-review.yml`) keeps its current semantics. The end state
is a cheap, reversible gate that a CI step or the loop can run after the
remediation surfaces have done their work to assert that no review comment was
left dangling — turning the reply discipline from a convention into something a
non-zero exit code enforces.

## Relevant Findings

- `maestro_review/cli.py` (`main`) — exit code is driven solely by `actioned_bugs`
  (newly posted/reopened bug findings) and infrastructure failures; there is no
  code path that inspects whether *existing* comments have replies. `--dry-run`
  already demonstrates a non-posting mode that re-uses the filtering helpers.
- `maestro_review/review.py` (`ReviewThread`) — already carries `reply_bodies`
  (comments after the root), `is_resolved`, and `is_outdated`; `_parse_review_threads`
  populates them from the GraphQL nodes. A thread with empty `reply_bodies` that
  is neither resolved nor outdated is precisely an unanswered inline comment.
- `maestro_review/review.py` (`_OVERFLOW_SENTINEL_OPEN` / `_OVERFLOW_SENTINEL_CLOSE`
  / `_overflow_fingerprint`) — defines how overflow findings are *written* into the
  review body, but the package has no parser to *read them back*; the only
  read-side parser is core-side and bug-only.
- `maestro/evolution.py` (`_eligible_overflow_bug_findings`, `_OVERFLOW_ACK_SENTINEL`,
  `_AI_BUG_COMMENT_RE`) — the core loop already parses the overflow block and the
  `<!-- maestro:overflow-ack -->` roll-up to decide which overflow **bugs** are
  still open; this proposal mirrors that logic in `maestro_review` across all
  severities and for inline threads too.
- `maestro_review/github.py` (`CodeHost`, `GitHubClient.get_pr_review_threads`) —
  the protocol exposes thread fetching but **no** accessor for review bodies or
  PR conversation comments, both of which the overflow check needs.
- `maestro/vcs.py` (`get_pr_review_bodies`, `get_pr_comment_bodies`) — core already
  has the two `gh pr view --json reviews|comments` accessors the reviewer lacks;
  the new `maestro_review` accessors should mirror their names and shape.
- `.claude/commands/process-comments.md` ("Select the comments") — documents the
  canonical eligibility rules: AI findings are roots whose first line is an
  emoji + bold title (🐛/💡/✏️), outdated/resolved threads are skipped, and an
  overflow finding is done once its fingerprint carries a terminal marker on the
  ack comment. The gate reuses these exact rules to decide "answered."

## Implementation Plan

1. [x] **Add review-body and comment-body accessors to the reviewer's GitHub client.**
   `maestro_review/github.py` — add `get_pr_review_bodies(pr_number) -> list[str]`
   (`gh pr view <pr> --json reviews`) and `get_pr_comment_bodies(pr_number) -> list[str]`
   (`gh pr view <pr> --json comments`) to both the `CodeHost` protocol and
   `GitHubClient`, mirroring the same-named accessors in `maestro/vcs.py`. On `gh`
   failure they log and return `[]` (consistent with `get_pr_review_threads`).
   - *Depends on:* none.
   - *Acceptance:* a mock returning the `reviews`/`comments` JSON yields the body
     strings in order, and a `GitHubError` from `_run` yields `[]`.

2. [x] **Add overflow + ack + AI-finding parsers to `review.py`.** Add an
   `OverflowFinding` dataclass and three helpers: `_parse_overflow_findings(body)`
   (extracts `of-<hash8>` / file / line / severity / title lines between the
   existing sentinels, all severities, with the legacy `**Additional findings**`
   header as fallback), `_parse_overflow_ack(comment_bodies)` (returns the set of
   acked fingerprints found under `_OVERFLOW_ACK_SENTINEL`), and an
   `_AI_FINDING_RE` (emoji + bold title) generalizing the bug-only
   `maestro/evolution.py::_AI_BUG_COMMENT_RE` to 🐛/💡/✏️.
   - *Depends on:* none.
   - *Acceptance:* a body with two overflow lines plus an ack comment acking one
     fingerprint parses to both findings and an ack set of size one; a malformed
     line is skipped without raising.

3. [x] **Add `find_unreplied_comments()` to `PRReviewer`.** Returns an
   `UnrepliedComments(inline, overflow)` report. An inline thread is unreplied
   iff its root matches `_AI_FINDING_RE`, it is **not** resolved, **not** outdated,
   and has empty `reply_bodies`. An overflow finding (parsed from the most recent
   review body carrying the sentinels) is unreplied iff its fingerprint is absent
   from `_parse_overflow_ack(get_pr_comment_bodies(...))`. Re-uses
   `get_pr_review_threads` / `_parse_review_threads`; makes no Claude call.
   - *Depends on:* 1, 2.
   - *Acceptance:* given one replied and one bare AI thread, one resolved bare
     thread, one non-AI root, plus one acked and one un-acked overflow finding,
     the report contains exactly the bare AI thread and the un-acked finding.

4. [x] **Wire a `--require-comment-replies` check-only mode into the CLI.**
   `maestro_review/cli.py` — add the flag (default from an optional
   `[review] require_comment_replies` key loaded by `_load_review_defaults`). When
   set, `main` skips the diff review and posting entirely, calls
   `find_unreplied_comments`, logs each unreplied item (`file:line — title` for
   inline, `of-<hash> file:line` for overflow), and `sys.exit(1)` if any remain,
   else `sys.exit(0)`. Infrastructure failure exits `1` like the existing paths.
   - *Depends on:* 3.
   - *Acceptance:* with the flag set and ≥1 unreplied finding mocked, `main`
     exits `1` and `reviewer.review` / `asyncio.run` are never called; with none,
     it exits `0`.

5. [x] **Document the new gate.** Update `maestro_review/CLAUDE.md` (CLI Usage +
   the `review.py` / `cli.py` module rows), the `[review]` block in `maestro.toml`,
   and `docs/review-process.md` (the validate → fix → reply discipline now has a
   verification gate). Cite symbols only — no `file.py:line`.
   - *Depends on:* 4.
   - *Acceptance:* `tests/test_no_line_citations_in_docs.py` and the
     `maestro_review` doc/config tests pass with the new flag described.

## Testing Strategy

All tests use the `maestro_review` mocking discipline — a fake `CodeHost` and a
patched `maestro_review.sdk.query`; no real network, subprocess, or filesystem.

- **Parsers (Step 2)** — `_parse_overflow_findings` over a well-formed block, a
  block with a malformed line, an empty/absent block, and the legacy-header
  fallback; `_parse_overflow_ack` over a roll-up with mixed acked/terminal lines;
  `_AI_FINDING_RE` matching each of 🐛/💡/✏️ and rejecting a human root.
- **`find_unreplied_comments` (Step 3)** — a matrix: replied vs bare thread,
  resolved/outdated exemption, non-AI root excluded, no-threads (empty) case, and
  overflow acked vs un-acked. Assert the report contents exactly.
- **CLI (Step 4)** — patch `PRReviewer`; assert exit `1` when the report is
  non-empty and exit `0` when empty, that the check-only path never invokes
  `asyncio.run`/`review`, that the `[review] require_comment_replies` default is
  honored when the flag is omitted, and that a GraphQL/`gh` failure surfacing as
  empty data is handled (no crash, deterministic exit).
- **Cross-package sync** — extend the existing proposal-064 sentinel/fingerprint
  cross-package test so the reviewer's overflow + ack parsing stays pinned to
  `maestro/evolution.py::_OVERFLOW_ACK_SENTINEL` and the sentinel constants,
  preventing drift between the writer and the new reader.

## Risk Assessment

- **Existing CI unchanged.** The gate lives behind a default-off flag and a new
  check-only branch; the diff-review job and its bug-only exit code are
  untouched. Rollback is simply not passing `--require-comment-replies`.
- **False failures on human-resolved threads.** A maintainer who resolves a
  thread without typing a reply should not trip the gate — mitigated by exempting
  `is_resolved` and `is_outdated` threads (matching `/process-comments`
  eligibility). This is a deliberate definition of "answered"; the strict literal
  "zero replies regardless of resolution" reading is noted as a tightening option
  if desired.
- **Classification drift.** Detecting AI findings by leading emoji + bold title
  could miss a reworded finding — mitigated by reusing the exact icons the
  reviewer emits and the same regex shape core already relies on.
- **Overflow parser drift.** A second parser for the overflow block risks
  diverging from the writer — mitigated by the cross-package sync test (Step 2 /
  Testing) that pins both to the shared sentinel and fingerprint scheme.
- **Trust chain.** The mode is read-only (`gh` reads + GraphQL), makes no SDK
  call, and posts nothing, so it adds no mutating surface and no approval path.

## Out of scope

- **Human comments.** The gate covers the reviewer's own findings only (overflow
  is an AI-only concept, and "both inline and overflow" maps to the reviewer's
  findings). Extending coverage to human inline/top-level comments and the
  `<!-- maestro:human-ack -->` roll-up is a follow-up.
- **Auto-replying.** This gate only *detects* unreplied comments; generating the
  replies stays with the autonomous loop and `/process-comments`.
- **Turning the gate on.** Wiring `--require-comment-replies` into
  `code-review.yml` or the loop is deferred — landing the flag first keeps the
  change minimal and the activation independently reversible.
