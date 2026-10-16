Proactively hunt for bugs, architectural gaps, and invariant inconsistencies in the Maestro codebase, then file a GitHub issue for each confirmed finding.

This command is **proactive diagnosis** — it does not wait for a failure to occur. It reads the code, compares it against the documented architecture invariants in CLAUDE.md, and looks for defects or gaps that have not yet been reported. The output is one GitHub issue per confirmed, distinct finding — zero, one, or many per invocation (or a dry-run preview of every draft). `/rca` and `/bug-fix` address known failures; this command *discovers* unknown ones.

`$ARGUMENTS` is a space-separated list of tokens. Recognized tokens:

- `--area=<module>` — restrict the hunt to one module (e.g. `--area=agent`, `--area=memory`). The value resolves to the first of `maestro/<value>.py`, `maestro/<value>/`, or a top-level package directory `<value>/` (e.g. `--area=maestro_sdk`, `--area=maestro_review`) that exists on disk. A value resolving to no existing path is rejected with `error=invalid_argument`, naming the unresolved value. May appear multiple times. If omitted, all modules are in scope.
- `--focus=<category>` — restrict to one finding category: `bugs`, `architecture`, or `inconsistencies`. May appear multiple times. A `--focus` value that is not one of those three categories is rejected with `error=invalid_argument`, naming the unrecognized value and listing the three valid categories. Defaults to all three.
- `--dry-run` — print every confirmed finding's draft title and body to stdout (each separated by a distinctive `===== FINDING <n>/<total> =====` header line, not `---`, per step 10); do not create any GitHub issue, and change no git state (the merged-branch switch and the on-`main` refresh below are both skipped, per *Switch to main if on a merged branch*).
- *(no tokens)* — full-scope hunt across all modules and all three finding categories.

Reject any token that is not a recognized flag (unknown `--flag=value`, bare words) with `error=invalid_argument` and the expected forms.

## Finding categories

### `bugs` — Defects in logic or control flow

Concrete defect patterns to look for, in priority order. Stop adding to the candidate list once you have 5; deeper searching beyond that rarely changes which finding scores highest.

1. **Unguarded None / missing-key dereferences** — An attribute access or dict lookup on a value that can be `None` or absent by contract, with no guard, no default, and no enclosing `try/except` that would catch it. Evidence: the type annotation includes `Optional[T]` or `T | None`, or a `get()` call returns `Optional`, and the result is immediately used without a null check. Confirm with a Read of the function and its tests before classifying.

2. **Incorrect error-category classification** — A new exception type or failure mode that falls through to the `else` branch of `_TRANSIENT_SDK_CATEGORIES` checks (or equivalent) and is either silently swallowed or misclassified as a different category, causing wrong retry / crash behavior.

3. **TOCTOU / atomic-write gaps** — A read-then-write sequence on a JSON state file that does not use `file_lock()` or `atomic_write()` as required by the "File persistence" invariant in CLAUDE.md, leaving a window where a concurrent write could corrupt state. Look for `open(..., 'w')` or `json.dump()` calls on `data/` paths outside `fileutil.py`.

4. **Unbounded growth in a capped data structure** — A list, dict, or JSONL file that CLAUDE.md says is capped (or whose cap constant exists in code) but where a code path appends without the truncation guard. Confirm by reading both the cap constant definition and the append site.

5. **Session-attachment violations** — A call to `query()` (bare, not `query_on_session`) in a main-phase function that CLAUDE.md says must be session-attached. The AST-enforced test (`tests/test_structural_guards.py`) covers the known cases; look for new main-phase functions added since the test's allowlist was last updated.

### `architecture` — Gaps between documented invariants and their enforcement

1. **Undocumented allowlist items** — A function, import, or pattern that was added to an AST-enforcement allowlist (in any `tests/test_*.py` that walks the AST) without a corresponding CLAUDE.md or `docs/` update explaining why it is exempt. The allowlist entry exists but no prose justifies it.

2. **Invariant documented but not tested** — CLAUDE.md states a behavioral rule (e.g., "X always does Y", "Z is bounded by N") but no test in `tests/` asserts it. Use Grep to find the invariant's key terms in test files; if no match, it is a gap. Do not report rule-body prose from CLAUDE.md that is inherently non-testable (architectural principles, stage descriptions).

