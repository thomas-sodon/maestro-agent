Triage a GitHub issue (specified, latest RCA-shaped, or oldest open) for the Maestro project against today's tree, focusing on the *intent* of the issue rather than literal line references. Produce one of two terminal outcomes: a fix plan or an issue closure.

This command is the targeted sibling of `/backlog`: it accepts an explicit issue number, auto-picks the **latest** open RCA-shaped issue under `--rca`, or — if neither is supplied — picks the oldest open actionable issue. The triage answers exactly one question — *does the bug behavior the issue describes still apply to today's code?* Cited file paths, line numbers, and symbols are anchors for the implementer, not gating evidence; line drift, renames, and file moves never by themselves veto a plan or fire a close. If the underlying behavior is still wrong in tree, this command writes a plan at `~/.claude/plans/fix-<NNN>-<slug>.md` framed around intent at *current* code locations. If today's code already exhibits the correct behavior or the entire feature surface the issue cited has been removed, this command closes the issue with a comment quoting the evidence. When neither verdict is clear-cut, the plan path is the default — a wrong plan costs a file delete, while a wrong close is publicly visible and notifies subscribers. `/backlog` is the conservative auto-walk variant with strict close triggers; `/bug-fix` is the targeted-or-walk variant — now including the latest-RCA auto-pick (`--rca`) absorbed from the former `/rca-fix` — with broader intent-based triage, the same auto-close discipline, and only the two plan/close terminals.

`$ARGUMENTS` is optional. Recognized tokens (any order, space-separated):

- A positive integer (with or without a leading `#`) — the GitHub issue number to triage. Reject anything else that looks like a number-attempt (`abc`, `1.2`, `0`, negative).
- `--rca` — auto-pick the **latest** (most recently created) open RCA-shaped issue (title starts with `[rca]` or label includes `rca`), newest-first. Mutually exclusive with an explicit integer.
- `--dry-run` — print the picked issue, the triage decision, and (for the plan branch) the plan body or (for the close branch) the close comment, without writing the plan file or calling `gh issue close`.
- *(no integer and no `--rca`)* — pick the oldest open actionable issue using the same skip rules as `/backlog`.

`--dry-run` composes with any of the three modes. Reject any other token shape — including an explicit integer combined with `--rca` — with `error=invalid_argument` and the expected forms (`/bug-fix 1234`, `/bug-fix --rca`, `/bug-fix`, `/bug-fix --dry-run`, `/bug-fix --rca --dry-run`, `/bug-fix 1234 --dry-run`).

## Authentication

GitHub access needs `GITHUB_TOKEN` from `.env` in the project root. Run every `gh`, `git push`, and `git fetch` command through the wrapper, which loads `.env` and then execs the command; the examples below already use it:

```
scripts/with-env.sh <command>
```

If `.env` does not exist or the token is empty, stop before the first `gh` call and tell the user to populate it. Local repo reads (`git log`, `git show`, file existence checks) do not need the token.

## Switch to main if on a merged branch

Land on a current tree before triage. This command verifies "today's tree", so a leftover task branch that has already merged into main would triage stale code. Switch to `main` **first** — but only when it is provably safe, and never under `--dry-run`: the switch and the on-`main` refresh below are both local mutations, so under `--dry-run` this entire section changes no git state — it prints the intent it *would* execute and proceeds on the current branch.

1. Resolve the current branch: `git rev-parse --abbrev-ref HEAD`.
   - `main` → already on `main`, so no switch is needed — but still refresh so triage reads the latest `main`, not a stale local one. On a clean tree (`git status --porcelain` empty), run the section's best-effort `scripts/with-env.sh git fetch origin main` and then `git merge --ff-only origin/main` to advance local `main` to the fetched tip before triage (the exit-0 refresh of step 4, minus the redundant `git switch main`; skip the fast-forward on the fetch-fallback path, where there is no fresher `origin/main` to advance to). If the tree is dirty, skip the refresh and proceed on the current `main`, leaving uncommitted changes untouched. If the fetch is unavailable (no token, offline) or the `git merge --ff-only origin/main` fails (local `main` has diverged), warn that local `main` could not be advanced and proceed on the current local `main`.
   - `HEAD` (detached) → no branch to evaluate; warn and proceed without switching.
2. Dirty-tree guard. If `git status --porcelain` is non-empty, do **not** switch — uncommitted changes are left untouched per *Safety*. Warn that the switch was skipped because of local changes and proceed on the current branch.
3. Refresh main (best-effort): `scripts/with-env.sh git fetch origin main`. On failure (no token, offline), fall back to the local `main` ref; the merged check still runs, just against a possibly-stale `main`.
4. Fully-merged test. The branch is fully merged when every commit on it is already in main — `git merge-base --is-ancestor HEAD origin/main` (or `HEAD main` on the fetch fallback) exits 0.
   - Exit 0 → `git switch main`, then fast-forward local `main` to the fetched tip with `git merge --ff-only origin/main` so the working tree lands on the merged code, not a stale local `main` that the fetch never advanced (skip this fast-forward on the fetch-fallback path, where there is no fresher `origin/main` to advance to). On success, announce the switch (`Switched from <branch> (fully merged) to main`). If `git switch main` fails (e.g. `main` is already checked out in a sibling worktree, or local `main` is missing/invalid) or the `git merge --ff-only origin/main` fails (e.g. local `main` has diverged), warn that the switch could not complete and proceed on the current branch — exactly like the dirty-tree, fetch-failure, and unmerged-branch fallbacks above.
   - Non-zero → the branch carries unmerged commits; leave it, warn that the branch has unmerged work, and proceed on the current branch.

The switch never discards commits (it fires only when the branch adds nothing to main), never runs against a dirty tree, and is a plain checkout of `main` followed by a fast-forward to `origin/main` — no branch is created, no history rewritten.

## Progress tracking

This command is invocable from multiple working folders on the same machine — different terminal tabs, sibling worktrees, parallel agent sessions. Two folders triaging the same issue in parallel risk duplicate GitHub fetches, a stomped plan file, or worst-case a duplicate `gh issue close`. Progress is tracked in a shared JSON file at `~/.claude/bug-fix-progress.json` so every invocation can see what other folders are working on and refuse stale work. The file is the only cross-folder coordination surface; there is no broker, no daemon, no distributed lock — just a shared file with atomic write semantics. The contract is *this folder loses cleanly with a named error when it sees a sibling claim*, never *both folders mutate*.

### File format

Object keyed by issue number (string). Missing file → treat as `{}`. Malformed JSON → warn, treat as `{}`, do not overwrite silently — print the parse error so the user can repair it.

```json
{
  "1234": {
    "folder": "<absolute path of this command's working directory>",
    "started_at": "2025-05-08T12:34:56Z",
    "status": "in_progress",
    "completed_at": null,
    "outcome": null
  }
}
```

