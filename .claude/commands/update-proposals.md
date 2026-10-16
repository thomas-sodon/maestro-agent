Update proposal documents `$ARGUMENTS` for the Maestro project to reflect today's tree, and reconcile `docs/proposals/README.md` with what the verification surfaces.

`$ARGUMENTS` selects scope:
- *(empty)* — every open proposal under `docs/proposals/<category>/`
- a positive integer (e.g. `030`) — that single proposal
- a category name (`core`, `security`, `publication`, `audience`, `income`, `research`, `ux`, `technical-plan`, `operations`) — every proposal in that subfolder

You modify proposal files and the README index in place. You do not edit code, tests, or config to make a proposal's claim match the codebase — drift flows one way, from code into the proposal. You do not commit, stage, push, run quality gates, or enter plan mode.

## Switch to main if on a merged branch

Land on a current tree **first** — before verifying any proposal. Verification reads the repo first-hand and mutates proposal files in place, so a leftover task branch that has already merged into `main`, or a local `main` trailing `origin/main`, would verify against stale code — it could flip a proposal to `shipped`, mark steps `[x]`, or rewrite a citation to match a symbol that no longer reflects `main`. Switch to `main`, but only when it is provably safe:

1. Resolve the current branch: `git rev-parse --abbrev-ref HEAD`.
   - `main` → already on `main`, so no switch is needed — but still refresh so verification reads the latest `main`, not a stale local one. On a clean tree (`git status --porcelain` empty), run the section's best-effort `scripts/with-env.sh git fetch origin main` and then `git merge --ff-only origin/main` to advance local `main` to the fetched tip before proceeding (the exit-0 refresh of step 4, minus the redundant `git switch main`; skip the fast-forward on the fetch-fallback path, where there is no fresher `origin/main` to advance to). If the tree is dirty, skip the refresh and proceed on the current `main`, leaving uncommitted changes untouched. If the fetch is unavailable (no `.env`/token, offline) or the `git merge --ff-only origin/main` fails (local `main` has diverged), warn that local `main` could not be advanced and proceed on the current local `main`.
   - `HEAD` (detached) → no branch to evaluate; warn and proceed without switching.
2. Dirty-tree guard. If `git status --porcelain` is non-empty, do **not** switch — uncommitted changes are left untouched. Warn that the switch was skipped because of local changes and proceed on the current branch.
3. Refresh main (best-effort): `scripts/with-env.sh git fetch origin main` (the wrapper loads `.env`; if it or the token is absent, skip the fetch). On any failure, fall back to the local `main` ref.
4. Fully-merged test. The branch is fully merged when every commit on it is already in main — `git merge-base --is-ancestor HEAD origin/main` (or `HEAD main` on the fetch fallback) exits 0.
   - Exit 0 → `git switch main`, then fast-forward local `main` to the fetched tip with `git merge --ff-only origin/main` so verification reads the merged code, not a stale local `main` that the fetch never advanced (skip this fast-forward on the fetch-fallback path, where there is no fresher `origin/main` to advance to). On success, announce the switch (`Switched from <branch> (fully merged) to main`). If `git switch main` fails (e.g. `main` is already checked out in a sibling worktree, or local `main` is missing/invalid) or the `git merge --ff-only origin/main` fails (e.g. local `main` has diverged), warn that the switch could not complete and proceed on the current branch — exactly like the dirty-tree, fetch-failure, and unmerged-branch fallbacks above.
   - Non-zero → the branch carries unmerged commits; leave it, warn that the branch has unmerged work, and proceed on the current branch.

The switch never discards commits (it fires only when the branch adds nothing to main), never runs against a dirty tree, and is a plain checkout of `main` followed by a fast-forward to `origin/main` — no branch is created, no history rewritten.

## Resolve scope

1. If `$ARGUMENTS` is a positive integer, zero-pad to 3 digits and glob `docs/proposals/**/<NNN>-*.md`. Zero matches → stop with `error=proposal_not_found`. Multiple matches → stop with `error=ambiguous_proposal_match` and list the candidates.
2. If `$ARGUMENTS` matches one of the nine category names, use every `NNN-*.md` in that subfolder.
3. If `$ARGUMENTS` is empty, use every `NNN-*.md` under all nine `docs/proposals/<category>/` subfolders.
4. Anything else → stop with `error=invalid_argument` and explain the expected forms.

