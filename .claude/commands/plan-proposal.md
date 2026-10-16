Plan the implementation of proposal `$ARGUMENTS` for the Maestro project.

`$ARGUMENTS` is optional. When empty (or whitespace-only), auto-pick the next most important proposal from the **Recommended Implementation Order** in `docs/proposals/README.md` per the **Pick the next most important proposal** section below; otherwise it must be a positive integer naming a proposal.

The proposal at `docs/proposals/**/NNN-*.md` is your **first draft only** — internal scaffolding. Your job is to verify it against today's reality (the code for code-touching proposals, the evidence and capability claims for service offerings), then write a **fresh** plan file at `~/.claude/plans/proposal-NNN-<slug>.md` that **stands on its own**: the output plan must not mention the proposal, the draft, drift, or any prior version. It reads as if freshly authored. Beyond reconciling completed-step checkboxes and status back into the proposal where verification proves prior work has already landed (see *Reconcile completed steps into the proposal*), you leave the proposal untouched; you do not commit, do not enter plan mode.

The plan covers **only the next increment** — the smallest coherent, independently-mergeable chunk of work that moves the proposal forward as one focused PR — not the proposal end-to-end. If the proposal is large, plan just the first unbuilt slice; later increments are named as forward-looking boundaries in `## Out of scope`, never enumerated as steps.

## Switch to main if on a merged branch

Land on a current tree **first** — before resolving the proposal or running the picker. Both the picker (which reads `docs/proposals/README.md`) and verification (which reads the repo first-hand) depend on the checked-out tree, so a leftover task branch that has already merged into main would ground the plan in stale code. Switch to `main`, but only when it is provably safe:

1. Resolve the current branch: `git rev-parse --abbrev-ref HEAD`.
   - `main` → already on `main`, so no switch is needed — but still refresh so the picker and verification read the latest `main`, not a stale local one. On a clean tree (`git status --porcelain` empty), run the section's best-effort `scripts/with-env.sh git fetch origin main` and then `git merge --ff-only origin/main` to advance local `main` to the fetched tip before proceeding (the exit-0 refresh of step 4, minus the redundant `git switch main`; skip the fast-forward on the fetch-fallback path, where there is no fresher `origin/main` to advance to). If the tree is dirty, skip the refresh and proceed on the current `main`, leaving uncommitted changes untouched. If the fetch is unavailable (no `.env`/token, offline) or the `git merge --ff-only origin/main` fails (local `main` has diverged), warn that local `main` could not be advanced and proceed on the current local `main`.
   - `HEAD` (detached) → no branch to evaluate; warn and proceed without switching.
2. Dirty-tree guard. If `git status --porcelain` is non-empty, do **not** switch — uncommitted changes are left untouched. Warn that the switch was skipped because of local changes and proceed on the current branch.
3. Refresh main (best-effort): `scripts/with-env.sh git fetch origin main` (the wrapper loads `.env`; if it or the token is absent, skip the fetch). On any failure, fall back to the local `main` ref.
4. Fully-merged test. The branch is fully merged when every commit on it is already in main — `git merge-base --is-ancestor HEAD origin/main` (or `HEAD main` on the fetch fallback) exits 0.
   - Exit 0 → `git switch main`, then fast-forward local `main` to the fetched tip with `git merge --ff-only origin/main` so the picker and verification read the merged code, not a stale local `main` that the fetch never advanced (skip this fast-forward on the fetch-fallback path, where there is no fresher `origin/main` to advance to). On success, announce the switch (`Switched from <branch> (fully merged) to main`). If `git switch main` fails (e.g. `main` is already checked out in a sibling worktree, or local `main` is missing/invalid) or the `git merge --ff-only origin/main` fails (e.g. local `main` has diverged), warn that the switch could not complete and proceed on the current branch — exactly like the dirty-tree, fetch-failure, and unmerged-branch fallbacks above.
   - Non-zero → the branch carries unmerged commits; leave it, warn that the branch has unmerged work, and proceed on the current branch.

The switch never discards commits (it fires only when the branch adds nothing to main), never runs against a dirty tree, and is a plain checkout of `main` followed by a fast-forward to `origin/main` — no branch is created, no history rewritten.

## Resolve the proposal

