Address all unresolved review comments on a pull request — AI bug, suggestion, and nit findings plus human comments, **including outdated threads** — with the previous session's implementation plan loaded as context so every change respects the original design.

This command takes **every** unresolved comment on a PR that asks for a change or a response — AI review findings of all severities (🐛 bug, 💡 suggestion, ✏️ nit), the thread-less *overflow* findings the reviewer lists in the review body, **and** human comments (inline review threads and top-level PR comments) — validates each on its own merits, and then either applies a minimal, plan-consistent change with a test or declines it with explicit reasoning, before running the quality gates **once** over the combined change. It is the **canonical human-paced** comment-remediation surface; the unattended counterpart is Maestro's autonomous loop (`maestro/evolution_tend.py::tend_evolution_prs`), which routes unresolved AI review threads of every severity (🐛 bug, 💡 suggestion, ✏️ nit), human inline review threads, and the thread-less overflow findings (all severities) into a remediation task at zero SDK cost, but remains narrower in two ways: it does **not** load the local on-disk implementation plan (it injects only the stored `EvolutionPlan` snapshot), and it does **not** process top-level (non-thread) human PR comments. Invoked locally on the PR branch, this command also reads the implementation plan that produced the PR (written by `/plan`, `/plan-proposal`, `/bug-fix`, or `/backlog` in a prior session in *this* folder), so each change is made with the full intent, reuse map, and acceptance criteria of the original work in view — context the unattended path does not load. Once the gates pass it ships the changes to the PR branch and posts the per-comment replies itself — the `Fixed locally …` wording is only accurate after the push lands, so the push comes first and a failed push aborts the replies. Every eligible comment is driven to a reply before the command reports done — **including outdated threads** (whose anchored code changed since the comment was posted), which earlier bug-only runs dropped silently; an outdated thread is re-validated against the *current* tree and earns either a fix, a decline, an answer, or an "already addressed" acknowledgement, but never a silent skip. The completion contract is explicit: after a successful push (or when there is nothing to ship), no eligible thread, overflow finding, or top-level human comment may be left without a reply or acknowledgement line.

## Authentication

GitHub access needs `GITHUB_TOKEN` from `.env` in the project root. Run every `gh`, `git push`, and `git fetch` command through the wrapper, which loads `.env` and then execs the command; the examples below already use it:

```
scripts/with-env.sh <command>
```

If `.env` is absent or `GITHUB_TOKEN` is empty, stop with `error=missing_github_token` and tell the user.

## Arguments

`$ARGUMENTS` is optional, up to two whitespace-separated tokens:

- *(empty)* — resolve the PR from the current branch (see *Resolve the PR*); process **every** eligible comment.
- `<PR#>` — a positive integer naming the PR to process; process every eligible comment on it.
- `<PR#> <comment-id>` — narrow to a single review-comment `databaseId` (the integer GitHub assigns each inline comment). Use this to target one comment — of any type (bug, suggestion, nit, or human) — instead of the whole batch.
- `<comment-id>` alone is **not** supported — always lead with the PR number when pinning a comment.

## Resolve the PR

1. **Determine the PR number.** If the first `$ARGUMENTS` token is a positive integer, use it. Otherwise derive it from the current branch:
   ```
   scripts/with-env.sh gh pr view --json number,headRefName,baseRefName,state
   ```
   - If `gh pr view` finds no PR for the current branch, stop with `error=no_pr_for_branch` and ask the user to pass `<PR#>` explicitly.
   - If the PR `state` is not `OPEN`, stop with `error=pr_not_open` — there is nothing to push a fix to.
2. **Confirm the branch.** Read the PR's `headRefName`. Run `git rev-parse --abbrev-ref HEAD`. The changes must land on the PR branch, so:
   - If HEAD already equals `headRefName`, proceed.
   - If HEAD is `main` or any other branch, stop with `error=wrong_branch` and tell the user to `git checkout <headRefName>` first. Do **not** auto-switch branches — the user owns branch state, and an unrelated dirty tree could be lost.
