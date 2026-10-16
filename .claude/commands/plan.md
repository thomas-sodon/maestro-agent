Plan the work for the free-text concern `$ARGUMENTS` for the Maestro project.

This command is the **free-text entry point to the plan pipeline**. `/plan-proposal` needs a numbered proposal document, `/bug-fix` needs a GitHub issue, `/backlog` picks one from the backlog for you — none of them accept a bare description of something you want built, changed, or fixed. This one does: it takes the concern in your own words, grounds it first-hand in today's tree, and writes a self-contained plan at `~/.claude/plans/plan-<slug>.md`, then records that plan as this folder's most-recent plan in `~/.claude/last-plan-by-folder.json` so a later `/impl` with no argument picks it up. The plan is the whole deliverable: no code is written, nothing is committed, no issue is filed, no proposal is authored.

The plan covers **only the next increment** — the smallest coherent, independently-mergeable chunk of work that resolves the concern's core and lands as one focused PR. If the concern is large, plan just the first unbuilt slice; later increments are named as forward-looking boundaries in `## Out of scope`, never enumerated as steps.

`$ARGUMENTS` is a space-separated list of tokens. Recognized flag tokens are listed below; **everything else is joined into the free-text concern** — required. An unrecognized `--`-prefixed token is never swallowed into the concern: stop with `error=invalid_argument`, naming the offending token and the recognized forms. Silently absorbing one is how a typo'd `--dryrun`, or a space-separated `--slug my-slug`, turns a write-nothing invocation into a real plan write. No concern text → stop with `error=missing_concern` and an example invocation (`/plan the dashboard status endpoint recomputes the diagnostic snapshot on every poll and it should be cached per iteration`).

Recognized tokens:
- `--dry-run` — print the plan body to stdout; write nothing (no plan file, no pointer update) and change no git state (the merged-branch switch and the on-`main` refresh below are both skipped)
- `--slug=<slug>` — force the output slug, overriding derivation; must be kebab-case and ≤60 chars, else `error=invalid_argument`. A leading `plan-` is stripped before use, so `--slug=plan-foo` and `--slug=foo` both write `plan-foo.md` rather than doubling the prefix.
- `--no-switch` — skip the merged-branch switch to `main` (and, when already on `main`, the refresh) and plan against the branch currently checked out (use when the concern is about work in flight on this branch)

## Switch to main if on a merged branch

Land on a current tree **first** — before grounding the concern. Verification reads the repo first-hand, so a leftover task branch that has already merged into main would ground the plan in stale code.

Two checks precede the switch itself. First, run the `#NNNN` issue-reference routing valve (see *Route the concern elsewhere when a plan is the wrong deliverable*): it reads only the argument text, costs nothing, and firing it *after* the switch would have already moved the user off their branch on a run that writes no plan. Second, the skip conditions — skip this entire section when `--no-switch` is set (announce that the switch was skipped by request), or under `--dry-run`, which changes no git state (print the switch it *would* have run and proceed on the current branch). Otherwise switch to `main`, but only when it is provably safe:

1. Resolve the current branch: `git rev-parse --abbrev-ref HEAD`.
   - `main` → already on `main`, so no switch is needed — but still refresh so grounding reads the latest `main`, not a stale local one. On a clean tree (`git status --porcelain` empty), run the section's best-effort `scripts/with-env.sh git fetch origin main` and then `git merge --ff-only origin/main` to advance local `main` to the fetched tip before grounding (the exit-0 refresh of step 4, minus the redundant `git switch main`; skip the fast-forward on the fetch-fallback path, where there is no fresher `origin/main` to advance to). If the tree is dirty, skip the refresh and proceed on the current `main`, leaving uncommitted changes untouched. If the fetch is unavailable (no `.env`/token, offline) or the `git merge --ff-only origin/main` fails (local `main` has diverged), warn that local `main` could not be advanced and proceed on the current local `main`.
   - `HEAD` (detached) → no branch to evaluate; warn and proceed without switching.
