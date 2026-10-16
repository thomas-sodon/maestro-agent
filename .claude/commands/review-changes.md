Review the changes on the current branch for the Maestro project.

`$ARGUMENTS` selects scope:
- *(empty)* — auto: review `all` when both uncommitted and committed-vs-`<base>` exist; otherwise whichever side has work; otherwise stop
- `uncommitted` — only the working-tree diff (staged + unstaged + untracked)
- `committed[:<base>]` — only commits on this branch that are not on `<base>`
- `all[:<base>]` — both uncommitted and committed since branching from `<base>`

`committed` and `all` scopes default to `main` as the base; pass `:<base>` (e.g. `committed:feat/parent`, `all:release/v2`) to override when reviewing a stacked or release branch. A malformed `:<base>` — empty after the colon, or containing whitespace — is a stop, not a silent fallback to `main`.

## Steps

1. **Pull plan context.** Before reading any diff, recover what this branch was *trying* to do so the rest of the review can judge whether it actually did it. Draw on two sources — the canonical plan file first (the authoritative design intent), then the session transcript (the original ask and what actually landed):

   **1a — Canonical plan file (primary).** Resolve the plan the same read-only way `/impl` and `/process-comments` do — folder pointer first, global most-recent as fallback:
   - **Folder pointer.** Read `~/.claude/last-plan-by-folder.json` (the shared map the plan-writing commands maintain, keyed by absolute working directory; see `.claude/commands/impl.md` *Plan pointer file*). Look up the entry for this working directory. If it exists and its `plan` file still exists and is non-empty, use it, announce `Plan context: <plan path> (written <written_at>)`, and record its absolute path as `plan_path` for the ledger.
   - **Fallback.** Otherwise pick the most recently modified non-empty `*.md` in `~/.claude/plans/`, warn `No folder plan pointer — using the most-recent plan globally; verify it matches this branch`, and record its path as `plan_path`.
   - **No plan file.** If `~/.claude/plans/` is empty or every file is size-zero, there is no plan file — `plan_path` is null; rely on 1b.
   - **Read it in full and sanity-check.** Extract the substance — what the change set out to do, the files and symbols it touches, the reuse map, the `Verification`/`Acceptance` criteria. If most paths it cites do not exist in the tree (it was written for a different branch or repo), warn `Plan looks unrelated to this branch — treating as low-confidence context`, set `plan_path` to null, and lean on 1b and the code instead. Never edit the plan file.

   **1b — Session transcript (supplement).** The Claude Code session that authored these changes is stored under `~/.claude/projects/<slug>/` where `<slug>` is the absolute working directory with `/` replaced by `-` (so `/home/operator/projects/maestro` → `-home-operator-projects-maestro`). Find the most recently modified non-empty `.jsonl` in that directory and extract: the **first user message** (the original request), any **`ExitPlanMode`** tool inputs (an interactive approved plan), and the **final `TodoWrite`** snapshot (what was actually completed). These files can be hundreds of KB, so parse with Python — do not Read the whole file.

   ```bash
   python3 - <<'PY'
   import json, os, glob
   slug = os.getcwd().replace('/', '-')
   session_dir = os.path.expanduser(f"~/.claude/projects/{slug}")
   files = sorted(
       (f for f in glob.glob(f"{session_dir}/*.jsonl") if os.path.getsize(f) > 0),
       key=os.path.getmtime, reverse=True,
   )
   if not files:
       print("NO_SESSION")
       raise SystemExit
   path = files[0]
   first_user, plans, todos = None, [], []
   with open(path) as f:
       for line in f:
           try:
               obj = json.loads(line)
           except Exception:
               continue
           if obj.get("type") == "user" and first_user is None:
               c = obj.get("message", {}).get("content")
               if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                   first_user = c
               elif isinstance(c, list):
                   for p in c:
                       if isinstance(p, dict) and p.get("type") == "text":
                           t = p.get("text", "").strip()
                           if t and not t.startswith("<"):
                               first_user = t
                               break
           if obj.get("type") == "assistant":
               for p in obj.get("message", {}).get("content", []) or []:
                   if isinstance(p, dict) and p.get("type") == "tool_use":
                       if p.get("name") == "ExitPlanMode":
                           plans.append(p.get("input", {}).get("plan", ""))
                       elif p.get("name") == "TodoWrite":
                           todos = p.get("input", {}).get("todos", [])
   print(f"=== Session: {os.path.basename(path)} ===")
   if first_user:
       print("\nORIGINAL REQUEST:\n" + first_user[:2000])
   for i, plan in enumerate(plans, 1):
       print(f"\nAPPROVED PLAN #{i}:\n{plan[:2000]}")
   if todos:
       print("\nFINAL TODO STATE:")
       for t in todos:
           print(f"  [{t.get('status', '?')}] {t.get('content', '')}")
   PY
   ```

   **Synthesize.** Build a 3–5 line **Plan Context** block from both sources to surface at the top of the review: the canonical plan file (1a) is the authoritative design intent; the transcript (1b) supplies the original request and what actually landed, and — for a branch authored via interactive plan mode with no plan file — its `ExitPlanMode` plan stands in as the design intent. When 1a and 1b disagree, prefer the plan file and note the divergence. If neither source yields anything (no plan file, and the transcript printed `NO_SESSION` or empty), say so in the block — do not invent intent. If the recovered context looks unrelated to the diff, note the mismatch, set `plan_path` to null (a real-but-unrelated plan — paths that exist but describe a different feature — must not reach the ledger as this diff's design intent), and proceed with diff-only context.

   Then recall this branch's **review-findings ledger** (see *Review-findings ledger* below) so this review can show which previously-raised findings have since been fixed, flag any that have regressed, and preserve each finding's history across sessions. A missing or foreign ledger simply means there is no prior review to reconcile against — start fresh.

2. **Determine scope.** First, parse `$ARGUMENTS` to find the requested scope — `uncommitted`, `committed[:<base>]`, `all[:<base>]`, or empty (auto) — *before* touching the base branch, so base resolution only runs for the scopes that actually consume a base.

   **If the requested scope is the explicit `uncommitted`**, no base is needed: skip base resolution, skip the `git rev-parse --verify <base>` validation, and skip `git rev-list`. Run only `git status --porcelain` (reused in step 3 to enumerate untracked files), then hand off to step 3 with `Scope: uncommitted`. This avoids a false hard-stop in a repo whose default branch is `master`, or in a worktree where `main` was never fetched.

   **Otherwise (`committed` / `all` / empty-auto)**, resolve and validate the base branch once per invocation:
   - If `$ARGUMENTS` supplied a `:<base>` suffix on `committed` or `all`, use that suffix verbatim as `<base>`. Otherwise default to `main`.
   - If the suffix is empty (e.g. `committed:`) or contains whitespace, stop with a one-line *malformed `:<base>`* error before the verify step — this is the malformed-argument case the header names, distinct from the missing-base error below.
   - Validate that `<base>` exists locally: `git rev-parse --verify <base> >/dev/null 2>&1`. If the verify fails, stop with a one-line error naming the missing base — do not silently fall back to `main`, since silent fallback is the bug class this command had before.

   Then run `git status --porcelain`, `git rev-parse --abbrev-ref HEAD`, and `git rev-list --left-right --count <base>...HEAD`. Each command's output feeds a later step:
   - `git status --porcelain` drives the uncommitted-vs-committed decision below and is reused in step 3 to enumerate untracked files.
   - `git rev-parse --abbrev-ref HEAD` identifies the current branch — needed for the on-`<base>` stop condition below.
   - `git rev-list --left-right --count <base>...HEAD` produces two tab-separated numbers; the **right count** (commits on HEAD not on `<base>`) seeds `N` in the `Scope: committed (N commits vs <base>)` header from the Output format section, and a non-zero right count is what "HEAD is ahead of `<base>`" means below.

   Then:
   - If $ARGUMENTS is `committed` or `all` (with or without a `:<base>` suffix), honour it.
   - If $ARGUMENTS is empty: if `git status --porcelain` has output **and** HEAD is ahead of `<base>` (right count of the `rev-list` above is non-zero), review `all` and note in the scope header that both uncommitted and committed work were present; else if `git status --porcelain` has output, review uncommitted; else if HEAD is ahead of `<base>`, review committed-vs-`<base>`; else stop and tell the user there is nothing to review.
   - If HEAD is `<base>` with no uncommitted changes, stop — there is no branch work to review.

3. **Gather the diff.**
   - Uncommitted scope: `git diff HEAD` (covers staged + unstaged) and `git status --porcelain` (covers untracked). Read each untracked file, but **bound the read** the way step 5 demands of the code it reviews — never pull an accidentally-untracked artifact into the review context unbounded: skip (or head-truncate to the first ~50 KB of) any untracked file larger than **50 KB**, and skip any untracked file under a known large-artifact path (`logs/`, `artifacts/`, `node_modules/`, `dist/`, `__pycache__/`, `.venv/`). Note each oversized-artifact skip in the review instead of reading the whole file into context.
   - Committed scope: `git diff <base>...HEAD` plus `git log <base>..HEAD --oneline` for commit narrative.
   - All scope: produce both diffs above and treat them as one unified review.

4. **Understand the change.** For every modified or added file, read the full file (not just the diff) to understand surrounding context. Trace how changed functions are called — use Grep to find callers, tests, and type definitions. Classify the change: bug fix, new feature, refactor, config change, test-only, or docs. Cross-check the diff against the **Plan Context** from step 1: are there scope items in the plan/todo that have no corresponding code change? Are there code changes that aren't motivated by anything in the plan?

5. **Check against Maestro's architecture.** The change must respect these constraints:
   - **Context bloat prevention**: Any data flowing into LLM prompts must be bounded. Identity lists capped at 10 items / 200 chars. Purpose hypothesis ≤ 500 chars. Evidence lists ≤ 10 items. `key_findings` ≤ 1000 chars. Prior step results: 200 chars/step, 2000 total. `result_summary` ≤ 500 chars. If the change adds new data to prompts, verify it has explicit size limits. This discipline binds the command's own inputs too: step 3's untracked-file read is size-bounded so an accidentally-untracked artifact cannot balloon the review context.
   - **File persistence**: JSON files must use `atomic_write()`. JSONL files must be append-only. Read-modify-write must use `file_lock()`.
   - **Self-model safety**: Changes to `consciousness.py` or the identity prompt are high-risk. Verify caps are preserved. `get_identity_prompt()` must stay under `MAX_IDENTITY_PROMPT_CHARS` (3000).
   - **Stage model integrity**: The 5-stage progression (awakening → purpose_found) must not be bypassed or short-circuited.
   - **Human-in-the-loop**: All self-modifications must require approval. No path should allow autonomous code changes without human review.
   - **Dashboard stays thin**: The dashboard must only write plan status and messages. No heavy computation in the dashboard process.

6. **Look for bugs and logic errors.** Focus on:
   - Off-by-one errors, missing edge cases, None/empty-collection handling
   - Race conditions in async code or file operations
   - Unbounded growth (lists, strings, files that could grow without limit)
   - Error handling: are exceptions caught at the right level? Are failures logged?
   - Type safety: does mypy's strict mode cover this, or are there `Any` escape hatches?
   - Security: command injection via subprocess, path traversal, token leakage in logs

7. **Evaluate test coverage.** Every new function, branch, and error path needs a test. Tests must:
   - Use `mem_fs` for all file I/O (never real filesystem)
   - Mock all externals (subprocess, network, sleep)
   - Test the actual logic, not just happy paths
   - If this is a bug fix, there should be a regression test that would have caught the bug

8. **Check for CLAUDE.md staleness.** If the change adds/removes modules, changes CLI commands, alters design decisions, or modifies architecture, CLAUDE.md must be updated to match.

9. **Check for committed leftovers.** Search the diff for: debug prints, commented-out code, `TODO`/`FIXME` without context, leftover `breakpoint()` / `pdb` calls, hard-coded paths, secrets, or `.env`-style values. For uncommitted scope, also flag any staged secret files.

10. **Verify quality gates would pass.** Confirm the change would pass:
    - `ruff check maestro/ tests/` — no lint errors
    - `mypy maestro/` — no type errors
    - `pytest tests/ -v` — all tests pass, 100% coverage
    - If touching `maestro_review/`: separate gates for that package too
    - If touching dashboard: browser tests must pass
    You may run them, but do not modify code as part of this command — the goal is review, not fix.

11. **Assess the "why".** Combine the **Plan Context** from step 1 with commit messages (committed scope) and diff inspection. Did the change actually solve the stated problem? Are there scope items the session set out to do that didn't land? Is there a simpler approach that was overlooked? If the plan and the diff disagree, that's a finding.

12. **Persist the review-findings ledger.** After the review is written, reconcile this run's findings against the branch ledger recalled in step 1 and write it back — see *Review-findings ledger* below. This runs on **every** review, clean or not: a clean review still flips previously-open findings to `fixed`. It is this command's only write and never edits the repo working tree. The persisted ledger — findings **plus** the plan context recovered in step 1 — is the durable bridge to the separate `/polish` session in step 13.

13. **Point the user at `/polish` in a separate session — do not run it here.** Reviewing and fixing are two sessions, not one: each stays focused, and the review-findings ledger (findings + plan context) carries the design intent across the session boundary, exactly as `/plan-proposal` hands a plan file to `/impl`. This command **never invokes the `polish` skill itself.** After writing the review (see Output format below) and persisting the ledger, end by pointing the user at the next session:
    - **Recommend `/polish`** if there is at least one **Bug**, **Concern**, or **Suggestion**: tell the user to run `/polish` in a fresh session — it will recall the ledger (findings + plan context) and apply the fixes, **Suggestions included** (every open ledger Issue is always fixed, so `--fix-suggestions` is not needed for findings this review recorded; that flag only governs fresh Suggestions discovered during the `/polish` run itself). Do not invoke it now.
    - **No `/polish` needed** if the review concluded the change looks clean (no findings at all).
    - **`/polish` cannot apply these** if the reviewed scope is `committed` only and the working tree has no uncommitted edits — `/polish` operates on uncommitted changes. Tell the user the findings are in already-merged commits and ask how they want to proceed (revert, follow-up branch, or fix-forward).

## Review-findings ledger

This command persists its findings to a durable, per-branch ledger so a later session — a re-run of this command, or `/polish` invoked tomorrow in a fresh session — can recall what was already raised and what has since been fixed. It is the review-side analog of the plan file `/impl` reads back. The ledger lives **outside the repo** at `~/.claude/reviews/<branch-slug>.json`, one file per branch, where `<branch-slug>` is the current branch (`git rev-parse --abbrev-ref HEAD`) sanitized into a filesystem-safe slug the same way the plan-writing commands sanitize names: lowercase, replace non-alphanumeric runs with `-`, trim leading/trailing `-`, collapse repeats (e.g. `feat/058-step10-foo` → `feat-058-step10-foo`). If HEAD is detached (no branch name), skip the ledger entirely and note it — there is no stable per-branch key.

Ledger shape (a single JSON object):

    {
      "repo": "<absolute working-directory path>",
      "branch": "<full branch name, e.g. feat/058-step10-foo>",
      "base": "<base the review used, or null for uncommitted scope>",
      "plan_context": "<the Plan Context synthesized in step 1, ≤800 chars — the design intent later sessions must respect; null when no session/plan was recovered>",
      "plan_path": "<absolute path of the ~/.claude/plans/ file the review leaned on, or null>",
      "updated_at": "2026-06-20T12:34:56Z",
      "findings": [
        {
          "id": "r1",
          "severity": "bug",          // bug | concern | suggestion
          "file": "maestro/agent.py",
          "line": 42,                  // best-effort; drifts, never the match key
          "title": "off-by-one in execution-group split",
          "detail": "<one or two lines: why it matters + the fix>",
          "status": "new",             // new | open | fixed
          "first_seen": "2026-06-20T12:00:00Z",
          "resolved_at": null          // ISO timestamp once status flips to fixed
        }
      ]
    }

### Recall (before the review)

Read `~/.claude/reviews/<branch-slug>.json`. Treat it as **absent** — start from an empty prior-findings set — when the file is missing, is malformed JSON (warn, do not overwrite blindly), or its `repo` does not equal this working directory or its `branch` does not equal the current branch (a sibling checkout reused the branch name, or two branches slugified to the same file — never read another tree's findings). When a valid ledger loads, carry its findings into *Persist & reconcile* below and surface their history in the review.

### Persist & reconcile (after the review is written)

After the review text is produced — on **every** run, including a fully-clean review with no new findings — reconcile this run's findings against the recalled ledger and write the merged result back. This step writes only the JSON ledger; it never edits the repo.

1. **Match by file + title, not line.** For each recalled finding still `new`/`open`, decide whether the *same* concern appears in this run's findings — match on `file` plus a semantic match of `title` (the same defect, even if it moved a few lines). Line numbers drift and are never the match key.
2. **Reconcile each finding:**
   - Recalled `new`/`open` finding **still present** this run → keep it, set `status` to `open` (it has now survived ≥1 review), preserve `first_seen`, refresh `line`/`detail`.
   - Recalled `new`/`open` finding **absent** this run → it was resolved since the last review: set `status` to `fixed`, `resolved_at` to now. Surface it in the review's *fixed since last review* line.
   - Recalled `fixed` finding that **reappears** this run → a regression: set `status` to `open`, clear `resolved_at`, and call it out as a regression in the review.
   - A this-run finding with **no** recalled match → append it with `status` `new`, `first_seen` now, and a fresh `id` (next `r<N>`).
   - Recalled `fixed` finding still absent → leave it `fixed` (kept as history).
3. **Carry the plan context forward.** Set `plan_context` to the Plan Context block synthesized in step 1 (≤800 chars; trim to fit), and `plan_path` to the canonical plan file recorded in step 1 — null when 1a resolved none, when 1a found it written for a different branch/repo, or when Synthesize found it unrelated to this diff (a same-repo wrong-feature plan must not be written as this diff's design intent). **If this run recovered no plan context** (no plan file, and step 1b printed `NO_SESSION` or the session looked unrelated), keep the prior ledger's `plan_context`/`plan_path` rather than clearing them — a later review that lost both sources must not erase the design intent the `/polish` session depends on.
4. **Write atomically, best-effort.** Set `updated_at` to the current ISO 8601 UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ` — no fractional seconds, literal `Z`), and set the object's identity/scope fields — `repo` (this working directory), `branch` (the current branch), and `base` (the base this review used, or null for uncommitted scope) — so a later recall's identity check and the `/polish` session both have them. Then create `~/.claude/reviews/` if missing, write to `~/.claude/reviews/<branch-slug>.json.tmp.<pid>`, then rename in place. If the write fails (permission denied, disk full, missing parent), warn and proceed — the ledger is an aid for later sessions, never a correctness requirement for this review.

## What to skip

- Style preferences or formatting — ruff handles this
- Type annotations on obvious cases — mypy handles this
- Minor naming preferences unless genuinely confusing
- Suggestions to make changes outside the diff — this is a review of *this* change, not a refactor of the surrounding code

## Output format

Write the review as plain text in the response. Start with a one-line scope header (`Scope: uncommitted` / `Scope: committed (N commits vs <base>)` / `Scope: all (N commits vs <base>)`), then a **Plan Context** block (3–5 lines) summarizing the original request, the implementation plan (the canonical plan file, or an interactive approved plan), and the final todo state recovered in step 1 — or a single line saying it was unavailable. Then a 2–3 sentence summary of the change and your overall assessment. Then list findings grouped by severity:

**Bugs** — Issues that will cause incorrect behavior. These block commit/merge.

**Concerns** — Design or safety issues that need discussion. May block commit/merge.

**Suggestions** — Nits and small improvements. Not blocking on their own, but bundled into the follow-up `/polish` session when other findings exist.

For each finding, include: the file and `path:line` reference, what the issue is, why it matters, and a concrete fix or question. Quote the relevant code.

When the branch has a ledger key (i.e. HEAD is not detached), add a one-line **Ledger** status after the findings: how many findings are new this run, how many carried over (still open from a prior review), how many were **fixed since last review**, and any **regressions** (a previously-fixed finding that reappeared). Omit the line only when the ledger was skipped (detached HEAD).

If the change looks good, say so clearly and briefly — don't manufacture issues.

End the review with one of:
- `Run /polish in a fresh session to apply fixes — it will recall the ledger (findings + plan context) and fix every recorded finding, Suggestions included.` — do **not** invoke `/polish` now; the follow-up session picks the work up from the ledger.
- `No fixes needed; no /polish required.` — only when the review is fully clean (no findings of any kind).
- `Findings are in committed scope with no uncommitted edits; /polish cannot apply them. <next-step question for the user>` — when the scope mismatch blocks the fix session.

## Safety

- Do not modify any files in the repo working tree — this command reviews only; fixes are delegated to a separate `/polish` session. Its sole write is the per-branch **review-findings ledger** under `~/.claude/reviews/` (outside the repo), written atomically and best-effort; see *Review-findings ledger*.
- Do not invoke the `polish` skill from this command. Reviewing and fixing are two sessions, bridged by the ledger — end by recommending `/polish`, never by running it.
- Read-only against `~/.claude/plans/`, the pointer file `~/.claude/last-plan-by-folder.json`, the plan, and the session transcript under `~/.claude/projects/`; never edit the plan file.
- Do not commit, push, stage, unstage, or discard changes.
- Do not run destructive git operations (`reset --hard`, `checkout --`, `clean -f`, branch deletes).
