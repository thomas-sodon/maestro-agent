# Pipeline workflows

One engine takes a change from plan to a merged PR; the proposal pipeline is the
same engine run lane by lane. Every stage is a fresh session, so context stays
small, and the plan, implement, review, polish and PR stages each run their
project skill.

| Workflow | What it does |
|---|---|
| `plan-to-merged-pr` | **Plan** (`/plan`, or `/plan-proposal` for a proposal lane; medium) → **Implement** (`/impl`; low) → **Review** (`/review-changes` framed by the plan and, for a lane, the proposal; high) with fixes (low), re-reviewed until no bug or concern finding remains (suggestions and nits go to the fixer but never block) — a defect left after `max_review_rounds` fixes stops the run before any push; a stacked branch is reviewed against its last base, not `main` → **Polish** (`/polish` until a clean round; low) → **PR** (commit, merge check against `origin/main` and any stacked bases, then `/create-pr`) → **Monitor** (CI, AI review, conflicts and comments via `/merge-main`, `/process-comments` and CI fixes, until the PR merges or settles). |
| `bug-fix-backlog` | Per lane of open bug issues (one issue, or several sharing a code site): **Triage** (`/bug-fix <n> --dry-run` in the lane worktree, one combined plan; medium) → **Verify** every verdict independently (a plan verdict gets a failing regression test written against today's code plus a read-only skeptic, and survives on a reproduction or an unrefuted skeptic; a close verdict needs two refuters with different lenses to both fail; high) → **Close** the confirmed-fixed issues after a state/linked-PR re-check (a refuted close is re-planned instead) → **Ship** the confirmed bugs through `plan-to-merged-pr` with the verified plan as `plan_text` and `Fixes #N` in the PR body. Anything neither reproduced nor surviving the skeptic is reported for a human, never closed. |
| `proposal-build-chain` | Runs `plan-to-merged-pr` in **build-only** mode for each lane of a proposal, in stack order: everything up to the commit and merge check, then push the branch — no PR. Stops at the first lane that does not push. |

Modes of `plan-to-merged-pr`:

| Mode | args | Use |
|---|---|---|
| full (default) | `{ concern }` | one concern from plan to a watched PR |
| build-only | `{ concern, ship: false, branch, dir, bases }` (+ `planner: 'plan-proposal', proposal_number, proposal` for a lane) | a stacked lane whose predecessors are not merged yet |
| ship-only | `{ ship_only: true, branch, dir, title \| existing_pr, predecessor_prs }` | open (or ready) the PR for a built lane once every PR below it has merged, then monitor |
| monitor-only | `{ pr, branch, dir }` | babysit an existing PR |

## Proposal orchestration (the part the main session does)

1. **Triage.** Read each proposal's *What is missing* and its steps' *Depends on*.
   Group steps that land together into one lane (one plan, one PR). Stack a lane
   after any lane that edits the same files; lanes with disjoint files can run as
   separate chains in parallel. Stop and report anything blocked by another
   proposal, an operator act or a time window, unless the operator waives it
   (record the waiver in the PR and the proposal doc).
2. **Build.** Run `proposal-build-chain` with the ordered lanes; each lane's
   `bases` lists the earlier, still-unmerged lane branches it stacks on.
3. **Ship in merge order.** Mergify merges any ready PR into `main`, so a stacked
   lane's PR must not exist — not even as a draft someone readies — until every
   PR below it has merged. Watch for those merges from the main session (a
   read-only loop on `gh pr view <n> --json state`), then run
   `plan-to-merged-pr` in ship-only mode for the lane, passing those PRs as
   `predecessor_prs`; the PR stage refuses to open anything until each reads
   `MERGED`. Workflow agents cannot wait for hours, so all waiting stays in the
   main session.
4. **Report** each PR as it opens and merges, and every blocker.

## Gate slots

Every stage that may run ruff / mypy / the test suite (directly or through a
skill) runs it through `scripts/gate-slot.sh`, or a host wrapper passed as
`gate`. The wrapper derives how many concurrent gate runs the machine admits,
and which CPUs each owns, from the usable CPUs (affinity grouped by SMT
sibling, capped by a cgroup CPU quota) and the total memory bound, via
`maestro.gate_capacity.measure_gate_slots` — so the same workflows fit an
8 vCPU / 24 GB host (two 4-CPU slots), a 16-core host (four), a memory-starved
host (fewer slots, each pinned to its memory-clamped width) or a small container
(one). A pinned slot is locked CPU by CPU, so two runs never hold an overlapping
CPU; the run is pinned with `taskset` to exactly its slot's CPUs, and the gate
lease reads that affinity and scopes its own slot files by it, so concurrent
slots run in parallel and `-n auto` and the lease both size to the slot. Under a
CFS quota with no cpuset, a slot still claims its CPU ids from the same pool but
is not confined to them, so the CPUs claimed by every run on a host never
exceed the CPUs it has. Exit 75 means
every slot is busy (re-run the same command); exit 125 (with a `gate-slot:`
line on stderr) means the wrapper itself cannot run (no command given, no `flock`, an unwritable lock directory, slots
that could not be planned, or a CPU that cannot be pinned) — report it, never
bypass the wrapper. A host whose own tooling requires a different wrapper (for
example a PreToolUse guard that only admits its fleet semaphore) passes it as
`gate`; never mix two semaphores on one host, since they lock different files.
Every other exit code is the gated command's own. Every checkout and agent on a
host must share `GATE_LOCK_DIR` (default `$HOME/.maestro/gate-slot-locks`). The
default wrapper path is resolved in each checkout, so a checkout whose tree
predates `scripts/gate-slot.sh` must be given a `gate` that exists (exit 127 =
wrapper missing: report, never bypass). Long runs are backgrounded and monitored
through their logs — no hard timeouts.

**No `git stash`.** All worktrees of a repo share one stash list (`refs/stash`),
so an agent in a parallel worktree never runs `git stash` in any form (push,
pop, apply, drop): another lane's pop can take its entry, or it can pop another
lane's (two salvage lanes swapped fixes this way on 2026-10-12). To show a test
failing before the fix, save only the fix's own source paths to a file in the
agent's own scratch area (`git add -N` any new fix files first, then
`git diff --binary HEAD -- <fix paths> > <file>`), `git checkout HEAD -- <fix
paths>` and remove any new fix files, leaving the tests in place, run the test,
then `git apply <file>`; a WIP commit on the agent's own branch also works.