3. **Gate-chain bypass** — A path in `generate_task()` or `_run_task_phase()` that can skip one or more entries in `GATE_CHAIN` (`maestro/task_gates.py`) without recording a `log_gate_activation()` call. The gate must fire and log or be explicitly skipped; a silent bypass is a gap.

4. **Provider-capability guard missing** — A mutating phase (branch push, PR open, merge) that routes through the SDK without a `validate_action_gate_coverage` check, violating the "fail loud on missing capabilities or guard-less mutating-phase routing" invariant.

5. **Broad `except` in a non-allowlisted site** — A `try/except Exception` (or bare `except`) block that logs-and-continues or has a `pass` body, outside the documented allowlist in `tests/test_no_silenced_exceptions.py`. The test catches these at CI time, but look for cases where the code path never reaches CI (dead code, `if TYPE_CHECKING:` guards, etc.).

### `inconsistencies` — Disagreements between two authoritative sources

1. **Cap constant value drift** — A numeric cap that appears more than once in the codebase (constant definition + usage + optional CLAUDE.md mention) where the values disagree. Examples: `MAX_LIST_ITEMS` used with a different literal elsewhere; `MAX_ITEM_CHARS` referenced in two modules with different defaults; a CLAUDE.md prose cap that disagrees with the constant in code.

2. **Literal-vs-type mismatch** — A string literal used in a comparison against a `Literal[...]` type alias from `maestro/types.py` where the literal is not in the alias's union. This is a latent `KeyError` or silent no-match that static type checking may miss if the comparison is buried in a dynamic dispatch path.

3. **Duplicate state-write logic** — Two modules that each implement the same write-to-JSONL operation (append, truncate, archive) differently, where CLAUDE.md says one of them is the canonical path. Look for patterns like `open(..., 'a')` on the same file in multiple modules — if one module uses `atomic_write` and another does not for the same file, that is the inconsistency.

4. **Divergent schema handling** — A JSON state file that is loaded and written by more than one module, where `ensure_schema()` fingerprint logic is only invoked in one of the two. The second writer may produce records that fail the startup migration in a future run.

5. **Test fixture vs. production code drift** — A `mem_fs` fixture path in `tests/` that does not match the path the production code actually uses, meaning the test never exercises the real file path. Confirm by reading both the test fixture setup and the production `open()` call.

## Switch to main if on a merged branch

Land on a current tree before hunting. This command reads today's tree first-hand — grepping and reading Maestro's source before filing a GitHub issue per finding — so a leftover task branch that has already merged into `main`, or a local `main` trailing `origin/main`, would hunt stale code and file issues for bugs already fixed on `main`. Switch to `main` **first** — but only when it is provably safe, and never under `--dry-run`: the switch and the on-`main` refresh below are both local mutations, so under `--dry-run` this entire section changes no git state — it prints the intent it *would* execute and proceeds on the current branch. This section runs after argument parsing (Steps §1, so `--dry-run` is known) and before the hunt reads any code (before Steps §3).

1. Resolve the current branch: `git rev-parse --abbrev-ref HEAD`.
   - `main` → already on `main`, so no switch is needed — but still refresh so the hunt reads the latest `main`, not a stale local one. On a clean tree (`git status --porcelain` empty), run the section's best-effort `scripts/with-env.sh git fetch origin main` and then `git merge --ff-only origin/main` to advance local `main` to the fetched tip before hunting (the exit-0 refresh of step 4, minus the redundant `git switch main`; skip the fast-forward on the fetch-fallback path, where there is no fresher `origin/main` to advance to). If the tree is dirty, skip the refresh and proceed on the current `main`, leaving uncommitted changes untouched. If the fetch is unavailable (no token, offline) or the `git merge --ff-only origin/main` fails (local `main` has diverged), warn that local `main` could not be advanced and proceed on the current local `main`.
   - `HEAD` (detached) → no branch to evaluate; warn and proceed without switching.