2. Dirty-tree guard. If `git status --porcelain` is non-empty, do **not** switch — uncommitted changes are left untouched. Warn that the switch was skipped because of local changes and proceed on the current branch.
3. Refresh main (best-effort): `scripts/with-env.sh git fetch origin main` (the wrapper loads `.env`; if it or the token is absent, skip the fetch). On any failure, fall back to the local `main` ref.
4. Fully-merged test. The branch is fully merged when every commit on it is already in main — `git merge-base --is-ancestor HEAD origin/main` (or `HEAD main` on the fetch fallback) exits 0.
   - Exit 0 → `git switch main`, then fast-forward local `main` to the fetched tip with `git merge --ff-only origin/main` so verification reads the merged code, not a stale local `main` that the fetch never advanced (skip this fast-forward on the fetch-fallback path, where there is no fresher `origin/main` to advance to). On success, announce the switch (`Switched from <branch> (fully merged) to main`). If `git switch main` fails (e.g. `main` is already checked out in a sibling worktree, or local `main` is missing/invalid) or the `git merge --ff-only origin/main` fails (e.g. local `main` has diverged), warn that the switch could not complete and proceed on the current branch — exactly like the dirty-tree, fetch-failure, and unmerged-branch fallbacks above.
   - Non-zero → the branch carries unmerged commits; leave it, warn that the branch has unmerged work, and proceed on the current branch.

The switch never discards commits (it fires only when the branch adds nothing to main), never runs against a dirty tree, and is a plain checkout of `main` followed by a fast-forward to `origin/main` — no branch is created, no history rewritten.

## Read the concern

Free text carries intent loosely, so pin it down before touching code. From the concern text extract:

1. **The observable.** What the user says is wrong, missing, or wanted — in their words. Keep this verbatim internally; it is the thing the finished plan must actually address.
2. **The nouns.** Module names, symbols, config keys, file paths, CLI commands, data files, and domain terms (`remediation`, `stand-down`, `dream cycle`, `quality gate`). These become the grep targets in the grounding step.
3. **The shape.** Classify as exactly one of:
   - **bug-shaped** — existing behavior is wrong (a wrong value, a missed branch, a crash, a silent no-op). The plan leads with a regression test.
   - **feature-shaped** — behavior that does not exist yet should exist.
   - **refactor-shaped** — behavior is correct but its structure, ownership, or duplication is the problem, with no user-visible change intended.
   - **docs-shaped** — the code is right and the documentation, CLAUDE.md invariant, or `docs/` page is wrong, missing, or drifted.

   The shape decides the plan's step ordering (see *Write the plan file*). When the text supports two shapes, pick the one the observable describes, not the one the suggested remedy implies — a user proposing a refactor to fix a wrong value is reporting a bug.
4. **The stated remedy, if any.** Users often name a fix. Record it as a hypothesis, never as the plan: the grounding step below either confirms it against the code or replaces it, and a remedy that matches a forbidden pattern (see *Forbidden remedies*) is dropped even when it was asked for.
5. **Explicit constraints.** Flags, files, or approaches the concern rules in or out ("without adding a new config key", "only in the dashboard"). These bind the plan and belong in `## Out of scope` when they exclude adjacent work.

If the concern is too vague to yield an observable — no symptom, no target, no desired end state — stop with `error=concern_too_vague`, quote what you extracted, and name the one or two specifics that would make it plannable. Do not guess a concern into existence.

## Route the concern elsewhere when a plan is the wrong deliverable

This command has three siblings that own adjacent shapes. Check for each before grounding; when one fires, stop and recommend it rather than producing a redundant plan. The first valve is text-only and therefore runs **earlier** — before the merged-branch switch (see that section) — so a routed-away run never leaves the user on a different branch than they started on; the other two need the concern's nouns and run here.

- **The concern names a GitHub issue** (contains `#NNNN`, or an `https://github.com/.../issues/NNNN` URL) → `/bug-fix NNNN` owns issue-anchored planning: it verifies the issue against today's tree, writes `~/.claude/plans/fix-<NNNN>-<slug>.md`, and embeds the `Fixes: #<NNNN>` line that makes the eventual PR auto-close the issue. Stop with the recommendation. A bare `#` in prose that is plainly not an issue reference (a heading marker, a shell comment) does not trigger this.
- **The concern is already covered by a proposal.** Grep `docs/proposals/**/*.md` for the concern's nouns. If an open proposal already covers this ground, `/plan-proposal NNN` derives an increment plan from it with the proposal's own dependency and status context in view. Stop and name the covering proposal. A proposal that merely *touches* the same module does not count — the coverage must be of this concern's substance.
- **The concern is a symptom of a structural class, not one change.** If grounding would clearly be an investigation into repeated point fixes — the concern says "keeps happening", "every fix adds another", "the third time we've patched this", or the nouns land on a subsystem with a visible trail of `fix-NNN-…` merges — the deliverable is a proposal, not a plan. Recommend `/propose <concern>`, which mines the live production data and the whack-a-mole history and closes the whole class. Do not pre-run that investigation here.