## What counts as drift

Each in-scope proposal is verified along five axes. Edit only what is drifted; leave still-correct prose, motivation, and risk analysis untouched. The proposal's argument and voice belong to the human author — your job is to keep its facts load-bearing.

1. **Path drift.** A path in the metadata `Affects` row, or in a file+symbol citation, no longer exists or has been renamed. Use Grep on the filename or on a unique structure inside it (class name, distinctive helper) to find the new location, and rewrite the path. If the path is genuinely gone with no successor, mark the line with an inline `<!-- TODO: original path no longer exists -->` rather than silently deleting — the human reading the diff decides whether the finding still applies.

2. **Line-number citations.** Proposals cite code by file path and symbol, **never by bare line number** (line numbers drift as code changes). If a citation carries a `:NN` or `:NN-NN` suffix (e.g. `maestro/agent.py:2540-2564`), strip the suffix, keep the path, and ensure the symbol it points at is named alongside it — read the file, confirm the symbol is still present, and rewrite the citation as `maestro/agent.py` (`<symbol>`).

3. **Symbol drift.** A function, class, helper, or TOML key referenced by name has been renamed. Grep both old and new names; update to the current name. If only the old name exists *and* its location matches what the proposal describes, leave it (the rename is what's missing from the codebase, not the proposal).

4. **Status drift.** Re-derive status from the code:
   - Read every file in `Affects`. Confirm the structures the proposal says it will add/change (dataclasses, helpers, config keys, phases, CLI commands).
   - Grep for each named symbol and TOML key. Confirm whether it is present, partly present, or absent.
   - For a `proposed` proposal whose described work is fully present and called from the loop / a CLI / a test, status flips to `shipped`.
   - For a `proposed` proposal where some named pieces are present but the core behaviour is not wired in, status flips to `partial` and you populate or update `What landed` / `What is missing` rows in the metadata table.
   - For a `partial` proposal whose remaining slices have all landed, status flips to `shipped`.
   - For a `partial` proposal where every Implementation-Plan step that writes new behaviour-code has landed and the **only** remaining `[ ]` steps are graduation/cut-over/collapse/decommission steps — each deferred behind a burn-in observation window (their prose reads "after … proven in production", "await the graduation PR", "observation gate before …", or they merely flip a default to its shipped posture / delete the retired logic the new path superseded) — status flips to a `burn-in:<substate>` token, **not** `shipped`. Pick which of `burn-in:ready` / `burn-in:observing` / `burn-in:graduated` by reading the entry criteria in the **Status Key** of `docs/proposals/README.md` (`### Status Key`) and matching the proposal's rollout position against them. Read them there — do not work from a paraphrase in this file: the Status Key is the single carrier of those criteria, and a second copy here would drift from it exactly the way the prose bullets drifted from the status column before the token was typed. A bare `burn-in` is not a valid token and `tests/test_proposal_status_vocabulary.py` rejects it. Do not force those deferred `[ ]` steps to `[x]`; they land later, through a separate human-reviewed graduation PR.
   - For a `burn-in` proposal whose deferred graduation/decommission steps have now themselves landed (the flag is graduated and the retired logic is gone), status flips to `shipped`. While any of those steps is still pending, it keeps a `burn-in:<substate>` token — re-picked against the Status Key's entry criteria on every reconciliation, not frozen at the substate it was first typed with, so a proposal that has moved along in its rollout since then carries the substate it now sits in. `/rollout NNN` is the narrower tool that opens the observation window for a `burn-in:ready` proposal by deploying its production canary.
   - For a `partial` or `burn-in` proposal where some slices have shifted between landed/missing buckets, rewrite the `What landed` / `What is missing` rows accordingly.
   - **Reconcile the Implementation-Plan step checkboxes.** Proposals that use the structured step list (`N. [ ]` / `N. [x]`, per proposal 059) carry per-step status markers that are part of status drift, not prose. For every step whose described work is now present in the tree, flip its marker `N. [ ]` → `N. [x]`; flip `[x]` → `[ ]` only when a step previously marked done has demonstrably been reverted out of the tree. Change the marker only — leave the step's prose verbatim. The metadata `Status` field, the `What landed` / `What is missing` rows, and these checkboxes must agree: a `partial` proposal has its landed steps `[x]` and its pending steps `[ ]`; a `burn-in` proposal has every behaviour-code step `[x]` and only its deferred graduation/decommission steps `[ ]`; a `shipped` proposal has every step `[x]`. This matters because `/impl NNN` selects the next unit of work by walking to the first `[ ]` step, so a stale `[ ]` on already-shipped work makes it try to re-implement completed work — and, for a `burn-in` proposal, `/impl` deliberately refuses those deferred `[ ]` steps (they are human-gated on the burn-in window), so mislabelling one `partial` would let it try to fire a graduation or deletion early.

5. **Dependency drift.** For each entry in the `Depends on` row, read the dependency's current `Status` in the README. If a dependency has shipped, soften numbered steps in the proposal's `Implementation Plan` section that literally name the prerequisite (e.g. "after the prerequisite lands" → "build on the existing `<symbol>`"), and update or remove the `Depends on` row. This is the only sanctioned prose rewrite in this command — it is bounded to numbered `Implementation Plan` steps, never `Description`, `Relevant Findings`, `Testing Strategy`, motivation, end-state vision, or risk analysis (those remain governed by the framing in **What counts as drift** and the first entry of **What to skip**). If a dependency was renumbered or moved category, update the cross-reference.

## Per-proposal workflow

Process proposals one at a time in the main agent so file edits never race. Within a single proposal, you may dispatch one **Explore** subagent to do read-heavy discovery in parallel (greps, file reads across the `Affects` set) — pass the proposal title, the `Affects` list, and the specific question (e.g. "for each file+symbol citation, confirm the symbol still exists at that path or report missing"). Explore is for discovery; final verification is yours.

For each in-scope proposal:

1. **Read the proposal in full.** Capture the metadata table, `Description`, `Relevant Findings`, `Implementation Plan`, `Testing Strategy`, and any `What landed` / `What is missing` rows.
2. **Verify `Affects`.** For each path: confirm it exists; if not, locate the successor and record the rewrite.
3. **Verify citations.** For each `path/to/file.py` (`<symbol>`) citation in `Relevant Findings` and `Implementation Plan`: read the file and confirm the symbol is still present. If any citation carries a bare `:NN`/`:NN-NN` line-number suffix, strip it and name the symbol instead (see drift bucket 2).
4. **Verify symbols.** For every named function, class, helper, dataclass, TOML key, and CLI subcommand in the proposal body: grep current code; record renames or deletions.
5. **Verify status.** Apply the re-derivation rules in drift bucket 4 above. Record the proposed status transition (`proposed → partial`, `partial → shipped`, `proposed → shipped`, or no change), and — for a step-structured proposal — which Implementation-Plan checkboxes flip in either direction: `N. [ ]` → `N. [x]` for a step whose work has landed, or `N. [x]` → `N. [ ]` for a step previously marked done that has demonstrably been reverted out of the tree (drift bucket 4).
6. **Verify dependencies.** Read each `Depends on` proposal's current status from the README. Record any soft-step rewrites or row changes.
7. **Apply edits.** Rewrite only the drifted spans using the Edit tool. Preserve unchanged prose verbatim. Section headings, table structure, bullet ordering, and code-block formatting stay as they are. Flip Implementation-Plan step checkboxes for status drift (drift bucket 4) — `[ ]` → `[x]` for work that has landed, and `[x]` → `[ ]` for a step demonstrably reverted out of the tree — marker only, the step's prose left verbatim. If the status flipped to `shipped`, prepend a single line at the top of the `## Description` section: `> Status updated to shipped on YYYY-MM-DD by /update-proposals — see [README.md](../README.md#shipped) for the landing record.` (use today's date from the environment context). If the status flipped to any `burn-in:<substate>`, ensure the burn-in banner is present at the top of `## Description` — `> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).` — adding it if absent and leaving it verbatim if already there; when a proposal later flips from a burn-in substate to `shipped`, remove that banner as part of the same edit. Do not delete the proposal file even if shipped — file deletion is a separate human decision.

