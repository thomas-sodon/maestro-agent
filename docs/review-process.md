# AI Code Review Process

End-to-end documentation for the automated code review pipeline.

## Overview

The review pipeline has two stages:

1. **AI Reviewer** — runs in CI on every pull request: posts inline comments for bugs, nits, and suggestions; on re-runs, escalates previously disputed findings
2. **Comment Remediation** — for each unresolved finding, either fixes it, files an issue, or replies that the defect could not be proven. This stage no longer runs in CI: the human-paced surface is the `/process-comments` slash command (`.claude/commands/process-comments.md`), which addresses every unresolved comment on the PR in one run and adds the prior session's implementation plan as context; the unattended surface is Maestro's autonomous loop, which routes unresolved review threads of every severity (🐛 bug, 💡 suggestion, ✏️ nit), human inline threads, and overflow findings (all severities) into remediation tasks (`maestro/evolution_tend.py::tend_evolution_prs`).

Every inline comment terminates in one of three outcomes: fixed, linked
to an issue, or dismissed by mutual agreement between the reviewer and the
remediation workflow.

## Stage 1: AI Reviewer (`maestro-review`)

### Flow

1. Fetch the **full branch diff** and PR metadata
2. Truncate diff to stay within token budget
3. Query Claude with the diff and PR context
4. Parse structured findings (file, line, severity, title, description, fix plan)
5. **Filter**: Remove findings already tracked by open GitHub issues (any severity)
6. **Escalate disagreements**: For each finding whose matching thread ends with
   a `Could not prove this defect` reply (the remediation workflow's
   disagreement marker, `maestro_review/markers.py`) and has no existing issue
   link, file an `ai-review` issue with labels `["ai-review", "<severity>"]` and
   reply `Tracked in #<n>` — the reviewer's second look confirms the disputed
   defect. Disputed findings are removed from the in-flight list
   so they are not re-posted as new comments.
