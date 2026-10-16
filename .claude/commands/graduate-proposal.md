Plan the human-gated graduation of proposal `$ARGUMENTS` for the Maestro project.

`$ARGUMENTS` is a single proposal number naming a proposal at `docs/proposals/**/NNN-*.md` whose behaviour-code has already landed and is burning in behind an opt-in flag. This command is the mirror-complement of `/plan-proposal`: where that one refuses burn-in proposals ("all the behaviour-code has landed and only human-gated graduation/decommission steps remain … not planning fodder"), this one plans exactly that graduation. It grounds the proposal's flag(s) and their current lifecycle stage first-hand, then writes a self-contained graduation plan to `~/.claude/plans/graduate-NNN-<slug>.md` — enumerating every mechanism the next graduation increment must change — and records it as this folder's most-recent plan so a later `/impl` picks it up.

The command is **read-mostly**: it plans, it does not perform the edits. It never edits config, the flag manifests, the proposal docs, or `CLAUDE.md`, and never commits, stages, pushes, opens a PR, or files an issue — those edits are `/impl`'s job under human review. Graduation stays human-in-the-loop. The graduation plan's mechanism (g) edit carries both status carriers to the increment's target status (`burn-in:graduated`, or `shipped` for the final decommission) in the same reviewed change.

Recognized tokens:
- `--dry-run` — print the plan body to stdout; write nothing (no plan file, no pointer update) and change no git state (the merged-branch switch and the on-`main` refresh below are both skipped).

## Argument parse

1. `$ARGUMENTS` must name a single proposal number. Normalize it deterministically, reusing `/plan-proposal`'s rule: validate it is a positive integer; strip any leading zeros; interpret the remainder as a decimal integer (never octal); then zero-pad to exactly 3 digits (`3` → `003`, `40` → `040`). An already-padded input like `040` normalizes to `040`. If after stripping leading zeros the number has more than 3 digits (e.g. `1000`), stop with `error=invalid_argument` — no proposal with a 4-or-more-digit number can exist.
2. If no number is given (empty or whitespace-only argument), stop with `error=missing_proposal` and an example invocation (`/graduate-proposal 122`).
3. Recognize the `--dry-run` flag token (print the plan body, write nothing, change no git state). An unrecognized `--`-prefixed token is never swallowed — stop with `error=invalid_argument`, naming the offending token and the recognized forms.

## Switch to main if on a merged branch

Land on a current tree **first** — before resolving the proposal or grounding its flags. Grounding reads the repo first-hand, so a leftover task branch that has already merged into main would ground the plan in stale code. Skip this entire section under `--dry-run`, which changes no git state (print the switch and refresh it *would* have run, and proceed on the current branch). Otherwise switch to `main`, but only when it is provably safe:

1. Resolve the current branch: `git rev-parse --abbrev-ref HEAD`.
   - `main` → already on `main`, so no switch is needed — but still refresh so grounding reads the latest `main`, not a stale local one. On a clean tree (`git status --porcelain` empty), run the section's best-effort `scripts/with-env.sh git fetch origin main` and then `git merge --ff-only origin/main` to advance local `main` to the fetched tip before grounding (the exit-0 refresh of step 4, minus the redundant `git switch main`; skip the fast-forward on the fetch-fallback path, where there is no fresher `origin/main` to advance to). If the tree is dirty, skip the refresh and proceed on the current `main`, leaving uncommitted changes untouched. If the fetch is unavailable (no `.env`/token, offline) or the `git merge --ff-only origin/main` fails (local `main` has diverged), warn that local `main` could not be advanced and proceed on the current local `main`.
   - `HEAD` (detached) → no branch to evaluate; warn and proceed without switching.
2. Dirty-tree guard. If `git status --porcelain` is non-empty, do **not** switch — uncommitted changes are left untouched. Warn that the switch was skipped because of local changes and proceed on the current branch.
3. Refresh main (best-effort): `scripts/with-env.sh git fetch origin main` (the wrapper loads `.env`; if it or the token is absent, skip the fetch). On any failure, fall back to the local `main` ref.
4. Fully-merged test. The branch is fully merged when every commit on it is already in main — `git merge-base --is-ancestor HEAD origin/main` (or `HEAD main` on the fetch fallback) exits 0.
   - Exit 0 → `git switch main`, then fast-forward local `main` to the fetched tip with `git merge --ff-only origin/main` so grounding reads the merged code, not a stale local `main` that the fetch never advanced (skip this fast-forward on the fetch-fallback path, where there is no fresher `origin/main` to advance to). On success, announce the switch (`Switched from <branch> (fully merged) to main`). If `git switch main` fails (e.g. `main` is already checked out in a sibling worktree, or local `main` is missing/invalid) or the `git merge --ff-only origin/main` fails (e.g. local `main` has diverged), warn that the switch could not complete and proceed on the current branch — exactly like the dirty-tree, fetch-failure, and unmerged-branch fallbacks above.
   - Non-zero → the branch carries unmerged commits; leave it, warn that the branch has unmerged work, and proceed on the current branch.