## README reconciliation

After every in-scope proposal is processed, update `docs/proposals/README.md` once:

1. For every proposal whose status changed:
   - Update the `Status` column in the **Open Proposals** table.
   - If the new status is `shipped`, remove the row from **Open Proposals** and append a row to the **Shipped** table. Populate `Landed as` from the verification: name the modules, functions, config keys, or CLI commands that now carry the behaviour. If `git log -- <path>` surfaces a recent merging PR for the central file (`Merge pull request #NNNN`), include it as `(PR #NNNN)`. If the proposal's link path also moved during verification, the row's `Title` link uses the new path — but a shipped row in the Shipped table doesn't link to the proposal file.
   - If the new status is a burn-in one, keep the row in **Open Proposals** with its `Status` cell set to the *same* `burn-in:<substate>` token written into the doc metadata — never a bare `burn-in` — and add or update the proposal's one-line entry in the **"Burn-in — pending graduation & decommission"** callout beneath the table. That entry names only what **remains** — the deferred flag graduation and/or the retired-logic decommission — plus any tripwire or dated constraint; the rollout position is carried by the substate token alone, so do not restate it in the bullet. When a proposal later flips to `shipped`, remove its callout entry as part of the same reconciliation.
2. For every proposal whose category folder or filename slug changed during verification, update the link path in the **Open Proposals** table.
3. For every proposal whose `Impact` rating changed (rare — only when the verification surfaced a clear reason such as a dependency shipping that downgrades the remaining work), update the `Impact` column.
4. For every track-narrative paragraph that names a proposal whose status just changed (e.g. "030 is partial" → "030 has shipped"), update the inline cross-reference. Leave narrative paragraphs alone otherwise.
5. Do not touch the methodology, status key, authoring instructions, decision tree, or recommended-order sections unless their content directly references a status that just changed.