2. Dirty-tree guard. If `git status --porcelain` is non-empty, do **not** switch — uncommitted changes are left untouched per *Safety*. Warn that the switch was skipped because of local changes and proceed on the current branch.
3. Refresh main (best-effort): `scripts/with-env.sh git fetch origin main`. On failure (no token, offline), fall back to the local `main` ref; the merged check still runs, just against a possibly-stale `main`.
4. Fully-merged test. The branch is fully merged when every commit on it is already in main — `git merge-base --is-ancestor HEAD origin/main` (or `HEAD main` on the fetch fallback) exits 0.
   - Exit 0 → `git switch main`, then fast-forward local `main` to the fetched tip with `git merge --ff-only origin/main` so the working tree lands on the merged code, not a stale local `main` that the fetch never advanced (skip this fast-forward on the fetch-fallback path, where there is no fresher `origin/main` to advance to). On success, announce the switch (`Switched from <branch> (fully merged) to main`). If `git switch main` fails (e.g. `main` is already checked out in a sibling worktree, or local `main` is missing/invalid) or the `git merge --ff-only origin/main` fails (e.g. local `main` has diverged), warn that the switch could not complete and proceed on the current branch — exactly like the dirty-tree, fetch-failure, and unmerged-branch fallbacks above.
   - Non-zero → the branch carries unmerged commits; leave it, warn that the branch has unmerged work, and proceed on the current branch.

The switch never discards commits (it fires only when the branch adds nothing to main), never runs against a dirty tree, and is a plain checkout of `main` followed by a fast-forward to `origin/main` — no branch is created, no history rewritten.

## Steps

1. **Parse `$ARGUMENTS`.** Collect `--area` values (if any), `--focus` values (if any), and the `--dry-run` flag. Unknown tokens → `error=invalid_argument`. Resolve each `--area` value per the matching rule above; if any value resolves to no existing path, stop with `error=invalid_argument` naming it. Validate each `--focus` value against the category set `{bugs, architecture, inconsistencies}`; if any value is not one of the three, stop with `error=invalid_argument`, naming the unrecognized value and listing the three valid categories. Build the active module list (all `maestro/*.py` + `maestro/*/` if no `--area` given, else the resolved paths). Build the active category list (`bugs`, `architecture`, `inconsistencies` if no `--focus` given, else the named ones).

2. **Authentication pre-check.** Verify `scripts/with-env.sh` exists and `.env` contains a non-empty `GITHUB_TOKEN`. In non-dry-run mode, if either is missing, warn now and tell the user what to populate — do not wait until step 10 to fail. In `--dry-run` mode the final post never runs, but step 8's duplicate query still needs the token: if it is missing, warn that the duplicate check will be skipped and continue (do not stop).

3. **Hunt for candidates.** For each active category, run the searches described in *Finding categories* above. Use Grep to locate patterns; use Read to confirm the context before recording a finding. Record each candidate as:

   ```
   category: <bugs | architecture | inconsistencies>
   title: <one-line description, ≤80 chars>
   file:line: <the decisive code location>
   severity: <high | medium | low>   # high = crash / data corruption / invariant broken silently; medium = wrong behavior, covered by tests; low = cosmetic / unlikely path>
   evidence: <2–5 sentences of evidence read from the code>
   ```

   Stop after 5 candidates per category (15 total maximum). Breadth-first, not depth-first — a shallow pass over the whole scope finds more diverse candidates than exhaustive analysis of one function.