3. **Dirty-tree gate.** Run `git status --porcelain`. If the tree has uncommitted changes unrelated to the comments being addressed, surface them and ask before proceeding — do not mix concerns into the changes.

## Load the implementation plan

This plan-aware context is what distinguishes this human-paced surface from the unattended autonomous path. Resolve the plan **read-only**, exactly as `/impl` does, using the per-folder pointer first and the global most-recent plan as fallback:

1. **Folder pointer.** Read `~/.claude/last-plan-by-folder.json` (the shared JSON map the plan-writing commands maintain, keyed by absolute working-directory path; see `.claude/commands/impl.md` *Plan pointer file*). Look up the entry for the current working directory's absolute path. If it exists and its `plan` file still exists and is non-empty, use it, and announce `Plan context: <plan path> (written <written_at>)`.
2. **Fallback.** If there is no pointer file, no entry for this folder, or the recorded plan is missing/empty, pick the most recently modified non-empty `*.md` in `~/.claude/plans/` and warn `No folder plan pointer — using the most-recent plan globally; verify it matches this PR`.
3. **No plan at all.** If `~/.claude/plans/` is empty or every file is size-zero, warn `No implementation plan found — proceeding with diff/code context only` and continue. The command still works without a plan; it is simply less context-aware (i.e. it degrades to diff/code context only, the same context the unattended autonomous path runs on).
4. **Read the plan in full and sanity-check it.** Extract the substance — what the change set out to do, which files and symbols it touches, the reuse map, and any `Verification` / `Acceptance` criteria. If most paths the plan cites do not exist in the current tree (the plan was written for a different PR or repo), do not silently trust it: warn `Plan looks unrelated to this PR — treating as low-confidence context` and lean on the code itself. Never edit the plan file.

Keep the plan in working memory for the *Validate* and *Apply* steps below: it tells you the intended design so each change is consistent with it rather than a myopic local patch, and it is the strongest tie-breaker for declining a comment that contradicts a deliberate, documented design choice.

## Select the comments

