# CI/CD and Code Review

## CI Runner Placement

CI runs on GitHub-hosted runners (`runs-on: ubuntu-latest`). The only workflow
is `quality-gates.yml`, with three jobs — `quality`, `browser-tests` and
`package-quality` — triggered on pushes and pull requests to `main` (plus the
`merge_group` and `workflow_dispatch` events). It needs no repository secrets
and no self-hosted resources, so it runs unchanged on forks. Every job sets up
Python 3.14 through the `.github/actions/py-setup` composite
(`actions/setup-python` plus a pinned uv), then installs dependencies with
`uv pip install` — the same `pip install -e ".[dev,email]"`, `maestro_sdk/` and
`maestro_review/` installs the [Quality Gates](../CLAUDE.md#quality-gates)
section runs by hand. The `browser-tests` job runs
`playwright install --with-deps chromium` before the browser suite.

No CI job deploys. While Maestro was deployed (it is archived and no longer
runs anywhere), production deploys were applied by a host-local systemd deploy
agent (`scripts/deploy-agent.sh`) that polled the repository's `production`
GitHub deployment records and ran `scripts/safe-deploy.sh` locally — owning the
≤30-minute graceful-stop window, the deployment-status lifecycle, and the
Telegram alerting. This repository ships no workflow that publishes those
records; an operator creates one by hand or from a deploy workflow of their own.
See §Continuous Deployment in [deployment.md](deployment.md) and
[deploy/systemd/README.md](../deploy/systemd/README.md#the-deploy-agent-executable-units).

`deploy/placement.toml` is the checked-in source of truth for placement: which
workload runs on which tier, and which `runs-on:` label routes a CI job there.
`tests/test_workload_placement.py` enforces it — the build reds, naming that
file, if any CI job is pointed back at a production-host runner.

All three `quality-gates.yml` jobs (`quality`, `browser-tests`, `package-quality`)
now start in parallel — none declares a `needs` clause. The old `browser-tests`
and `package-quality` fan-in behind `quality` existed only to ration the single
self-hosted runner slot that used to be the bottleneck; with a runner per
job (and neither sibling package even depends on the `maestro` package `quality`
checks), that serialization bought nothing but a delay to full CI feedback.
Dropping it is a deliberate trade: the heavy jobs now also run when `quality` is
red, spending some extra runner time to learn every failure in one pass rather
than sequentially.

The `quality` job runs the core test suite across cores with
`pytest -n auto --dist loadfile` — `--dist loadfile` keeps a file's tests on one
worker so the module-scoped whole-tree fixtures are built once, roughly halving
wall clock. A failed parallel run is classified by `maestro gate-classify`, the
same owner the shell runner's lease path uses: a resource artifact — an
OOM-killed worker's crash report or lost coverage, which would trip the 100% gate
spuriously, or a timeout — earns one serial re-run whose result is
authoritative, while a red that already carries its answer (a test failure or
collection error) fails at once rather than spending the job timeout repeating
itself serially. That contract — the scheduler flag and the classified serial
fallback — is pinned by `tests/test_quality_gate_scheduler.py`, which owns it for
the shell runner (`scripts/quality-gates.sh`), the hand-run gate docs, and this
CI step alike, so the workflow and the shell runner cannot drift apart.

## AI Code Review — Claude Agent SDK

The standalone `maestro_review` package (`pip install -e "maestro_review/"`)
provides the `maestro-review` CLI, an automated pull-request reviewer. This
repository's own CI does not run it — it needs a Claude credential as a
repository secret — but an operator can add a workflow that runs it on every
pull request. It analyzes the PR diff with read-only tools via the Claude Agent SDK, then posts inline review comments on the PR via the GitHub PR Review API. Each finding includes a severity level, description, and either a concrete code suggestion (rendered as a GitHub "Apply suggestion" button) or a fix plan.

Before posting, findings go through two deduplication stages:

1. **Issue-tracked filtering**: Findings matching an open GitHub issue (by file path + title keywords) are skipped.
2. **Thread-aware deduplication** (via GraphQL): Existing review threads on the PR are fetched. Findings matching an **unresolved** thread or a **resolved** thread containing a GitHub issue link are skipped. Findings matching a **resolved** thread without an issue link (i.e., a code-fix attempt that didn't work) trigger a reply explaining the defect persists.

The `maestro-review` CLI **fails (exit 1) whenever the reviewer produces a comment** — any finding of any severity (🐛 bug, 💡 suggestion, or ✏️ nit) that is newly posted or reopened — blocking the PR from merging until those comments are addressed. Findings already tracked by open issues, escalated to issues, or matching an existing review thread are not freshly produced and do not affect the exit code. Infrastructure failures (SDK crashes, API errors) also exit 1 so a broken review run surfaces in CI rather than silently passing. Authentication uses `CLAUDE_CODE_OAUTH_TOKEN` stored as a GitHub repository secret. A workflow running it requires `pull-requests: write`, `contents: read`, and `issues: write` permissions (`issues: write` is needed to create issues for nits and suggestions).

## Reply-Coverage Audit — Advisory, Read-Only

`maestro-review --require-comment-replies` switches the CLI to a check-only
mode: `PRReviewer.find_unreplied_comments` audits the PR's current comment state
and exits `1` if any of the reviewer's own findings — inline review threads or
thread-less overflow findings — still lack a reply. The audit makes **no Claude
call and posts nothing**; it needs only a `GITHUB_TOKEN` with read access. Run
inside a review workflow, it belongs *before* the diff review step, so it reads
the comment state as of this push before the reviewer posts this push's
findings. Maestro's own loop reads a `review` or `reply-coverage` check as
advisory via `ADVISORY_CHECK_NAMES` in `maestro/vcs.py`. See
[review-process.md](review-process.md) for the full semantics.

## Action Pins — Runtime Floor and Update Feedback

CI's action pins are governed on two axes, each owned by a test module.

*Runtime currency* — that a pinned major runs on a supported Node runtime rather
than a removed one — is enforced across every workflow and composite by
`tests/test_workflow_action_runtime.py`, which owns a `MIN_NODE_RUNTIME` floor and
reds on any below-floor pin, so a stale major is a red build rather than a surprise
outage. The first-party `setup-python` pin the Python jobs share (and the
`python-version` it sets) lives in exactly one place — the
`.github/actions/py-setup` composite — swept by that same floor, and the
single-source property (exactly one `setup-python` pin, under `.github/actions/`,
and exactly one `python-version`) is pinned by the same module.

*Update feedback* — learning a new action major exists before its deprecation
deadline — was closed by a weekly grouped Dependabot sweep (`github-actions` and
`pip`) while Maestro was developed. The archived release ships no
`.github/dependabot.yml`, so no update PRs are opened; a fork that resumes
development should restore one. `tests/test_dependency_constraints.py` still
keeps a pin that leaves its pyproject range red.

## Merge Queue — Serialize Landings, Test Against the Merge Tip Once

Under strict branch protection a PR can only land when its head is current with
`main` *and* carries a passing required-check run on that current head. So every
time `main` advances, every other open PR goes stale and must merge `main`
forward and re-run the full required suite to regain a landable head — with N
open PRs across M `main` advances, up to N×M required-check runs, almost all
superseded before their PR is ready. Now that CI is billed per runner-minute,
that re-run storm is a direct dollar cost rather than a free queue wait.

A GitHub **merge queue** removes branch-currency as a per-PR landing
precondition. An approved, green PR enters the queue; the queue builds a
temporary branch stacking `main` plus the queued PRs, runs the required suite
once against that merged tip, and merges on green. A behind PR is tested once, at
the front of the queue, against the state it will actually land into — not
re-tested on each `main` advance.

`quality-gates.yml` fires on the queue's **`merge_group`** event: that event is
listed among the workflow's `on:` triggers (alongside `push`, `pull_request`,
and `workflow_dispatch`), so the `quality`, `browser-tests`, and
`package-quality` jobs run on the queue's temporary branch exactly as they do on
a `push: [main]` run. The existing `concurrency` group already routes a
`merge_group` event to a per-`github.sha`, run-to-completion group (its
`cancel-in-progress` is `false` for any non-`pull_request` event), so each queue
entry runs to completion; that concurrency shape is owned by
`tests/test_quality_gates_concurrency.py`, and the `merge_group` trigger contract
by `tests/test_merge_queue_workflow.py`.

The trigger is **inert** until an operator enables the queue: `merge_group` never
fires until "Require merge queue" is turned on. Enabling it is a one-time
operator action on `main`'s branch protection, not a code change:

1. Enable **"Require merge queue"** on `main`.
2. Set the queue's **required status checks** to the `quality-gates.yml` jobs
   (`quality`, `browser-tests`, `package-quality`).
3. **Relax "require branches to be up to date before merging"** — the queue now
   tests each PR against the merged tip at the front of the queue, so per-PR
   branch currency is no longer the landing precondition it was.

The queue serializes the *human's* approved merges and runs CI once each; it
grants the loop no new landing path, because enqueuing still uses the merge
permission the loop is already action-gated against.

### Cutover & observation

> **Archived — no active deployment.** This subsection records the merge-queue
> cutover as it ran on the original private repository while the experiment was
> live; read its present-tense descriptions of production, branch protection and
> the observation window as historical. This repository has no production
> deployment and no merge queue enabled.

Production had cut over to the queue-serialized model: the operator had enabled
"Require merge queue" on `main` and relaxed strict branch currency (the three
steps above), and `maestro.production.toml` sets `tend_main_merge_enabled =
false` in `[loop]`, so the tend phase no longer eagerly merges `origin/main`
into behind PRs and re-pushes on every `main` advance — the redundant
per-`main`-advance re-run storm collapses. Base `maestro.toml` keeps the flag
`true`, so every non-production environment is unchanged and the **kill-switch
is one line**: delete the `tend_main_merge_enabled = false` override (and, if
needed, re-tighten branch protection) to revert production to eager resync.
Config is read once at process start (`Config.from_env`), so the override — and
any reversion — takes effect only on the next loop restart, never mid-run.

Observe over a bounded window, measured since this PR's production merge, and
roll back if any tripwire trips:

- A newly landed PR's check history shows **one** `merge_group` required-check
  run and **no** per-`main`-advance re-run of the required suite.
- No new `maestro: merge main into <branch>` merge commits appear on open PR
  branches, and the tend result's `merged_updated` stays `0` for behind PRs.
- Median approve-to-land latency does not rise materially week-over-week.

Retiring the now-dead machinery proceeds incrementally now that this window has
closed cleanly: `auto-update.yml` has been removed. The eager-merge branch in
`_tend_single_pr` and the `tend_main_merge_enabled` flag itself remain as later
increments.

## AI Comment Processor — Local, Plan-Aware (`/process-comments`)

The comment processor no longer runs in CI — neither the `process-comments` job (removed from the former code-review workflow) nor the standalone `comment-processor.yml` workflow (deleted) exist. It is now invoked locally, addressing every unresolved bug comment on the PR in one run, via the `/process-comments` slash command (`.claude/commands/process-comments.md`). The command runs on the PR branch and loads the prior session's implementation plan (via the `~/.claude/last-plan-by-folder.json` pointer, the same one `/impl` reads) so each defect is fixed with the original change's intent and reuse map in view — context the CI agent never had. It stops before commit/push; the user ships with `/commit-push`.

The standalone `maestro-process-comments` CLI (`CommentProcessor` in `maestro_review/processor.py`) that once backed this flow has been retired (proposal 063); the steps below describe the validate → fix → reply discipline the command follows when processing unresolved AI review threads:

1. **Fetch**: Retrieves unresolved review threads from the PR via GraphQL.
2. **Filter**: Identifies AI-posted comments by their emoji+bold-title format, skipping human comments.
3. **Validate**: For each AI comment, proves the defect — first by writing a failing test, then by static analysis reasoning when a test is impractical.
4. **Fix valid defects**: Applies a minimal fix, keeping the failing test from validation as the fix's regression test. Runs quality gates (ruff, mypy, pytest) after each fix. If gates pass, commits the fix and replies with an explanation.
5. **File issues for invalid findings**: Creates a GitHub issue (labels: `ai-review`, `needs-triage`) linking the comment details and validation reasoning, then replies with the issue link.
6. **Push once**: After all comments are processed, pushes committed fixes to the PR branch if final quality gates pass.

Running it requires `contents: write`, `pull-requests: write`, and `issues: write` permissions. Issue creation uses the GitHub REST API (`gh api repos/.../issues --method POST`) instead of `gh issue create` because the latter uses GraphQL internally, which fails with the automatic `GITHUB_TOKEN` in GitHub Actions (`Resource not accessible by integration`).

## Package Quality Gates

A single `package-quality` job in `quality-gates.yml` runs lint, type checking, and tests for **both** sibling packages — `maestro_review/` then `maestro_sdk/` — in one runner. It checks out once, sets up Python once, installs both packages in one step (`uv pip install -e "maestro_review/[dev]" -e "maestro_sdk/[dev]"` — neither pulls in the main `maestro` package), then runs the two gate sets in sequence. It declares no `needs` clause, so it starts in parallel with the `quality` and `browser-tests` jobs in its own ephemeral runner container rather than fanning in behind the fast gate (see §CI Runner Placement for the deliberate runner-time-for-wall-clock trade). Because the gate sets share a job, a red `maestro_review` step fails the job before the `maestro_sdk` steps run (fail-fast within the job):

```bash
# maestro_review gates (from the repo root)
ruff check maestro_review/ tests/test_maestro_review/
mypy maestro_review/
pytest tests/test_maestro_review/ -v --no-cov

# maestro_sdk gates (working-directory: maestro_sdk)
ruff check src/ tests/
mypy src/
pytest tests/ -v
```