## Output

After the run, print a summary:

- `NNN: <title> — <buckets fixed>` for each edited proposal (e.g. `030: Artifact Persistence Foundation — line, symbol`).
- `NNN: <title> — clean` for each checked proposal with no drift.
- `NNN: <title> — <old> → <new>` for each status promotion, listed separately under a `Status changes:` heading.
- A final line: `Reviewed N proposals; edited M; promoted P statuses.`

If a proposal could not be verified (file genuinely deleted with no successor, dependency in a contradictory state, status check inconclusive because the code is half-migrated), report it as a warning line at the end (`! NNN: <title> — <reason>`) and skip the edit. The human resolves it manually.

## What to skip

- Do not rewrite still-correct prose. The motivation, end-state vision, risk analysis, and testing strategy belong to the human author.
- Do not add new findings, implementation steps, or risks the original author did not write. Drift fixes are bounded to facts already in the document.
- Do not delete proposal files. Status promotion routes through the README only.
- Do not modify `docs/proposals/TEMPLATE.md`.
- Do not edit code, tests, or config to make a proposal's claim true.
- Do not commit, stage, push, or run quality gates. The deliverable is a working-tree diff the human reviews.
- Do not invoke `EnterPlanMode`.

## Safety

- Read-only against everything except `docs/proposals/**/*.md` and `docs/proposals/README.md`, **plus one safe git-state change**: a `git switch main` and a fast-forward of local `main` to `origin/main` when the current branch is clean and fully merged — or, when already on a clean `main`, that fast-forward alone (see *Switch to main if on a merged branch*), plus the best-effort `git fetch origin main` that check uses.
- Never relocate a proposal between category folders as part of this command — category is a human-curated decision (see the decision tree in `docs/proposals/README.md`). A category mismatch is a warning, not an edit.
- Never delete a proposal file in this command.
- Do not run destructive git operations.