1. If `$ARGUMENTS` is empty or whitespace-only, run **Pick the next most important proposal** below to obtain `NNN`, then continue with step 2. Otherwise, validate `$ARGUMENTS` is a positive integer; if not, stop with `error=invalid_argument` and an explanation of the expected form (`/plan-proposal 040`, or `/plan-proposal` with no argument to auto-pick).
2. Normalize `NNN` deterministically: strip any leading zeros, interpret the remainder as a decimal integer (never octal), then zero-pad to exactly 3 digits (`3` → `003`, `40` → `040`). An already-padded input like `040` is accepted and normalizes to `040`. If after stripping leading zeros the number has more than 3 digits (e.g. `1000`), stop with `error=invalid_argument`, because no proposal with a 4-or-more-digit number can exist.
3. Glob `docs/proposals/**/<NNN>-*.md` (recursive — proposals live in category subfolders: `core/`, `security/`, `publication/`, `audience/`, `income/`, `research/`, `ux/`, `technical-plan/`, `operations/`).
   - Zero matches → stop with `error=proposal_not_found`. Suggest the user check `docs/proposals/README.md` for available numbers.
   - Multiple matches → stop with `error=ambiguous_proposal_match` and list the matches.
4. Read the matched file in full. Extract:
   - The metadata table values (`Impact`, `Status`, `Affects`, `Depends on`, and `Derived from` if present — the last two are optional rows).
   - The slug from the filename (the part after `NNN-`, without `.md`).
   - The proposal title (the `# NNN: <Title>` heading).
   - The category folder (the parent directory: `core`, `security`, `publication`, `audience`, `income`, `research`, `ux`, `technical-plan`, or `operations`).
5. Determine the flavor. A proposal is **docs-only / service-flavored** if any of these hold:
   - The category is `income/`.
   - The `Affects` row reads `documentation only — no code surface yet` (or a close paraphrase).
   - The body uses the service-offering layout (`Capability basis`, `Target customers`, `Deliverable`, `Pricing`, `Differentiator`, `Risk assessment`) instead of `Relevant Findings` / `Implementation Plan` / `Testing Strategy`.

   Otherwise it is **code-touching** and the canonical verification path applies. The flavor decides which verification section runs below and which output-plan layout you write.

## Pick the next most important proposal

Run this section only when `$ARGUMENTS` was empty. It produces an `NNN` and then yields back to step 2 of **Resolve the proposal** (zero-pad, glob, read, parse) — the rest of the flow is identical.

1. Read `docs/proposals/README.md` and locate the `## Recommended Implementation Order` heading. Walk the body of that section (everything from the heading until the next top-level `##`), capturing every numbered list item that names a proposal as `**#NNN <Title>**`. Preserve textual order — items inside `### <Track name>` sub-headings (security hardening, artifact-review, visitor-awareness, income & self-sustaining-operations) are walked in the same outer order they appear in the file.
2. Read the `## Open Proposals` table from the same README to build a `{NNN → status}` map. Columns are `# | Title | Impact | Status` — the `#` cell is a markdown link of the form `[NNN](category/NNN-slug.md)`, so extract `NNN` from the link text rather than matching a bare `| NNN |` delimiter; valid status values are `proposed`, `partial`, `approved`, `in-progress`, `deferred`, `shipped`, `superseded`, and the three burn-in substates `burn-in:ready` / `burn-in:observing` / `burn-in:graduated`. A bare `burn-in` is **not** a valid token — every burn-in status carries a substate naming where the proposal sits in its rollout (see the README Status Key), and `tests/test_proposal_status_vocabulary.py` rejects the bare form. Proposals absent from this table have shipped (or never existed).
3. **Defensive parse failure.** If the README has no `## Recommended Implementation Order` heading, or that section parses to zero entries, stop with `error=recommended_order_missing` and surface the README path so the operator can repair the document before retrying.
4. For each `NNN` in textual order from the Recommended Implementation Order, apply skip rules and record the skip reason for the final summary if it does not match. Zero-pad each captured NNN to 3 digits (3 → 003, 40 → 040) before applying the skip rules below, using the same normalization as step 2 of *Resolve the proposal* (strip leading zeros, interpret as decimal, re-pad) so the two zero-pad paths stay in sync.
   - **Skip** if `NNN` is missing from the Open Proposals map → reason `shipped or removed`. (The README curator may not have pruned the order yet.)
   - **Skip** if the mapped status is anything other than `proposed` or `partial` → reason `status: <status>`. (`approved` / `in-progress` means an implementer is already on it; `burn-in` means all the behaviour-code has landed and only human-gated graduation/decommission steps remain, held for a separate review PR — not planning fodder; `deferred` means deliberately postponed.)
   - **Skip** if a plan file already exists at `~/.claude/plans/proposal-<NNN>-*.md` (glob match against the zero-padded NNN) → reason `plan already written: <filename>`. Mirrors `/backlog`'s anti-replan rule. Because plans are now increment-scoped, an existing plan file means *an* increment was planned, not that the whole proposal is done — so the auto-picker deliberately skips a multi-increment proposal once its first increment is written. Advancing such a proposal to its next slice is an explicit act: once the planned increment has landed and the proposal's status/notes reflect it, re-run `/plan-proposal NNN` (which overwrites the plan, re-deriving the next unbuilt increment from the updated proposal — re-running before the increment lands simply re-plans the same slice), or delete the stale plan file and re-run.