`started_at` and `completed_at` are pinned to ISO 8601 UTC with second precision and a literal `Z` suffix (`YYYY-MM-DDTHH:MM:SSZ`) — no fractional seconds, no `+00:00` form. The single canonical representation lets the 24h-TTL math and the terminal-path identity check (which compares the original `started_at` byte-for-byte) work without drift across implementations. The byte-for-byte check is a raw string comparison — implementations must not parse-then-reformat either side before comparing, because a parser/formatter round-trip can rewrite `Z` to `+00:00` (or vice versa) and produce a phantom mismatch.

Status values:

- `in_progress` — a folder is currently triaging. The terminal state is reached only when a plan is written or the issue is closed.
- `plan_complete` — terminal. A plan file was written; `outcome` is the absolute plan path.
- `issue_closed` — terminal. The issue was closed via `gh issue close`; `outcome` is the one-line close reason.

There are exactly two terminal statuses.

### Read the file at the start of triage

Read the progress file and look up the candidate issue number — on the targeted path this happens immediately after the initial `gh issue view` confirms the issue exists and is actionable (state, lock, duplicate label), so a sibling claim short-circuits before the local-plan glob and the open-PR queries spend their cost; on the auto-pick path this happens per candidate during backlog iteration as the first per-issue gate beyond the in-memory list-data skip rules (labels, locked), so a sibling-claimed issue is dropped before existing-plan globs and open-PR queries run for it. Branch on the existing entry:

| Entry shape | Targeted path | Auto-pick path |
|---|---|---|
| No entry | proceed (write claim later) | proceed |
| `in_progress`, `folder` matches this working dir | warn `stale claim from <started_at>`, proceed (overwrite) | warn, proceed |
| `in_progress`, `folder` is a different path, `started_at` within 24h | stop with `error=issue_claimed_by_other_folder`, print issue URL + claiming folder + `started_at` | silent skip (same disposition as existing-plan / open-PR skip), log in skip table with reason `claimed_by <folder>` |
| `in_progress`, `folder` is a different path, `started_at` older than 24h | warn `abandoned claim from <folder> at <started_at>`, proceed (overwrite) | warn, proceed |
| Terminal status, `folder` matches this working dir | warn `previously <status> at <completed_at>`, proceed | silent skip (auto-pick already handles re-iteration via existing skip rules) |
| Terminal status, `folder` is a different path | warn with prior `outcome`, proceed | silent skip |

The 24-hour TTL is the only stale-claim recovery — no PID check, no signal-based liveness probe (the claiming process may not even be running anymore; the working folder is the identity). A crashed sibling leaves an `in_progress` entry that any folder reclaims after 24h elapse.

User-driven recovery before the TTL expires: the user opens `~/.claude/bug-fix-progress.json` and deletes the offending entry, then re-runs `/bug-fix`. The command never deletes another folder's claim on its own.

### Write the in-progress claim

After the issue is resolved enough to identify (state/lock/duplicate-label checks have cleared on the targeted path, or the candidate has survived the in-memory list-data skip rules on the auto-pick path) and before the remaining prechecks (existing-plan glob, open-PR query) and the **Read and parse the picked issue** section, write the `in_progress` claim. Use atomic write semantics: serialize the full updated object, write to `~/.claude/bug-fix-progress.json.tmp.<pid>`, then rename in place. Read-modify-write must preserve unrelated keys untouched. If the write fails (disk full, permission denied, parent dir missing), surface a warning and proceed — progress tracking is best-effort and never blocks the triage from running.

In `--dry-run` mode, do not write the claim. Print the claim that *would* be written (issue number, folder, `started_at`) and continue.

### Update the claim at the terminal path

Each terminal path writes the final claim state as the very last step before the user-facing print, with the same atomic read-modify-write so unrelated entries stay intact:

