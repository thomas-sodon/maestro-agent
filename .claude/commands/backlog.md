Pick the oldest open GitHub issue for the Maestro project and either write a plan that resolves it or close it with a reasoning comment.

This command walks the open-issue backlog from oldest to newest, takes the first issue that has not already been acted on, and produces exactly one outcome: a fresh plan at `~/.claude/plans/fix-<NNN>-<slug>.md` (same layout and verification as `/bug-fix`), or a closure with an explanatory comment. Run repeatedly to drain the backlog one issue at a time. It does not modify labels, milestones, assignees, or the issue body, and it never reopens or edits any issue other than the one it acts on.

`$ARGUMENTS` is optional. Recognized tokens:
- `--dry-run` — print the picked issue, the decision, and (for the plan branch) the plan body, without writing the plan file or closing the issue.
- *(empty)* — pick the oldest actionable issue and take the action.

## Authentication

GitHub access needs `GITHUB_TOKEN` from `.env` in the project root. Run every `gh`, `git push`, and `git fetch` command through the wrapper, which loads `.env` and then execs the command; the examples below already use it:

```
scripts/with-env.sh <command>
```

If `.env` does not exist or the token is empty, stop before the first `gh` call and tell the user to populate it. The local repo reads (`git log`, file existence checks) do not need the token.

## Pick the oldest actionable issue

1. List open issues, oldest first:

   ```
   scripts/with-env.sh gh issue list --state open --sort created --order asc --limit 1000 --json number,title,createdAt,labels,url
   ```

   `gh issue list` paginates GraphQL pages internally up to `--limit`, so the ceiling **must exceed the current open-issue count** for the walk to fetch the whole open backlog — `--limit 1000` sits comfortably above the realistic open-issue count. Because the sort is `created asc`, any residual truncation at that ceiling drops only the *youngest* issues; provided at least one of the fetched oldest issues is actionable, the first actionable issue is within the fetched set. The residual bound remains: if the open-issue count ever exceeds the ceiling *and* every fetched issue is skipped, an actionable youngest issue can still be masked — hence the cap must stay above the open-issue count.

   `gh issue list` already excludes pull requests by default. If the list is empty, stop with `error=no_open_issues` — there is nothing to do.

2. Iterate from oldest to newest. **Skip** an issue (recording the skip reason for the final summary) if any of:
   - Lock state is not available from `gh issue list --json`, so it is checked at the per-candidate fetch in **Read and parse the picked issue** below: a locked pick is recorded with skip reason `locked` and iteration resumes at the next-oldest entry.
   - Its labels include `wontfix`, `duplicate`, `question`, `discussion`, or `epic`. These have been triaged into a non-actionable bucket and should not be auto-acted on.
   - A plan file already exists at `~/.claude/plans/fix-<NNN>-*.md`. The previous `/backlog` run (or a manual `/bug-fix`) already produced a plan; advancing prevents re-planning the same issue every invocation.
   - An open PR is linked to the issue. Detect via a single `gh pr list --state open --search "#<NNN> in:body" --json number,url,body`, then keep only PRs whose body contains a GitHub closing keyword immediately before the reference — a case-insensitive `\b(close[sd]?|fix(es|ed)?|resolve[sd]?):?\s+#<NNN>\b` match. The leading `\b` keeps the keyword whole (so `discloses #<NNN>` is not read as `closes #<NNN>`); the optional `:?` accepts GitHub's colon-separated form (`Fixes: #<NNN>`); the trailing `\b` bounds the issue number on a non-digit, so triaging `#12` does not match a PR's `Fixes #123` — bare-substring matching of the digits alone is insufficient, matching Trigger A's guard. This covers every closing keyword GitHub recognizes (`close`/`closes`/`closed`, `fix`/`fixes`/`fixed`, `resolve`/`resolves`/`resolved`), not just `Fixes`/`Closes`, and excludes a bare non-closing mention like `see #<NNN>`. Keep the `in:body` field — PR-title keywords are not auto-linked by GitHub. A non-empty filtered result means an implementer is already on it.

3. The first non-skipped issue is the **picked issue**. If iteration exhausts the list without finding one, stop with `error=no_actionable_issue` and print the skip table (issue number, title, skip reason) so the user sees what was filtered.

## Read and parse the picked issue

Fetch the full body and metadata. **`gh issue view --json` does not accept `locked`** — passing it produces `Unknown JSON field: "locked"` and aborts the call. Lock state comes from the separate REST call below; do not collapse the two by adding `locked` to the `--json` list:

