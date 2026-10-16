Review the current uncommitted changes, fix any bugs found, and run the quality gates.

This command is the pre-commit polish workflow: review → fix bugs (and **all** open review-ledger Issues) → record any *fresh* unresolved findings back to the ledger → verify gates pass. It does not commit, push, or create a PR — use `/commit-push` or `/create-pr` for that after this completes cleanly.

## Flags

`$ARGUMENTS` may include any of these tokens (whitespace-separated, order-independent):

- `--fix-suggestions` — also apply **fresh** Suggestions discovered during this run (not already in the ledger), not just Bugs and Concerns. Pass it in the follow-up `/polish` session when you want fresh nits applied too — e.g. after a `/review-changes` that surfaced suggestions. Without this flag, fresh Suggestions are surfaced in the final report and — unless the user or `plan_context` explicitly declined them — appended to the ledger (see Step 11) but not fixed in this run — so a later `/polish` run fixes them as open ledger Issues. This flag governs only whether a *freshly-discovered* Suggestion is fixed in the same run it is found; it does **not** govern ledger findings: every open review-ledger Issue is always fixed regardless of its class or this flag (see Step 6).

Any other tokens in `$ARGUMENTS` are treated as free-form context (e.g. a findings list pasted in by `/review-changes`) and read for guidance.

## Steps

1. **Verify there is work to do.** Run `git status --porcelain`. If the output is empty, stop — there are no uncommitted changes to polish.

2. **Gather the diff.**
   - `git diff HEAD` — staged + unstaged together.
   - `git status --porcelain` to enumerate untracked files; read each in full.
   - `git rev-parse --abbrev-ref HEAD` for branch context.
   - Recall this branch's **review-findings ledger** (see *Review-findings ledger* below) so findings a prior `/review-changes` session raised are in scope even when this run was invoked without a findings list in `$ARGUMENTS`.

3. **Understand the change.** For every modified or added file, read the full file (not just the diff) so the surrounding context is in scope. Trace callers and tests via Grep where the change touches non-trivial logic. Classify each file: bug fix, new feature, refactor, config change, test-only, or docs.

4. **Check Maestro architecture invariants.** The change must respect these — flag any violation as a bug to fix:
   - **Context bloat caps** — identity lists (10 × 200 chars), purpose hypothesis (≤500), evidence lists (≤10), `key_findings` (≤1000 LLM-distilled, 150–500 per consumer), `result_summary` (≤500), prior step results (200/step, 2000 total). Any new prompt input must have a documented cap.
   - **File persistence** — JSON files use `atomic_write()`, JSONL files are append-only, read-modify-write uses `file_lock()`.
   - **Self-model safety** — changes to `consciousness.py` or the identity prompt are high-risk; `get_identity_prompt()` must stay under `MAX_IDENTITY_PROMPT_CHARS` (3000).
   - **Stage model integrity** — the 5-stage progression (awakening → purpose_found) must not be bypassed.
   - **Human-in-the-loop** — no path should allow autonomous code changes without human review.
   - **Dashboard stays thin** — only plan-status and message writes, no heavy computation.

5. **Look for bugs.** This is the fix-target phase. Be deliberate about what counts as a *bug* (incorrect behavior, leak, race, broken invariant, missing test for new behavior) versus a *suggestion* (taste, naming, cosmetic refactor). Bugs include:
   - Off-by-one, missing edge cases, None / empty-collection handling
   - Race conditions in async code or file operations
   - Unbounded growth (lists, strings, files)
   - Errors caught at the wrong level or swallowed silently
   - `Any` escape hatches that hide real type bugs
   - Subprocess command injection, path traversal, token leakage in logs
   - Leftover debug prints, `breakpoint()` / `pdb`, hard-coded paths, secrets, `.env`-style values
   - Test gaps: any new function, branch, or error path without a test
   - Tests doing real I/O, real network, real `sleep`, or missing `mem_fs`
   - Missing regression test for a bug fix already present in the diff
   - Tests that only exercise the happy path

