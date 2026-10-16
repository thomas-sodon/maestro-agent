Run the full set of quality gates for the Maestro project — plus any failure class the current PR's live CI is red on — fixing any failures encountered, then commit and push the fixes.

The four command groups documented under "Quality Gates" in `CLAUDE.md` live in one place — `scripts/quality-gates.sh` — which this command, `/merge-main`, and the `quality-gate-hook.sh` Stop hook all call so the gate definitions never drift. Run it with a scope:

```
scripts/quality-gates.sh <scope>   # all | maestro | review | sdk | browser | unit | changed
```

It streams each group's tool output, then prints one `GATE <name>: PASS|FAIL(<stage>)` line per group and a final `OVERALL: PASS|FAIL`, exiting non-zero if any in-scope group failed.

This command differs from the opt-in Stop hook in four ways: it runs on demand (not just at agent stop), it includes the browser tests (the hook's `changed` scope never runs browser), with the `all` scope it runs every group regardless of which files have changed, and — unlike the purely local hook — it reconciles against the current PR's live CI, folding the gate group behind any failing CI check into the run so a class the requested scope skipped (or that only fails on the runner) still gets addressed. The three CI check jobs map onto the local groups: `quality` → `maestro`, `browser-tests` → `browser`, and the merged `package-quality` → `review` + `sdk` (that one job runs both sibling gate sets); a red CI check that is *not* one of these (the `AI Code Review` workflow, `deploy`) is out of scope here.

`$ARGUMENTS` is a space-separated list of tokens. Recognized tokens (anything else: warn and treat as `all` for scope, `fix` for mode):

Scope tokens:
- *(empty or `all`)* — all four groups
- `maestro` — only the `maestro/` package gates
- `review` — only the `maestro_review/` package gates
- `sdk` — only the `maestro_sdk/` package gates
- `browser` — only the browser tests
- `unit` — `maestro` + `review` + `sdk` (skip browser; fastest full-coverage signal)

Mode tokens:
- *(empty or `fix`)* — run gates, fix any failures by editing source or tests, then commit and push the fixes once the in-scope set is green (default)
- `check` — run gates and report only; never modify files, never commit. The PR CI inspection still runs — unless `no-ci` is also set — and is read-only: it expands the scope so the local status of every CI-red class is reported, but nothing is fixed.
- `no-commit` — modifier for fix mode: fix failures but stop short of committing or pushing, leaving the changes in the working tree
- `no-ci` — modifier: skip the current-PR CI inspection entirely and run only the requested local scope. Use when offline, when the branch has no PR, or when a local-only pass is what you want.

## Steps

1. **Parse $ARGUMENTS.** Apply scope and mode tokens as above.

2. **Locate the repo root.** `cd "$(git rev-parse --show-toplevel)"`. Stop and surface the error if this fails.

3. **Inspect the current PR's CI, and fold each failing check's class into the scope (skip in `no-ci` mode).** The local gate groups mirror CI, so a red CI check almost always reproduces under the matching local group — this step finds which classes CI is red on and unions them into the scope so they get run (and, in fix mode, fixed) even when the requested scope skipped them. Route every `gh` call through `scripts/with-env.sh`, which loads `GITHUB_TOKEN` from `.env`.
   - **Resolve the PR for the current branch.** Run `scripts/with-env.sh gh pr view --json number,headRefName,state,url`. Skip this whole step — keeping the requested scope unchanged — and note the reason when: there is no PR for the branch (`no pull requests found`), the branch is `main`, `GITHUB_TOKEN`/`gh` is unavailable, or the PR `state` is not `OPEN` (a closed/merged PR has no live CI to reconcile). A skip here is normal, not an error.
   - **Read the check rollup.** Run `scripts/with-env.sh gh pr checks <PR#> --json name,state,bucket,link,workflow`. **Capture its output regardless of exit code** — `gh pr checks` exits non-zero when any check is pending or failing, which is expected here, not a failure. Classify by the `bucket` field: `fail`/`cancel` = failing, `pending` = not yet conclusive (note it, do not treat as failing or as passing), `pass`/`skipping` = fine. If every check is `pass`/`skipping`, note `PR CI is green` and continue with the requested scope.
   - **Map each failing check to its gate class** and union that class into the in-scope set:

     | Failing CI check | `workflow` | Gate class to add |
     |---|---|---|
     | `quality` | Quality Gates | `maestro` |
     | `browser-tests` | Quality Gates | `browser` |
     | `package-quality` | Quality Gates | `review` + `sdk` |

     The merged `package-quality` job runs both sibling gate sets, so a failure there could be in either — union in **both** `review` and `sdk` (the script dedups, and running an already-green scope is cheap). Disambiguate with the `workflow` field — the `AI Code Review` workflow's check is also named `review`, but it is **not** the `package-quality` gate (whose `Quality Gates` workflow runs the `review` + `sdk` scopes). A failing check that is not one of the three gate jobs above — the `AI Code Review` workflow (which now also runs the advisory reply-coverage audit as a `continue-on-error` step, so on new pushes an unreplied-finding audit surfaces here rather than as its own check), a legacy `Reply Coverage` check on an older PR whose head predates the fold (not a required check, often red), `deploy`, or anything else — is **out of this command's scope**: record it for the report and point the user at `/process-comments` (for AI review findings) rather than trying to fix it here.
   - **Set the in-scope set** to the union of the requested scope tokens and the derived gate classes, and record each failing check's `link` for step 6. The gate script takes multiple scope tokens and dedups them, so pass them all at once (e.g. `scripts/quality-gates.sh unit browser`). Announce the resulting scope and why each extra class was added, e.g. `In-scope set: unit + browser (CI check browser-tests is failing)`.

4. **Confirm tooling.** Verify `python3.14 --version`, `python3.14 -m ruff --version`, `python3.14 -m mypy --version`, `python3.14 -m pytest --version`. If the in-scope set from step 3 includes `browser` or `all` (whether requested or folded in from a failing `browser-tests` CI check), also verify `python3.14 -m playwright --version` and that chromium is installed. If any required tool is missing, stop and tell the user to run the install commands from `CLAUDE.md`'s "Environment Prerequisites" section.

5. **Run the in-scope gates.** Map the in-scope set from step 3 (requested ∪ CI-derived) onto the script's scope tokens and run:
   ```
   scripts/quality-gates.sh <scope...>   # all | maestro | review | sdk | browser | unit (one or more, space-separated)
   ```
   **Run this in the foreground.** Invoke the script as a normal foreground `Bash` call with an explicit `timeout` of `600000` (the 10-minute max) — never pass `run_in_background: true`. A backgrounded gate run is reaped when the turn yields, so the suite dies silently and the work stalls with nothing continued. If the `all` scope (which includes the browser tests) would run past ten minutes, invoke one `scripts/quality-gates.sh <group>` call per group instead so each fits the ceiling. If a run is ever backgrounded anyway, poll it with `BashOutput` until the shell exits before doing anything else, and never end your turn while a gate run is still pending.

   The script runs each in-scope group independently — a failure in one group does not skip later groups, so you get a complete picture in one run (within a group the ruff→mypy→pytest chain still short-circuits). Read the per-group `GATE <name>: PASS|FAIL(<stage>)` lines plus the streamed tool output to capture which stage (ruff / mypy / pytest / coverage) failed in each group.

6. **Fix failures (default mode).** Skip this step in `check` mode. Otherwise, for each failing gate, follow the discipline below — diagnose, fix, re-run that gate (`scripts/quality-gates.sh <that-group>`). Iterate per gate up to **3 fix rounds**; if still red, stop and surface the remaining failures to the user. Apply fixes in this order so upstream noise doesn't mask downstream signal: **ruff → mypy → pytest → coverage → browser**. After all targeted fixes land, re-run the **full** in-scope set (`scripts/quality-gates.sh <scope...>`) once to catch regressions before reporting.
   - **CI-derived classes that do not reproduce locally.** A class folded in from a failing CI check (step 3) is fixed exactly like any other gate once it reproduces under the local run. If the local gate for that class comes up *green* while CI is red, do not declare victory — pull the failing run's log for the concrete error using the `link` recorded in step 3: derive the run id from the check URL and run `scripts/with-env.sh gh run view <run-id> --log-failed`. The usual cause is main-drift (CI ran against a tree that includes commits your branch lacks); the upstream merge in step 7 often resolves it, so proceed to commit/push and let the fresh CI run confirm. If it is a genuine runner-only failure you cannot reproduce or fix locally, surface it in the report rather than silently pushing over it.

7. **Commit and push the fixes (default fix mode only).** Skip this step entirely in `check` mode, in `no-commit` mode, or when no files were modified during the fix rounds. Otherwise proceed only when the final full in-scope re-run from step 6 is completely green — never commit code with a failing gate. Then commit and push by following the `/commit-push` command's flow, reusing its scripts so the logic never drifts:
   - Run `scripts/preflight.sh github-token not-main dirty-tree` first. If it fails (e.g. on `main`, missing `GITHUB_TOKEN`, or nothing to commit), surface the one-line diagnostic and skip the commit instead of working around it.
   - Stage only the files changed during the fix rounds, by explicit name — never `git add -A`, never stage `.env` or secrets.
   - Write a commit message explaining *why* the fix was needed (per CLAUDE.md), ending with the `Co-Authored-By: Claude <noreply@anthropic.com>` trailer.
   - Merge any upstream commits and resolve conflicts before pushing. If the merge brought in new upstream commits (a fast-forward, a merge commit, or a resolved conflict — i.e. the working tree changed), re-run the in-scope gate set (`scripts/quality-gates.sh <scope...>`, the same requested ∪ CI-derived scope from step 3) over the merged tree before pushing — the merge may have introduced code that no longer passes; if it is no longer green, treat it as a push failure and abort the push, surfacing the failure to the user rather than landing a still-failing tree. When the merge was a no-op (already up to date, or no upstream), the step-6 gates already cover the tree and need not re-run. Then push through `scripts/git-push-tracked.sh`, the shared helper `/commit-push` and `/merge-main` use, which sets upstream tracking on the first push and never force-pushes; branch on its exit code following `/merge-main` step 7's recovery cycle (not `/commit-push` step 7, which never runs gates): exit 0 pushed; exit 3 non-fast-forward, so re-integrate the advanced remote branch (fetch + merge, resolving conflicts), re-run the in-scope gate set over the re-merged tree, and re-push only when it is green — abort and surface the failure if it is not, since this recovery merge can break the tree exactly like the first; any other non-zero, stop and surface the error.
   - After pushing, capture the PR URL via `scripts/with-env.sh gh pr view --json url --jq .url` if one exists. The push re-triggers the PR's CI; this command does **not** wait for that fresh run to finish — it reports the branch/PR and lets CI verify the fix remotely.

8. **Report.** Lead with the CI reconciliation from step 3, then print a result block per group from the final run:
   - **PR CI reconciliation:** the PR number/URL and its check rollup at the start of the run — which checks were `pass` / `pending` / `failing` — the gate classes folded into the scope as a result, and any failing check that was **out of scope** here (e.g. `AI Code Review`, a legacy `Reply Coverage` check on an older PR, `deploy`) with the pointer to `/process-comments` or the relevant surface. If the CI step was skipped, say why (no PR, on `main`, PR not open, `no-ci`, or missing `gh`/token).
   - `PASS` with the headline metric (e.g. `pytest: 4052 passed, coverage 100%`).
   - `FAIL` with the failing stage and a concise extract — quote up to ~10 failures (file:line — message). For larger failure sets, give the count and the exact command to re-run for full output. For a CI-derived class that stayed green locally, say so and quote the CI-log extract instead — fetch it read-only by deriving the run id from the `link` recorded in step 3 and running `scripts/with-env.sh gh run view <run-id> --log-failed`. This is the same read-only fetch step 6 uses in fix mode, restated here so `check` mode — which skips step 6 — can still quote the extract this report requires.
   - `SKIP` with the reason (out of scope or tooling missing).

   In fix mode, also print:
   - A list of files modified during fix rounds.
   - For each fix: the gate that triggered it, whether it was requested-scope or CI-folded, the diagnosis (source bug / test bug / coverage gap / lint), and a one-line description of the change.
   - The commit/push result: the commit subject and branch pushed (plus PR URL if found), and a note that the push re-triggered CI; or the reason commit/push was skipped (check/no-commit mode, on `main`, nothing to commit, or a still-failing gate).

   End with a one-line overall verdict: `OVERALL: PASS` or `OVERALL: FAIL (groups: …)`.

9. **Suggest next action.** If anything is still failing after the fix budget is exhausted, point at the most upstream remaining failure and explain what's blocking the auto-fix. If a CI-red class could not be reproduced or fixed locally, name it and say whether the upstream merge/push is expected to clear it or whether it needs a look on the runner. If an out-of-scope CI check (e.g. `AI Code Review`) is failing, point at `/process-comments`. If everything is green and the fixes were committed and pushed, report the branch and the PR URL if found (and that CI will re-verify remotely); if commit/push was skipped, say why.

## Diagnosis & fix discipline

Apply these rules in fix mode. The goal is to make the gate genuinely pass, not paper over the failure.

### ruff failures
- First pass: `python3.14 -m ruff check --fix <scope>` — applies safe autofixes (imports, formatting, simple lints).
- Re-run `ruff check`. For remaining violations, read the rule and the surrounding code; edit to fix the underlying issue.
- Never disable a rule project-wide. Per-line `# noqa: <rule>` is acceptable only when the rule is genuinely wrong for that line, with a one-line comment explaining why.
- Never use `--unsafe-fixes` without inspecting the diff first.

### mypy failures
- Read each error: file:line, the actual error, the surrounding code.
- Decide whether the **code** is wrong (real type bug) or the **annotation** is wrong (overly loose / missing). Fix the underlying issue.
- Do not silence with `# type: ignore` unless there's a documented library-side mypy bug; if you do, add a one-line comment explaining why and link to the upstream issue if known.
- Avoid `cast` and `Any` as escape hatches — they propagate type holes.

### pytest failures (test failures)
- For each failing test: read the test, then read the production code under test. Decide which is the source of truth:
  - **Production code is buggy**: follow CLAUDE.md rule 11 — confirm there's a test that captures the correct behavior (the existing failing test usually is one), then fix the production code.
  - **Test is wrong/outdated**: update the test to match the correct behavior. Understand *why* the test was written before changing it — don't just delete a guard.
- Never use `pytest.skip`, `xfail`, or marker filters to bypass a failing test.
- Never delete a failing test without an equivalent or stronger test in its place.

### pytest coverage failures
- Read the missing lines and the surrounding function. Identify the branch or input shape that exercises them.
- Add a focused test in the matching `tests/test_<module>.py` file that hits the missing lines through realistic inputs — exercise the actual logic, not a mock-everything happy path.
- Do not add `# pragma: no cover`, lower the coverage threshold, or remove `--cov-fail-under` to make the gate pass. Use `# pragma: no cover` only when the line is genuinely unreachable (e.g. a runtime-only branch on a static config), with a comment explaining why.
- The Maestro coverage threshold is 100%; treat any uncovered line as a real gap.

### browser failures
- Read the failing assertion, the relevant page state (HTML/JS), and the test setup.
- Distinguish a flaky-selector / timing issue from a real app bug. For real app bugs, fix the dashboard code. For test issues, tighten the selector or fixture.
- Never increase test timeouts as a fix — investigate the root cause. Tests must respect the per-test runtime gate (60s for browser).
- The browser fixture must use the production `create_router()` per `CLAUDE.md`'s "Testing Principles" — don't reimplement the server.

### CI-reported failures (from the PR check rollup)
- A CI check maps to a gate class (`quality`→`maestro`, `browser-tests`→`browser`, `package-quality`→`review` + `sdk`); once folded into the scope (step 3) it runs locally and its stage-specific failure (ruff / mypy / pytest / coverage / browser) is fixed by the discipline above — there is no separate "CI fix", just the local gate reproducing the CI red.
- Read the CI failure at the stage granularity: `scripts/with-env.sh gh run view <run-id> --log-failed` names the failing step, which tells you which of the sections above applies.
- When the local gate is green but CI is red, the tree the two ran against differs. The usual cause is main-drift — CI included commits your branch lacks — which the upstream merge in the commit/push step resolves; commit, push, and let the fresh run confirm. Do not silence a CI failure you cannot reproduce; surface it.
- The `AI Code Review` check, `deploy`, and any non-gate check are **not** fixed here — they are not local gate groups. Route AI review findings to `/process-comments`; surface other check failures to the user.

### General fix discipline
- Stay focused: fix only what's needed to make the failing gate pass. Don't refactor surrounding code or add unrelated improvements.
- Re-read each modified file after the fix to confirm there's no leftover debug code, no dead branches, no inconsistent naming.
- After fixing one gate, re-run that gate before moving on — fixes can cascade.
- After all targeted fixes, re-run the entire in-scope gate set from scratch to catch regressions.

## What this command does NOT do

- Does not commit or push in `check` or `no-commit` mode, and never commits while a gate is failing or while on `main`.
- Does not run anything outside the four documented groups plus the CI inspection and the final commit/push (no smoke iteration, no `maestro preflight`, no maestro CLI).
- Does not bypass gates by skipping tests, lowering coverage, or globally disabling lints.
- Does not modify files in `check` mode — pure verification.
- Does not fix non-gate CI checks (the `AI Code Review` workflow, `deploy`, etc.) — those are out of scope; it names them and points at `/process-comments` or the user.
- Does not wait for the re-triggered CI run after pushing, and does not re-run or restart CI itself; it only reads the existing rollup and lets the push's fresh run verify remotely.
- Does not read the PR CI when `no-ci` is set, when there is no open PR for the branch, or when `gh`/`GITHUB_TOKEN` is unavailable — it degrades to the requested local scope.

## Safety

- All commands run from the repo root.
- Group failures are independent — never let one group's failure cause another to be silently skipped.
- Never run destructive git operations (no `reset`, `checkout --`, branch deletes, force-push) and never commit or push to `main`. The only git mutations allowed are staging the fix files, committing, merging upstream, and a normal push of the current branch.
- The PR CI inspection is **read-only**: it only reads the check rollup and run logs via `gh`. It never re-runs, cancels, or restarts CI, never edits the PR, and never posts a comment.
- Only commit and push after the in-scope gates (requested ∪ CI-derived) are green — never push a still-failing tree.
- Each gate has a 3-round fix budget; surface remaining failures to the user rather than looping indefinitely.
- CI enforces the gates on the PR and is the safety net that catches anything missed. `.claude/quality-gate-hook.sh` is an opt-in Stop hook you can register in `.claude/settings.local.json` to re-run scoped gates when the agent stops.
