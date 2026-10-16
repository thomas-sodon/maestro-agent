Create a pull request for the current changes in the Maestro project.

## Authentication

GitHub access needs `GITHUB_TOKEN` from the `.env` file in the project root. Run every `gh`, `git push`, and `git fetch` command through the wrapper, which loads `.env` and then execs the command; the examples below already use it:

```
scripts/with-env.sh <command>
```

This ensures the token is available for GitHub API calls without requiring global shell configuration.

## Steps

1. **Check prerequisites.** Run the shared GitHub-token precondition:
   ```
   scripts/preflight.sh github-token
   ```
   It checks that `.env` defines a non-empty `GITHUB_TOKEN` and exits non-zero with a one-line diagnostic if not — stop and surface that to the user. Then confirm there is work to turn into a PR by checking *either* of:
   - Uncommitted work: `git status --porcelain` is non-empty (staged, unstaged, or untracked changes).
   - Unpushed commits on the current branch: `git log @{u}..HEAD` is non-empty when an upstream is configured, or `git log origin/main..HEAD` is non-empty when no upstream is configured yet.

   Only stop and tell the user if *both* are empty. A clean working tree with unpushed commits is a valid starting point — it's exactly the case this command exists to handle.

2. **Determine branch name.** If already on a non-main feature branch, use it. Otherwise, ask the user for a branch name or derive one from $ARGUMENTS if provided. Branch naming convention: `feat/short-description`, `fix/short-description`, or `refactor/short-description`.

3. **Create and switch to the branch** (if not already on a feature branch):
   ```
   git checkout -b <branch-name>
   ```

4. **Stage and commit changes.** If `git status --porcelain` is empty (clean tree with only unpushed commits), skip this step and go to Step 5. Otherwise, review all changes with `git diff` and `git status`. Stage relevant files (prefer explicit file names over `git add -A`). Write a clear commit message that explains *why*, not just *what*. End the commit message with:
   ```
   Co-Authored-By: Claude <noreply@anthropic.com>
   ```
   Use a HEREDOC for the commit message to preserve formatting.

5. **Push the branch.** Push the current branch with the shared helper, which sets upstream tracking (`git push -u origin HEAD`) on the first push and pushes normally if it already tracks a remote, and never force-pushes:
   ```
   scripts/git-push-tracked.sh
   ```
   Branch on its exit code:
   - **Exit 0** — pushed; continue to step 6.
   - **Exit 3** — the push was rejected as non-fast-forward: the branch already exists on the remote with commits you do not have locally. Stop and surface this to the user — for a new PR branch this usually means the branch name collides with existing remote work; do not force-push or blindly merge. (Matches the Safety note: "If the branch already exists on the remote, confirm with the user before pushing.")
   - **Any other non-zero exit** — the push failed for another reason (auth, protected branch, hook). Stop and surface the error to the user.

6. **Check for a pre-existing open PR on the branch.** The Step 5 push has *already* delivered the new commits to any open PR for this branch — GitHub auto-updates an open PR whenever its branch is pushed — so a duplicate `gh pr create` would only fail with a raw "a pull request already exists" error. Detect that state before creating:
   ```
   scripts/with-env.sh gh pr view --json number,url,state
   ```
   `gh pr view` with no argument resolves the PR associated with the just-pushed current branch.
   - **An open PR already exists** (`gh` exits 0 and `state` is `OPEN`) — report its URL and stop here. GitHub has already updated that PR with the pushed commits, so there is nothing more to do; do **not** run `gh pr create` (it would only error on the duplicate).
   - **No PR, or the branch's PR is closed/merged** (`gh` exits 0 with `state` `CLOSED` / `MERGED`, or `gh` exits non-zero *because no PR exists* — its output says "no pull requests found") — there is nothing to short-circuit. Continue to the issue-determination and create steps below. Only an **open** PR short-circuits creation: a previously closed or merged PR on the same branch name must not block a fresh `gh pr create`.
   - **`gh pr view` errored for an unrelated reason** (a non-zero exit whose output does **not** say "no pull requests found" — an auth error, a network/rate-limit failure, or a misconfigured `gh`) — do **not** treat this as "no PR" and fall through to `gh pr create`: were an open PR to actually exist, that create call would re-hit the raw "a pull request already exists" error this step exists to prevent. Stop and surface the failure to the user instead.