5. The first non-skipped entry is the **picked proposal**. Announce it before proceeding, e.g.:

       Auto-picked: 040 (Publish Dry-Run CLI) — first actionable item from Recommended Implementation Order.

   Then continue with step 2 of **Resolve the proposal** (zero-pad, glob, read, parse).
6. If iteration exhausts the order without finding one, stop with `error=no_actionable_proposal` and print the skip table (proposal number, title, skip reason) so the operator can see what was filtered. Suggest two remedies: pass an explicit number, or delete a stale plan file under `~/.claude/plans/proposal-*.md` and re-run.

## Verify the proposal against current code (code-touching flavor)

Run this section when the flavor is code-touching. Skip to the next section for docs-only / service-flavored proposals.

This step is internal: you read the current code first-hand so the plan you write is grounded in today's state of the tree. Findings flow into the body of the plan (correct paths, correct names, correct steps) — they do **not** appear as a "what the proposal said vs. what is true" comparison. The output plan is silent about the proposal. Do not delegate citation verification to subagents.

1. **`Affects` files.** For every path listed, read the full file. If a file no longer exists or was renamed, the plan uses the current path.
2. **`Relevant Findings` citations.** For every file+symbol citation (e.g., `maestro/agent.py` (`run_loop`), `maestro/vcs.py` (`create_pr`)), read the file and confirm the cited symbol or structure is still there. The plan cites by file path and current symbol name — **never bare line numbers**, which drift; strip any line-number suffix a citation still carries.
3. **Function and class names.** For every function or class cited (e.g., `_implement_approved_plans`, `create_pr`, `Publisher._stage`), grep for it. The plan refers to the current names — never to a renamed-away symbol.
4. **TOML config keys.** For every config key the proposal adds (e.g., `[publication] enabled`, `[artifacts] review_every`), grep `maestro.toml`, `maestro/config.py`, and `maestro/toml_config.py` to check whether the key already exists, partly exists, or conflicts with a sibling key. The plan accounts for what is already present.
5. **Dependencies.** For every prior proposal in `Depends on`, read its current `Status` row in `docs/proposals/README.md`. If a dependency is unshipped, the plan includes the prerequisite work or scopes itself to what is buildable today; if shipped, the plan only includes what remains.
6. **The proposal's own status.** If `partial`, read the README's note for the proposal (and any `What landed` / `What is missing` sections in the proposal body) so the plan covers only the **next unbuilt increment** of what remains — not the whole remainder — presented as the work to do, not as "what the proposal still needs."

## Verify the proposal against current capability (docs-only / service flavor)

Run this section when the flavor is docs-only / service-flavored. Skip if you ran the section above.

A service offering does not propose code edits, so there are no code citations to validate. Verify the offering claim against today's evidence trail and adjacent surfaces instead:

