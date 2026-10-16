Commit all current changes and push to the remote branch.

## Authentication

GitHub access needs `GITHUB_TOKEN` from the `.env` file in the project root. Run every `gh`, `git push`, and `git fetch` command through the wrapper, which loads `.env` and then execs the command; the examples below already use it:

```
scripts/with-env.sh <command>
```

## Steps

1. **Check prerequisites.** Run the shared preconditions — GitHub token present, not on `main`, and there are changes to work with:
   ```
   scripts/preflight.sh github-token not-main dirty-tree
   ```
   The script checks that `.env` defines a non-empty `GITHUB_TOKEN`, that the current branch is not `main` (never push directly to `main`), and that `git status --porcelain` is non-empty (staged, unstaged, or untracked changes). It exits non-zero with a one-line diagnostic on the first failed check; if it does, stop and surface that to the user.

2. **Review changes.** Run `git diff` (staged and unstaged) and `git status` to understand all changes. Also run `git log --oneline -5` to see recent commit style.

3. **Stage changes.** Stage relevant files using explicit file names (prefer `git add <file>...` over `git add -A`). Never stage `.env`, credentials, or secret files. If $ARGUMENTS is provided, use it as guidance for which files to include or as the commit message.

4. **Commit changes.** Write a clear, concise commit message that explains *why*, not just *what*. Follow the repository's existing commit message style. End the commit message with:
   ```
   Co-Authored-By: Claude <noreply@anthropic.com>
   ```
   Use a HEREDOC for the commit message to preserve formatting:
   ```
   git commit -m "$(cat <<'EOF'
   <type>: <description>

   Co-Authored-By: Claude <noreply@anthropic.com>
   EOF
   )"
   ```

5. **Merge upstream changes.** Fetch from the configured remote unconditionally, then check whether the branch has an upstream with new commits before pushing:
   ```
   scripts/with-env.sh git fetch
   ```
   `git fetch` with no remote argument honors the branch's configured upstream remote (falling back to `origin` when none is set). Next, probe explicitly for an upstream tracking ref rather than inferring it from a diff command's error output:
   ```
   git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null
   ```
   If this probe exits non-zero, the branch has no upstream tracking ref (`git log HEAD..@{u}` would otherwise print a noisy `fatal: no upstream configured` on stderr) — skip to step 7. Only when the probe succeeds, run `git log HEAD..@{u} --oneline` to list new upstream commits; if it prints nothing there are no new commits, so skip to step 7. Otherwise continue to step 6.

6. **Merge and resolve conflicts.** Merge the upstream branch:
   ```
   git merge @{u} --no-edit
   ```
   If the merge produces conflicts:
   - Run `git diff --name-only --diff-filter=U` to list conflicted files.
   - For each conflicted file, read the full file to understand both sides of every conflict marker (`<<<<<<<`, `=======`, `>>>>>>>`).
   - Resolve every conflict by editing the file — remove all conflict markers.
   - Stage resolved files with `git add <file>`.
   - Complete the merge with `git commit --no-edit`.
   - If a conflict resolution is ambiguous or high-risk, explain it to the user before proceeding.

7. **Push to remote.** Push the current branch with the shared helper, which pushes normally when the branch already tracks a remote, sets upstream tracking (`git push -u origin HEAD`) on the first push, and never force-pushes:
   ```
   scripts/git-push-tracked.sh
   ```
   Branch on its exit code:
   - **Exit 0** — pushed; continue to step 8.
   - **Exit 3** — the push was rejected as non-fast-forward (the remote branch advanced after step 5, e.g. a concurrent push). Re-integrate by repeating steps 5–6 (fetch and merge the upstream branch, resolving any conflicts), then push again with `scripts/git-push-tracked.sh`. If it still exits 3 after a few attempts, stop and tell the user the branch is being pushed to concurrently.
   - **Any other non-zero exit** — the push failed for another reason (auth, protected branch, hook). Stop and surface the error to the user.

8. **Look up the PR.** After pushing, check whether the current branch has an open pull request:
   ```
   scripts/pr-url.sh
   ```
   The helper runs `gh pr view --json url --jq .url` through `scripts/with-env.sh`.
   - **Exit 0** — capture the output: a non-empty line is the PR URL; empty stdout means the branch simply has no PR to link.
   - **Any non-zero exit** — a genuine failure (a gh/auth/network error, or a missing `.env`/`GITHUB_TOKEN`), which the helper re-emits on stderr rather than masking as "no PR". Surface it to the user rather than reporting "no PR".

9. **Report the result.** Show a summary: what was committed (files changed, insertions, deletions), whether upstream changes were merged (and any conflicts resolved), and the remote branch it was pushed to. If a PR URL was found in step 8, include it as the last line of the summary so the user can click straight through.

## Safety

- Never commit `.env`, credentials, or secret files.
- Never force-push.
- Never push to `main` directly.
- If there are no changes to commit, stop and tell the user rather than creating an empty commit.
- Do not run quality gates here — the stop hook handles that automatically.