These are recommendations to the user, not delegations: **never invoke `/bug-fix`, `/plan-proposal`, or `/propose` from inside this command.** When none fires, continue.

## Ground the concern in today's tree

The plan must be grounded in code you read first-hand, not in the concern's description of the code. Do not delegate this verification to subagents.

1. **Map nouns to modules.** Grep/Glob each noun from *Read the concern* against `maestro/`, `maestro_review/`, `maestro_sdk/`, `tests/`, `docs/`, and `maestro.toml`. Read the implicated functions **in full, plus their direct callers**. Reading a function without its callers is how a plan proposes a change that breaks three call sites it never saw.
2. **Confirm the observable exists.** For a bug-shaped concern, find the specific code path that produces the reported behavior and be able to state it in one sentence. If the behavior is **not** reproducible in today's tree — already fixed, or the cited surface no longer exists — stop with `error=concern_not_reproducible`, cite the current code and the commit that changed it (`git log -S<symbol> --oneline -5`), and let the user decide whether to re-file. Never plan a fix for a bug that is not there.
3. **Check the invariants.** Read the CLAUDE.md sections covering the implicated subsystem. A concern that asks for something a load-bearing invariant forbids (a broad `except` that silences a failure, a bare `query()` in a main loop phase, an unbounded list entering a prompt, a human-approval bypass) does not get planned around the invariant — the plan either finds the compliant way to get the outcome or the report says the concern collides with an invariant and names it. Also check for AST-enforced tests that would fail (`tests/test_no_silenced_exceptions.py`, `tests/test_structural_guards.py`, `tests/test_no_line_citations_in_docs.py`).
4. **Find the adjacent tests.** Locate the test file(s) covering the implicated code and read the tests nearest the change site. They reveal the intended contract, supply the fixtures the new test will reuse (`mem_fs`, `SDKMock`), and occasionally assert the very behavior the concern calls wrong — which is decisive information the plan must confront rather than silently break.
5. **Check the config surface.** If the concern implies a new knob, grep `maestro.toml`, `maestro/config.py`, and `maestro/toml_config.py` for an existing key that already covers it. Reusing a key beats adding a rival one.

Every path, symbol, and line range that ends up in the plan comes from this step. Cite by file path and symbol; a line range is acceptable in a plan (unlike in `docs/proposals/`, where `tests/test_no_line_citations_in_docs.py` bans it) but the symbol name is what makes the citation survive drift.

## Research what the concern does not say

Use the **Explore** subagent to fill gaps: usage patterns, similar prior implementations to reuse, other call sites of a helper being changed, how a sibling subsystem solved the same problem. One Explore call is the default; up to 3 in parallel only when the concern genuinely spans several layers (e.g. the loop, persistence, and the dashboard in one change). Each agent gets the concern's one-sentence observable, a 2-paragraph summary of the implicated code, and one specific question.

Explore is for **discovery only**. It never substitutes for the first-hand reading above.

## Scope to one increment

Before writing, cut the work down:

1. State the **core**: the one change without which the concern is not addressed at all.
2. Sort everything else into *needed for the core to be safe and testable* (stays) versus *adjacent improvement the concern makes tempting* (goes to `## Out of scope`). Rename-the-surrounding-module, harden-the-neighbouring-path, and add-a-sensor-while-we-are-here all go.
3. If the core alone is still too large for one focused PR, plan its first slice and name the rest as forward-looking boundaries. A plan whose `Implementation steps` exceed roughly seven entries is a signal the increment was not cut.
4. Order the steps so an interrupted run leaves the tree working, and gate any behavior change behind a config flag defaulting to today's behavior when the concern touches the autonomous loop.

### Forbidden remedies

The plan MUST NOT be built from these, even when the concern explicitly asks for one (same family as `/rca`'s and `/propose`'s forbidden fixes):

- Bumping a timeout, cap, retry count, or threshold as the primary remedy, unless the grounding step proves the limit itself is the root.
- Silencing: broad `except`-and-continue, log-level demotions, `# type: ignore` on a real type bug, or any quality-gate bypass (`pytest.skip`, coverage pragmas, `--no-verify`).
- Auto-recovery that hides the failure instead of fixing it (blind retries on a non-transient failure, `restart: always`).
- Adding one more special-case gate or marker to an existing pile for this one symptom — that is the next mole, and the deliverable is `/propose`, not this plan.

