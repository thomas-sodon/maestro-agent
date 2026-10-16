Roll proposal `$ARGUMENTS` one burn-in substate forward for the Maestro project — from `burn-in:ready` to `burn-in:observing`, opening its observation window by deploying the production canary.

`$ARGUMENTS` is `<NNN>`: a proposal number naming a proposal at `docs/proposals/**/NNN-*.md` that sits in `burn-in:ready`. A burn-in proposal's rollout position is carried by one typed token in two places that must agree — the `| **Status** |` metadata row of the proposal doc and the status cell of its row in the `## Open Proposals` table of `docs/proposals/README.md` (bound together by `tests/test_proposal_status_vocabulary.py`). This command moves that token from `burn-in:ready` to `burn-in:observing` and edits both places together so they cannot fall out of step.

The advance deploys the production canary. The observation window cannot open without the flag being live in production, so this advance *performs* the flip rather than waiting for it. It writes the whole canary set together (see *Deploy the canary*): the `maestro.production.toml` enable line, the `PROD_ONLY_CANARIES` declaration, the window's criteria subsection in `docs/self-evolution.md`, a pin test, and the proposal's own status carriers. Base defaults are never touched, so reverting is still just deleting the canary line. The window starts when the resulting PR merges to production, not when this command runs.

The later `burn-in:observing` → `burn-in:graduated` token move is not this command's: the graduation PR planned by `/graduate-proposal` (its mechanism (g) edit) carries both status carriers to `burn-in:graduated` alongside the reviewed cut-over, and `/update-proposals` re-derives the substate on every reconciliation.

Recognized tokens:
- `--dry-run` — report the verdict and the edits that *would* be made (the full canary diff); write nothing and change no git state (the merged-branch switch and the on-`main` refresh below are both skipped).
- `--flag <config_field>` — repeatable: names which of the proposal's flags this canary turns on, by `Config` field name. Use it when the proposal stages its flags (e.g. "the deadline flag is the one to flip first") or names several and the first stage cannot be read unambiguously. Without it, the command derives the first-stage flag set from the proposal (see *Deploy the canary*).

## Argument parse

1. The first positional token must name a single proposal number. Normalize it deterministically, reusing `/plan-proposal`'s rule: validate it is a positive integer; strip any leading zeros; interpret the remainder as a decimal integer (never octal); then zero-pad to exactly 3 digits (`3` → `003`, `40` → `040`). If after stripping leading zeros the number has more than 3 digits (e.g. `1000`), stop with `error=invalid_argument` — no proposal with a 4-or-more-digit number can exist.
2. If no number is given (empty or whitespace-only argument), stop with `error=missing_proposal` and an example invocation (`/rollout 118`).
3. A second positional token naming `graduated` or `burn-in:graduated` stops with `error=invalid_argument` and points to `/graduate-proposal NNN`, whose graduation PR carries that token. Any other extra positional token (including `observing`, which is implied) stops with `error=invalid_argument`, naming the value; never coerce it.
4. Recognize the `--dry-run` flag token and the repeatable `--flag <config_field>` option. `--flag` with no value following it stops with `error=invalid_argument`. An unrecognized `--`-prefixed token is never swallowed — stop with `error=invalid_argument`, naming the offending token and the recognized forms.

## Switch to main if on a merged branch

Land on a current tree **first** — before resolving the proposal or checking its evidence. The entry-criterion gate reads `maestro.production.toml` and the flag manifests first-hand, so a leftover task branch that has already merged into main, or a local `main` trailing `origin/main`, would give a wrong verdict. Skip this entire section under `--dry-run`, which changes no git state (print the switch and refresh it *would* have run, and proceed on the current branch). Otherwise, first require a clean tree: the advance writes config and code, so if `git status --porcelain` is non-empty, stop with `error=dirty_tree` before any switch or edit. A canary diff mixed into unrelated uncommitted work could not be reviewed or reverted on its own. Then switch to `main`, but only when it is provably safe:

1. Resolve the current branch: `git rev-parse --abbrev-ref HEAD`.
   - `main` → already on `main`, so no switch is needed — but still refresh so the gate reads the latest `main`, not a stale local one. Run the section's best-effort `scripts/with-env.sh git fetch origin main` and then `git merge --ff-only origin/main` to advance local `main` to the fetched tip before checking evidence (the exit-0 refresh of step 3, minus the redundant `git switch main`; skip the fast-forward on the fetch-fallback path, where there is no fresher `origin/main` to advance to). If the fetch is unavailable (no `.env`/token, offline) or the `git merge --ff-only origin/main` fails (local `main` has diverged), warn that local `main` could not be advanced and proceed on the current local `main`.
   - `HEAD` (detached) → no branch to evaluate; warn and proceed without switching.