6. **Decide before fixing.** Findings recalled from this branch's **review-findings ledger** are *always* fix targets: every open ledger Issue (status `new` or `open`) gets fixed in this run regardless of its Bug/Concern/Suggestion classification, and neither the `--fix-suggestions` flag nor the "Concerns only if contained" gate below applies to them. The classification below governs only findings discovered *fresh* in this run (not already present in the ledger):
   - **Bugs** — fix now in this command.
   - **Concerns** — design or safety issues; fix only if straightforward and contained, otherwise surface to the user and let them decide.
   - **Suggestions** — by default, note in the final report; do not fix. **If `$ARGUMENTS` contains `--fix-suggestions`, treat Suggestions as fixable in this run** and apply them with the same care as bugs (small, scoped edits; no refactor sprawl). Suggestions that would change observable behavior or expand scope still get surfaced to the user instead of applied silently.

   If a *fresh* bug fix is non-trivial or might change observable behavior the user did not intend, ask the user before applying it. **Ledger Issues are always fixed**: apply the fix and record any behavior-changing decision in the final report — only the hard stops in *Safety* (suspected secrets, the same gate failing three times) override this. Any *fresh* finding left unfixed at the end of the run — a deferred Bug, an un-fixed Concern, or a Suggestion — is appended to the ledger in Step 11 so a follow-up `/polish` run fixes it, **except a fresh finding the user reviewed and explicitly declined (or that `plan_context` declined on deliberate-design grounds), which is surfaced in the report but not appended** — appending it would relaunder the decline into an always-fixed open ledger Issue and reverse the user's decision on the next run.

7. **Apply fixes.**
   - Edit files directly with `Edit` / `Write`.
   - For each bug fix in production code, add or update a regression test that would have caught the bug. Tests must use `mem_fs`, mock all externals (subprocess, network, sleep), and never touch the real filesystem.
   - If the change adds or removes a module, alters a CLI command, or modifies a load-bearing design decision, update `CLAUDE.md` and `docs/modules.md` to match.
   - Keep the change set scoped — do not refactor unrelated code.

8. **Re-review the fixed state.** Run `git diff` again over the working tree. Confirm:
   - Every fix actually addresses the bug it was meant to fix.
   - No new bugs were introduced.
   - No leftover debug code or noise.