When the concern's stated remedy matches one of these, the plan carries a clean replacement instead, and the report says which remedy was dropped and why.

## Write the plan file

1. **Derive the title and slug.** Synthesize a short imperative title (≤8 words) describing the end state, not the symptom (`Cache the diagnostic snapshot per iteration`, not `Snapshot is slow`). Then compute the slug: use `--slug` if given; otherwise lowercase the title, replace non-alphanumeric runs with `-`, trim leading/trailing `-`, collapse repeated `-`, and truncate to ≤60 chars at a word boundary. If the result is empty, fall back to `concern`.
2. **Compute the candidate output path:** `~/.claude/plans/plan-<slug>.md`. The `plan-` prefix keeps this command's output distinguishable from `/bug-fix`'s `fix-<NNN>-…` and `/plan-proposal`'s `proposal-<NNN>-…` siblings in a shared plans directory. This is only the *candidate* — step 3 may rebind it.
3. **Check for a plan this one supersedes.** Glob `~/.claude/plans/plan-*.md` and read the `# Plan:` heading and `## Context` of any whose slug overlaps this one's significant words. If one covers the same concern, **rebind the output path to that plan's path** — overwriting it in place instead of writing a near-duplicate sibling — and note the reuse in the report; unrelated plans are left alone. An exact path collision is overwritten without ceremony. The rebound value is the **resolved output path**: step 5, the pointer write, and the report all use it rather than the step-2 candidate. Without that rebinding the supersede is advisory only, and the near-duplicate sibling this step exists to prevent gets written anyway.
4. In `--dry-run` mode, print the plan body to stdout and stop here — no file, no pointer update, no supersede.
5. **Write atomically:** write the body to `<resolved output path>.tmp.<pid>`, then `os.rename` it into place, so a concurrent reader sees either the prior plan or the new one, never a truncated file (the same discipline `/bug-fix` and the pointer write use).

### Plan layout

The plan reads as **freshly authored**. It does not say "the concern said", does not quote the user's request, does not include a drift or comparison section, and does not name this command. Anyone reading it in isolation should see only a plan.

1. **`# Plan: <Title>`** — the synthesized title.
2. **`## Context`** — fresh prose: what is wrong or missing, why it matters, and the intended end state. Cite the target file and symbol once so the reader knows where the work lands. Do not paste the concern text verbatim and do not paste log excerpts.
3. **`## Implementation steps`** — numbered list scoped to the one increment. Each step has a short title, the file(s) it touches and the symbol(s) within them, and a 1–3 sentence description. Shape-specific ordering:
   - **bug-shaped** — step 1 adds the regression test that captures the bad state (name the test file and test name, and the inputs that trigger it); step 2 applies the root-cause fix at the cited symbol; an optional step tightens the invariant so the same defect fails loudly and earlier. Per CLAUDE.md rule 5 the test lands in the *same commit* as the fix — never check in a failing test alone.
   - **feature-shaped** — build the smallest working surface first, then its tests, then wire it into its caller. Behavior that changes the autonomous loop lands behind a config flag defaulting to today's behavior.
   - **refactor-shaped** — introduce the new structure alongside the old, move call sites, then delete the old one in the final step. Name the characterization tests that prove behavior did not change; a refactor with no test evidence of equivalence is not plannable as one increment.
   - **docs-shaped** — state the drifted claim and the code that contradicts it, then the corrected wording. If the target is CLAUDE.md, respect its document discipline (one line per invariant, no line-number citations).
4. **`## Critical files`** — bulleted list of files to be modified, with their current paths.
5. **`## Reused functions and utilities`** — bulleted list of existing helpers to call instead of reimplementing, each as `path — symbol — one-line purpose`. If the change is self-contained with nothing to reuse, write the single bullet `None — change is self-contained.` so the reader knows the section was considered.
6. **`## Verification`** — concrete steps to confirm the change works end-to-end. Default to the project gates:
   - `python3.14 -m ruff check maestro/ tests/`
   - `python3.14 -m mypy maestro/`
   - `python3.14 -m pytest tests/ -v`

   Name any new test explicitly (`pytest tests/test_<module>.py::<test_name> -v`). Add the browser command when the change touches `maestro/dashboard/` or `tests/browser/`, and the `maestro_review/` or `maestro_sdk/` gate triple when it touches those packages. When the original behavior needed a runtime trigger, add a one-line manual repro naming it.
7. **`## Out of scope`** — bullets framing related work this plan does not cover, phrased as forward-looking boundaries ("Reworking the surrounding module is out of scope"), never as deferrals from a draft. Later increments of a large concern are named here as future work, as are the constraints the concern itself ruled out.