2. Refresh main (best-effort): `scripts/with-env.sh git fetch origin main` (the wrapper loads `.env`; if it or the token is absent, skip the fetch). On any failure, fall back to the local `main` ref.
3. Fully-merged test. The branch is fully merged when every commit on it is already in main — `git merge-base --is-ancestor HEAD origin/main` (or `HEAD main` on the fetch fallback) exits 0.
   - Exit 0 → `git switch main`, then fast-forward local `main` to the fetched tip with `git merge --ff-only origin/main` so the gate reads the merged code, not a stale local `main` that the fetch never advanced (skip this fast-forward on the fetch-fallback path). On success, announce the switch (`Switched from <branch> (fully merged) to main`). If `git switch main` fails (e.g. `main` is already checked out in a sibling worktree, or local `main` is missing/invalid) or the `git merge --ff-only origin/main` fails (e.g. local `main` has diverged), warn that the switch could not complete and proceed on the current branch — exactly like the fetch-failure and unmerged-branch fallbacks above.
   - Non-zero → the branch carries unmerged commits; leave it, warn that the branch has unmerged work, and proceed on the current branch.

The switch never discards commits (it fires only when the branch adds nothing to main), never runs against a dirty tree, and is a plain checkout of `main` followed by a fast-forward to `origin/main` — no branch is created, no history rewritten.

## Resolve the proposal

1. Glob `docs/proposals/**/<NNN>-*.md` (recursive — proposals live in category subfolders).
   - Zero matches → stop with `error=proposal_not_found`; suggest the user check `docs/proposals/README.md` for available numbers.
   - Multiple matches → stop with `error=ambiguous_proposal_match` and list the matches.
2. Read the matched doc's `| **Status** |` metadata token, and the status cell of the `[NNN]` row in the `## Open Proposals` table of `docs/proposals/README.md`.
3. If the two disagree, stop with `error=status_mismatch` before any edit, quoting both values. Do not pick a side — recommend `/update-proposals NNN` to re-derive the status from the code, and let the human run it.

## Transition table

Only one move is allowed:

- `burn-in:ready` → `burn-in:observing`

Refuse everything else with a named error, editing nothing:

- `error=not_in_burn_in` — the current status does not start with `burn-in:` (e.g. `proposed`, `partial`, `shipped`). Recommend `/plan-proposal NNN` (behaviour-code still to build) or `/update-proposals NNN` (status drifted from the code); **never invoke** either — the human runs them.
- `error=not_ready` — the current status is any `burn-in:` token other than `burn-in:ready`. For `burn-in:observing` the canary is already past this command — recommend `/graduate-proposal NNN`; for `burn-in:graduated` or an off-vocabulary `burn-in:` token, recommend `/update-proposals NNN`. **Never invoke** either.

## Gate on the Status Key

Read the substate entry criteria from the `### Status Key` of `docs/proposals/README.md` at run time. Do not restate them here, so this command cannot drift from them. Confirm the relevant criterion first-hand against the tree, never on the proposal doc's own say-so. Identify the proposal's flag(s) from its burn-in bullet in `docs/proposals/README.md`, its doc, or its manifest entry in `maestro/flag_lifecycle.py`, and ground the evidence the criterion names. For example, grep `maestro.production.toml` for a canary section enabling the flag, or read the flag's `Config` default in `maestro/config.py` and its manifest stage in `maestro/flag_lifecycle.py`. The `maestro audit-flags` read surface is the existing view of burn-in horizons; point the operator at it rather than reimplementing horizon logic.

This advance *creates* the `burn-in:observing` criterion's evidence, so the gate checks first-hand that the Status Key's *current* `burn-in:ready` entry still holds for each picked flag. If a production line already enables a flag, the canary was deployed by hand. Stop with `error=canary_already_deployed`, name the line, and recommend `/update-proposals NNN` to reconcile the stale token, rather than writing a second declaration. The deploy preconditions in *Deploy the canary* are the rest of this gate.

If the criterion has a disjunct whose evidence lives outside the tree (a measurement or observation result rather than a config default, manifest stage, or production canary), this command does not advance on it — it grounds only tree-resident evidence. When only such a disjunct could be satisfied, stop with `error=entry_criterion_unmet`, name the ungroundable disjunct, and recommend `/update-proposals NNN`, where the human can reconcile the status against that evidence.

If the criterion is not met, stop with `error=entry_criterion_unmet`: quote the criterion verbatim from the Status Key and name the evidence that is missing (e.g. "no `maestro.production.toml` section enables `<flag>`"). If the evidence is ambiguous (the proposal names no flag, or its flag cannot be located), stop with the same error and say what could not be grounded — never advance on a guess.

## Deploy the canary

This section grounds the canary and defines its edit set. Nothing is written here: the set is written in *Apply the change*, after the baseline, so a refusal at any step edits nothing.

### Pick the flags