```
scripts/with-env.sh gh issue view <NNN> --json number,title,body,state,labels,createdAt,closedAt,url
scripts/with-env.sh gh api repos/{owner}/{repo}/issues/<NNN> --jq .locked
```

If the lock fetch returns `true`, record the skip reason `locked` and re-pick from the original list at the next-oldest entry. `state == "closed"` should not happen (the list query filtered to open) — if it does, treat as a race and re-pick from the original list at the next-oldest entry.

Determine flavor (RCA-shaped vs non-RCA) and parse the body using the rules in `/bug-fix`'s **Read and parse the picked issue** section. Both flavors are accepted; non-RCA issues parse loosely.

A "Root cause unidentified" sentinel and a body without any `file:line` anchor — which `/bug-fix` likewise does not stop on — do **not** stop this command either; they become inputs to the close-vs-plan decision below.

## Verify against today's tree

Run the full verification pass from `/bug-fix`'s **Verify the issue against today's tree** section: already-fixed scan, cited-files-exist check, cited-line-ranges check, suspect-commit liveness, adjacent-test discovery, architecture-invariant cross-check. Findings flow into the decision below — they are **never** written back to the issue.

Also run the forbidden-pattern check from `/bug-fix`'s **Validate the suggested fix against forbidden patterns** section if the issue contains a `## Suggested fix`. Drop the suggestion if it matches; the plan, if written, is built from the root cause regardless.

## Decide: close or plan

The decision is binary. Default to **plan** unless one of the two close triggers below holds with strong evidence — a plan is reversible (delete the file) while a close is publicly visible and notifies subscribers.

### Close trigger A — already fixed

All of the following hold:
- The issue cites at least one `file:line` anchor.
- A commit *since* `issue.createdAt` touches the cited file.
- That commit's diff lands at or directly adjacent to the cited line range.
- The commit subject is fix-shaped: it starts with `fix:` / `Fix `, **or** references the issue number `<NNN>` at a boundary — either an explicit closing keyword (`Closes` / `Fixes` / `Resolves`) immediately followed by `#<NNN>` at a word boundary, or the bare `#<NNN>` token bounded by non-digits on both sides. Bare-substring matching of the digits alone is insufficient: a subject containing `#1234` must **not** match issue 234.

Mark the issue for closure. Capture the suspect SHA, ISO date, and one-line subject.

### Close trigger B — target removed

All of the following hold:
- The issue cites at least one path.
- Every cited path is missing from the current tree (deleted, not renamed — if you can locate it under a new name, it is **not** missing).
- The issue body contains no other `file:line` anchor that resolves to a currently-present file.

Mark the issue for closure. Capture each missing path and the commit SHA that removed it (`git log --diff-filter=D --pretty=format:'%h %ad %s' --date=iso -- <path>`, take the most recent).

### Plan path