## Record the plan pointer

After the plan file is written, record it as this folder's most-recent plan so `/impl` invoked with no argument picks it for this working directory instead of the global most-recently-modified plan in `~/.claude/plans/`. This step never runs on the `--dry-run` path (no file was written there). The pointer lives in a shared JSON file at `~/.claude/last-plan-by-folder.json`, keyed by absolute working directory:

    {
      "<absolute path of this command's working directory>": {
        "plan": "<absolute path of the plan file just written>",
        "written_at": "2026-05-24T12:34:56Z"
      }
    }

Use atomic read-modify-write: read the file (missing → treat as `{}`; malformed JSON → warn, treat as `{}`, do not overwrite silently), set the entry for the current working directory's absolute path to the just-written plan's absolute path plus the current ISO 8601 UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ` — no fractional seconds, literal `Z`), serialize the full object, write to `~/.claude/last-plan-by-folder.json.tmp.<pid>`, then rename in place. Preserve unrelated keys untouched. This is best-effort: if the write fails (permission denied, disk full, missing parent), warn and proceed — the pointer is an optimization for `/impl`'s auto-pick, never a correctness requirement.

## Report

Print the **resolved output path** — the step-3 rebind when a supersede fired, otherwise the step-2 candidate — as the final user-facing message:

    Wrote plan: ~/.claude/plans/plan-<slug>.md

Append a single block of bullet addenda directly under the path when any of these hold, and nothing when the run was clean:

- The concern's stated remedy was dropped — name it and the forbidden pattern it matched, plus the replacement the plan carries.
- The concern collides with a load-bearing CLAUDE.md invariant — name the invariant so the implementer scopes around it rather than papering over it.
- The plan superseded an existing plan file — name the path it overwrote.
- The concern was cut to an increment — one line naming what moved to `## Out of scope`.
- The switch or refresh of `main` was skipped (dirty tree, unmerged branch, `--no-switch`, or — when already on `main` — the fetch was unavailable or `git merge --ff-only origin/main` failed on a diverged local `main`) — say which, since the plan was grounded in that tree rather than a freshly-fetched `main`.
- An open PR or branch already touches the implicated code (`git branch -a --contains` on the cited symbol's last-change SHA, or a quick `gh pr list --search <noun>`) — name it so the work is not duplicated.

Close with the next step: `/impl` to implement the plan (no argument needed — the pointer is recorded).

In `--dry-run`, print the full plan body instead of the path, clearly marked as not written. If a routing valve or `error=` stop fired, report that verdict and the recommended command instead of a plan path.

## What to skip

- Do not write code, tests, or documentation. The plan is the deliverable; implementation is `/impl`'s job.
- Do not commit, stage, push, open a PR, or create a branch.
- Do not file or comment on a GitHub issue (`/rca` and `/bug-hunt` own that surface), and do not author a proposal (`/propose` owns that one).
- Do not run the quality gates. They belong in the plan's `Verification` section, executed by whoever implements it.
- Do not invoke `EnterPlanMode`. This command writes a static plan file and exits; entering plan mode afterwards is the user's call.
- Do not invoke `/impl`, `/bug-fix`, `/plan-proposal`, `/propose`, or `/backlog` from inside this command — the routing valve recommends, it never delegates.
- Do not plan more than one concern per invocation. Two unrelated concerns in one argument string → plan the first and say so, or stop with `error=concern_too_vague` if neither dominates.

## Safety

- Read-only against the repo except for safe git-state changes: a `git switch main` and a fast-forward of local `main` to `origin/main` when the current branch is clean and fully merged — or, when already on a clean `main`, that fast-forward alone (see *Switch to main if on a merged branch*), plus the best-effort `git fetch origin main` that check uses. Writes go only to the resolved `~/.claude/plans/plan-<slug>.md` and the per-folder pointer file `~/.claude/last-plan-by-folder.json` — both outside the repo, and neither is a substitute for the plan itself.
- Never delete a plan file. The supersede rule overwrites one path in place; every other plan in `~/.claude/plans/` is left untouched.
- Do not run destructive git operations. No `reset --hard`, no force-push, no branch deletion, no history rewrite.
- Redact tokens, `Bearer ` headers, API keys, and Telegram bot tokens (`(?:bot)?\d+:[A-Za-z0-9_-]{30,}`) from anything quoted into the plan — plans are frequently pasted into PRs.