7. **Determine the issue to close (bug-fix PRs).** A bug-fix PR should auto-close its GitHub issue on merge via a [closing keyword](https://docs.github.com/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue) in the body. Gather candidate issue numbers from all three sources below — do **not** stop at the first hit, because the cross-source conflict check at the end needs every candidate. The branch name is the authoritative per-PR signal (you are opening a PR *for this branch*); the folder pointer is not branch-aware and can be stale.

   - **Branch name (authoritative).** Parse the branch for an embedded issue number after a `fix` segment — `fix/<NNN>-…`, `fix-<NNN>-…`, or `feat/fix-<NNN>-…` (e.g. `feat/fix-1234-stale-session` → `1234`). Call this `branch_issue`.
   - **Plan file.** `/bug-fix` and `/backlog` write the fix plan with a `Fixes: #<NNN>` metadata line directly under its title; take the issue number from its first `Fixes: #<NNN>` (or `Closes: #<NNN>`) line. Resolve the plan path:
     - Prefer the branch-derived plan — strip a leading `feat/`, `fix/`, or `refactor/` from the branch and read `~/.claude/plans/<slug>.md` if it exists. If no such file exists **and** the branch ends in a trailing `-<token>` segment whose `<token>` matches the `/impl` uid shape — a 12-digit timestamp optionally followed by hex characters, `^[0-9]{12}[0-9a-f]*$` (the `-<uid>` that `/impl` appends to every branch it auto-creates, see `/impl`'s *Unique branch suffix*): the branch is `feat/<plan-slug>-<uid>` while the plan file stays `<plan-slug>.md` — strip that one uid-shaped trailing segment and retry the lookup once. Only a uid-shaped segment is stripped: an ordinary trailing slug word is left intact (`feat/add-caching-layer` is **not** stripped to `add-caching`, so it can never resolve a shorter, unrelated plan like `add-caching.md`). Because the plan filename equals the branch slug up to that optional `-<uid>` suffix and only the uid shape is ever stripped, this path is branch-specific and cannot point at another branch's issue.
     - Only if no branch-derived plan exists, fall back to `~/.claude/last-plan-by-folder.json` (keyed by absolute working directory; entry's `plan` path must still exist). This pointer records the *most recently written* plan for the directory, **not** the plan for this branch, so treat it as unreliable: discard it if `branch_issue` exists and the pointer plan's number ≠ `branch_issue`, **and** discard it when `branch_issue` is absent unless `commit_issue` corroborates the same number — a branch with no embedded `fix-<NNN>` is almost certainly not a bug-fix branch, so an uncorroborated folder pointer must never add a closing keyword.
     - Call the surviving plan number `plan_issue`.
   - **Commit messages.** `git log origin/main..HEAD` — if a commit already contains `Fixes #<NNN>` / `Closes #<NNN>`, call it `commit_issue`.

   Reconcile `branch_issue`, `plan_issue`, and `commit_issue`:
   - **No source yielded a number** → this is not an issue-closing PR (a plain `feat`/`refactor`). Proceed without a closing keyword — do not invent one.
   - **The numbers that surfaced all agree, or only one source yielded a number** → that is the issue.
   - **Distinct numbers remain after the stale-pointer cross-check** → stop and ask the user which issue this PR closes; never guess.

   Once a single number is settled, verify it is a real, open issue before trusting it:
   ```
   scripts/with-env.sh gh issue view <NNN> --json number,state,title
   ```
   - Open issue → use it; include `Fixes #<NNN>` in the PR body (Step 8).
   - Already closed, or `gh` returns "not found" → do **not** add the closing keyword. Warn the user with the number and what was found, and ask whether to proceed without it (or to supply the correct number).

8. **Create the pull request.** Run this step only when Step 6 found that no open PR exists for the branch (an open PR would have short-circuited at Step 6). Include the `Fixes #<NNN>` line **only** when Step 7 identified an open issue; omit it entirely otherwise. The closing keyword must live in the PR body — GitHub auto-closes the issue when this PR merges into `main`.
   ```
   scripts/with-env.sh gh pr create --title "<title>" --body "$(cat <<'EOF'
   ## Summary
   <1-3 bullet points>

   Fixes #<NNN>

   ## Test plan
   - [ ] Quality gates pass (ruff, mypy, pytest)
   - [ ] <additional items as appropriate>

   Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
   ```
   Keep the title under 70 characters. Use the body for details.

9. **Report the result.** Show the PR URL so the user can review it — whether it is a freshly created PR (Step 8) or the pre-existing open PR the push updated (Step 6). If a `Fixes #<NNN>` keyword was included, note which issue will auto-close on merge.

## Safety

- Never commit `.env`, credentials, or secret files.
- Never force-push or push to `main` directly.
- If the branch already exists on the remote, confirm with the user before pushing.
- Do not run quality gates here — CI enforces them on the PR. `.claude/quality-gate-hook.sh` is an opt-in Stop hook you can register in `.claude/settings.local.json` to run scoped gates when the agent stops.
- Only add a `Fixes #<NNN>` keyword when Step 7 identifies an open issue with confidence — never guess a number, never close-reference an already-closed or non-existent issue, and never invent one for a `feat`/`refactor` PR that has no underlying issue.
