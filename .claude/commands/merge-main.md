Merge the latest changes from main into the current branch and resolve any conflicts.

## Authentication

GitHub access needs `GITHUB_TOKEN` from the `.env` file in the project root. Run every `git fetch` and `git push` command through the wrapper, which loads `.env` and then execs the command; the examples below already use it:

```
scripts/with-env.sh <command>
```

## Steps

1. **Check prerequisites.** Run the shared preconditions — not on `main`, HEAD attached to a branch, and a clean working tree:
   ```
   scripts/preflight.sh not-main attached-head clean-tree
   ```
   It confirms the current branch is not `main` (switch to a feature branch first), that HEAD is attached to a branch rather than detached (check out a branch first), and that `git status --porcelain` is empty (commit uncommitted changes before merging). The `attached-head` check guarantees the downstream `git rev-parse --abbrev-ref HEAD` uses — in step 7's recovery cycle and the *Originating plan* derivation — resolve to a real branch name rather than the literal string `HEAD`. It exits non-zero with a one-line diagnostic on the first failed check; if it does, stop and surface that to the user.

2. **Fetch the latest from origin.**
   ```
   scripts/with-env.sh git fetch origin main
   ```

3. **Check if a merge is needed.** Run `git log HEAD..origin/main --oneline` to see if there are new commits. If there are none, tell the user the branch is already up to date and stop.

4. **Merge origin/main into the current branch.**
   ```
   git merge origin/main --no-edit
   ```
   If the merge completes cleanly (exit code 0), skip to step 6.

5. **Resolve conflicts.** If the merge produces conflicts:
   - Run `git diff --name-only --diff-filter=U` to list conflicted files.
   - **Resolve the originating plan first.** Run *Originating plan* (below) to locate the plan a previous session wrote for this branch's work, if there is one. When a relevant plan is found, its stated intent — what the change is trying to do, which files/symbols it touches, its verification / acceptance criteria — is the authoritative description of *this branch's* intended end state, and the tie-breaker when both sides changed the same logic. When none is found, resolve from git history alone exactly as before.
   - For each conflicted file, read the full file to understand both sides of every conflict marker (`<<<<<<<`, `=======`, `>>>>>>>`).
   - For each conflict, understand the intent of both changes before choosing a resolution. Read `git log --oneline HEAD..origin/main -- <file>` and `git log --oneline origin/main..HEAD -- <file>` to understand what each side was doing, then weigh both against the originating plan: the merged result must preserve the plan's intended change *and* keep main's change — never silently drop either side to match the plan.
   - Resolve every conflict by editing the file to produce the correct merged result — remove all conflict markers.
   - After resolving all conflicts in a file, stage it with `git add <file>`.
   - After all files are resolved, complete the merge with:
     ```
     git commit --no-edit
     ```
   - If a conflict resolution is ambiguous or high-risk (e.g., both sides modified the same logic in incompatible ways), explain the conflict and your resolution to the user before proceeding. When an originating plan informed the resolution, cite the specific plan step or acceptance criterion that drove it so the user can check it against intent.

6. **Run the quality gates.** The gate command groups live in `scripts/quality-gates.sh` (the single source of truth, shared with `/quality-gates` and the `quality-gate-hook.sh` Stop hook). Run the groups for whatever the merge touched — each prints `GATE <name>: PASS|FAIL(<stage>)` and exits non-zero on failure:

   - **Always** (core): `scripts/quality-gates.sh maestro`
   - **If `maestro_review/` or `tests/test_maestro_review/` were touched:** `scripts/quality-gates.sh review`
   - **If `maestro_sdk/` was touched:** `scripts/quality-gates.sh sdk`
   - **If `maestro/dashboard/` or `tests/browser/` were touched:** `scripts/quality-gates.sh browser`

   **Run each group in the foreground** with an explicit `Bash` `timeout` of `600000` (the 10-minute max) — never `run_in_background: true`. A backgrounded gate run is reaped when the turn yields, so the suite dies silently and the merge stalls half-verified. Invoke the groups as separate foreground calls (as listed) so each fits the ceiling. If a run is ever backgrounded anyway, poll it with `BashOutput` until the shell exits before doing anything else, and never end your turn while a gate run is still pending.

   If any gate fails, investigate and fix the issue. The failure is likely caused by the merge introducing incompatibilities. After fixing, stage the fix, amend the merge commit, and re-run the failing group until it passes.

7. **Push the merged commits to the remote.** Only run this step after every quality gate from step 6 has passed. Do not push if any gate failed. Push the current branch with the shared helper, which pushes normally when the branch already tracks a remote, sets upstream tracking (`git push -u origin HEAD`) on the first push, and never force-pushes:
   ```
   scripts/git-push-tracked.sh
   ```
   Branch on its exit code:
   - **Exit 0** — the push succeeded; continue to step 8.
   - **Exit 3** — the push was rejected as non-fast-forward (the remote feature branch has commits this branch does not). Run the recovery cycle below to integrate them and retry.
   - **Any other non-zero exit** — the push failed for another reason (auth, protected branch, hook failure). Stop and surface the error to the user — do not retry blindly.

   **Non-fast-forward recovery cycle (exit 3).** The remote feature branch has new commits. Integrate them and try again:
   1. Fetch the remote branch:
      ```
      scripts/with-env.sh git fetch origin "$(git rev-parse --abbrev-ref HEAD)"
      ```
   2. Merge it in (no rebase, to keep history consistent with the rest of this workflow):
      ```
      git merge "origin/$(git rev-parse --abbrev-ref HEAD)" --no-edit
      ```
   3. If this merge produces conflicts, resolve them using the same procedure as step 5, then `git commit --no-edit`.
   4. Re-run the relevant quality gates — re-apply step 6's package-detection rules to the post-recovery-merge state, re-detecting which packages have changes now that the commits just integrated from `origin/<branch>` are counted as well (not only the original `origin/main` merge), and run the gate groups that apply. This matters because the new upstream commits may have introduced incompatibilities, possibly in a package the original `origin/main` merge did not touch — so the subset that applied in step 6 can be too narrow. Fix any failures with new commits — don't amend the merge commit, so each fix stays a distinct, auditable commit and it's clear which change addressed which round of upstream integration.
   5. Push again with `scripts/git-push-tracked.sh`.
   6. Perform this recovery cycle **at most 3 times** (i.e. up to 3 re-integration and re-push attempts *after* the initial rejected push, for at most 4 push attempts total). If the helper still exits 3 after the 3rd recovery attempt, stop and surface the situation to the user — the branch is being actively pushed to and they need to coordinate.