9. **Run quality gates.** Detect which packages were touched by the diff and run the matching gates:

   **Core package (run if `maestro/` or `tests/` were touched):**
   ```
   python3.14 -m ruff check maestro/ tests/
   python3.14 -m mypy maestro/
   python3.14 -m maestro.cli gate-lease -- python3.14 -m pytest tests/ -v {worker_args}
   ```

   **Classify a failed core-suite run.** If the parallel core-suite run above fails, do not blindly re-run it — pipe its combined output to `python3.14 -m maestro.cli gate-classify --tool tests --returncode <rc>` (pass the run's exit status as `<rc>`), which prints one classification token. `FAILED` / `COLLECTION_ERROR` are an *answered* red: the run already enumerated the failures, so fix them rather than re-running. `COVERAGE` / `TIMEOUT` / `UNAVAILABLE` are resource artifacts — an OOM-killed xdist worker's lost coverage tripping `--cov-fail-under=100`, or a runner that was killed or could not start — so re-run the suite once through `python3.14 -m maestro.cli gate-lease` and let that result decide, rather than "fixing" a coverage drop that only appears under contention. This mirrors `scripts/quality-gates.sh gate_maestro`, which the agent may also invoke directly instead of the raw command. The `python3.14 -m maestro.cli` module form (rather than a bare `maestro`) mirrors that script's `maestro()` shell function so the CLI self-resolves to *this* checkout — a stale `~/.local/bin/maestro` from a sibling checkout would otherwise load its own config models against this checkout's `maestro.toml` and raise a spurious pydantic `extra_forbidden`.

   **Review package (if `maestro_review/` or `tests/test_maestro_review/` were touched):**
   ```
   python3.14 -m ruff check maestro_review/ tests/test_maestro_review/
   python3.14 -m mypy maestro_review/
   python3.14 -m pytest tests/test_maestro_review/ -v --no-cov
   ```

   **Browser tests (if `maestro/dashboard/` or `tests/browser/` were touched):**
   ```
   python3.14 -m pytest tests/browser/ -v -m browser --no-cov --browser chromium
   ```

   If nothing was touched in a tier, skip its gates.

   **Run each of these in the foreground** with an explicit `Bash` `timeout` of `600000` (the 10-minute max) — never `run_in_background: true`. A backgrounded gate run is reaped when the turn yields, so the suite dies silently and the work stalls with nothing continued. Run the tiers as separate foreground calls so each fits the ceiling. If a run is ever backgrounded anyway, poll it with `BashOutput` until the shell exits before doing anything else, and never end your turn while a gate run is still pending.

10. **Fix gate failures.** For each failing gate, find the root cause and fix it. Do not silence errors with `# noqa`, `# type: ignore`, or pytest skips unless there is a real, narrow reason and the user is told. After each fix, re-run the failing gate plus any gates whose inputs changed. Repeat until every relevant gate passes. If the same gate fails three times in a row on the same root cause, stop and surface the situation to the user — do not keep guessing.

11. **Update the ledger, then report.** First, update this branch's **review-findings ledger** (see *Review-findings ledger* below): (a) mark each recalled finding this run actually resolved as `fixed`; (b) append every *fresh* finding this run discovered but did **not** resolve — an unfixed Bug, Concern, or Suggestion — as a new `new`-status entry so a follow-up `/polish` run fixes it, **except a fresh finding the user (or `plan_context`) explicitly declined, which is surfaced in the report but not appended** (see *Mark fixed & append fresh findings*, Pass 2). Leave recalled findings you deliberately did not fix unchanged. Then write it back and summarize plainly:
    - Files reviewed.
    - Whether `--fix-suggestions` was active.
    - Bugs fixed (one line each, with `path:line` reference).
    - Concerns surfaced but not fixed (with reason).
    - Ledger findings (any open Issue recalled from the ledger, including Suggestions): listed independent of `--fix-suggestions` — mark each as fixed when its fix landed, or as not-fixed with the reason when a *Safety* hard stop or a reverted fix prevented it (mirroring Step 6 and *Mark fixed & append fresh findings*).
    - Fresh Suggestions (discovered this run, not from the ledger): when `--fix-suggestions` was active, list each one as fixed/skipped (with reason for any skipped); otherwise list them as not fixed this run.
    - Fresh findings appended to the ledger: every unfixed fresh Bug, Concern, or Suggestion added for a follow-up run to fix, one line each — so the user knows what the next `/polish` run will pick up.
    - Quality-gate results — which gates ran, which passed, how many fix iterations were needed.
    - Any non-obvious decisions or trade-offs made during fixing.

## Review-findings ledger

`/review-changes` persists its findings to a durable, per-branch ledger at `~/.claude/reviews/<branch-slug>.json` (defined in `.claude/commands/review-changes.md` *Review-findings ledger*). This command recalls that ledger so the findings a prior `/review-changes` session raised stay in scope even when this run was invoked without a findings list in `$ARGUMENTS` (e.g. resumed in a fresh session), and marks each finding it resolves as `fixed` so the next `/review-changes` reconciliation sees it closed. It also **appends** the *fresh* findings it discovers but does not fix — using the same schema `/review-changes` writes — so the ledger accumulates unresolved work across sessions and a follow-up `/polish` run fixes it (see *Mark fixed & append fresh findings*).

### Recall (start of run)

Compute `<branch-slug>` from `git rev-parse --abbrev-ref HEAD` the same way `/review-changes` does — lowercase, replace non-alphanumeric runs with `-`, trim leading/trailing `-`, collapse repeats; a detached HEAD has no branch name, so skip the ledger entirely. Read `~/.claude/reviews/<branch-slug>.json`; treat it as **absent** when the file is missing, is malformed JSON (warn, do not overwrite blindly), or its `repo` does not equal this working directory or its `branch` does not equal the current branch — a sibling checkout that reused the branch name (or a slug collision) must never read another tree's findings, and this command applies fixes, so a wrong-tree read is the costlier mistake. A loaded ledger's `new`/`open` findings join the work list alongside any findings passed in `$ARGUMENTS` — union them, de-duplicated by `file` + `title`. **Every open ledger finding is a fix target for this run** (see Step 6): `/polish` always fixes all open Issues in the ledger, not only those re-supplied in `$ARGUMENTS`. The ledger is how this command recovers review intent across sessions: `/review-changes` runs in one session and writes it; `/polish` runs in the next and reads it.

A loaded ledger also carries `plan_context` (and `plan_path` if a `~/.claude/plans/` plan backed the review) — the design intent the originating `/review-changes` session captured. Keep every fix consistent with it, the same way `/process-comments` uses the implementation plan: it is the strongest tie-breaker for narrowing — and, for a *fresh* finding, declining — a fix that would contradict a deliberate design choice. It does **not** authorize declining an open ledger Issue: those are always fixed (see Step 6; only the *Safety* hard stops override), so for a ledger Issue `plan_context` narrows the fix to stay design-consistent rather than skipping it. If `plan_path` is present and the file still exists, you may read it in full for richer context.

### Mark fixed & append fresh findings (after fixes land and gates pass)

Once the fixes are applied and the relevant quality gates pass, update the ledger in two passes, then write it back once.

**1 — Mark recalled findings fixed.** For each recalled `new`/`open` finding this run actually resolved — its change is in the working tree and the concern no longer holds — set `status` to `fixed` and `resolved_at` to the current ISO 8601 UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ` — no fractional seconds, literal `Z`). Leave any recalled finding this run did not resolve — one blocked by a *Safety* hard stop (see Step 6) — as it was; do not mark a finding fixed if its fix was reverted or never landed.

**2 — Append fresh, unresolved findings.** For every finding this run discovered *fresh* (not recalled from the ledger) that it did **not** resolve — a Bug deferred to the user, a Concern left for the user to decide, or a Suggestion not fixed this run — append a new entry so a follow-up `/polish` run fixes it as an open ledger Issue (Step 6). Do **not** append a fresh finding this run fixed (it needs no future work), one that matches an existing ledger finding by `file` + `title` (it is already tracked), nor one the user reviewed and explicitly declined — or that `plan_context` declined on deliberate-design grounds. A declined finding appended as a `new` entry would become an always-fixed open ledger Issue (Step 6) that the next run applies unconditionally, reversing the user's decision; surface such a decline in the report (Step 11) instead of laundering it into the ledger. Each appended entry uses the `/review-changes` schema exactly: `id` the next free `r<N>` (max existing numeric suffix + 1, starting at `r1` when the ledger has no existing findings), `severity` (`bug` | `concern` | `suggestion`), `file`, best-effort `line`, `title`, a one-to-two-line `detail`, `status` `new`, `first_seen` the timestamp above, `resolved_at` `null`. A fresh Suggestion appended here becomes an open ledger Issue the next run always fixes — that is the intended cross-run behavior, so `--fix-suggestions` governs only whether a Suggestion is fixed in the same run it is discovered.

**Write back.** Refresh the ledger's `updated_at` to that timestamp and write it back atomically and best-effort — create `~/.claude/reviews/` if missing, write to `~/.claude/reviews/<branch-slug>.json.tmp.<pid>`, then rename in place; on write failure, warn and proceed. When Recall found **no** ledger file at all (genuinely missing — not a foreign or malformed one) and this run has fresh findings to append, create the object with `repo` set to this working directory, `branch` the current branch, `base` `null` (this command polishes uncommitted work), `plan_context`/`plan_path` `null`, and `findings` the appended entries — matching the identity fields `/review-changes` checks on recall so the next session accepts it. If Recall instead treated an **existing** file as absent because it was malformed or its `repo`/`branch` was foreign, do **not** overwrite it — warn and skip persisting the append; losing the append is cheaper than clobbering another tree's or a corrupt ledger. This per-branch ledger is the only file this command writes outside the repo working tree.

## What to skip

- Style or formatting — ruff handles this.
- Type annotation tightening that is not fixing a real type bug — mypy handles cosmetic gaps.
- Suggestions outside the diff — this command polishes *this* change, not the surrounding code.
- Renaming / restructuring for taste — out of scope.

## Safety

- Do not commit, push, stage, unstage, or discard changes.
- Do not run destructive git operations (`reset --hard`, `checkout --`, `clean -f`, branch deletes).
- The only file written outside the repo working tree is the per-branch **review-findings ledger** under `~/.claude/reviews/` (marking resolved findings `fixed` and appending fresh unresolved findings); see *Review-findings ledger*. It is written atomically and best-effort, never a correctness requirement for the fixes, and a malformed or foreign ledger is never overwritten blindly.
- Do not bypass a quality-gate failure with a workaround — fix the root cause.
- If a *fresh* fix would change observable behavior the user might not intend (a defaulted value, a dropped feature, a contract change), surface it before applying. Ledger Issues are still fixed (see Step 6) — apply the fix and record the behavior-changing decision in the report rather than skipping it.
- If the working tree contains files that look like secrets (`.env`, credentials, tokens), stop and tell the user — never edit, stage, or delete them.
- If the same gate fails three times on the same root cause, stop and ask the user rather than continuing to guess.