The switch never discards commits (it fires only when the branch adds nothing to main), never runs against a dirty tree, and is a plain checkout of `main` followed by a fast-forward to `origin/main` — no branch is created, no history rewritten.

## Resolve the proposal and gate on graduation-readiness

1. Glob `docs/proposals/**/<NNN>-*.md` (recursive — proposals live in category subfolders: `core/`, `security/`, `publication/`, `audience/`, `income/`, `research/`, `ux/`, `technical-plan/`, `operations/`).
   - Zero matches → stop with `error=proposal_not_found`; suggest the user check `docs/proposals/README.md` for available numbers.
   - Multiple matches → stop with `error=ambiguous_proposal_match` and list the matches.
2. Read the matched file in full, including its metadata `Status` row and its `docs/proposals/README.md` row.
3. **Gate on graduation-readiness.** Proceed only when the proposal sits in a burn-in substate — one of `burn-in:ready` / `burn-in:observing` / `burn-in:graduated` (behaviour-code landed and gated). Read the entry criteria that pick between the three in the `### Status Key` of `docs/proposals/README.md` — do not restate them here.
   - If the status is `proposed` / `partial` / `in-progress`, its behaviour-code is not built yet: STOP and recommend `/plan-proposal NNN`. **Never invoke** the sibling — recommend it, and let the human run it.
   - If there is nothing left to graduate (the proposal is already `shipped`, or every flag has already cut over — leaving only the out-of-scope collapse/decommission, whether or not the retired logic is gone), STOP with a clear verdict. Grounding (next section) is authoritative on whether a flag has cut over; see its grounded-stage stop-gate.

   For the burn-in evidence that judges readiness, point the operator at the existing `maestro audit-flags` read surface (backed by `flag_lifecycle.cutover_burn_in_report`) rather than reimplementing horizon logic.

## Ground the flags first-hand

Grep the proposal number and the flag name(s) across `maestro/flag_lifecycle.py`, `maestro/config.py`, `maestro.toml`, `maestro.production.toml`, `CLAUDE.md`, and the proposal doc. Classify each flag's current lifecycle stage:
- a `_UNMANIFESTED_DEFAULT_OFF` allow-list entry (grandfathered default-off, no horizon yet),
- an `EXPERIMENTAL_FLAGS` entry (opt-in experiment with a dated horizon),
- a `PROD_ONLY_CANARIES` canary (live in production behind a `maestro.production.toml` section), or
- an already-cutover `CUTOVER_FLAGS` entry (default-on, sitting on its collapse path).

This first-hand grounding is what the generated plan is built from — never a paraphrase of the proposal doc's own claims.

**Grounded-stage stop-gate.** A flag already classified above as a `CUTOVER_FLAGS` entry has *already* cut over to its default, so the only increment left for it is the collapse/decommission this command scopes out (see the next section). Skip any such already-cutover flag; if **every** flag the proposal owns is already a `CUTOVER_FLAGS` entry, STOP with a clear verdict — there is no in-scope cutover to plan, and the collapse/decommission is a later, human-gated increment (route the human there rather than minting an empty plan). Proceed only for a flag whose cutover — or canary → base default — is still pending.

## Enumerate the next graduation increment