1. With `--flag`, take exactly the named `Config` fields. A named field the proposal does not own (absent from its doc, its burn-in bullet, and its manifest `note`) stops with `error=invalid_argument`.
2. Without `--flag`, derive the **first-stage** set from the proposal's burn-in bullet and doc. When the proposal stages its flags (one to flip first, another in a later window or behind a human go/no-go), take only the first stage. When it names a single flag, take that one. If the first stage cannot be read unambiguously, stop with `error=ambiguous_canary_flags`, list the candidate flags and what the proposal says about each, and suggest re-running with `--flag`. Never flip a later-stage flag early. A flag held for a later window is deliberately dark.

### Ground each flag

For each picked flag, confirm first-hand:

- its `Config` field in `maestro/config.py` defaults to `False`;
- its key in `maestro.toml` is `false`, and which `[section]` it lives in. Record the TOML key separately from the `Config` field name — the two diverge for several flags (e.g. `[mcp] context7_enabled` ↔ `context7_mcp_enabled`, `[sdk] single_use_evolution_calls_enabled` ↔ `sdk_single_use_evolution_calls_enabled`). The manifest takes the field name; the overlay takes the key;
- it has an `EXPERIMENTAL_FLAGS` entry in `maestro/flag_lifecycle.py` (its base default-off horizon), and it is not already in `PROD_ONLY_CANARIES`, `PROD_ONLY_OPERATIONAL`, `PROD_ONLY_DISABLED`, or `CUTOVER_FLAGS`.

Any failure stops with `error=flag_not_deployable`, naming the flag and the precondition that failed.

### Ground the window

Read, from the proposal doc and its burn-in bullet, what the observation window is: its minimum length, the tripwires or falsifiers that halt graduation, the read surface they are measured from, and the rollback. These become the criteria subsection, so they must come from the proposal. Never invent a threshold or a window length. If the proposal states no window length or no tripwire, stop with `error=window_criteria_missing` and recommend `/update-proposals NNN`, so the human writes the criteria before production is flipped.

Also ground a **justification**: the observability reason this evidence accrues only in production (e.g. the phase that exercises the flag only runs where a `PROD_ONLY_OPERATIONAL` switch is on, or the signal folds over live host/PR state). `find_unjustified_canaries` reds the build on a post-baseline canary without one. If no production-only reason can be grounded, stop with `error=canary_unjustified`. The flag belongs dark-launched until a reason exists, not in production on a guess.

### The canary set

Use the most recent `PROD_ONLY_CANARIES` entry, its `maestro.production.toml` block, and its `docs/self-evolution.md` subsection as templates, and match their shape, voice and density. The set, written together:

