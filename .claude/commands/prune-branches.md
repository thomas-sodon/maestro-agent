Delete local git branches whose origin counterpart has been deleted.

## Authentication

`GITHUB_TOKEN` is loaded from the `.env` file in the project root **if present, but is not required**. This is a purely-local cleanup whose only network step is a single `git fetch --all --prune`, run by `scripts/prune-branches.sh` through `scripts/with-env.sh --optional-token` (soft-token mode): the wrapper loads `.env` when it has a token, otherwise it execs the fetch with whatever ambient auth exists (SSH keys, a git credential helper, public access). The fetch itself is the arbiter — it proceeds when it succeeds without a token, and only a fetch that genuinely fails stops the command (e.g. a private-HTTPS origin with no token). The force-delete step below is purely local and needs no token.

## Steps

1. **Check prerequisites.** Run the shared precondition — inside a git work tree:
   ```
   scripts/preflight.sh work-tree
   ```
   It checks that `git rev-parse --is-inside-work-tree` succeeds, exiting non-zero with a one-line diagnostic if it fails. `GITHUB_TOKEN` is deliberately **not** a precondition here (see Authentication): the fetch runs in soft-token mode and is its own arbiter, so a tokenless-but-fetchable origin still prunes. If the check fails, stop and surface the error to the user.

2. **Parse $ARGUMENTS.** Recognize the following flags (any other token: warn the user but do not abort):
   - `--dry-run` — pass through to the helper in step 3 so it prints the plan and stops without deleting anything.
   - `--force` — consumed here, **not** passed to the helper: after the safe-delete pass, ask the user once for blanket confirmation to force-delete the unmerged remainder in step 4. Without this flag, ask per-branch.

3. **Run the prune helper.**
   ```
   scripts/prune-branches.sh
   ```
   (append `--dry-run` when step 2 saw it). The helper performs the whole deterministic sequence — single-sourced so it never drifts: capture the protected set (current branch, `main`/`master`/`develop`, and every branch checked out in a worktree), `git fetch --all --prune` (via `with-env.sh`), enumerate `[gone]`-upstream branches via `git for-each-ref … %(upstream:track)`, subtract the protected set, print the **Prune plan** (each candidate's name, last commit, and merged-against-trunk status — measured against the default branch's freshly-fetched remote-tracking ref, named on the plan's `merged status measured against:` line, not the checked-out `HEAD` — plus a `protected (excluded …)` line naming any `[gone]` branch the protected set kept off the candidate list), and — unless `--dry-run` — run the safe `git branch -d` pass, printing a **Results** section that lists the safe-deleted branches and the `-d`-refused (unmerged) remainder separately.

   - If the helper exits non-zero (e.g. the fetch failed, or a bad argument), stop and surface its diagnostic — do not proceed against stale data.
   - If it reports "nothing to prune", report that to the user and stop.
   - If `--dry-run` was passed, the helper stops after the plan; report the plan and stop without deleting.

4. **Handle the unmerged remainder.** The helper **never** escalates `-d` to `-D`; it hands you the unmerged branches it refused. The actual safe-delete gate stays git's own `git branch -d`, which refuses a branch not merged into `HEAD`/upstream — the plan's trunk-relative merged status is advisory display, not the delete decision. For each refused branch from the helper's **Results**, show the commits it carries that are **not** on the trunk — compare against the **ref token** the helper reports on its `merged status measured against:` line: the bare ref only (e.g. `origin/main`), with any parenthetical annotation stripped, so the trunk-less degrade line `HEAD (no trunk ref resolved)` contributes just the token `HEAD` (i.e. `git log <branch> ^HEAD`). Comparing against that resolved trunk ref rather than an unconditional `^HEAD` makes the listing show only work a force-delete would truly lose:
   ```
   git log <branch> ^<trunk> --oneline | head -20
   ```
   Then:
   - Without `--force`: ask the user per-branch which to force-delete. Run `git branch -D <branch>` only on the user-approved subset.
   - With `--force`: ask once for blanket confirmation across the full unmerged list, then run `git branch -D <branch>` on each. Never delete unseen.

5. **Report the result.** Final summary:
   - safe-deleted: count and branch names (from the helper's Results)
   - force-deleted: count and branch names (if any, from step 4)
   - skipped: any branch on the helper's `protected (excluded …)` line (current / `main`/`master`/`develop` / worktree), plus any the user declined in step 4
   - total candidates considered

## Safety

The first three invariants are mechanically enforced inside `scripts/prune-branches.sh` and pinned by `tests/test_prune_branches_script.py`, not merely restated here:

- Never delete `main`, `master`, `develop`, or the currently checked-out branch — even if their upstream is gone (protected-set subtraction in the helper).
- Never escalate from `-d` to `-D` automatically — the helper only ever runs `git branch -d`. Unmerged branches require either the `--force` flag with one explicit blanket confirmation, or per-branch user approval, in step 4.
- Never delete a branch checked out in a worktree — the helper adds every worktree branch to the protected set; surface it as skipped instead.
- This command does not push, commit, or modify origin — it is purely local cleanup. Do not run quality gates here.
- If `git fetch --prune` fails, the helper exits non-zero before any deletion — stop and surface, since `[gone]` detection is invalid without a successful fetch.