For each flag, scope to the **single next increment** — experimental / unmanifested-default-off → cutover default-on, or prod-only-canary → base default — and write the concrete edits. A cutover-flag *collapse* (deleting a `CutoverFlag`'s `off_path_symbol` and its manifest entry) is a later increment, out of scope here.

The generated plan must enumerate, grounded per proposal, every mechanism the increment touches:

- **(a) `Config` field default** — the field in `maestro/config.py` flipped to its shipped default.
- **(b) `maestro.toml` key** — the key and its inline default comment. Once the canary graduates, the edit also drops the key's `[production: …]` tag (rendered by `flag_lifecycle.overlay_tag`); `tests/test_base_toml_overlay_tags.py` reds on a tag left behind.
- **(c) `maestro/flag_lifecycle.py` manifest move** — move an `ExperimentalFlag` out of `EXPERIMENTAL_FLAGS` and add a `CutoverFlag` to `CUTOVER_FLAGS`; or, for a canary, delete the `PROD_ONLY_CANARIES` entry **and** its `PROD_ONLY_CANARY_BASELINE` frozenset member. Update the test-enforced census-count docstrings (e.g. the "A source census finds …" cut-over census) that state the manifest size — bump the stated count to match the post-graduation manifest.
- **(d) `tests/test_config_flag_lifecycle.py`** — delete any `_UNMANIFESTED_DEFAULT_OFF` entry for the flag and rename its per-flag guard `test_<flag>_flag_manifested_default_off` → `test_<flag>_flag_manifested_cutover_default_on`.
- **(e) `tests/test_cutover_flag_collapse.py`** — increment `_MAX_CUTOVER_FLAGS` when a cutover entry is added (the cap must equal the manifest size).
- **(f) `maestro.production.toml`** — remove the canary enable line(s) and fix the adjacent hand-maintained `Reversion: delete these N lines` count.
- **(g) the proposal doc `| **Status** |` row and the `docs/proposals/README.md` table** — an Open-row status edit for a `burn-in:graduated` step, or an Open→Shipped-table move for `shipped`; keep the two bound.
- **(h) a conditional `CLAUDE.md` edit** — only when the flag or subsystem name appears in invariant prose, updating any embedded count.

Also name the passive gates the graduation diff must satisfy but not edit: `tests/test_proposal_status_vocabulary.py`, `tests/test_proposals_readme_shipped_order.py`, `tests/test_no_line_citations_in_docs.py`, and `flag_lifecycle.find_stage_violations`.

## Write and record the graduation plan

Every `~/.claude/...` path used as a **file-write target** — the plan file here and the pointer file below — must be resolved to an absolute path **before** it reaches any file API: expand the leading `~` via `$HOME/...` in a shell context or `os.path.expanduser("~")` in a Python write. The leading tilde is a display convention only; the Write tool performs no shell expansion and `os.rename`/`open` treat `~` as a literal path component, so a bare `~/.claude/...` write target either fails or silently writes under a `~` directory in the cwd.

1. Compute the output path `~/.claude/plans/graduate-<NNN>-<slug>.md` (resolved to an absolute path per the rule above), where `<slug>` is the proposal filename's slug (the part after `NNN-`, without `.md`). Overwrite if it already exists.
2. Write the plan atomically: write to `~/.claude/plans/graduate-<NNN>-<slug>.md.tmp.<pid>`, then `os.rename` it into place. The plan is a standard `# Plan:` / `## Context` / `## Implementation steps` / `## Critical files` / `## Verification` layout enumerating the mechanism set above as its steps, with the project's standard quality gates (`ruff check`, `mypy`, `pytest`) as verification.
3. Record the per-folder pointer in `~/.claude/last-plan-by-folder.json` (atomic read-modify-write, preserving unrelated keys): read the file (missing → treat as `{}`; malformed JSON → warn, treat as `{}`, do not overwrite silently), set the entry for the current working directory's absolute path to the just-written plan's absolute path plus the current ISO 8601 UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ` — no fractional seconds, literal `Z`), serialize the full object, write to `~/.claude/last-plan-by-folder.json.tmp.<pid>`, then rename in place. Best-effort: on write failure (permission denied, disk full, missing parent), warn and proceed — the pointer is an optimization for `/impl`'s auto-pick, never a correctness requirement.
4. Under `--dry-run`, print the plan body to stdout and skip both writes (no plan file, no pointer update).
5. After writing, print the plan path as the final user-facing message:

       Wrote plan: ~/.claude/plans/graduate-NNN-<slug>.md

## Safety

- Read-only against the repo except the safe `git switch main` and the fast-forward of local `main` to `origin/main` (plus the best-effort `git fetch origin main`) described in *Switch to main if on a merged branch* — a fast-forward only, never a history rewrite. Writes go only to `~/.claude/plans/graduate-NNN-<slug>.md` and the per-folder pointer `~/.claude/last-plan-by-folder.json`, both outside the repo.
- The command **never edits** config (`maestro/config.py`, `maestro.toml`, `maestro.production.toml`), the flag manifests (`maestro/flag_lifecycle.py`), the proposal docs (`docs/proposals/**`, including `README.md`), or `CLAUDE.md` — it plans those edits; `/impl` performs them under human review.
- The command **never commits, stages, pushes, opens a PR, or files an issue**.
- Never invoke `/plan-proposal` or any sibling command; the readiness gate *recommends* it and the human runs it.
- Do not run destructive git operations (`reset --hard`, `checkout --`, `clean -f`, branch deletes, force-push).