## Args

`bug-fix-backlog`:

```js
{
  lanes: [{ key: 'redrain', issues: [4602, 4577] }, { key: 'regex', issues: [4571] }],  // + optional branch, dir per lane
  root: '/abs/worktrees/dir', date: '20261012',  // lane defaults: dir <root>/fix-<issues>, branch bugfix/<key>-<issues>-<date>
  ship: true, close: true,                       // ship false = build only; close false = report confirmed-fixed issues instead
  efforts: { triage: 'medium', verify: 'high', close: 'low', ... },  // plus the plan-to-merged-pr keys, passed through
  gate, commit_trailers, max_review_rounds, max_polish, max_monitor_rounds,
}
```

Lanes must not share a key, issue, `dir` or `branch`; the workflow refuses to start otherwise.

Each lane result carries the engine's `status` (or `nothing_to_ship` / `failed_at_triage` /
`branch_mismatch`) plus `closed`, `needs_human`, `skipped` and `bugs` (with the regression tests
written). A lane issue the triage returns no verdict for, or conflicting verdicts for, lands in `needs_human`;
one that an open PR already closes is `skipped` rather than shipped again. A close verdict is acted on
only when the lane worktree is exactly `origin/main` and the triage wrote a close comment; otherwise
it lands in `needs_human`, as does a plan verdict judged in a worktree behind `origin/main`. A `nothing_to_ship`
lane that had plan verdicts removes the reproducer's uncommitted tests and reports what is still
uncommitted as `dirty` (or `cleanup_failed` when that step fails).

`plan-to-merged-pr` also takes `plan_text` (with `branch`): an already-written plan, so the
planning skill is skipped.

`plan-to-merged-pr` (a plain string is taken as `concern`):

```js
{
  concern: 'what to change',                    // for planner 'plan-proposal': the steps in scope for this lane
  planner: 'plan',                              // or 'plan-proposal'
  proposal_number: 148,                         // planner 'plan-proposal': the skill's argument
  proposal: 'docs/proposals/…/148-….md',        // frames the review for a proposal lane
  context: 'scouting hints for the planner',
  dir: '/abs/path/to/checkout',                 // work only here; created from the last base if absent
  branch: 'feat/…',                             // use this branch instead of letting the planner create one
  bases: ['feat/149-step5-…'],                  // stacked-on branches, ordered so the last contains the others; needs `branch`,
                                                //   and, unless ship: false, `predecessor_prs` (a ready PR must not carry unmerged bases)
  ship: true,                                   // false = build only (push, no PR)
  ship_only: false, existing_pr: 4650,          // ship-only: open, or update + ready, the PR
  predecessor_prs: [4642, 4646],                // must read MERGED before any PR is opened or readied
  title: 'PR title', pr_note: 'extra PR-body text, e.g. an operator waiver',
  gate: './scripts/gate-slot.sh',
  commit_trailers: ['Co-Authored-By: …'],       // the workflow's own commits (create-pr keeps its own trailer)
  max_review_rounds: 5, max_polish: 6, max_monitor_rounds: 10,
  efforts: { plan: 'medium', impl: 'low', review: 'high', fix: 'low', polish: 'low', merge_check: 'low', monitor: 'low' },  // pr unset = session effort
}
```

`proposal-build-chain`:

```js
{
  lanes: [{
    key: 'p148a',
    dir: '/abs/path/to/worktree',
    branch: 'feat/148-steps5-7-…',
    bases: ['feat/149-step5-…'],
    proposal: 'docs/proposals/core/148-….md',
    number: 148,
    concern: 'Steps 5, 6 and 7 as one plan: …',
  }],
  gate, commit_trailers, max_review_rounds, max_polish, efforts,  // passed through to every lane
}
```

Results carry a `status`: `pushed` (build-only); `failed_at_plan`,
`failed_at_impl`, `review_failed` (a review or fix stage did not report),
`blocked_at_review` (defects left at the cap), `blocked_at_predecessors`,
`failed_at_merge_check`, `blocked_at_merge_check` (gates not green),
`failed_at_push`, `blocked_at_pr`; or, after monitoring, the PR's last observed
state (`OPEN` / `MERGED` / `CLOSED`) or `monitor_failed`. A build chain lane
that throws reports `failed`. To resume a stopped run, re-invoke with the same args and
`resumeFromRunId`; completed stages replay from cache.