1. **Capability basis.** The proposal cites prior work that proves the offering is viable (a fix, an audit, a demonstrated capability). Read those artifacts — open the cited PRs, commits, or files. If they no longer exist or were rewritten, the plan reflects what the evidence actually shows now, not the proposal's framing.
2. **Adjacent surfaces.** If the offering implies a future code surface (intake form, pricing schema, dashboard tab, billing integration), grep for whether scaffolding already exists (`maestro/dashboard/`, `maestro/cli.py`, `maestro.toml`, route definitions). The plan acknowledges what is already there so it does not propose rebuilding it.
3. **Deliverable template.** If the proposal names a deliverable shape (e.g., audit report, written spec, paired failing/passing tests), check whether a template or prior example exists in the repo or research notes. The plan reuses what already exists.
4. **Dependencies.** For every prior proposal in `Depends on`, read its current `Status` row in `docs/proposals/README.md`. If a dependency is unshipped, the plan reflects what is buildable today.
5. **The proposal's own status.** If `partial`, follow the same partial-handling rule as the code-touching flow.

## Reconcile completed steps into the proposal

The first-hand verification above already read the `Affects` files and every cited symbol to ground the plan; that same reading reveals which of the proposal's steps have **already landed** in the tree — typically merged since the proposal was last touched. Before writing the plan, write those completions back into the proposal so its checkboxes and status match today's tree and the plan's "next increment" is derived from an accurate view of what remains. This is the **only** write this command makes into `docs/proposals/`, and it is bounded to the **status axis** — step checkboxes, the metadata `Status` field, the `What landed` / `What is missing` rows, and this proposal's single README row. Citations, paths, symbol names, dependency prose, motivation, and risk analysis are **left verbatim** here; those broader drift axes are `/update-proposals`' job, not this command's.

Skip this section for a docs-only / service-flavored proposal with no checkbox step list — a service offering has no Implementation-Plan steps to flip; if such a proposal's status has genuinely changed, note it in the closing message and leave the edit to `/update-proposals`.

1. **Re-derive per-step completion from the verification findings.** Structured proposals (per proposal 059) carry a `N. [ ]` / `N. [x]` marker per step. Using only what the first-hand verification already established — never a fresh guess — decide each step's state:
   - Work fully present and wired in → flip `N. [ ]` → `N. [x]`.
   - A step previously `[x]` whose work has **demonstrably been reverted** out of the tree → flip `N. [x]` → `N. [ ]`.
   - Cannot confirm either way → leave the marker as-is; do not flip on doubt.

   Flip the marker only — the step's title and prose stay **verbatim**.
2. **Re-derive the metadata `Status`**, scoped to this one proposal, with the same rule `/update-proposals` uses:
   - `proposed` with every described piece present and wired → `shipped`.
   - `proposed` with some pieces present but the core behaviour not wired in → `partial`; populate or update `What landed` / `What is missing`.
   - `partial` whose remaining slices have all landed → `shipped`.
   - `partial` where every step that writes new behaviour-code has landed and the **only** remaining `[ ]` steps are graduation/cut-over/collapse/decommission steps deferred behind a burn-in observation window → a `burn-in:<substate>` token, **not** `shipped`; leave those deferred `[ ]` steps `[ ]`. Pick which of `burn-in:ready` / `burn-in:observing` / `burn-in:graduated` by reading the entry criteria in the **Status Key** of `docs/proposals/README.md` (`### Status Key`) and matching the proposal's rollout position against them. Read them there — do not work from a paraphrase in this file: the Status Key is the single carrier of those criteria, and a second copy here would drift from it exactly the way the prose bullets drifted from the status column before the token was typed.
   - `burn-in:<substate>` whose deferred graduation/decommission steps have now themselves landed → `shipped`; while any remain, it keeps a burn-in token — re-picked against the Status Key's entry criteria on every reconciliation, not frozen at the substate it was first typed with, so a proposal that has moved along in its rollout since then carries the substate it now sits in.
   - `partial` or `burn-in` with slices that shifted between landed/missing → rewrite `What landed` / `What is missing`; keeps its current status.
   - Nothing landed since the proposal was last touched → no status change, no edit; continue to *Research*.

   The `Status` field, the `What landed` / `What is missing` rows, and the checkboxes must **agree**: a `partial` proposal has landed steps `[x]` and pending steps `[ ]`; a `burn-in` proposal has every behaviour-code step `[x]` and only its deferred graduation/decommission steps `[ ]`; a `shipped` proposal has every step `[x]`.