7. **Deduplicate**: Match remaining findings against existing PR review threads
   - Unresolved thread exists → skip (don't re-post)
   - Resolved thread with issue link → skip (already tracked)
   - Resolved thread without issue link → reply noting defect persists (bugs only)
   - Nits/suggestions in resolved threads → skip
8. Post the review with inline comments for any remaining new findings

### Exit Code (CI Pass/Fail)

The review job **fails whenever the reviewer produces a comment**. Every finding
describes an actual defect (the prompt forbids stylistic-only notes), so any
comment — bug, suggestion, or nit — must be addressed before the PR proceeds:

- **Exit 1** (job fails) — one or more findings of **any** severity (bug,
  suggestion, or nit) were newly posted or reopened this run
- **Exit 0** (job passes) — no comment was produced: the diff was clean, or
  every finding was already tracked by an open issue, escalated to an issue, or
  deduplicated against an existing review thread (so nothing fresh was posted)
- **Exit 1** — infrastructure failures (API errors, SDK crashes) also fail the
  job so a broken review run surfaces rather than silently passing

Deduplication means a finding blocks the run that first posts it; a later re-run
that matches the same still-open thread skips it and does not re-block. Fixing
the code so the finding no longer reproduces — or resolving the thread — is what
keeps subsequent runs green. Comments are addressed in Stage 2 (the local
`/process-comments` command and the autonomous loop).

### Reply-Coverage Gate (`--require-comment-replies`)

This opt-in, default-off flag turns the reply discipline into a non-zero exit
code. It is **check-only**: when set, the run skips the diff review and posting
entirely and instead audits the PR's current comment state via
`PRReviewer.find_unreplied_comments`. The audit makes **no Claude call and posts
nothing** — it is deterministic and zero-SDK-cost.

A finding is "answered" by kind:

- **Inline** — an AI review thread is *unreplied* iff its root comment is an AI
  finding (the reviewer's icon + bold title), the thread is neither resolved nor
  outdated (both exempt, matching `/process-comments` eligibility), and it
  carries no reply.
- **Overflow** — a thread-less finding listed in the review body is *unreplied*
  iff its `of-<hash>` fingerprint is absent from the `<!-- maestro:overflow-ack -->`
  acknowledgement roll-up (parsed by `_parse_overflow_ack`).

The gate covers the **reviewer's own findings only** — human comments are out of
scope. It exits `1` if any such finding is still unanswered, `0` otherwise. The
audit **fails closed**: a `gh`/GraphQL outage exits `1` rather than being
mistaken for "all comments replied" (connectivity is probed first). Because the
flag is default-off, the existing diff-review CI job keeps its bug-only exit
semantics (above) untouched.

In a review workflow the gate runs as an **advisory step** ahead of the diff
review: `maestro-review --require-comment-replies` reads the comment state as of
this push *before* the reviewer posts this push's findings, so it is typically
red while the reviewer's prior-push findings sit unreplied (before the
remediation surfaces, the autonomous loop and `/process-comments`, have replied)
and green on a later push once those replies land. The audit makes no Claude
call (see [docs/ci.md](ci.md)). This repository's own CI runs no review
workflow.

### Deduplication

Findings are matched to existing review threads by comparing:
- **File path** — must match exactly
- **Title** — first an exact `**bold**`-block substring check on the thread's
  root comment; if that misses, a normalized token-overlap fallback (overlap
  coefficient ≥ 0.6 after dropping a small filler stop-list, with ≥ 2
  significant tokens on each side) catches the same defect when Claude's
  wording drifts across runs

This prevents duplicate comments across re-runs.

## Stage 2: Comment Remediation (`/process-comments` and the autonomous loop)

Stage 2 is no longer a CI job, and the standalone `maestro-process-comments`
CLI that once implemented it has been retired (proposal 063). The human-paced
surface is the `/process-comments` slash command, which runs the validate →
fix → reply flow below across **all** unresolved bug comments on the PR and
adds the prior session's implementation plan as fixing context. The unattended
counterpart is Maestro's autonomous loop: `tend_evolution_prs` detects the same
threads mechanically at zero SDK cost (`maestro/evolution_tend.py::_eligible_bug_comments`)
and routes them into a remediation task whose directive carries the same
discipline (`maestro/agent.py::_format_bug_comments_problem`).

### Flow

1. Fetch unresolved review threads on the PR
2. Skip **outdated** threads (the commented code has changed)
3. Skip non-AI comments — only those matching the reviewer's emoji-prefixed
   format are processed
4. Skip **terminally processed** findings (already received "Could not prove",
   "Fixed automatically", "Fixed locally", or "Tracked in #").
   The legacy phrase "Could not prove this bug" is still recognized so threads
   closed under the old wording stay closed.
5. **Validate**: Try to build a failing test that proves the defect exists.
   If a test cannot practically be written, fall back to static analysis.
   - If validation fails: reply `Could not prove this defect …`, leave the
     thread unresolved. The next reviewer run picks up the disagreement.
6. **Fix**: Apply a minimal fix, keeping the failing test as its regression
   test, then run the quality gates (ruff + mypy + pytest). Repair any
   pre-existing tests the fix broke rather than weakening the fix. A fix that
   cannot land files a deduplicated tracking issue and replies `Tracked in #N`.
7. **Ship and reply**: Merge upstream changes, push to the PR branch, and only
   then post the per-thread replies, using the canonical marker wording from
   `maestro_review/markers.py`. In the autonomous path the remediation task
   commits and run() pushes the PR branch directly; the tend phase is the
   fallback deliverer.

### What Happens When a Test Can't Be Written

This applies uniformly across severities.

- **Defect not proven at all**: Reply explaining why, leaving the thread
  **unresolved**. The next reviewer run interprets that reply as a
  disagreement signal; if it reproduces the finding, it escalates to an issue
  (see Stage 1, step 6). If it does not reproduce the finding, the thread is
  treated as terminally agreed and left alone.
- **Defect proven by static analysis**: A fix is attempted normally — there is
  simply no validation test to carry into the fix, so one is written with the
  fix instead. If the fix passes quality gates, it ships and the thread
  receives a terminal "fixed" reply. If the fix cannot land, a tracking issue
  is created via the same failed-fix path as test-confirmed defects.

## CI Workflows

No review workflow ships in this repository's CI; the reviewer needs a Claude
credential as a repository secret. A workflow an operator adds should run one
`review` job per pull request with `cancel-in-progress: true` (safe to cancel and
re-run) and `issues: write` (to escalate disputed findings to GitHub issues),
running the reply-coverage audit (`maestro-review --require-comment-replies`)
*before* the diff review.

### Comment processing is human-paced, not CI-triggered

The comment processor no longer runs in CI. The `process-comments` CI job and
the standalone `comment-processor.yml` workflow have been removed, and the
`maestro-process-comments` CLI (`CommentProcessor`) has since been retired
entirely (proposal 063). The human-paced path is the `/process-comments` slash
command (`.claude/commands/process-comments.md`): it runs the
validate → fix → reply flow locally on the PR branch across all unresolved bug
comments, with the prior session's implementation plan loaded as context so each
defect is fixed with the original design in view. Because a human invokes it on
demand, there is no
"Working on a fix..." claim to leave orphaned and no `cancel-in-progress`
concern — the reason the old CI job pinned `cancel-in-progress: false`.

## Labels

| Label | Applied by | Meaning |
|-------|-----------|---------|
| `ai-review` | Reviewer / Remediation | Finding originated from AI code review |
| `nit` | Reviewer / Remediation | Low-severity style/naming issue |
| `suggestion` | Reviewer / Remediation | Code improvement suggestion |
| `bug` | Reviewer / Remediation | Confirmed defect, tracked for manual fix |
| `needs-triage` | Remediation | Issue needs manual review |