1. **Fetch unresolved review threads** via the GraphQL query below — a superset of the thread shape `maestro_review/github.py::GitHubClient.get_pr_review_threads` returns (it also selects `author.login` so human inline comments can be attributed and other bots' threads excluded):
   ```
   scripts/with-env.sh gh api graphql -f query='
     query($owner:String!,$repo:String!,$pr:Int!){
       repository(owner:$owner,name:$repo){
         pullRequest(number:$pr){
           reviewThreads(first:100){nodes{
             id isResolved isOutdated
             comments(first:100){nodes{databaseId path body author{login}}}
           }}
         }}}' -f owner=<owner> -f repo=<repo> -F pr=<PR#>
   ```
   Derive `<owner>/<repo>` from `scripts/with-env.sh gh repo view --json owner,name` or the `origin` remote.
2. **Classify each thread's root comment** (the first comment in the thread):
   - **AI finding.** Its first line is an emoji + a bold title: 🐛 = bug, 💡 = suggestion, ✏️ = nit. These are the icons the reviewer emits (`maestro_review/review.py`); the 🐛 subset matches `maestro/evolution_tend.py::_AI_BUG_COMMENT_RE` (`^🐛 **<title>**`). Take the **severity from the emoji** — earlier this command was bug-only; it now addresses all three. Read the root's `databaseId` (needed to reply) and `path`.
   - **Human comment.** The root body does **not** match the AI finding format and its `author.login` is a real user. Treat such a thread as a human change-request or question. **Skip** non-AI-finding roots whose `author.login` ends in `[bot]` (e.g. another bot's thread, CI status) — they are out of scope; only AI findings and human users are processed.
3. **Filter to eligible threads:**
   - `isResolved` is false. **`isOutdated` does not exclude a thread** — an outdated-but-unresolved thread (its anchored code changed since the comment, but it was never resolved) is still in scope and must be driven to a reply. Carry each thread's `isOutdated` flag forward: it changes how the thread is *validated* (see *Validate*), not whether it is selected. (This is where this human-paced surface diverges from the autonomous path, which still skips outdated threads in `maestro/evolution_tend.py`.)
   - The root is an AI finding (any severity) **or** a human comment (per the classification above).
   - No reply in the thread already carries a terminal marker — the canonical bug set is `maestro/evolution_tend.py::_BUG_COMMENT_TERMINAL_MARKERS` (`Could not prove this defect`, `Could not prove this bug`, `Fixed automatically`, `Fixed locally with implementation-plan context.`, `Tracked in #`); on **any** thread (regardless of severity) this command additionally treats its own `Answered with implementation-plan context.` reply as terminal — a human-question answer **and** an already-addressed outdated thread of any type (including a 🐛 bug, see *Validate*) both use it, so it must close the thread for every severity or an outdated bug thread would be re-selected and re-replied every run; and for non-bug comments it also treats its own `Declined with implementation-plan context.` reply as terminal (a declined 🐛 bug uses the canonical `Could not prove this defect` instead). A terminally-processed thread is done; skip it.
4. **Parse the overflow findings (thread-less AI findings).** The reviewer puts findings it cannot place inline into an *overflow* block in the review **body** — these have no thread, no `databaseId`, and no resolved state, so the thread query above never sees them:
   - Fetch the review bodies with `scripts/with-env.sh gh pr view <PR#> --json reviews` (the same accessor the autonomous path reads, `maestro/vcs.py::SubprocessCodeHost.get_pr_review_bodies`). Take the **most recent** review whose body contains the block between the `<!-- maestro:overflow -->` and `<!-- /maestro:overflow -->` sentinels (legacy fallback: the bullet list directly under the `**Additional findings** (could not be placed inline):` header).
   - Each block line has the shape `- of-<hash8> **<file>:<line>** [<severity>] — <title>: <description>`; the `of-<hash8>` fingerprint is the finding's stable handle. The canonical *bug* parser is `maestro/evolution_tend.py::_eligible_overflow_bug_findings`; this command extends it to **all** severities — keep `[bug]`, `[suggestion]`, and `[nit]` lines.
   - Fetch the PR's issue comments (`scripts/with-env.sh gh pr view <PR#> --json comments`) and locate the roll-up acknowledgement comment containing `<!-- maestro:overflow-ack -->`. Skip any finding whose fingerprint already appears there on a line with one of the terminal markers from step 3 — it is done.
5. **Parse top-level human PR comments (non-thread).** A human can also comment in the PR conversation rather than inline. From the same `gh pr view <PR#> --json comments` payload, keep a comment when its `author.login` is a real user (not ending in `[bot]`) and it asks for a change or a question about the code. **Exclude:** this command's own roll-up comments (those starting with `<!-- maestro:overflow-ack -->` or `<!-- maestro:human-ack -->`), automation/bot comments, and pure approvals or chatter (e.g. "LGTM", "thanks") that request nothing. Locate the human-acknowledgement roll-up comment containing `<!-- maestro:human-ack -->`; skip any comment whose id already appears there on a line with a terminal marker from step 3.
6. **Build the work list:**
   - If `$ARGUMENTS` pinned a `<comment-id>`, the work list is just that inline thread; overflow findings and top-level human comments are excluded — the pin targets one inline comment. If the thread is resolved or already terminal (per the marker set in the filter bullet above), stop and say so rather than reprocessing; an **outdated** thread is processed normally (it is in scope).
   - Otherwise the work list is **every** eligible item: AI finding threads (in thread order), then eligible human inline threads, then every unacknowledged overflow finding (in block order), then every unacknowledged top-level human comment. Report how many of each you found, broken down by severity / type.
   - If nothing is eligible, stop with `error=no_eligible_comment` and report what was filtered out (resolved / bot-authored / already-fixed / already-acknowledged). Outdated threads are **not** a filtered-out category any more — an outdated-but-unresolved, non-terminal thread is eligible.

## Validate and address each comment

Process **every** item on the work list, applying a validate→apply/decline discipline adapted to the item's type, augmented with the plan. An overflow finding is validated and addressed exactly like a thread of the same severity — its `file:line`, severity, title, and description come from the parsed overflow line; only the acknowledgement mechanism differs (see *Commit, push, and reply*). A top-level human comment is addressed like a human inline thread; only the reply mechanism differs. For each item, decide whether to change code and what reply it earns:

1. **Validate** — determine, per type, whether to act:
   - **Outdated thread (any type) — check this first.** When the thread's `isOutdated` flag (carried from *Select*) is true, the code it was anchored to has changed since the comment was posted, so re-validate the concern against the **current** tree before anything else. (a) If an intervening change already addressed it — the flagged code is gone or rewritten so the concern no longer holds — record `verdict=already-addressed` (no code change) and reply by acknowledging it. (b) If the concern still applies to the current code (it merely moved, or the change was unrelated), fall through to the type-specific validation below and act on it exactly as a live thread of that type. Never treat `isOutdated` as "already done" without reading the code — confirm one way or the other.
   - **Bug (🐛).** Confirm a genuine defect, preferring a test over static reasoning. **Test-based (preferred):** using `Read`/`Glob`/`Grep`/`Bash`, write or identify a focused test that FAILS with the defect present and PASSES once fixed; run it to confirm the failure. Place tests per the repo's testing principles (`mem_fs`, mocked externals, no real I/O). **Static analysis (fallback):** when a test is impractical (race condition, type-level, security), trace data flow and edge cases. If the plan shows the flagged behavior was a deliberate, documented design choice (an invariant the reviewer misread), that is strong evidence of a false positive — record `verdict=not-a-defect` and do not invent a fix.
   - **Suggestion / nit (💡 / ✏️) and actionable human request.** Validate that the change is a genuine, in-scope improvement: it must be (a) correct, (b) a real improvement to clarity, correctness, efficiency, or test coverage, and (c) consistent with the plan's intent and the architecture invariants in `CLAUDE.md`. Prefer a concrete check — a new or adapted test demonstrating the improved behavior holds. If the suggestion is wrong, redundant, or conflicts with a deliberate design choice the plan documents, record `verdict=declined` with the reasoning and do not change code to chase it.
   - **Human question / discussion (non-actionable).** No code change is requested — answer it from the code and the plan. Record `verdict=answer` with the answer text.
2. **Apply** — only once the item is validated as worth acting on:
   - **Read the file and surrounding context.** Make a **minimal, targeted** change that follows both the comment and the implementation plan's intent — reuse the helpers and patterns the plan named instead of reimplementing; respect the architecture invariants in `CLAUDE.md`.
   - **Add a test** that proves the change. A bug fix's test must fail before and pass after. An applied suggestion/nit gets a test covering the new behavior where it is testable; a purely stylistic nit with no behavioral change is noted as such (no test) rather than inventing a vacuous one.
   - If acting on the item properly would materially exceed its scope (it exposes a deeper design problem the plan did not anticipate), do not balloon the change — record `verdict=out-of-scope`, leave it unchanged, and surface it to the user in the report rather than acting silently.
3. **Decline / answer / acknowledge** — record the reasoning (declined non-bug, not-a-defect bug), the answer (human question), or the already-addressed acknowledgement (outdated thread an intervening change resolved) for the reply; no code change.
4. **Keep changes coherent.** Address each item on its own merits; when two comments touch the same code, reconcile them into one consistent change rather than conflicting patches. Track a per-item verdict so the report and replies are accurate.

## Quality gates

After all changes are applied, run the gates **once** over the combined change for the packages it touched (detect from `git diff`):

**Core** (if `maestro/` or `tests/`):
```
python3.14 -m ruff check maestro/ tests/ && python3.14 -m mypy maestro/ && python3.14 -m maestro.cli gate-lease -- python3.14 -m pytest tests/ -v {worker_args}
```

**Classify a failed core-suite run.** If the parallel core-suite run above fails, do not blindly re-run it — pipe its combined output to `python3.14 -m maestro.cli gate-classify --tool tests --returncode <rc>` (pass the run's exit status as `<rc>`), which prints one classification token. `FAILED` / `COLLECTION_ERROR` are an *answered* red: the run already enumerated the failures, so fix them rather than re-running. `COVERAGE` / `TIMEOUT` / `UNAVAILABLE` are resource artifacts — an OOM-killed xdist worker's lost coverage tripping `--cov-fail-under=100`, or a runner that was killed or could not start — so re-run the suite once through `python3.14 -m maestro.cli gate-lease` and let that result decide, rather than "fixing" a coverage drop that only appears under contention. This mirrors `scripts/quality-gates.sh gate_maestro`, which the agent may also invoke directly instead of the raw command. The `python3.14 -m maestro.cli` module form (rather than a bare `maestro`) mirrors that script's `maestro()` shell function so the CLI self-resolves to *this* checkout — a stale `~/.local/bin/maestro` from a sibling checkout would otherwise load its own config models against this checkout's `maestro.toml` and raise a spurious pydantic `extra_forbidden`.

**Review** (if `maestro_review/` or `tests/test_maestro_review/`):
```
python3.14 -m ruff check maestro_review/ tests/test_maestro_review/ && python3.14 -m mypy maestro_review/ && python3.14 -m pytest tests/test_maestro_review/ -v --no-cov
```

**Browser** (if `maestro/dashboard/` or `tests/browser/`):
```
python3.14 -m pytest tests/browser/ -v -m browser --no-cov --browser chromium
```

**Run each of these in the foreground** with an explicit `Bash` `timeout` of `600000` (the 10-minute max) — never `run_in_background: true`. A backgrounded gate run is reaped when the turn yields, so the suite dies silently and the remediation stalls before the push. Run the package groups as separate foreground calls so each fits the ceiling. If a run is ever backgrounded anyway, poll it with `BashOutput` until the shell exits before doing anything else, and never end your turn while a gate run is still pending.

Fix failures at the root cause — never silence with `# noqa`, `# type: ignore`, or pytest skips. If the same gate fails three times on the same root cause, stop and surface to the user. If a change is correct but broke other tests that relied on the old behavior, update those tests, not the change.

## Self-review

Run `git diff` and check the combined change: no leftover debug code or `breakpoint()`/`pdb`, no hard-coded paths or secrets, error paths handled, each test exercises the changed branch and not just the happy path, and every change stays within its comment's scope.

## Commit, push, and reply

A reply that says `Fixed locally …` is only true once the change is on the PR branch, so this command ships the changes **first**, then posts the per-comment replies itself. The push is the gate: if it fails, the replies are aborted (an un-pushed "Fixed" reply would misrepresent the PR state).

1. **Nothing to ship?** If no item produced a code change (every verdict is `not-a-defect`, `declined`, `answer`, `already-addressed`, or `out-of-scope`), there is no code change to push. Skip steps 2–3 and post only the reply and acknowledgement lines in steps 4–6 — those don't depend on a pushed change.
2. **Commit the changes.** Stage only the files you changed (explicit `git add <file>...`, never `git add -A`, never `.env`/secrets/credentials). Commit with a HEREDOC message that explains *why* the changes were needed (not just "address comments"), ending with `Co-Authored-By: Claude <noreply@anthropic.com>`:
   ```
   git commit -m "$(cat <<'EOF'
   fix: address PR #<PR#> review comments

   <one line per applied change>

   Co-Authored-By: Claude <noreply@anthropic.com>
   EOF
   )"
   ```
3. **Merge upstream, then push to the PR branch.** Fetch and fast-forward any new upstream commits before pushing so the push is not rejected:
   ```
   scripts/with-env.sh git fetch origin
   # Skip the merge when the branch has no upstream tracking ref — `git merge @{u}`
   # would abort with `fatal: no upstream configured`, which is not a conflict.
   git rev-parse --abbrev-ref --symbolic-full-name @{u} >/dev/null 2>&1 && git merge @{u} --no-edit
   ```
   If the branch has no upstream yet the merge is skipped (nothing to fast-forward) and you proceed straight to the push. If the merge conflicts, resolve every conflict by editing the files (remove all `<<<<<<<`/`=======`/`>>>>>>>` markers), `git add` the resolved files, and `git commit --no-edit`. **If the merge brought in new upstream commits** (a fast-forward, a merge commit, or a resolved conflict — i.e. the working tree changed), re-run the quality gates from the *Quality gates* section over the merged tree before pushing — mirroring `/merge-main` step 6 (which re-runs gates whenever a merge changed the workspace). An upstream change that interacts with the changes, or a bad conflict resolution, must abort the push rather than land on the branch and then carry a `Fixed locally …` reply; if those gates fail, treat it as a push failure (stop, report, and abort the step-4 replies). When the merge was a no-op (no upstream, or already up to date), the gates already passed over the changes and need not re-run. Then push to the PR branch (the `headRefName` confirmed in *Resolve the PR* — **never** `main`, **never** force-push):
   ```
   scripts/with-env.sh git push origin HEAD
   ```
   If the push fails for any reason, **stop here**: report the failure and the unposted replies, and do **not** post any reply — the changes are not on the branch.
4. **Post the reply for each processed thread**, only after a successful push (or, in the nothing-to-ship case, directly). Use the canonical marker wording (`maestro_review/markers.py`) so the threads read consistently with autonomously-processed ones:
   - **Applied** (bug fix, or suggestion/nit/human change applied): reply body `Fixed locally with implementation-plan context.\n\n<one-line reasoning>` (the `_FIXED_LOCALLY_MARKER` text from `maestro_review/markers.py` — a terminal marker, so later runs and the autonomous path treat the thread as done), posted via
     ```
     scripts/with-env.sh gh api repos/<owner>/<repo>/pulls/<PR#>/comments --method POST --input - <<'EOF'
     {"body":"Fixed locally with implementation-plan context.\n\n<reasoning>","in_reply_to":<comment-id>}
     EOF
     ```
   - **Not a defect** (a 🐛 bug only): post the disagreement reply (`Could not prove this defect …`, the `_DISAGREEMENT_MARKER` text from `maestro_review/markers.py`) so the next `maestro-review` run detects the disagreement and escalates it to an issue rather than re-flagging.
   - **Declined** (a suggestion/nit or human request validated but not applied): reply `Declined with implementation-plan context.\n\n<reasoning>`. This is terminal for re-runs, but it is **not** the bug disagreement marker — a declined nit must not be escalated as a disputed bug.
   - **Answer** (a human question): reply `Answered with implementation-plan context.\n\n<answer>` — terminal for re-runs, no code change implied.
   - **Already addressed** (an outdated thread an intervening change resolved): reply `Answered with implementation-plan context.\n\n<one line: the flagged code changed since this comment so the concern no longer applies>` — the same terminal `Answered …` wording, since no code change was made this run and the thread is genuinely done.
   - **Out of scope** (any type): post a **non-terminal** reply whose first line is the *out-of-scope marker* — the stable prefix `Surfaced for human review — out of scope for automated remediation.` — followed by `\n\n<one line on what acting on it would take>`, so the thread carries a response while staying open for the user to decide. This is the one new marker string this command introduces (it is **not** in `maestro_review/markers.py`); spell it out verbatim here and refer back to it as the *out-of-scope marker* below. It is deliberately **not** a terminal marker (a re-run will reprocess it, and it must not read as a fix or a closed disagreement) — **skip posting it if the thread already carries a reply beginning with the out-of-scope marker prefix** (match on that stable prefix, **not** the full reply, since the trailing reasoning line varies between runs), to avoid duplicates. Because it is non-terminal and **not** in `maestro/evolution_tend.py::_BUG_COMMENT_TERMINAL_MARKERS`, a **live** (non-outdated) out-of-scope 🐛 bug thread can still be selected by the autonomous path (`maestro/evolution_tend.py::_eligible_bug_comments` / `tend_evolution_prs`) and routed into a remediation task; only *outdated* threads are shielded there (the autonomous path skips outdated). So "out of scope for automated remediation" is best-effort for live bug threads — surfacing it for human review does not guarantee the loop will leave it alone — which is the user's cue to resolve or convert it to an issue if the loop should not retry it.
   Post one reply per processed thread (including outdated threads); if an individual `gh api` reply call fails, record it and continue with the remaining threads rather than aborting the batch.
5. **Record overflow verdicts on the acknowledgement comment**, behind the same push gate (or directly in the nothing-to-ship case). An overflow finding has no thread to reply to or resolve, so its terminal state lives on the single roll-up acknowledgement comment instead:
   - Locate the PR issue comment containing `<!-- maestro:overflow-ack -->`. If none exists, create it with `scripts/with-env.sh gh pr comment <PR#> --body ...`, starting the body with that sentinel line.
   - Append one line per processed finding, keyed by fingerprint and reusing the exact marker wording from `maestro_review/markers.py` so the autonomous parser (`maestro/evolution_tend.py::_eligible_overflow_bug_findings`) treats a bug as terminal: `of-<hash> — Fixed locally with implementation-plan context.` for an applied finding (`_FIXED_LOCALLY_MARKER`), `of-<hash> — Could not prove this defect — <reasoning>` for an unproven bug (`_DISAGREEMENT_MARKER`), or `of-<hash> — Declined with implementation-plan context. — <reasoning>` for a declined suggestion/nit (terminal for this command's re-runs; the autonomous bug parser ignores non-`[bug]` lines).
   - Update an existing comment via `scripts/with-env.sh gh api --method PATCH repos/<owner>/<repo>/issues/comments/<comment-id> -f body=<updated body>`. Never rewrite or remove existing acknowledgement lines — only append.
   - `out-of-scope` findings get a **non-terminal** line `of-<hash> — <out-of-scope marker> — <what acting on it would take>`, where `<out-of-scope marker>` is the stable prefix introduced in step 4, so they too carry an acknowledgement, while a re-run still reprocesses them. Append it only if the fingerprint does not already carry a line bearing the out-of-scope marker (match on `of-<hash>` + the stable prefix, **not** the full line, since the trailing text varies) — no duplicates; also surface them in the report.
6. **Record top-level human verdicts on the human acknowledgement comment**, behind the same push gate (or directly in the nothing-to-ship case). A top-level PR comment is not a thread, so — mirroring the overflow roll-up — its terminal state lives on a single `<!-- maestro:human-ack -->` comment:
   - Locate the PR issue comment containing `<!-- maestro:human-ack -->`. If none exists, create it with `scripts/with-env.sh gh pr comment <PR#> --body ...`, starting the body with that sentinel line.
   - Append one line per processed comment, keyed by the comment's id: `<id> — Fixed locally with implementation-plan context.` (applied), `<id> — Declined with implementation-plan context. — <reasoning>` (declined), or `<id> — Answered with implementation-plan context. — <answer summary>` (answered). Never rewrite existing lines — only append.
   - `out-of-scope` top-level comments get a **non-terminal** line `<id> — <out-of-scope marker> — <what acting on it would take>`, where `<out-of-scope marker>` is the stable prefix introduced in step 4, so they too carry an acknowledgement while a re-run reprocesses them. Append it only if the id does not already carry a line bearing the out-of-scope marker (match on `<id>` + the stable prefix, **not** the full line, since the trailing text varies) — no duplicates; also surface them in the report.
7. **Completion guarantee — every comment has a reply.** Before reporting success, confirm that **every** item on the work list now carries a response: each processed thread (including every outdated one) has a posted reply, and each overflow finding and top-level human comment has an acknowledgement line (terminal for applied/declined/answered/already-addressed, non-terminal for out-of-scope). Re-fetch the threads (the GraphQL query from *Select*) and the acknowledgement comments and verify this. If any item is missing its reply — typically because an individual `gh api` call failed in step 4–6 — retry that one post once; if it still fails, the run is **incomplete**: list each comment left without a reply in the report so the user can re-run or post manually. The only sanctioned reason for an item to lack a reply is an aborted push (step 3), which already stops before step 4 and is reported there.

## Report

End with a plain-text summary:

- **PR** — number, title, branch.
- **Plan context** — the plan path used (or that none was found), and one line on how it shaped the changes.
- **Comments** — how many eligible items were found, broken down by type/severity (🐛 bug / 💡 suggestion / ✏️ nit / human inline / human top-level), how many of those threads were **outdated**, and the title (or first line) / file / `databaseId`-or-id for each.
- **Overflow findings** — how many overflow findings were parsed from the review body, how many remained after acknowledgement filtering, and the fingerprint / file / severity / title for each (or that none were present).
- **Verdicts** — per item: `defect-confirmed` / `applied` (with evidence type: test / static analysis / verification), `not-a-defect` (with why), `declined` (with why), `answer` (with the question), `already-addressed` (an outdated thread an intervening change resolved, with how), or `out-of-scope` (with what acting on it would take).
- **Changes** — files modified, one line each; the tests added or adapted.
- **Quality gates** — which ran, which passed, how many fix iterations.
- **Shipped** — the commit pushed to the PR branch (or that there was nothing to ship), and any upstream merge/conflicts resolved before the push.
- **Replies** — per thread, which reply was posted (`Fixed locally …` / disagreement / `Declined …` / `Answered …` / non-terminal out-of-scope `Surfaced for human review …`); per overflow finding and per top-level human comment, which acknowledgement line was written; or that the push failed and replies were aborted.
- **Completeness** — confirm every eligible comment now carries a reply or acknowledgement line (the completion guarantee), or list each one still missing a reply (e.g. a `gh api` post that failed twice) so the user can re-run or post it manually.

## Safety

- Read-only against `~/.claude/plans/`, the pointer file `~/.claude/last-plan-by-folder.json`, and the plan; the only repo writes are the code/test changes under the working directory and the single commit that ships them.
- Never auto-switch branches, force-push, push to `main`, or run destructive git (`reset --hard`, `checkout --`, `clean -f`, branch deletes). Commit and push only to the PR branch confirmed in *Resolve the PR*, and only the files you changed (never `git add -A`).
- The push to the PR branch is the gate for replies: never post a `Fixed locally …` reply for a change that is not on the branch. If the push fails, abort the replies and report.
- Never commit or read out `.env`, credentials, or secret files.
- If a comment cannot be proven a defect, or a suggestion cannot be shown worth applying, do not fabricate a change — record `not-a-defect` / `declined` for it and move on.
- All eligible comments — including outdated threads — are addressed in one invocation — act on every confirmed item, ship the batch to the PR branch, then post the replies and acknowledgement lines. The run is not complete until every eligible comment carries a reply or acknowledgement line; any left without one (e.g. a failed `gh api` post) is reported, never silently dropped.