1. **`maestro.production.toml`** — under the flag's `[section]` (appending the section if the overlay lacks it), after that section's last canary block: a comment block naming the proposal and what flipping the flag changes in `MAESTRO_ENV=production`; that base `maestro.toml` keeps the default-off byte-identical behaviour; a pointer to the new criteria subsection of `docs/self-evolution.md`; that the flag is declared as an `EnvOverlayCanary` entry in `maestro/flag_lifecycle.py` (`PROD_ONLY_CANARIES`); and a closing `Reversion: delete this line.` (or `these N lines.`, counted exactly). Then one `<toml_key> = true` line per flag.
2. **`maestro/flag_lifecycle.py`** — append one `EnvOverlayCanary` per flag to the end of `PROD_ONLY_CANARIES`: `flag=` the `Config` field name, `env="production"`, a `review_by=` horizon that falls after the window's earliest possible end (follow the sibling canaries' convention), a `note=` naming the proposal, the TOML key, the criteria subsection, the deploy-at-production-merge window and "Also in EXPERIMENTAL_FLAGS on the base default-off axis (distinct concern).", and the grounded `justification=`. Add the flag to the manifest's header comment listing the canaries that also appear in `EXPERIMENTAL_FLAGS`. Never add it to `PROD_ONLY_CANARY_BASELINE`; that set is a frozen grandfather list.
3. **`maestro.toml`** — tag the flag's base key line with the `flag_lifecycle.overlay_tag` rendering (`# [production: true — canary, proposal NNN]`), prefixed to the line's existing comment. Leave the value unchanged: the tag is how a reader of base defaults learns production runs the flag on, and `tests/test_base_toml_overlay_tags.py` reds if it is missing.
4. **`docs/self-evolution.md`** — a new `### <Title> canary (proposal NNN)` subsection next to the sibling canary subsections: a paragraph on the failure the flag addresses and what the canary turns on where; then the operator-checkable criteria as bullets (the observation window counted from the PR's production merge, each tripwire/falsifier, and the burn-in read surface including `maestro audit-flags`); then the closing paragraph saying graduation is a separate human-reviewed PR and reverting is deleting the canary line.
5. **`tests/test_config_flag_lifecycle.py`** — a `test_<slug>_canary_declared` pin modelled on the existing per-canary pins: the flag resolves `True` under `build_config_via_pydantic(..., env_name="production")` and `False` in base, and its `PROD_ONLY_CANARIES` entry exists with `env == "production"`, a future `review_by`, a non-empty `justification`, and no baseline membership.
6. **The proposal doc** — the `| **Status** |` token, and the `| **What is missing** |` row: restate the window step as running via the `maestro.production.toml` canary, with a pointer to the new subsection.
7. **`docs/proposals/README.md`** — the `[NNN]` row's Open Proposals status cell. Also update the proposal's burn-in bullet: its claim that no canary line enables the flag becomes false with this edit. Replace that lead with where the canary lives (the overlay line, its `PROD_ONLY_CANARIES` declaration, the subsection), following the sibling `observing` bullets. Keep the bullet's remaining work and tripwires.

If a picked flag is only part of the proposal's rollout, the proposal still moves to `burn-in:observing`: its first window is running. The later flags stay dark, and the bullet and "What is missing" row name them as the next stage.

## Baseline the vocabulary lint

Before any edit (and under `--dry-run` too), run `python3.14 -m pytest tests/test_proposal_status_vocabulary.py -q --no-cov`. If it is already red, stop with `error=vocabulary_lint_red_baseline`, quoting the failure — a pre-existing failure must never be blamed on this command's edit.

Also baseline the guards the canary set must satisfy: `python3.14 -m pytest tests/test_config_flag_lifecycle.py tests/test_base_toml_overlay_tags.py tests/test_overlay_safety.py tests/component/test_production_posture_regression.py -q --no-cov`. If any is already red, stop with `error=canary_guards_red_baseline`, quoting the failure.

## Apply the change

Under `--dry-run`, print the verdict and every edit below as a before/after diff, and write nothing.

Write the full canary set from *Deploy the canary*, which includes the two status carriers — the proposal doc's `| **Status** |` token and the `[NNN]` row's cell in the `## Open Proposals` table of `docs/proposals/README.md` — edited together. Nothing outside that set is edited.

## Verify

Run `python3.14 -m pytest tests/test_proposal_status_vocabulary.py -q --no-cov`. If it fails, stop with `error=vocabulary_lint_failed`, quoting the failure, after reverting: revert every canary edit, since the set lands whole or not at all.

Then run the canary guards baselined above (`tests/test_config_flag_lifecycle.py`, `tests/test_base_toml_overlay_tags.py`, `tests/test_overlay_safety.py`, `tests/component/test_production_posture_regression.py`). If any fails, revert every canary edit and stop with `error=canary_guards_failed`, quoting the failure. The canary set lands whole or not at all. Then run the first Quality Gates line from `CLAUDE.md` (ruff, mypy, full pytest with coverage). Flipping a production default can change the behaviour a component test asserts (proposal 139's deploy had to adapt `tests/component/conftest.py`). A red there is real follow-up work, not a failed edit: keep the edits, stop with `error=gates_red_after_deploy`, quote the failures, and recommend `/polish`.

On success, report the proposal, the transition applied, the evidence that satisfied the gate, the flags turned on (and any later-stage flags left dark), the `review_by` chosen, and that the window opens at the PR's production merge. Recommend `/create-pr`.

## Safety

- Read-only against the repo except the safe `git switch main` and the fast-forward of local `main` to `origin/main` (plus the best-effort `git fetch origin main`) described in *Switch to main if on a merged branch*, and the edits in *Apply the change*.
- The command **never commits, stages, pushes, opens a PR, or files an issue** — the edits are left in the working tree for human review, and the canary goes live only when that reviewed PR merges.
- The command **never edits** base default *values* (`maestro/config.py`, `maestro.toml`), an existing manifest entry (`EXPERIMENTAL_FLAGS`, `CUTOVER_FLAGS`, `PROD_ONLY_CANARY_BASELINE`), or `CLAUDE.md`; its only config writes are appending the canary to `maestro.production.toml` and `PROD_ONLY_CANARIES`, and the `[production: …]` tag comment on the flag's `maestro.toml` key line — the only `maestro.toml` write.
- The command touches only a `burn-in:ready` proposal: it never removes a canary line, never writes `burn-in:graduated`, and never moves a proposal to `shipped` or out of the Open Proposals table — the flag cut-over and the `burn-in:graduated` token belong to `/graduate-proposal` → `/impl`, and `shipped` to `/update-proposals`.
- Never invoke `/plan-proposal`, `/update-proposals`, `/graduate-proposal`, or any sibling command; refusals *recommend* them and the human runs them.
- Do not run destructive git operations (`reset --hard`, `checkout --`, `clean -f`, branch deletes, force-push). Reverting a failed canary set restores only the files this run edited, from the content read before the first write.