8. **Report the result.** Summarize: how many commits were merged from `origin/main`, whether there were conflicts (and how they were resolved) — and, when there were conflicts, which originating plan (if any) informed their resolution and how it was located (branch name vs. folder pointer, or none found) — whether all quality gates passed, whether upstream feature-branch changes had to be integrated before pushing, and whether the push ultimately succeeded.

## Originating plan (for conflict resolution)

A previous session may have written a plan that the changes on this branch implement — via `/plan` or `/plan-proposal` then `/impl`, or `/bug-fix` / `/backlog`. When such a plan exists it is the clearest statement of what this branch is *supposed* to end up doing, so a conflict between this branch and `origin/main` should be resolved toward the plan's intended end state rather than guessed from diffs alone. Resolve it **lazily — only when step 5 has conflicts to resolve** (a clean merge never needs it), and treat it as **read-only, best-effort context**: if nothing is found, or what is found is not clearly related, fall back to git history exactly as the command did before. Never block, re-order, or abort the merge over a missing or ambiguous plan.

1. **Derive a candidate from the branch name.** Run `git rev-parse --abbrev-ref HEAD` and match it against the branch conventions the plan-writing commands use:
   - **Proposal-derived** — `feat/proposal-<NNN>-<slug>`, `feat/<NNN>-step<M>-<step-slug>`, or `feat/<NNN>-<slug>` (a leading zero-padded number). Extract `NNN`. The plan file is `~/.claude/plans/proposal-<NNN>-*.md` (glob; newest match if several); the proposal document is `docs/proposals/**/<NNN>-*.md`. Prefer the plan file; use the proposal's `Implementation steps` and `*Acceptance:*` criteria as the intent source when no plan file exists.
   - **Plan-file-derived** — any other `feat/<slug>`. The plan file is `~/.claude/plans/<slug>.md`. If no such file exists and the branch ends in a trailing `-<token>` segment whose `<token>` matches the `/impl` uid shape — a 12-digit timestamp optionally followed by hex characters, `^[0-9]{12}[0-9a-f]*$` (the `-<uid>` that `/impl` appends to every branch it auto-creates, see `/impl`'s *Unique branch suffix*), so the branch is `feat/<plan-slug>-<uid>` while the plan file stays `<plan-slug>.md` — strip that one uid-shaped trailing segment and retry the lookup once; an ordinary trailing slug word is left intact so it can never resolve a shorter, unrelated plan (step 4's file-overlap check is a further backstop).
2. **Fall back to the folder pointer.** If the branch name yields no existing plan, read `~/.claude/last-plan-by-folder.json` (read-only) and look up the entry for this command's absolute working directory; if it names a plan file that still exists and is non-empty, use it, and announce that the pick came from the folder pointer rather than the branch name so the user can object.
3. **Do not fall back to the global most-recent plan.** `~/.claude/plans/` is shared across sibling checkouts; picking the globally newest plan risks driving conflict resolution with an unrelated branch's plan. Branch-name match first, folder pointer second — otherwise treat the branch as having no originating plan and resolve from git history alone.
4. **Confirm relevance before trusting it.** Read the resolved plan in full, then check that at least one file it cites intersects the conflicted files from step 5 (or the files this merge changed). If there is zero overlap the plan probably is not what these changes are based on (or is stale) — announce that and use it only as weak background, never as the tie-breaker. If most cited paths do not exist in the tree at all, discard it as belonging to a different repo.
5. **Announce the pick.** Before resolving conflicts, state which plan (path) is informing the resolution and how it was located (branch name vs. folder pointer), or that no originating plan was found and resolution is git-history-only — so each conflict resolution in the report is traceable to a source.

## Safety

- Never run this on `main` — only on feature branches.
- Never force-push after merging — the `scripts/git-push-tracked.sh` helper pushes plainly (or `git push -u origin HEAD` to set upstream) and never passes `--force`.
- Never push if any quality gate failed — fix the failure first, then re-run gates, then push.
- Never discard local branch changes in favor of main — always produce a proper merge.
- The originating plan is advisory context only — it never overrides "produce a proper merge." Never drop `origin/main`'s side of a conflict just because the plan does not mention it, and never abort or re-order the merge over a missing, stale, or ambiguous plan.
- Read-only against `~/.claude/plans/` and `~/.claude/last-plan-by-folder.json` — this command never writes a plan file or the pointer.
- If the working tree is dirty, stop immediately rather than risk losing uncommitted work.
- Explain any non-trivial conflict resolutions to the user.