3. **Apply the edits** with the Edit tool, touching only the drifted markers and rows — table structure, step ordering, and all unchanged prose stay verbatim. On a flip to `shipped`, prepend one line at the top of `## Description`: `> Status updated to shipped on YYYY-MM-DD by /plan-proposal — see [README.md](../README.md#shipped) for the landing record.` (today's date from the environment context). On a flip to any `burn-in:<substate>`, ensure the burn-in banner is present at the top of `## Description` — `> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).` — adding it if absent and removing it on a later flip to `shipped`. Never delete the proposal file, even when fully shipped.
4. **Reconcile this proposal's one README row** in `docs/proposals/README.md`:
   - Update its `Status` column in the **Open Proposals** table.
   - On a flip to `shipped`, remove its row from **Open Proposals** and append a row to the **Shipped** table, filling `Landed as` from the verification (the modules, functions, config keys, or CLI commands that now carry the behaviour; add `(PR #NNNN)` when `git log -- <path>` surfaces a recent `Merge pull request #NNNN` for the central file).
   - On a flip to burn-in, keep its row in **Open Proposals** with its `Status` cell set to the *same* `burn-in:<substate>` token written into the doc metadata — never a bare `burn-in` — and add or update this proposal's one-line entry in the **"Burn-in — pending graduation & decommission"** callout beneath the table. That entry carries only the proposal's **remaining work and tripwires**: the rollout position is carried by the substate token alone, so do not restate it in the bullet. On a later flip to `shipped`, remove that callout entry as part of the same reconciliation.
   - Update any track-narrative sentence that names this proposal's just-changed status. Leave every other row, narrative, and the methodology / status-key / decision-tree / recommended-order sections untouched.
5. **If reconciliation flips the proposal fully to `shipped`**, no unbuilt increment remains: skip *Write the plan file*, announce that the work has fully landed and the proposal + README were reconciled to `shipped`, and stop. (The auto-picker would not have picked a shipped proposal; an explicit `/plan-proposal NNN` on an already-complete proposal ends here.)

The reconciliation stays inside `/update-proposals`' status-axis guarantees: marker-and-status only, drift flows one way from code into the proposal, code/tests/config are never edited to make a claim true, and no proposal file is ever deleted.

## Research what the proposal does not say

Use the **Explore** subagent to fill gaps the proposal does not cover. For code-touching proposals: usage patterns, adjacent tests, similar prior implementations, callers of changed functions. For docs-only / service offerings: prior intake forms, pricing artifacts, customer-facing copy, related dashboard surfaces, prior engagements or evidence tasks the offering can lean on. Single Explore call is the default; up to 3 in parallel only when the change is genuinely cross-cutting (e.g., touches the loop, the dashboard, and persistence in one change). Each agent gets the proposal title, a 2-paragraph summary of the work, and one specific question.

Explore is for **discovery only**. It is not a substitute for the first-hand verification in the previous section.

## Write the plan file

Every `~/.claude/...` path used as a **file-write target** in this section and in *Record the plan pointer* below — the plan file here and the pointer file there — must be resolved to an absolute path **before** it reaches any file API: expand the leading `~` via `$HOME/...` in a shell context or `os.path.expanduser("~")` in a Python write (the same idiom `.claude/commands/review-changes.md` uses for its `~/.claude/...` paths). The leading tilde is a display convention only; it is **not** expanded by the Write tool or by `os.rename`/`open`, so a literal `~/.claude/...` argument either fails outright or silently writes under a `~` directory inside the current working directory. This rule applies only to file-API arguments — the user-facing `Wrote plan: ~/.claude/plans/...` message below is printed text and stays as written.

1. Compute the output path: `~/.claude/plans/proposal-<NNN>-<slug>.md` (resolved to an absolute path per the rule above before any write). Overwrite if it already exists.
2. Write the plan with the layout that matches the proposal's flavor (see below). In either case, the output must read as a freshly authored plan: no mention of the proposal, no "the proposal said", no `Source:` line, no drift section, no comparisons to a prior version. Anyone reading the plan in isolation should not be able to tell it was derived from a proposal.

   ### Code-touching plan layout

   1. **`# Plan: <Title>`** — heading. Use the proposal's title text as the human-readable title; do not include the proposal number.
   2. **`## Context`** — *fresh* prose explaining why this change is being made, what problem it solves, and the intended outcome. Do not copy the proposal's `Description`. Do not cite the proposal.
   3. **`## Implementation steps`** — a fresh numbered list scoped to **only the next increment**: the smallest coherent chunk of work that lands as one focused PR and leaves the proposal meaningfully advanced. Do not enumerate the full end-to-end implementation; if the proposal is large, plan just the first unbuilt slice and defer the rest to `## Out of scope`. Each step has a short title, the file(s) it touches and the symbol(s) within them, and a 1–3 sentence description of the change. Reuse existing patterns surfaced during Explore. Order steps so partial progress is safe within the one PR — an interrupted run leaves the codebase in a working state, each step behind its own flag where possible.
   4. **`## Critical files`** — bulleted list of files to be modified, with their current paths.
   5. **`## Reused functions and utilities`** — bulleted list of existing functions or classes to call instead of reimplementing. Each line: `path — symbol — one-line purpose`.
   6. **`## Verification`** — concrete steps to confirm the change works end-to-end. Default to the project's standard gates:
      - `python3.14 -m ruff check maestro/ tests/`
      - `python3.14 -m mypy maestro/`
      - `python3.14 -m pytest tests/ -v`

      Add scenario-specific manual verification when relevant (e.g., "invoke `maestro publish-dry-run <task_id>` and confirm exit code 2 on a known secret"). If the change touches the dashboard, include the browser test command.
   7. **`## Out of scope`** — bullets listing related work this plan does **not** cover, framed as forward-looking boundaries (e.g., "Multi-proposal chaining is out of scope"), not as deferrals from a draft. Because the plan covers only the next increment, name the later increments here as future work, phrased as forward-looking boundaries (not as "the rest of the proposal").

   ### Docs-only / service-offering plan layout

   Use this layout when the flavor is docs-only / service-flavored. The standard quality gates and file-level steps do not apply — the work is offering definition, not code editing.

   1. **`# Plan: <Title>`** — heading. Same rule as above.
   2. **`## Context`** — fresh prose explaining what the offering is, why it is viable now (grounded in what the capability check confirmed), and what the end state looks like once the offering can take its first paying engagement.
   3. **`## Launch steps`** — a fresh numbered list scoped to **only the next increment**: the smallest coherent chunk of launch work that moves the offering forward (e.g., the first unbuilt artifact), not the full path to first engagement. Defer later increments to `## Out of scope`. Draw from the offering's typical launch work — define intake scope and engagement boundaries, decide pricing rules, draft customer-facing description, draft the deliverable template (e.g., audit report skeleton), define success criteria for the first engagement, decide where intake lives (email, form, dashboard tab) without building it — but include only the steps that belong to this increment. Each step has a short title, the artifact(s) it produces (file paths if docs-bound), and a 1–3 sentence description.
   4. **`## Reusable artifacts`** — bulleted list of existing assets to reuse: prior audit reports, evidence tasks, dashboard surfaces, intake templates, research notes, similar service docs. Each line: `path or pointer — what it is — how this offering reuses it`.
   5. **`## Verification`** — how the operator confirms this increment's launch work is complete (not that the whole offering is ready for first engagement — later increments cover that). Quality gates do not apply here; instead list manual checks scoped to this increment (e.g., "intake scope rule documented in `docs/services/<slug>.md`", "pricing rule reviewed against capability evidence", "deliverable template exists"). If the proposal will eventually grow a code surface, name the future check that will prove that surface, but do not run it now.
   6. **`## Out of scope`** — bullets listing related work this plan does **not** cover, framed as forward-looking boundaries. Because the plan covers only the next increment, name the later launch increments here as future work. For docs-only proposals, explicitly call out that no code surface (intake routes, pricing schema, dashboard tab, billing) lands in this plan; a follow-on proposal will introduce it.

3. **Record the plan pointer** for this working directory per *Record the plan pointer* below, so a later `/impl` with no argument picks this plan rather than the global most-recent plan.

4. After writing, print the plan path as the final user-facing message:

       Wrote plan: ~/.claude/plans/proposal-NNN-<slug>.md

   If the proposal was auto-picked (the picker section ran), prepend a one-line `Auto-picked: NNN (<Title>)` line so the operator sees how the proposal was chosen. If the proposal status is `partial` or any dependency is unshipped, append a one-line warning to the message — both annotations may apply at once. If *Reconcile completed steps into the proposal* wrote back to the proposal (checkboxes flipped and/or status promoted), append a one-line `Reconciled NNN — <what changed>` note so the operator knows the working-tree diff under `docs/proposals/` is expected.

## Record the plan pointer

After the plan file is written, record it as this folder's most-recent plan so `/impl` invoked with no argument picks it for this working directory instead of the global most-recently-modified plan in `~/.claude/plans/`. The pointer lives in a shared JSON file at `~/.claude/last-plan-by-folder.json`, keyed by absolute working directory:

    {
      "<absolute path of this command's working directory>": {
        "plan": "<absolute path of the plan file just written>",
        "written_at": "2026-05-24T12:34:56Z"
      }
    }

Resolve the pointer path's leading `~` to an absolute path before any write, per the tilde-expansion rule at the top of *Write the plan file* — the tilde is a display convention only and must never reach `os.rename`/`open` unexpanded.

Use atomic read-modify-write: read the file (missing → treat as `{}`; malformed JSON → warn, treat as `{}`, do not overwrite silently), set the entry for the current working directory's absolute path to the just-written plan's absolute path plus the current ISO 8601 UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ` — no fractional seconds, literal `Z`), serialize the full object, write to `~/.claude/last-plan-by-folder.json.tmp.<pid>`, then rename in place. Preserve unrelated keys untouched. This is best-effort: if the write fails (permission denied, disk full, missing parent), warn and proceed — the pointer is an optimization for `/impl`'s auto-pick, never a correctness requirement.

## What to skip

- Do not rewrite the proposal's prose, findings, citations, paths, symbol names, or dependency framing. The one sanctioned write to a proposal file is the bounded status-axis reconciliation in *Reconcile completed steps into the proposal* — completed-step checkbox flips and the `Status` / `What landed` / `What is missing` rows; everything else is left verbatim, and broader drift is `/update-proposals`' job.
- Do not modify `docs/proposals/README.md` beyond the single reconciled proposal's status row (and, on a flip to `shipped`, moving that one row to the **Shipped** table) per *Reconcile completed steps into the proposal*. Leave every other row — and the methodology, status key, decision tree, and recommended-order sections — untouched; the rest of the table is curated by hand.
- Do not modify `docs/proposals/TEMPLATE.md`. It is the canonical skeleton for new proposals, not an output target.
- Do not commit, stage, or push.
- Do not run quality gates as part of this command — they belong in the plan's `Verification` section, executed by whoever implements the plan. Docs-only plans omit the standard gates entirely.
- Do not invoke `EnterPlanMode`. The command writes a static plan file and exits; whether to enter plan mode after reading the plan is the user's call.
- Picker is read-only. Do not modify `docs/proposals/README.md` while parsing the Recommended Implementation Order or the Open Proposals table.
- Picker does not delete or overwrite files in `~/.claude/plans/`. The "plan already written" skip rule is a glob existence check; resolution of stale plan files is the operator's call.
- Picker does not call any GitHub API. Skip rules are local-filesystem only — open-PR detection and merged-but-unrecorded status detection are out of scope; if the curator has not pruned a shipped item from the Open Proposals table yet, the picker may pick it and the verification phase will produce a coherent plan against today's tree.

## Safety

- Read-only against the repo except for safe git-state changes: a `git switch main` and a fast-forward of local `main` to `origin/main` when the current branch is clean and fully merged — or, when already on a clean `main`, that fast-forward alone (see *Switch to main if on a merged branch*), plus the best-effort `git fetch origin main` that check uses. Writes go to `~/.claude/plans/...`, the per-folder pointer file `~/.claude/last-plan-by-folder.json`, and — bounded to the status axis — the single planned proposal's file and its one README row (completed-step checkbox flips and status per *Reconcile completed steps into the proposal*).
- Never delete a proposal file, and never rewrite its prose, findings, or citations. The sole sanctioned edit is the bounded status-axis reconciliation (completed-step checkboxes and status rows); code, tests, and config are never edited to make a proposal's claim true. If the proposal is severely out of date beyond that axis, the plan you write reflects today's reality (not the proposal's stale claims) and you flag the situation in the closing message to the user.
- Do not run destructive git operations.
