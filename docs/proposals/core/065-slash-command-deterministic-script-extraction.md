# 065: Extract Deterministic Git Plumbing from Slash Commands into Scripts

| Field | Value |
|-------|-------|
| **Impact** | LOWER |
| **Status** | shipped |
| **Affects** | new `scripts/preflight.sh`, new `scripts/prune-branches.sh`, new `scripts/git-push-tracked.sh`, new `scripts/pr-url.sh`, `.claude/commands/commit-push.md`, `.claude/commands/create-pr.md`, `.claude/commands/merge-main.md`, `.claude/commands/prune-branches.md`, tests |
| **Derived from** | the `scripts/with-env.sh` + `scripts/quality-gates.sh` extraction precedent |
| **What landed** | Steps 1–4 — `scripts/preflight.sh` (shared git preconditions behind the five check tokens `work-tree`, `not-main`, `clean-tree`, `dirty-tree`, `github-token`, consumed by all four command files), `scripts/git-push-tracked.sh` (upstream-fallback push + reserved non-fast-forward exit code, consumed by `commit-push`, `create-pr`, `merge-main`), `scripts/pr-url.sh` (PR-URL lookup with empty-stdout-on-no-PR fallback, consumed by `commit-push`), and `scripts/prune-branches.sh` (protected-set + `[gone]`-upstream candidate detection + safe `git branch -d` pass that never escalates to `-D`, with `--dry-run` early exit, consumed by `prune-branches`) are extracted and wired in, each pinned by `tests/test_preflight_script.py`, `tests/test_git_push_tracked_script.py`, `tests/test_pr_url_script.py`, and `tests/test_prune_branches_script.py` respectively. |
| **What is missing** | Nothing remains — all four scripts are extracted, wired into their command files, and pinned by static-text tests. |

## Description

> Status updated to shipped on 2026-06-17 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

The git-centric slash commands (`commit-push`, `create-pr`, `merge-main`,
`prune-branches`) describe, in prose, sequences of deterministic shell steps:
precondition checks, gone-branch detection, upstream-tracking push fallbacks,
PR-URL lookup. Each run, the model re-derives these fixed sequences from the
markdown and executes them by hand — spending tokens on logic that has exactly
one correct form, and risking drift between four command files that specify the
same checks independently.

This proposal applies the dividing line the codebase already follows — *a
deterministic sequence belongs in a script; judgment stays in the command* — to
four more pieces of plumbing. `scripts/with-env.sh` (env loading) and
`scripts/quality-gates.sh` (gate definitions, shared by `/quality-gates`,
`/merge-main`, and the Stop hook) established the pattern precisely so the logic
"never drifts." The four extractions here are the remaining mechanical residue
in the git commands: shared preconditions, the prune candidate-and-safe-delete
pass, the push upstream-fallback with non-fast-forward signalling, and PR-URL
lookup.

The end state: each git command shrinks to *preflight → [the model does the
judgment part] → push helper → report*. Conflict resolution, commit-message
authoring, and force-delete approval — the parts that genuinely need a model or
a human — stay in the command files. The brittle, repeated plumbing becomes
centralized, single-sourced, and pinned by tests.

## Relevant Findings

- `scripts/with-env.sh` — already extracts the `set -a && source .env && set +a`
  prefix that "had to be repeated dozens of times across the command files";
  the header comment states the rationale (shell state does not persist between
  tool invocations) this proposal extends.
- `scripts/quality-gates.sh` — the precedent for a script as "single source of
  truth" shared across `/quality-gates`, `/merge-main`, and
  `.claude/quality-gate-hook.sh` so "the gate definitions never drift."
- `.claude/commands/commit-push.md` (step 1, step 2, step 8, step 9) — specifies
  in prose: `.env`/`GITHUB_TOKEN` non-empty check, not-on-`main` check, the
  `push` vs `push -u origin <branch>` upstream fallback, and the
  `gh pr view --json url --jq .url` lookup with "exits non-zero → no PR" handling.