If neither close trigger holds, mark the issue for planning. Surface concerns separately so they ride into the closing message:
- Line numbers shifted but symbol still present.
- A suspect commit listed in the issue's `## Cross-checks` is no longer in the tree.
- The issue is non-RCA (looser provenance).
- The `## Suggested fix` was dropped as forbidden but a clean root-cause replacement at the same `file:line` exists.
- The issue lacks any `file:line` anchor at all (the plan will need a target — the implementer must read the issue's symptom description and supply one). This is a concern, not a blocker.

### Decision-level safety

If both close triggers fire on the same issue (e.g., file removed *and* a "fix: drop unused module" commit landed at the same lines), prefer **trigger A** — the commit message is the strongest signal that the resolution was intentional.

If neither trigger fires *and* the issue lacks any actionable content (no description, no symptom, no path, no error message), do not write a low-quality plan. Stop with `error=insufficient_actionable_content` and surface the issue URL so the user can triage by hand. Do not close — the issue may simply need more context from the reporter.

## Close path

When the decision is **close**:

1. Build the comment body. ≤6 lines. Required elements:
   - One-line close reason: `appears already fixed` or `target removed from tree`.
   - Cited evidence:
     - Trigger A: ``Resolved by `<SHA>` (<ISO date>) — "<commit subject>".``
     - Trigger B: List each missing path with the removal SHA: ``- `<path>` removed in `<SHA>` (<ISO date>).``
   - Reversal instruction: `Reopen if the underlying issue is still observable against today's tree.`
   - Provenance: `Auto-closed by /backlog after verifying against the current default branch.`

   Do **not** include `Fixes #<NNN>` (this is a comment, not a PR). Do **not** include log excerpts, full stack traces, or anything secret-shaped. Redact tokens / `Bearer ` headers / API keys before quoting.

2. In `--dry-run` mode, print the issue URL, the trigger name, and the comment body to stdout and stop. Do not call `gh issue close`.

3. Otherwise, post and close in one call:

   ```
   scripts/with-env.sh gh issue close <NNN> --comment "$(cat <<'EOF'
   <comment body>
   EOF
   )"
   ```

   `gh issue close --comment` posts the comment then closes; preferred over two separate calls so a partial failure cannot leave a comment on a still-open issue.

4. Print the issue URL and the one-line trigger summary as the final user-facing message. Example:

       Closed: <url> — appears already fixed (resolved by abc1234)

5. Do not change labels. Do not assign anyone. Do not lock. The next `/backlog` invocation will skip this issue (closed → not in the open-issue list).

## Plan path

When the decision is **plan**:

1. Compute the output path: `~/.claude/plans/fix-<NNN>-<slug>.md`. Slug derivation, plan-file layout, and the `Fixes: #<NNN>` metadata line all follow `/bug-fix`'s **Write the plan file** section verbatim. Overwrite if the path exists (the existing-plan check in step 2 of *Pick* should have skipped this issue, but the file-write is idempotent so a race does not corrupt anything).

2. In `--dry-run` mode, print the plan body to stdout and stop without writing the file or touching the issue.

3. Otherwise, write the file, record the plan pointer for this working directory per *Record the plan pointer* below, then print:

       Wrote plan: ~/.claude/plans/fix-<NNN>-<slug>.md

   If the verification surfaced concerns, append them as a one-block bullet list directly under the path so the user can sanity-check before invoking `/impl`. If the issue was non-RCA, say so on its own line. If the verification was clean and the issue was RCA-shaped, no addendum.

4. Do not comment on the issue, do not change labels, do not close, do not link the plan from the issue. The issue closes automatically when the eventual PR with `Fixes #<NNN>` merges. The plan file is the only repo/GitHub artifact this branch produces.

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

- Do not act on more than one issue per invocation. The decision (close-or-plan) and its action are bounded to the picked issue. The user runs the command repeatedly to walk the backlog.
- Do not modify labels, milestones, or assignees on any issue (picked or skipped).
- Do not edit the body of any issue. Do not redact secrets in-place; redact only in the quoted material that goes into the plan body or the close comment.
- Do not close any issue outside the two strict close triggers. When the evidence is mixed, the plan path is the safe default; the user reviews the plan before `/impl` runs anything.
- Do not run quality gates as part of this command — they belong in the plan's `Verification` section, executed by `/impl`.
- Do not invoke `EnterPlanMode`. Do not invoke `/impl`, `/bug-fix`, or `/rca` from inside this command. The verification and plan-write logic is inlined by reference; recursion would re-do the same work twice.
- Do not push branches, open PRs, or merge anything.

## Safety

- Read-only against the repo and the local filesystem outside `~/.claude/plans/`. Repo/GitHub writes are limited to: a single plan file at `~/.claude/plans/fix-<NNN>-<slug>.md` **or** a single `gh issue close --comment` call. Never both, never more than one. The plan path additionally does a best-effort, local-only update to the `~/.claude/last-plan-by-folder.json` pointer (this folder's most-recent plan for `/impl`); it is not a repo/GitHub mutation.
- Closing a GitHub issue is publicly visible and triggers subscriber notifications. Restrict closure to the two strict triggers; when in doubt, plan instead. The asymmetry is deliberate — a wrong plan costs a file delete; a wrong close costs a reopen + an apology comment.
- Redact tokens, OAuth headers, and obvious secrets before they enter the plan body or the close comment. Telegram bot tokens (`bot\d+:[A-Za-z0-9_-]{30,}`) appear in `httpx` request lines — strip them.
- Never amend, force-push, reset, or otherwise rewrite git history. The verification phase uses `git log` / `git show` / `git status` only.
- If the working tree has uncommitted changes when this command runs, leave them alone. Branch creation is `/impl`'s job; this command never branches.
- If the picking phase exhausts the list without finding an actionable issue, stop with the skip table — do not loosen the skip criteria to force an action. An empty backlog (or a backlog full of `wontfix` / `duplicate` / `question` issues) is a valid end state.
- The whole pipeline is read-only against the live Maestro loop and runtime data dir. This command does not touch `data/`, `docker-compose.yml`, source files, or any other shared state.