| Terminal path | `status` | `outcome` |
|---|---|---|
| Plan written | `plan_complete` | absolute plan path |
| Issue closed | `issue_closed` | one-line close reason (matching the comment's first line) |

`completed_at` is the current ISO 8601 UTC timestamp in both cases (same `YYYY-MM-DDTHH:MM:SSZ` shape as `started_at`). In `--dry-run` mode, do not write the update. Print the update that *would* be written and continue.

### Release the progress-file claim

Before deleting, re-read `~/.claude/bug-fix-progress.json` and inspect the entry under `<NNN>`:

- Entry is missing → nothing to do; another folder hand-edited the file or the claim was never written. Exit silently.
- Entry's `folder` and `started_at` both match this folder's original claim → delete the entry (atomic read-modify-write, preserving unrelated keys), then exit.
- Entry's `folder` is a different path *or* `started_at` differs from this folder's original claim → a sibling reclaimed the issue. Leave the file alone — deleting by issue number would clobber the sibling's active triage. Exit silently; the sibling will produce the terminal outcome on its own schedule.

The release path never surfaces an error of its own. Sibling overwrites detected here are normal.

## Resolve the target issue

Three paths, branching on `$ARGUMENTS`: an explicit integer routes to the **targeted path**; `--rca` routes to the **RCA auto-pick path** (latest RCA-shaped, newest-first); neither routes to the **oldest-first auto-pick path**.

### Targeted path — explicit issue number

1. Strip a leading `#`. Validate the remainder is a positive integer. Otherwise stop with `error=invalid_argument`.

2. Fetch the issue. **`gh issue view --json` does not accept `locked`** — passing it produces `Unknown JSON field: "locked"` and aborts the call. Fetch the lock state separately via the REST API. The two calls must stay separate; never collapse them by adding `locked` to the `--json` list:

   ```
   scripts/with-env.sh gh issue view <NNN> --json number,title,body,state,labels,createdAt,closedAt,url
   scripts/with-env.sh gh api repos/{owner}/{repo}/issues/<NNN> --jq .locked
   ```

   - `gh` returns "not found" → stop with `error=issue_not_found`.
   - `state == "closed"` → stop with `error=issue_already_closed` and quote the close timestamp; suggest reopening only if the bug is still observable against today's tree.
   - the lock fetch returns `true` → stop with `error=issue_locked` and the URL; the user must unlock before this command can act.
   - Title or labels suggest a duplicate (title contains `duplicate`/`dup`, label `duplicate`) → stop with `error=duplicate` and tell the user to point at the canonical issue.
   - The labels include `wontfix`, `question`, `discussion`, or `epic` → warn but **proceed**. The user explicitly named this issue, so the triage runs; the closing message names the surprising label so the user can reconsider scope before invoking `/impl`.

3. Progress-file precheck (early claim). Read `~/.claude/bug-fix-progress.json` and look up the entry for `<NNN>`. Apply the targeted-path branch from *Progress tracking / Read the file at the start of triage* — an `in_progress` claim from a *different* folder within the 24h TTL stops with `error=issue_claimed_by_other_folder` and prints the claiming folder + `started_at`; everything else (no entry, own stale claim, terminal entry from any folder, abandoned claim past TTL) warns-and-proceeds. Then write this folder's `in_progress` claim atomically per *Progress tracking / Write the in-progress claim*. This fires before the existing-plan glob and the open-PR queries so a sibling claim short-circuits the targeted run before any further `gh` API spend. Any race re-check that aborts this folder's triage must release the own claim per *Release the progress-file claim*.

4. Existing plan precheck. Glob `~/.claude/plans/fix-<NNN>-*.md`:
   - Match found → warn that a plan already exists, name the path, and **proceed**. The targeted path overwrites (the user explicitly asked for a fresh triage). Surface the existing plan path in the closing message so the user knows a previous plan was replaced.
   - No match → proceed silently.

5. Open-PR precheck. Run one search, then post-filter for a closing keyword:

   ```
   scripts/with-env.sh gh pr list --state open --search "#<NNN> in:body" --json number,url,body
   ```

   Keep only PRs whose body contains a GitHub closing keyword immediately before the reference — a case-insensitive `\b(close[sd]?|fix(es|ed)?|resolve[sd]?):?\s+#<NNN>\b` match. The leading `\b` keeps the keyword whole (so `discloses #<NNN>` is not read as `closes #<NNN>`); the optional `:?` accepts GitHub's colon-separated form (`Fixes: #<NNN>`); the trailing `\b` bounds the issue number on a non-digit, so triaging `#12` does not match a PR's `Fixes #123` — bare-substring matching of the digits alone is insufficient. This covers every closing keyword GitHub recognizes (`close`/`closes`/`closed`, `fix`/`fixes`/`fixed`, `resolve`/`resolves`/`resolved`), not just `Fixes`/`Closes`, and excludes a bare non-closing mention like `see #<NNN>`. Keep the `in:body` field — PR-title keywords are not auto-linked by GitHub. A non-empty filtered result means an implementer is already on it — warn (with the PR URL) but **proceed**. The user may want a fresh take; surface the open PR in the closing message. This is the early check; the close-path re-runs the same detection immediately before `gh issue close` per *Concurrent sessions*, since a sibling session may open a PR during triage.

Every early precheck (steps 1-5) halts under `--dry-run` identically — early-precheck stops fire on conditions that prevent a coherent dry-run output (no integer, no issue, wrong issue, sibling claim), so they predate the dry-run/re-check warning carve-out and always halt before any draft output is prepared.

The picked issue is whichever number `$ARGUMENTS` named.

### RCA auto-pick path — `--rca`

The picker is fixed to the **latest** open RCA-shaped issue (most recently created); there is no targeting here. Combining `--rca` with an explicit integer is rejected at argument parse with `error=invalid_argument`.

1. List open RCA-shaped issues, newest first. Run both queries (label and title prefix) and union the results so an issue tagged either way is found:

   ```
   scripts/with-env.sh gh issue list --state open --label rca --sort created --order desc --limit 50 --json number,title,createdAt,labels,url
   scripts/with-env.sh gh issue list --state open --search '"[rca]" in:title' --sort created --order desc --limit 50 --json number,title,createdAt,labels,url
   ```

   `gh issue list` already excludes pull requests by default. Dedupe by issue number; if an issue appears in both lists, prefer the row from the label query (richer metadata). Re-sort the unioned list by `createdAt` descending (newest first). If the list is empty, stop with `error=no_open_rca_issues` — there is nothing to do.

2. Iterate the unioned list newest-to-oldest, applying the same per-candidate skip rules as the oldest-first path: skip (recording the reason for the final summary) when the labels include `wontfix`/`duplicate`/`question`/`discussion`/`epic`, a plan already exists at `~/.claude/plans/fix-<NNN>-*.md`, an open PR is linked (a single `#<NNN> in:body` search post-filtered for a GitHub closing keyword — a case-insensitive `\b(close[sd]?|fix(es|ed)?|resolve[sd]?):?\s+#<NNN>\b` match, the boundaries keeping the keyword whole and bounding the issue number on a non-digit so `#12` does not match `#123`, covering the full closing-keyword set, not just `Fixes`/`Closes`), or the progress-file entry shows a sibling claim within the 24h TTL or any terminal status (per *Progress tracking / Read the file at the start of triage*, auto-pick column). Lock state is not in `gh issue list --json`, so a locked pick is caught at the re-fetch in step 4 and recorded with reason `locked`.

3. The first non-skipped issue is the **picked issue**. If iteration exhausts the list without finding one, stop with `error=no_actionable_rca_issue` and print the skip table (issue number, title, skip reason).

4. After picking, write the `in_progress` claim per *Progress tracking / Write the in-progress claim*, then re-fetch the picked issue and re-run the open-PR detection against it before reading-and-parsing — a sibling session may have closed, locked, labeled, or PR-linked it between this run's pick and now. `gh issue view --json` does not accept `locked`; fetch the lock state separately via the REST call shown in the targeted path. If any skip rule now fires (closed, lock returns `true`, skip-class label, newly-linked PR, newly-written sibling plan, newly-written sibling progress claim within TTL), release the own claim per *Release the progress-file claim*, drop the picked issue, and resume iteration at the next entry. See *Concurrent sessions* for the bounded re-pick rule (cap 3).

The picked issue is the latest RCA-shaped issue that survives the skip rules and the re-fetch.

### Oldest-first auto-pick path — no integer, no `--rca`

Walk the open-issue backlog from oldest to newest using `/backlog`'s **Pick the oldest actionable issue** section verbatim. The skip rules (`locked`, `wontfix`/`duplicate`/`question`/`discussion`/`epic`, existing plan, open linked PR) apply — the user did not target this issue, so silent skipping prevents re-acting on a previously-handled item every invocation.

In addition to the existing skip rules, an `in_progress` claim in `~/.claude/bug-fix-progress.json` from a *different* folder within the 24h TTL is a **silent skip** on the auto-pick path (rationale in *Progress tracking / Read the file at the start of triage*). Log it in the skip table with reason `claimed_by <folder>` so the user can see the contention if no actionable issue surfaces. A terminal entry from any folder (`plan_complete` or `issue_closed`) is also a silent skip — auto-pick already silently skips on existing-plan / open-PR signals, so a finished claim follows the same disposition.

If the iteration exhausts the list without a non-skipped match, stop with `error=no_actionable_issue` and print the skip table (issue number, title, skip reason).

After picking, write the `in_progress` claim per *Progress tracking / Write the in-progress claim* — the early claim short-circuits sibling sessions racing on the same candidate before any further `gh` API spend, and it makes the iteration's "step to the next candidate on a sibling claim" loop visible at the file level. Then re-run the issue fetch and the open-PR detection against the picked number before reading-and-parsing — a sibling session may have closed, locked, labeled, or PR-linked it between this run's pick and now. If a skip rule now fires, release the own claim per *Release the progress-file claim* and resume iteration at the next-oldest entry. See *Concurrent sessions* for the bounded re-pick rule (cap 3).

## Read and parse the picked issue

The issue fetch during target resolution already produced the body and metadata. Determine flavor, then parse the body; both flavors are accepted, and non-RCA (or loose-shape RCA) issues parse loosely.

### Determine flavor

The issue is **RCA-shaped** if any of the following hold:

- The title starts with `[rca]`.
- The labels include `rca`.
- The body contains both `## Symptom` and `## Root cause` sections.

On the `--rca` path the picked issue is RCA-shaped by construction (label `rca` or `[rca]` title prefix); the body check (`## Symptom` + `## Root cause`) then acts as a quality signal rather than a flavor decider — an issue carrying the `rca` label but missing both sections is RCA-shaped for routing but parses loosely (treat the same as a non-RCA issue). Surface this in the closing message so the user knows the plan is built on thinner evidence.

### Parse the body

For full-shape RCA issues, extract:

- `## Summary` — one-line root-cause statement.
- `## Symptom` — what failed externally; the timestamp; iteration/phase.
- `## Lifecycle snapshot` — verbatim snapshot of `lifecycle.json` at the anchor.
- `## Proximate cause` — the `file:line` of the immediate exception or invariant violation.
- `## Root cause` — the upstream defect's `file:line`, OR the explicit "Root cause unidentified" sentinel with the next-step pointer.
- `## Suggested fix` — present or absent (absent is normal per the `/rca` template).
- `## Cross-checks` — recent suspect commit SHAs, journal/audit cross-checks, free-text hypothesis verdict.
- `## Evidence index` — the runtime files the RCA reader pointed at.

For non-RCA or loose-shape issues, extract whatever is parseable: a description of the failure, any cited paths, any cited error/exception, any reproduction steps, any commit SHAs.

A "Root cause unidentified" sentinel in `## Root cause` and a body without any `file:line` anchor do **not** stop this command — they become inputs to the triage decision below. A missing `file:line` is a concern, not a stop trigger; the issue's *intent* (what behavior is wrong) is what governs the verdict, and an issue that describes a clear symptom without naming a path is still triageable.

## Triage against today's tree — by intent

The triage answers one question: **does the behavior the issue describes still apply to today's code?**

This is a critique pass. The issue was a snapshot in time; the bug may have already been fixed, the file may have been renamed, or the cited line range may have shifted. Findings flow into the triage decision below — they are **never** written back to the issue. Interpret every finding through the intent lens: the mechanical checks produce evidence, they do not by themselves render verdicts. Line numbers, symbol names, and even file paths drift; the question is whether the bug class the issue describes is still observable, not whether `path:line` quotes still resolve.

### Verify the issue against today's tree

1. **Already fixed.** For every file the issue cites, run:

   ```
   git log --since="<issue.createdAt>" --pretty=format:'%h %ad %s' --date=iso -- <path>
   ```

   For each commit since the issue that touches the cited file, run `git show <SHA>` and read the diff at the cited line range. If the change is at or adjacent to the cited line and the commit message looks like a fix (`fix:` prefix, "Fix ", `Closes #<NNN>`, `Fixes #<NNN>`, or directly references the issue number), that is a strong signal the bug is fixed. Capture the suspect commit SHA, date, and subject for the close comment.

2. **Cited files exist.** For every path the issue names, confirm it exists in the current tree. If a file was renamed or deleted, locate the current path by name + neighbouring symbols; the plan uses the current path. A file that has been *removed* (not renamed) with no equivalent surface in today's tree is a close-favoring signal under intent-killing evidence.

3. **Cited line ranges still match.** For every `path:line` reference in `## Proximate cause` and `## Root cause`, read that span. Confirm the named function, branch, exception, or assertion is still there. Line numbers shifting is anchor drift (a concern, not a verdict); a symbol that was rewritten or removed in a way that changes the underlying behavior is a stronger intent-killing signal.

4. **Suspect commits in the issue.** For every SHA listed under `## Cross-checks` "Recent suspect commits", run `git show <SHA> --stat` to confirm the commit is still in the tree (not amended away on a rewritten branch). A missing SHA is anchor drift, not a verdict shift — the cause-and-effect framing may need re-evaluation, but the symptom may still be observable.

5. **Adjacent tests.** Use `Glob` to find tests adjacent to the cited file (e.g., `tests/test_<module>.py`). Note whether a regression test could plausibly slot into an existing file or whether a new test file is needed. The plan's first step depends on this answer. If an adjacent test already asserts the corrected behavior, that is a strong intent-killing signal.

6. **Architecture invariants.** Cross-check the candidate fix shape against `CLAUDE.md`'s "Architecture invariants" list. If a candidate fix would violate an invariant the issue does not address, that is an invariant collision — default to **plan** (see *Decide: plan or close*) and surface the invariant as an addendum note so the implementer scopes around it rather than papering over it.

Sort findings into three buckets:

- **Intent-killing evidence** — the bug's behavior is provably absent in today's code. Examples: a fix-shaped commit landed at the cited area since `issue.createdAt` and the current behavior matches what the issue asked for; the entire code surface the issue complained about has been removed; an adjacent test asserts the corrected behavior; the configuration knob the issue argued against no longer exists. Multiple weak signals can compound into intent-killing if they converge — *no single check is the trigger; the verdict is whether the bug behavior survives*.
- **Anchor drift** — line numbers shifted, the cited symbol was renamed or moved files, the suspect commit named in `## Cross-checks` is no longer in tree, or the issue lacks any `file:line` but the body describes a clear symptom. None of these by themselves invalidate the intent. They are concerns the implementer threads through the plan; they ride into the closing message as an addendum so the user can sanity-check before `/impl` runs.
- **Invariant collision** — the only implementable fix would violate a load-bearing CLAUDE.md invariant the issue does not address. Rare but real (e.g., the issue asks for behavior that contradicts the human-in-the-loop evolution invariant or the journal-over-logs invariant).

### Validate the suggested fix against forbidden patterns

If the issue includes a `## Suggested fix` section, the plan inherits it as a starting point — but only if it survives the forbidden-pattern check. Drop the suggestion (and write a fresh fix from the root cause) if it matches any of:

- **Silencing a non-transient error** — `try/except: pass`, broad `except Exception:` that logs-and-continues, `# type: ignore` on a real type bug, demoting a `CRITICAL`/`ERROR` log to `WARNING`/`DEBUG`. Do not extend the SDK transient-error category list (`RATE_LIMIT`, `STALE_SESSION`, `CLI_CRASH`, `STREAM_STALL`) to cover a new symptom.
- **Adding auto-recovery to the loop** — retries on non-transient errors, top-level catch around `run_loop`, changing `restart: "no"` on the `loop` service in `docker-compose.yml`. The loop is supposed to die hard so the next operator sees the same evidence.
- **Symptomatic patches** — increasing a timeout, raising a limit, bumping a cap, adding a retry, *unless* the evidence proves the limit itself is the root cause and not a guard against a deeper defect.
- **Bypassing quality gates** — `pytest.skip`/`xfail` (except for the failing-test-first pattern), `# pragma: no cover`, lowering `cov_fail_under`, project-wide ruff/mypy disables.
- **Removing the offending code without replacement** — deletion is not a fix unless the code was provably dead.

Allowed and encouraged patterns (these survive the check): fix the upstream defect at `file:line`; convert a silent bad state into a louder, earlier failure (`assert`, type narrowing, explicit `raise`); add a regression test that captures the bad state (required by CLAUDE.md rule 11); tighten an existing invariant (e.g. shrink an unbounded list cap) when the evidence proves it was the source of unbounded growth.

A forbidden suggested fix never by itself invalidates the bug — only the suggested implementation. The plan, if written, is built from the root-cause intent regardless of whether the suggested fix survives.

## Decide: plan or close

Two terminal outcomes. Default to **plan** unless intent-killing evidence converges (→ close); a plan is reversible (delete the file) while a close is publicly visible and notifies subscribers, so the burden of proof for closing is higher than for planning.

- **Plan** — the bug's intent still applies. Cited line numbers may have drifted, the cited file may have been moved or renamed, the suggested fix may have matched a forbidden pattern, but the underlying wrong behavior is still present in today's tree. Write the plan framed around intent at current code locations.
- **Close** — the bug's intent no longer applies. Today's code already behaves correctly, or the entire feature surface the issue cited has been removed. Close the issue on GitHub with a comment quoting the evidence.

Decision rules (applied in order; first match wins):

- **Intent-killing evidence converges → close.** Capture the evidence verbatim for the close comment.
- **Otherwise → plan.** Insufficient evidence, invariant collisions, forbidden-only fixes, and weak-evidence cases all default to plan — anchor drift, missing anchors, and invariant concerns become addendum notes for the implementer. The plan body points at *current* code locations only; never copy stale `file:line` quotes from the issue.

## Close path

When the decision is **close**:

1. Build the comment body. ≤8 lines. Required elements:
   - One-line close reason: `bug behavior no longer present in tree` *or* `feature surface cited by the issue removed from tree` *or* `today's code already behaves as the issue requested`.
   - Cited evidence (one bullet per signal that fired):
     - Fix-shaped commit: ``Resolved by `<SHA>` (<ISO date>) — "<commit subject>".``
     - Path removal: ``- `<path>` removed in `<SHA>` (<ISO date>); no equivalent surface in today's tree.``
     - Adjacent-test confirmation: ``Test `<test::name>` asserts the corrected behavior.``
     - Behavioral change: ``Today's code at `<current_path>:<current_line>` exhibits the requested behavior.``
   - Reversal instruction: `Reopen if the underlying behavior is still observable against today's tree and supply a fresh repro.`
   - Provenance: `Auto-closed by /bug-fix after verifying intent against current main.`

   Do **not** include `Fixes #<NNN>` (this is a comment, not a PR). Do **not** include log excerpts, full stack traces, or anything secret-shaped. Redact tokens / `Bearer ` headers / API keys / Telegram bot tokens (`bot\d+:[A-Za-z0-9_-]{30,}`) before quoting.

2. **Race re-check.** Run the close-path re-checks in *Concurrent sessions / Re-checks before mutating* (re-fetch the issue, re-run the open-PR detection). If a sibling session closed, locked, or PR-linked the issue during triage, surface the named race error and stop without calling `gh issue close`. In `--dry-run` mode, the same re-checks run; any race becomes a warning printed alongside the dry-run output rather than a stop.

3. In `--dry-run` mode, print the issue URL, the close reason, and the comment body to stdout and stop. Do not call `gh issue close`.

4. Otherwise, post and close in one call:

   ```
   scripts/with-env.sh gh issue close <NNN> --reason completed --comment "$(cat <<'EOF'
   <comment body>
   EOF
   )"
   ```

   `gh issue close --comment` posts the comment then closes; preferred over two separate calls so a partial failure cannot leave a comment on a still-open issue. If `gh` itself returns "already closed" or "could not close" — a race the step 2 re-check missed — surface `error=issue_already_closed_race` with the `gh` stderr quoted; do not retry.

5. Update the progress file: set `status="issue_closed"`, `completed_at` to now, `outcome` to the one-line close reason. Atomic read-modify-write per *Progress tracking / Update the claim at the terminal path*. (Dry-run already stopped at step 3, so this step is unreached on the dry-run path.)

6. Print the issue URL and the one-line close reason as the final user-facing message. Example:

       Closed: <url> — bug behavior no longer present in tree (resolved by abc1234)

7. Do not change labels. Do not assign anyone. Do not lock. Do not write a plan file (close and plan are mutually exclusive).

## Plan path

When the decision is **plan**:

1. Compute the output path: `~/.claude/plans/fix-<NNN>-<slug>.md`. Slug derivation, plan-file layout, and the `Fixes: #<NNN>` metadata line all follow *Write the plan file* below. Overwrite if the path exists.

2. **Frame the plan around intent.** The plan's `Context` and `Implementation steps` sections describe the wrong behavior in the words of *today's code*, not the words of the issue. Where the issue cited `file.py:123` and today's code has the same behavior at `file.py:147` (line drift) or at `other_file.py:50` (refactor), the plan points at the *current* location and includes a one-line note that the issue's anchor drifted. Where the issue cited a path that has been split into several, the plan names each current location. Line numbers and symbol names in the plan reflect today's tree only — never copy stale anchors forward. Where the issue lacks any anchor at all, the plan opens with the symptom the issue described and names the path(s) where the implementer should start looking, derived from the symptom.

3. **Race re-check.** Run the plan-path re-checks in *Concurrent sessions / Re-checks before mutating* (re-fetch the issue and re-glob `~/.claude/plans/fix-<NNN>-*.md`). If the issue closed during triage (a sibling session won a close-path race), surface the named race error and stop without writing — the plan would target a closed issue. If a sibling session wrote a plan at a *different* slug during triage, write this command's plan anyway and add a sibling-plan bullet to the addendum. In `--dry-run` mode, race outcomes become warnings printed alongside the plan body rather than stops.

4. In `--dry-run` mode, print the plan body to stdout and stop without writing the file.

5. Otherwise, write the file using atomic write semantics: write the plan body to a sibling temp file `~/.claude/plans/fix-<NNN>-<slug>.md.tmp.<pid>`, then `os.rename` it into place — so a concurrent reader sees either the prior plan or the new one, never a truncated file (the same temp-file-plus-rename discipline the in-progress claim and last-plan-by-folder pointer writes already use). Then update the progress file: set `status="plan_complete"`, `completed_at` to now, `outcome` to the absolute plan path. Atomic read-modify-write per *Progress tracking / Update the claim at the terminal path*. (Dry-run already stopped at step 4, so this step is unreached on the dry-run path.) Also record the plan pointer for this working directory per *Record the plan pointer* below. Then print:

       Wrote plan: ~/.claude/plans/fix-<NNN>-<slug>.md

   Append a single block of bullet addenda directly under the path when any of these hold:
   - Anchor drift surfaced — list each drift one line at a time (`issue cited X → plan targets Y`).
   - The issue was non-RCA (looser parsing) — say so on its own line.
   - The issue lacked any `file:line` anchor — name the symptom the plan is anchored to.
   - An invariant collision surfaced during triage — name the load-bearing CLAUDE.md invariant the only implementable fix risks colliding with so the implementer scopes around it rather than papering over it.
   - The targeted path replaced an existing plan — name the prior path.
   - A sibling session wrote a plan at a different slug during triage — name the sibling path (`sibling plan at <path>`) so the user dedupes by hand.
   - An open PR is linked to the issue — name the PR URL.
   - The issue carried a `wontfix` / `question` / `discussion` / `epic` label — name the label so the user reconsiders scope.
   - The suggested fix in the issue matched a forbidden pattern but the plan supplies a clean replacement — say so on its own line.

   If verification was clean, the issue was RCA-shaped with intact anchors, and no targeted-path warnings fired, no addendum.

6. Do not comment on the issue, do not change labels, do not link the plan from the issue. The issue closes automatically when the eventual PR with `Fixes #<NNN>` merges.

### Write the plan file

1. **Compute the output path:** `~/.claude/plans/fix-<NNN>-<slug>.md`. Derive the slug from the issue title:
   - Strip a leading `[rca] ` prefix if present.
   - Lowercase, replace non-alphanumeric runs with `-`, trim leading/trailing `-`, collapse repeated `-`.
   - Truncate to ≤60 chars at a word boundary.
   - If the resulting slug is empty (rare), fall back to `issue-<NNN>`.

   Overwrite the path if it already exists.

2. **Write the plan** with the layout below. The plan reads as freshly authored: it does not say "the issue said", does not name the issue as a draft, does not include a drift section, does not compare against a prior version. The only mention of the GitHub issue is a single `Fixes: #<NNN>` metadata line below the title so the eventual PR auto-closes the issue when merged.

   #### Plan layout

   1. **`# Plan: <Title>`** — heading. Use the issue's title with any `[rca]` prefix stripped. Do not include the issue number in the heading itself.
   2. **`Fixes: #<NNN>`** — single metadata line directly under the heading, on its own line. Picked up by the implementer when they later run `/create-pr` so the PR body includes `Fixes #<NNN>` and the merge auto-closes the issue.
   3. **`## Context`** — fresh prose explaining what is broken, why it matters, and the intended outcome of the fix. Do not copy the issue's `Symptom` or `Root cause` text verbatim; rewrite into a forward-looking framing of the bug. Cite the root-cause `file:line` once (using today's path/line) so the reader knows the target, but do not paste log excerpts (those live in the issue and the runtime data dir).
   4. **`## Implementation steps`** — numbered list. Required step ordering and content:
      1. **Add a regression test that captures the bad state.** Per CLAUDE.md rule 11, prove the bug with a test before fixing it. Name the test file (existing or new) and the test name. Describe the bad state the test asserts against and the inputs that trigger it. Tests must use `mem_fs`, mock all externals (subprocess, network, sleep), and never touch the real filesystem. The test goes in *as part of the same step as the fix* (combined commit) — do not check in a failing test on its own.
      2. **Apply the root-cause fix at `file:line`.** Cite the current path and current line range as verified above. Describe the change in 1–3 sentences. Reuse existing patterns surfaced during verification.
      3. **Tighten the invariant** (optional but encouraged). If the bug exists because a silent bad state was tolerated, add an `assert`, type narrowing, or explicit `raise` at the same site so the same defect would now fail loudly and earlier. Cite the file and line.
      4. **Update `CLAUDE.md` or `docs/modules.md`** (only when the fix changes a documented invariant or a module's purpose). Skip otherwise — most bug fixes don't move documentation.

      Order steps so an interrupted run leaves the tree in a working state. Each step has a short title, the file(s) it touches with current line ranges, and a 1–3 sentence description.
   5. **`## Critical files`** — bulleted list of files to be modified, with current paths.
   6. **`## Reused functions and utilities`** — bulleted list of existing helpers to call instead of reimplementing. Each line: `path:line — symbol — one-line purpose`. If the fix is a self-contained one-line change with nothing to reuse, write a single bullet `None — fix is self-contained.` so the reader knows the section was considered.
   7. **`## Verification`** — concrete steps to confirm the fix works end-to-end. Default to project gates:

      - `python3.14 -m ruff check maestro/ tests/`
      - `python3.14 -m mypy maestro/`
      - `python3.14 -m pytest tests/ -v`

      Plus the new regression test, named explicitly (`pytest tests/test_<module>.py::<test_name> -v`). If the fix touches `maestro/dashboard/` or `tests/browser/`, include the browser test command. If the fix touches `maestro_review/`, include its gate triple. If the original failure required a runtime trigger (e.g., a specific lifecycle state, a specific SDK reply shape), add a one-line manual repro check that names the trigger so the implementer knows what to exercise. End the section with: `When opening the PR, include "Fixes #<NNN>" in the body so the issue auto-closes on merge.`
   8. **`## Out of scope`** — bullets framing related work this plan does not cover (e.g., "Refactoring the surrounding module is out of scope", "Hardening adjacent error paths is out of scope", "Reviewing other call sites of the same helper is out of scope"). Forward-looking boundaries, not deferrals from a draft.

### Record the plan pointer

After the plan file is written, record it as this folder's most-recent plan so `/impl` invoked with no argument picks it for this working directory instead of the global most-recently-modified plan in `~/.claude/plans/`. This step never runs on the `--dry-run` path (no file was written there). The pointer lives in a shared JSON file at `~/.claude/last-plan-by-folder.json`, keyed by absolute working directory:

    {
      "<absolute path of this command's working directory>": {
        "plan": "<absolute path of the plan file just written>",
        "written_at": "2026-05-24T12:34:56Z"
      }
    }

Use atomic read-modify-write: read the file (missing → treat as `{}`; malformed JSON → warn, treat as `{}`, do not overwrite silently), set the entry for the current working directory's absolute path to the just-written plan's absolute path plus the current ISO 8601 UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ` — no fractional seconds, literal `Z`), serialize the full object, write to `~/.claude/last-plan-by-folder.json.tmp.<pid>`, then rename in place. Preserve unrelated keys untouched. This is best-effort: if the write fails (permission denied, disk full, missing parent), warn and proceed — the pointer is an optimization for `/impl`'s auto-pick, never a correctness requirement.

## What to skip

- Do not act on more than one issue per invocation. The decision (plan-or-close) and its action are bounded to the picked issue. The user runs the command repeatedly to walk the backlog.
- Do not modify labels, milestones, or assignees on any issue (picked or skipped). The close path's only mutation is the `gh issue close --comment` call.
- Do not edit the body of any issue. Do not redact secrets in-place; redact only in quoted material that goes into the plan body or close comment.
- Do not run quality gates as part of this command — they belong in the plan's `Verification` section, executed by `/impl`.
- Do not invoke `/impl`, `/backlog`, or `/rca` from inside this command. The verification and plan-write logic is inlined above; recursion would re-do the same work twice.
- Do not push branches, open PRs, or merge anything.
- Do not let anchor drift (line shift, symbol rename, file move/split) drive the verdict. If the bug behavior is still observable, plan; if it is not, close. Anchor accuracy is documentation for the implementer, never the gate.

## Concurrent sessions

Two `/bug-fix` invocations running in parallel — different terminal tabs, different git worktrees, sibling agents on the same machine — share `~/.claude/plans/` and the same GitHub repo. The early prechecks (issue fetch, plan glob, open-PR query) resolve at the start of triage; the TOCTOU window between then and the mutation (plan write or `gh issue close`) is wide enough for a sibling session to land first and turn this command's mutation into a duplicate close, a stomped plan, or work on a stale picked issue. There is no cross-session lock — best-effort re-checks at each mutation boundary are the contract, so the worst-case race outcome is *this session loses cleanly with a named race error*, never *duplicate close* or *silent plan stomp*. A sentinel-file lock would only push the race elsewhere; do not invent one.

### Re-checks before mutating

Run immediately before the plan write or the `gh issue close` call. The early prechecks are not enough — they happen before triage, not before the mutation.

Both paths:

- Re-fetch the issue. **`gh issue view --json` does not accept `locked`** — passing it produces `Unknown JSON field: "locked"` and aborts the call. Lock state comes from the separate REST call; do not collapse the two by adding `locked` to the `--json` list:

  ```
  scripts/with-env.sh gh issue view <NNN> --json state,closedAt,labels
  scripts/with-env.sh gh api repos/{owner}/{repo}/issues/<NNN> --jq .locked
  ```

  - `state == "closed"` → stop with `error=issue_already_closed_race` and print the URL plus `closedAt`. Do not reopen, do not append the prepared comment to the now-closed issue.
  - the lock fetch returns `true` → stop with `error=issue_locked_race` and the URL.
  - A skip-class label appeared since the early fetch:
    - `duplicate` — on the auto-pick path, treat as a stale pick (see *Auto-pick path: re-pick on stale claim*); on the targeted path, stop with `error=duplicate_race` and tell the user to point at the canonical issue (matching the early-fetch stop semantics — a duplicate is a duplicate regardless of when the label lands).
    - `wontfix`, `question`, `discussion`, `epic` — on the auto-pick path, treat as a stale pick (see *Auto-pick path: re-pick on stale claim*); on the targeted path, the user named the issue deliberately, so warn but proceed.

Close path only:

- Re-run the open-PR detection (a single `#<NNN> in:body` search post-filtered for a GitHub closing keyword — a case-insensitive `\b(close[sd]?|fix(es|ed)?|resolve[sd]?):?\s+#<NNN>\b` match, the boundaries keeping the keyword whole and bounding the issue number on a non-digit so `#12` does not match `#123`, covering the full closing-keyword set, not just `Fixes`/`Closes`). Non-empty → stop with `error=linked_pr_appeared` and the PR URL. A linked PR will close the issue via a closing keyword such as `Fixes #<NNN>` on merge; closing now would notify subscribers twice and create a phantom comment race.
- If the `gh issue close` call itself returns "already closed" or "could not close" — the late race the re-check missed — surface `error=issue_already_closed_race` with the `gh` stderr quoted; do not retry.

Plan path only:

- Re-glob `~/.claude/plans/fix-<NNN>-*.md`. A new entry with a *different* slug means a sibling planned the issue at a different scope — write this command's plan anyway and surface the prior path as a `sibling plan at <path>` bullet in the closing addendum so the user dedupes by hand. Same slug → overwrite (the bodies should converge at the same scope; the user inspects via `git diff` of `~/.claude/plans/` if curious). Two plans for one issue is a soft conflict, not a stop trigger.
- Atomic plan write. Write the plan body to a sibling temp file `~/.claude/plans/fix-<NNN>-<slug>.md.tmp.<pid>`, then `os.rename` it into place — a sibling session reading mid-write then sees either the prior plan or the new one, never a truncated file.

Both terminal paths (close, plan):

- Re-read `~/.claude/bug-fix-progress.json`. Compare the entry for `<NNN>` against the claim this command wrote at the start of triage. The `started_at` comparison below is the byte-for-byte string equality pinned in *Progress tracking / File format* — do not parse-then-reformat either side, or a `Z` ↔ `+00:00` round-trip will produce a phantom mismatch.
  - Entry has `status="in_progress"` with `folder` matching this folder and the original `started_at` → no race, proceed.
  - Entry has `status="in_progress"` with a *different* `folder` (a sibling overwrote this folder's claim) → stop with `error=progress_claim_lost` and print both folders, both `started_at` timestamps, and the issue URL. Do not write the plan, do not call `gh issue close`. The sibling that overwrote the claim will produce the terminal outcome; this folder steps aside cleanly.
  - Entry has a terminal status (`plan_complete` / `issue_closed`) with a *different* `folder` → a sibling already finished. Stop with `error=progress_claim_lost` and the prior `outcome`. Do not mutate.
  - Entry is missing entirely → another folder hand-edited the file mid-triage. Treat the same as a folder-mismatch race: stop with `error=progress_claim_lost` and a `claim file mutated by external editor` note.
- `--dry-run` keeps the same outcomes as warnings rather than stops, consistent with the rest of the re-check policy: print the would-be race error alongside the prepared output and continue.

### Release the own claim on race-error stops

Every race-error stop that fires *after* this folder's `in_progress` claim has been written must release the own claim before exiting, using the same logic as *Release the progress-file claim*. This covers `error=issue_already_closed_race`, `error=issue_locked_race`, `error=duplicate_race`, `error=linked_pr_appeared`, and `error=progress_claim_lost` from the re-checks above, plus the `error=issue_already_closed_race` shape that fires from the `gh issue close` call's own "already closed" return. Without this release, a cross-folder user is locked out of the issue for 24h while the TTL drains, even though no work was done.

The release path is idempotent and folder-scoped: if the entry now belongs to a sibling (the typical `error=progress_claim_lost` shape), the release detects the folder/`started_at` mismatch and leaves the file alone. If the entry is still this folder's claim (the GitHub-side races above), the release deletes it. The race-error message is still surfaced loudly per *Race outcomes are loud, not silent* — the release runs *before* the print so the file is clean by the time the user sees the error.

The early-precheck stops on the targeted path (`error=issue_claimed_by_other_folder`, `error=issue_already_closed`, `error=issue_locked`, `error=duplicate`, `error=invalid_argument`, `error=issue_not_found`) all fire *before* claim-write per the targeted-path step ordering — argument validation and `gh issue view` (steps 1-2) precede the early-claim step (step 3), and the `issue_claimed_by_other_folder` shape is the claim check itself failing before this folder writes anything — so there is no own claim to release on those paths. The auto-pick-only stops (`error=no_actionable_issue`, `error=no_open_rca_issues`, `error=no_actionable_rca_issue`, `error=picking_thrash`) are likewise clean of an own claim at stop time: the exhaustion stops (`no_actionable_issue` on the oldest-first path, `no_open_rca_issues` / `no_actionable_rca_issue` on the `--rca` path) fire when iteration ends without writing a claim for any candidate (cheap skip rules and sibling-claim skips both pre-date claim-write), and `picking_thrash` fires after the third post-pick re-fetch invalidates the candidate — each invalidated re-pick has already released its own claim per *Auto-pick path: re-pick on stale claim* before the next pick.

### Auto-pick path: re-pick on stale claim

In either auto-pick path (oldest-first or `--rca`) the picked issue can be invalidated by a sibling session that closed it, locked it, labeled it, or had a PR appear on it between pick and act. The own `in_progress` claim is already written by this point (per the early-claim ordering above), so the re-fetch runs against a claimed candidate. Re-run the issue fetch and the open-PR detection against the picked number immediately after writing the claim and before reading-and-parsing. If any skip rule now fires (closed, locked, skip-class label, newly-linked PR), release the own claim per *Release the progress-file claim*, drop the picked issue, and resume iteration at the next entry in the list's iteration order (next-oldest on the oldest-first path, next-newest on the `--rca` path). Cap re-picks at 3; beyond that, stop with `error=picking_thrash` and print the picked-then-dropped table so the user sees the contention.

The targeted path never re-picks — the user named the issue deliberately. A stale targeted pick surfaces as a race error from the re-checks above, not a silent swap to another issue.

### Race outcomes are loud, not silent

Every race surfaces a named error (`error=issue_already_closed_race`, `error=issue_locked_race`, `error=duplicate_race`, `error=linked_pr_appeared`, `error=picking_thrash`, `error=issue_claimed_by_other_folder`, `error=progress_claim_lost`) plus the URL of the contended issue or PR. Do not retry automatically — the bounded auto-pick re-pick is the sole exception. The user re-runs the command when ready. The two progress-file errors are the only races detected against the `~/.claude/bug-fix-progress.json` file (the rest are GitHub-side); they fire in the same loud-not-silent shape as the GitHub races.

`--dry-run` does not mutate, so the re-checks become warnings rather than stops in dry-run mode. Print the would-be race error alongside the dry-run plan body or close comment and continue, so the user sees both the prepared output and the contention.

## Safety

- Read-only against the repo and against GitHub except for two narrow writes: the plan file at `~/.claude/plans/fix-<NNN>-<slug>.md` (plan path), or a single `gh issue close --comment` call (close path). These two paths are mutually exclusive — never both, never more than one mutation per invocation. The `~/.claude/bug-fix-progress.json` claim file is updated by both terminal paths and conditionally cleared on race-error stops (only if the entry still belongs to this folder, per *Release the progress-file claim*); it lives outside the repo and outside GitHub, is local-only coordination state, and is never a substitute for either of the two repo/GitHub mutations. The `~/.claude/last-plan-by-folder.json` pointer file is updated best-effort on the plan path only (recording this folder's most-recent plan for `/impl`'s auto-pick); like the progress file it is local-only state outside the repo and GitHub, never a substitute for the repo/GitHub mutation.
- The close path closes only when intent-killing evidence converges. Anchor drift alone never fires close; the intent question is decided on behavior, not on `file:line` accuracy. When the evidence is mixed, the plan path is the safe default — a wrong plan costs a file delete, a wrong close costs a reopen and an apology comment.
- If the cited `file:line` no longer exists or has been rewritten away, do **not** stop. Reframe the plan around the same intent at the new location; drift becomes an addendum, not a verdict. Do not infer a new target by name-matching alone — confirm the symptom-to-target mapping holds in today's code before pointing the plan at it.
- If the suggested fix in the issue is entirely forbidden patterns and no clean replacement is implementable at the same scope, write a plan scoped to the underlying intent rather than the suggested implementation. If there is genuinely no clean fix at any scope, write a plan that documents the constraint and proposes the safest partial improvement. Do not improvise a different scope silently — note in the plan addendum that the suggested fix was forbidden and describe what was substituted.
- If the issue body contains tokens, OAuth headers, or obvious secrets that the RCA forgot to redact, treat the issue as still trustworthy for planning but redact them from any quotes you write into the plan body or close comment. Do not edit the issue itself.
- Redact tokens, OAuth headers, API keys, and Telegram bot tokens (`bot\d+:[A-Za-z0-9_-]{30,}`) before they enter the plan body or close comment.
- If the working tree has uncommitted changes when this command runs, leave them alone — and skip the merged-branch switch entirely (see *Switch to main if on a merged branch*). Branch *creation* is `/impl`'s job; this command never creates branches. The git-state changes it makes are a safe `git switch main` and a fast-forward of local `main` to `origin/main`, fired only when the current branch is clean and fully merged — or, when already on a clean `main`, that fast-forward alone.
- Never amend, force-push, reset, or otherwise rewrite git history. Beyond the conditional `git switch main` (with its fast-forward of local `main` to `origin/main`, which also fires on its own when already on `main`) and best-effort `git fetch origin main` in *Switch to main if on a merged branch*, the verification phase uses `git log` / `git show` / `git status` only.
- The whole pipeline is read-only against the live Maestro loop and runtime data dir. This command does not touch `data/`, `docker-compose.yml`, source files, or any other shared state.
- If the picking phase (auto-pick path) exhausts the list without finding an actionable issue, stop with the skip table — do not loosen the skip criteria to force an action. An empty backlog (or a backlog full of `wontfix` / `duplicate` / `question` issues) is a valid end state.