- `.claude/commands/create-pr.md` (step 1, step 5) — repeats the same
  `.env`/`GITHUB_TOKEN` precondition and the `git push -u origin <branch-name>`
  upstream-set form.
- `.claude/commands/merge-main.md` (step 1, step 7) — repeats the not-on-`main`
  + clean-tree preconditions and specifies the upstream-vs-no-upstream push
  fallback plus the non-fast-forward rejection detection (matching messages
  "Updates were rejected because the remote contains work that you do not
  have locally", "fetch first", or "non-fast-forward") that should gate the
  model's involvement.
- `.claude/commands/prune-branches.md` (steps 2, 4, 5, 6, 7) — ~90% mechanical:
  capture protected/current/worktree-pinned names, `git fetch --all --prune`,
  identify `[gone]`-upstream branches via
  `git for-each-ref ... %(upstream:track)`, subtract the protected set, and run
  the safe `git branch -d` pass. Only step 8 (force-delete of the unmerged
  remainder) needs human approval.
- `tests/test_quality_gate_hook_changed_files.py` (module docstring) — documents
  that shell-script behavior is pinned via **static-text** tests, *not*
  subprocess runs: "exercising it would require running the shell script as a
  real subprocess against a real git tree, which the unit suite forbids." This
  is the testing shape the new scripts must follow.

## Implementation Plan

Each step adds one self-contained script and rewires the commands that consume
it; an interrupted run leaves the commands working (they can always fall back to
inline git, since the script merely centralizes what the prose already says).

1. [x] **`scripts/preflight.sh` — shared git preconditions.**
   new `scripts/preflight.sh`, `.claude/commands/{commit-push,create-pr,merge-main,prune-branches}.md`.
   A script taking named check tokens (`work-tree`, `not-main`, `clean-tree`,
   `dirty-tree`, `github-token`) that runs each requested check and exits
   non-zero with a one-line diagnostic on the first failure. Replace the
   precondition paragraphs in the four commands with a single
   `scripts/preflight.sh <checks…>` invocation, keeping the "stop and surface to
   the user" instruction in prose.
   - *Depends on:* none.
   - *Acceptance:* a static-text test pins the full check-token table — each
     token (`work-tree`, `not-main`, `clean-tree`, `dirty-tree`, `github-token`)
     mapped to its git command and to a non-zero exit on failure — so a future
     edit cannot silently drop a check. Behavioral correctness (e.g. `not-main`
     actually refusing on `main` and passing on a feature branch) is left to
     manual review per the Testing Strategy.
2. [x] **`scripts/git-push-tracked.sh` — upstream-fallback push with NFF signal.**
   new `scripts/git-push-tracked.sh`, `.claude/commands/{commit-push,create-pr,merge-main}.md`.
   A script that pushes via `git push` when an upstream is configured and
   `git push -u origin HEAD` otherwise, and exits with a **distinct reserved
   code** on a non-fast-forward rejection (so the command engages the model only
   when integration is actually required). Never passes `--force`. Routed
   through `scripts/with-env.sh` for the token. Replace the push paragraphs in
   the three commands with this helper, keeping the conflict-resolution and
   retry-loop judgment in prose. **Behavior-change disclosure:** only
   `merge-main` (step 7) detects a non-fast-forward rejection today; `commit-push`
   (step 8) and `create-pr` (step 5) have no NFF path — a rejected push there
   currently surfaces git's raw error with no special handling. For those two
   commands the reserved NFF exit code is therefore a **new signal**, not a
   faithful extraction, and their prose must be updated to branch on it.
   - *Depends on:* none.
   - *Acceptance:* a static-text test pins the upstream-detection branch (the
     `git push` vs `git push -u origin HEAD` selection), the reserved
     non-fast-forward exit code, the three NFF rejection-message patterns
     (matching `merge-main.md` step 7), and pins the safe push forms — that the
     only push commands are `git push` and `git push -u origin HEAD`, with no
     `--force` / `--force-with-lease` flag in a functional position (verified by
     enumerating the `git push` lines, not a bare `'git push' in content`
     substring or `'--force' not in content` check — see the Testing Strategy).
     Behavioral correctness
     (upstream selection and NFF signalling) is left to manual review per the
     Testing Strategy.
3. [x] **`scripts/pr-url.sh` — PR-URL lookup.**
   new `scripts/pr-url.sh`, `.claude/commands/commit-push.md`.
   Wrap `gh pr view --json url --jq .url` (via `scripts/with-env.sh`) so that "no
   PR for this branch" yields empty stdout and exit 0 rather than a surfaced
   error. Replace `commit-push` step 9 with this helper.
   - *Depends on:* none.
   - *Acceptance:* a static-text test pins the `gh pr view --json url --jq .url`
     invocation and the empty-stdout-on-no-PR contract (the exit-0 / empty-stdout
     fallback path). Behavioral correctness (empty stdout and exit 0 when
     `gh pr view` reports no PR, the captured URL otherwise) is left to manual
     review per the Testing Strategy.
4. [x] **`scripts/prune-branches.sh` — candidate detection + safe-delete pass.**
   new `scripts/prune-branches.sh`, `.claude/commands/prune-branches.md`.
   Move steps 2, 4–7 into the script (step 3 argument parsing remains in the
   command, since `--force` governs the step 8 force-delete judgment that stays
   in prose): compute the protected set (current branch,
   `main`/`master`/`develop`, worktree-pinned branches from
   `git worktree list --porcelain`), `git fetch --all --prune` (via
   `with-env.sh`), enumerate `[gone]` branches, subtract the protected set, print
   the plan, and run the safe `git branch -d` pass — collecting successes and
   `-d`-refused (unmerged) branches separately. Support `--dry-run` (stop after
   the plan). The script **never** escalates to `-D`; it prints the unmerged
   remainder for the command to hand to the user (step 8 judgment stays in prose).
   - *Depends on:* none.
   - *Acceptance:* a static-text test pins the protected-name set (current
     branch, `main`/`master`/`develop`, worktree-pinned names), the
     `%(upstream:track)` `[gone]` detector, the `git fetch --all --prune` and
     `git branch -d` commands, the `--dry-run` early exit, and that no `-D`
     escalation form is present — verified by enumerating the `git branch` lines
     and asserting none contains `-D`, not a bare `'git branch -d' in content`
     substring (true even with a `-D` line present) or a `'git branch -D' not in
     content` check a safety comment would defeat (see the Testing Strategy).
     Behavioral correctness (safe-deleting the
     merged branch, listing the unmerged one as "refused", never touching the
     current/protected/worktree branches, and deleting nothing under
     `--dry-run`) is left to manual review per the Testing Strategy.

## Testing Strategy

Follow the established shell-script testing shape (`mem_fs`/no-real-I/O rules
forbid running these as subprocesses in the unit suite): add Python **static-text
pin** tests under `tests/` — one module per new script — mirroring
`tests/test_quality_gate_hook_changed_files.py`. Each test resolves the script
path and asserts the load-bearing invariants are present and the dangerous forms
are absent:

- `test_preflight_script.py` — pins every check token and its git command.
- `test_git_push_tracked_script.py` — pins the upstream-detection branch, the
  reserved non-fast-forward exit code, the three NFF rejection-message patterns
  (matching `merge-main.md` step 7: "Updates were rejected because the remote
  contains work that you do not have locally", "fetch first",
  "non-fast-forward"), and pins the safe push forms by **line-level
  enumeration** — collect every non-comment line containing `git push` (e.g.
  `re.findall(r'(?m)^(?!\s*#).*\bgit push\b.*$', content)`) and assert each one
  matches only `git push` or `git push -u origin HEAD` with no `--force` /
  `--force-with-lease` token. A bare `assert "git push" in content` is
  insufficient: `"git push"` is a substring of `"git push --force"`, so it
  silently passes for the exact regression it must catch. Enumerating the push
  lines verifies *exclusivity* (no dangerous form present), still static-text,
  and the comment-line filter — not a string-absence check — is what keeps a
  self-documenting safety comment (e.g. `# never pass --force`) from interfering.
- `test_pr_url_script.py` — pins the `gh pr view --json url --jq .url` form and
  the empty-stdout-on-no-PR contract.
- `test_prune_branches_script.py` — pins the protected-name set, the
  `%(upstream:track)` `[gone]` detector, the `git fetch --all --prune` and
  `git branch -d` commands, the `--dry-run` early exit, and that no `-D`
  escalation exists by **line-level enumeration** — collect every non-comment
  line containing `git branch` and assert none contains `-D`. A bare
  `assert "git branch -d" in content` is insufficient: it stays true even when a
  `git branch -D` line is added alongside the safe `-d` pass, so it cannot catch
  a `-d`→`-D` escalation. Enumerating the `git branch` lines verifies the
  exclusivity the invariant requires, and the comment-line filter keeps a
  self-documenting safety comment from interfering.

Because the unit suite cannot run these scripts as real subprocesses against a
real git tree, the static-text pins verify the *presence* of the load-bearing
commands and the *exclusivity* of the safe form of each dangerous operation
(`git push` without `--force`, `git branch -d` not `-D`) by enumerating the
matching non-comment lines — not a bare `X in content` substring pin, which
passes even when the dangerous form is appended (`"git push"` is a substring of
`"git push --force"`), nor a string-absence check that a self-documenting safety
comment would defeat; behavioral
correctness (exit codes, push fallback, delete decisions, `--dry-run` early exit)
is left to manual review — mirroring the note in
`tests/test_quality_gate_hook_changed_files.py` that "behavioral coverage is left
to manual review." Each step's `Acceptance` line above is framed accordingly: a
static-text invariant, plus a manual-review note for the behavioral contract.

Each script must pass `shellcheck` (as `with-env.sh` already does). The four
command files are updated in the same PR as the script they consume; cross-check
that no command still inlines the extracted sequence.

## Risk Assessment

Low blast radius — these are developer-workflow helpers, not loop or runtime
code; nothing in `maestro/` imports them and the Python coverage gate is
unaffected. The main risk is a subtle behavior change versus the prose (e.g. a
push-fallback edge case), mitigated by (a) keeping each script a faithful
transcription of the existing steps — with one disclosed exception: for
`commit-push` and `create-pr` the NFF exit code from `git-push-tracked.sh` is a
**new signal** that does not exist in those commands today (only `merge-main`
has NFF handling), so those two gain new reactive behaviour and their prose must
be updated to handle it; (b) the static-text pins above, and (c) the
fact that a broken helper fails loud and non-zero rather than silently
mis-acting. Rollback is a plain revert: restore the inline prose in the affected
command file; no state, schema, or migration is involved.

Safety invariants are preserved in script form, not weakened: `prune-branches.sh`
never escalates `-d`→`-D` and never touches protected/current/worktree branches;
`git-push-tracked.sh` never force-pushes; none of the scripts push to `main`.
These are exactly the guarantees the command "Safety" sections assert today, now
mechanically enforced and test-pinned rather than restated in prose.

## Out of scope

- The reasoning-heavy commands (`bug-fix`, `rca`, `impl`,
  `plan-proposal`, `backlog`, `process-comments`) — these are model harnesses
  with no mechanical sequence to extract beyond the preflight/push helpers above.
- Conflict resolution, commit-message and PR-body authoring, and the
  quality-gate fix-diagnosis discipline — all genuine judgment, correctly left
  in the command files.