4. **Score and rank.** Rank all candidates by:
   - `severity` (high > medium > low)
   - Finding category tie-break: `bugs` > `architecture` > `inconsistencies`
   - Within the same severity+category: prefer findings whose `file:line` is in a hot path (mentioned in CLAUDE.md's "Core loop" orientation) over cold paths.

   This ranking is not a filter — every candidate advances to confirmation in step 5. The rank only sets the **order** in which findings are deep-read, deduped, and (later) posted, so the most significant issue is filed first.

5. **Deep-read and confirm every candidate.** Walk the ranked list in order. For each candidate, do the thorough analysis:
   - Read the full function(s) at `file:line` and their direct callers and callees.
   - Read the relevant test file(s) — confirm whether this case is covered.
   - Run `git log --oneline -10 -- <file>` to check for recent changes at the site.
   - Cross-check the relevant CLAUDE.md invariant(s) by name.
   - Determine: is this a confirmed defect, a gap, or a false positive?

   Drop any candidate that is a **false positive** (code is actually correct after full read), recording the reason it was ruled out. Every candidate that survives is a **confirmed finding**; collect them all — do not stop at the first.

   **Dedupe confirmed findings.** Two findings are duplicates when they overlap in code location (same function or same `file:line` span) *and* share the same preliminary defect classification from the deep-read above — the same category and symptom, the signals available at this step before step 6 formally determines root cause. Keep the higher-ranked one and drop the rest, noting the merge. The result is the ordered list of **distinct confirmed findings**, one issue per entry; if step 6's root-cause analysis later reveals that two survivors share one root cause, fold the lower-ranked one into the higher there.

   If no candidate survives (all false positives), stop with `error=no_confirmed_finding` and summarize why each was ruled out.

6. **Determine the root cause.** *Steps 6–9 run once per distinct confirmed finding, in ranked order — each finding gets its own root-cause analysis, duplicate check, and issue body.* For each finding, separate: symptom → proximate cause (`file:line`) → root cause (the upstream reason the defect exists — missing invariant, wrong contract, absent test, recent regression). Apply the same layered analysis as `/rca`'s step 9. If only the proximate cause is clear, say so explicitly; do not invent a root cause.

7. **Draft the suggested fix** (optional). Include a suggested fix only if:
   - The root cause was identified in step 6.
   - The fix is a root-cause change, not a workaround.
   - The fix does not match any of the forbidden patterns in the *Forbidden suggestion patterns* section below.

   If any condition fails, omit the suggested fix. The issue is complete without one.

8. **Check for an existing open issue covering this finding.** If no `GITHUB_TOKEN` is available (dry-run with an empty token, per step 2), skip this check and note in the output that duplicates were not checked. Otherwise run:

   ```
   scripts/with-env.sh gh issue list --state open --search "<3-5 word key phrase from the finding title>" --json number,title,url
   ```

   Do not filter by `--label bug`: step 10 may create issues without that label when it does not exist, so a label filter would silently miss prior `/bug-hunt` issues (and can error outright if the label is absent). If `gh` errors (auth, rate limit), warn and proceed as if no match — the duplicate check is best-effort, not a gate. If a match is found (same symptom, same code location), do not open a duplicate: drop this finding from the post list, record the existing issue URL and the reason it covers the finding, and continue to the next finding. A duplicate on one finding never aborts the others; only if *every* confirmed finding is a duplicate does the command post nothing (report the matched URLs).

9. **Build the issue body.** Build one body per confirmed, non-duplicate finding using the template in *Issue body template* below. Keep code quotes tight: 5–15 lines each, `file:line`-anchored.

10. **Post or preview.** Process the findings in ranked order — the top-ranked issue is filed first.

    In `--dry-run` mode: print every draft title and body to stdout, each separated by a distinctive `===== FINDING <n>/<total> =====` header line (not `---`, which already appears inside each body as a horizontal rule), and stop.

    Otherwise, derive `<repo>` once from:
    ```
    scripts/with-env.sh gh repo view --json nameWithOwner -q .nameWithOwner
    ```
    Then, for each confirmed, non-duplicate finding, post:
    ```
    scripts/with-env.sh gh issue create \
      --repo "<repo>" \
      --title "<title>" \
      --label "bug" \
      --body "$(cat <<'EOF'
    <body>
    EOF
    )"
    ```
    `gh issue create` prints the new issue's URL on success — capture that URL and the finding's title for the report in step 11. If `gh` returns `could not add label`, retry that issue once without `--label "bug"`. If a `gh issue create` fails for any other reason, warn, record the failure, and continue posting the remaining findings — one failed post does not abort the rest.

11. **Return the list of created-issue links.** End the run with a markdown bulleted list of every issue created this run, one link per line, so the user can open them directly:

    ```markdown
    ## Issues created

    - [<title>](<url>) — <severity>/<category>
    - [<title>](<url>) — <severity>/<category>
    ```

    Then add a one-line summary: how many findings were confirmed, how many issues were posted, and how many were skipped as duplicates or failed to post. If a finding was skipped as a duplicate, list it under a separate `## Skipped (duplicates)` heading with the existing issue link. If nothing was posted because every finding duplicated an existing issue, state that plainly in place of the created-issues list. (`--dry-run` never reaches this step — it prints and stops at step 10.)

## Issue body template

```markdown
## Summary

<one-line root-cause statement, ≤120 chars. If root cause was not identified, state "Proximate cause identified; root cause unidentified — see below.">

**Category:** <bugs | architecture | inconsistencies>
**Severity:** <high | medium | low>
**Found by:** `/bug-hunt` proactive scan

## Location

`<file>:<line>` — `<function or class name>`

## Description

<3–8 sentences. What is wrong, why it matters, and what conditions trigger it. Do not pad with obvious background; every sentence must carry information not in the summary line.>

## Evidence

```
-- <file>:<line_range> --
<tight code quote, 5–15 lines>
```

<Prose connecting the code to the defect. One paragraph.>

## Proximate cause

<The immediate defect at `file:line` — the specific expression or branch that is wrong.>

## Root cause

<The upstream reason the defect exists, OR explicit "Root cause unidentified — see Investigation pointer below.">

## Tests

- **Existing test coverage:** <yes (name the test) | partial (what is missing) | none>
- **Regression test needed:** <yes — describe what it should assert | no — existing test already catches it>

## Suggested fix

<Root-cause fix at `file:line`, OR omit this section entirely if step 7 conditions are not met.>

## Investigation pointer

<Only present if root cause is unidentified. One sentence naming the next concrete step to narrow it: which function to read, which log field to capture, which test to add first.>

## Cross-checks

- **CLAUDE.md invariant:** <which invariant this violates, or "none directly cited">
- **Recent commits at site:** <SHA list from `git log`, or "none in last 10 commits">
- **Related open issues:** <none, or URL if a partially-overlapping issue exists>

---
*Generated by `/bug-hunt` — proactive static scan, not a live failure.*
```

**Title format:** `[bug-hunt] <module>: <one-line symptom>`. Examples:
- `[bug-hunt] memory: archive() appends without file_lock — corruption risk`
- `[bug-hunt] agent: bare query() in contemplate() breaks session-attachment`
- `[bug-hunt] task_gates: gate_activations.jsonl append bypasses atomic_write`

Keep titles under 80 characters. No emoji. No iteration numbers.

## Forbidden suggestion patterns

Same rules as `/rca`. A suggested fix MUST NOT:

- Silence a non-transient error (`try/except: pass`, broad `except Exception:` that logs-and-continues, `# type: ignore` on a real type bug).
- Add auto-recovery to the loop (retries on non-transient errors, `restart: always` in `docker-compose.yml`).
- Apply a symptomatic patch (`increase timeout`, `raise the limit`, `bump the cap`) unless evidence proves the limit itself is the root cause.
- Bypass quality gates (`pytest.skip`, `# pragma: no cover`, lowering coverage thresholds).
- Remove the offending code without a replacement.

Allowed: fix the upstream defect at `file:line`; add an `assert` / type narrowing / explicit `raise` that converts a silent bad state into a louder failure; add a regression test (described, not written from this command); tighten an existing invariant when evidence shows it is the root cause.

## What to skip

- Findings already covered by an open GitHub issue — the duplicate check in step 8 catches these.
- Style issues, naming, or formatting — those belong in `/polish` or a lint pass.
- Test-coverage gaps alone (no untested code path without an accompanying defect) — surface as a `Regression test needed: yes` addendum to a real finding, not as a standalone issue.
- Speculation without a `file:line` anchor. Every candidate must have a concrete code location before it enters the scoring pool.
- Findings in `artifacts/`, `.git/`, generated files, or vendored dependencies.
- Anything in `maestro_review/` or `maestro_sdk/` unless `--area` explicitly names that package directory (`--area=maestro_review` or `--area=maestro_sdk`).

## Safety

- This command is **read-only against the repo and the filesystem except for one safe git-state change**: a `git switch main` and a fast-forward of local `main` to `origin/main` when the current branch is clean and fully merged — or, when already on a clean `main`, that fast-forward alone (see *Switch to main if on a merged branch*), plus the best-effort `git fetch origin main` that check uses. It must not edit source files, commit, push branches, or touch `data/`.
- The only writes are `gh issue create` calls — one per confirmed, non-duplicate finding (or none in `--dry-run`, or none when every finding duplicates an existing issue).
- Redact tokens, OAuth headers, API keys, and Telegram bot tokens (`bot\d+:[A-Za-z0-9_-]{30,}`) before they appear in the issue body.
- Do not invoke `/rca`, `/bug-fix`, `/impl`, or `/quality-gates` from inside this command. Analysis is fully inlined; implementation is the user's next step.
- If the hunt exhausts all candidates without a confirmed finding (all were false positives after deep-read), stop with `error=no_confirmed_finding` and print the ruled-out candidates with their false-positive reasons. Never post a speculative issue.
