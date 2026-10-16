# 040: Publish Dry-Run CLI

| Field | Value |
|-------|-------|
| **Impact** | LOWER |
| **Status** | proposed |
| **Pre-condition drift** | 033's `Publisher._stage(self, proposal, artifacts_dir, staging_dir)` shipped **without** the `host=None` / `runner=None` keyword-only optionals this proposal pre-supposed (see Relevant Findings). Implementer must either land those parameters as a precondition slice or call `_stage` against a `Publisher` whose `host`/`runner` collaborators are no-ops. |
| **Affects** | `maestro/cli.py`, `maestro/publication.py` (small `_stage` signature widening or no-op-collaborator wrapper — see Pre-condition drift above) |
| **Depends on** | 030 (shipped), 031 (shipped), 032 (shipped), 033 (shipped) |

## Description

Adds `maestro publish-dry-run <task_id>`: an operator/developer CLI that simulates 033's `Publisher._stage()` against a `tempfile.TemporaryDirectory()` and prints what the publisher *would* do for a candidate task — without `gh`, without `git push`, without any network call, and without filesystem mutation outside the temp dir. Output is the staging file tree, accumulated `scrub_findings`, and the first 2000 chars of the rendered `README.md`.

The motivation is operational: before flipping `[publication] enabled = true` on a fresh deploy, or before approving a borderline `pending_approval` proposal in the dashboard, Thomas wants to preview exactly what would be staged and committed. The dashboard's README preview (032) shows the LLM-written README, but not the *full* file scrub at publish time — the dry-run does. It also makes the publisher's most security-critical pure function (`_stage`) directly testable from a developer's shell against any persisted task.

This is intentionally LOWER impact: zero production surface area, zero network, no new persistence. It can land alongside or after 033 with no coordination cost.

## Relevant Findings

- 033's `Publisher._stage(p, artifacts_dir, staging_dir)` shipped without the proposed `host=None` / `runner=None` keyword-only optionals; the dry-run will need either to land those parameters as a precondition or to call `_stage` against a `Publisher` whose `host`/`runner` collaborators are no-ops. <!-- 033 shipped without the host/runner optionals; this slice is unblocked but the contract this proposal assumed is still missing. --> This proposal exercises that hook — the dry-run path is the second consumer of `_stage`, which means an accidental coupling to `host` or `runner` becomes a test failure rather than a runtime surprise at publish time.
- `maestro/cli.py` already follows an argparse-subcommand pattern (`maestro reviews`, `maestro publications`, `maestro history`). The dry-run subcommand follows the same shape: parser, handler, thin entry point in `main()`.
- `Memory.get_artifact_reviews(n=30)` returns the last *n* `ArtifactReview` rows in file order across **all** tasks — there is no `task_id` argument and no per-task filtering. Per-task selection is the caller's job: scan `Memory._load_all_reviews()` and filter by `task_id`, following the established "filter first, then tail" precedent in the `maestro reviews` verdict-filter path (`cli.py`). The dry-run uses the latest `verdict == "publish"` row for the task as the source of truth for `proposed_readme` and `publishable_files`.
- The existing `maestro publications` CLI handler is the closest formatting precedent: tabular for human reading, `--json` for scripts.
- 032's dashboard renders the LLM-written `proposed_readme` inside a `<details>` block, but does not show the result of running the full-file scrub against `publishable_files`. The dry-run is the only path that surfaces that pre-publish — useful before approving a borderline proposal.

## Implementation Plan

1. **Argparse subcommand** in `maestro/cli.py`:
   ```
   maestro publish-dry-run <task_id> [--json]
   ```
   The handler, in order:
   1. `task = memory.get_task(task_id)`. Missing or no `artifacts_dir` → exit code **1**, stderr `error=task_missing` or `error=no_artifacts_dir`.
   2. `reviews = [r for r in memory._load_all_reviews() if r.task_id == task_id]` (there is no `task_id`-filtered accessor — scan-and-filter, mirroring the "filter first, then tail" path at `cli.py`); pick the latest with `verdict == "publish"`. Missing → exit **1**, stderr `error=no_publish_verdict`.
   3. Synthesise an in-memory `PublicationProposal` (no JSONL write) carrying `proposed_name`, `proposed_description`, `proposed_readme`, `publishable_files`, `scrub_findings` from the review row.
   4. Inside a `tempfile.TemporaryDirectory()` context, instantiate a `Publisher` with `host=None` and `runner=None` (or call `Publisher._stage` directly — tied to whichever shape 033 lands), passing the task's `artifacts_dir` and the temp dir.
   5. Catch `PublicationBlockedError` from the staging scan: exit **2**, stderr `error=secret_scan_blocked:<preview>` or `error=binary_in_publishable_files:<rel>`. Distinct from exit 1 so CI/scripts can branch on "blocked vs. nothing to publish".
   6. On success: walk the staging dir, print the file tree (relative paths, sizes), then `scrub_findings`, then the first 2000 chars of `README.md`. With `--json`, emit a single JSON object with the same fields. Exit **0**.

2. **Exit codes.** `0` success, `1` lookup failures (task missing / no artifacts dir / no publish verdict), `2` hard-block during staging. `1` and `2` are distinct so a wrapper script can tell "no candidate" from "candidate but unsafe".

3. **No state mutation.** The dry-run never writes to `publication_proposals.jsonl`, never updates `artifact_reviews.jsonl`, never touches the journal, never sends Telegram. The temp dir is automatically cleaned by the context manager regardless of exit path.

4. **CLAUDE.md.** Add `maestro publish-dry-run <task_id>` to the CLI Usage table with a one-line description: *"Dry-run the publisher's staging step against a task's artifacts. No network, no fs writes outside a temp dir."*

5. **Recommended operator workflow.** Document in CLAUDE.md and the proposal landing PR: (a) run dry-run before flipping `[publication] enabled` on a fresh deploy to validate the bundle scan path against real persisted tasks; (b) run dry-run on any borderline `pending_approval` proposal as the last gate before clicking Approve in the dashboard. Both are advisory — the production publish runs the same checks regardless.

## Testing Strategy

1. **Happy dry-run** (`tests/test_cli.py` extension): fixture task with an artifact dir + a `publish` review → exit 0, stdout contains the staging tree (`README.md`, `LICENSE`, the publishable files), `scrub_findings` line, and the README preview. Asserts NO `subprocess.run` call (mocked), NO `gh` invocation, NO `git` invocation, NO write to `publication_proposals.jsonl`.
2. **Missing task** → exit **1**, stderr `error=task_missing`.
3. **Task with no `artifacts_dir`** → exit **1**, stderr `error=no_artifacts_dir`.
4. **Task with no `publish` review** → exit **1**, stderr `error=no_publish_verdict`.
5. **Hard-block during staging**: artifact contains `sk-ant-…` → exit **2**, stderr `error=secret_scan_blocked:…`. The temp dir is cleaned (verify the path no longer exists after the call returns).
6. **Binary file in `publishable_files`** → exit **2**, stderr `error=binary_in_publishable_files:<rel>`.
7. **`--json` output** parses cleanly with `json.loads` and contains the expected keys.

Run: `python3.12 -m pytest tests/test_cli.py -v -k publish_dry_run`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Dry-run leaks a credential to the operator's terminal | Low | `_stage` runs the same hard-block scan as production publish; output goes only to stdout under operator control. The README preview is bounded to 2000 chars and is sourced from `proposed_readme`, which is already 032-scrubbed. Redact-level findings are tagged (`redact_path:N`), not raw matched strings. |
| Dry-run drifts from the production `_stage` path | Low | The CLI calls `Publisher._stage` directly — there is no parallel implementation. A regression in `_stage` is detected by both the production tests (033) and the dry-run tests. |
| Operator confuses dry-run output for a real publish | Low | Output header explicitly labels the run `DRY RUN — no repo created, no commits made`. Exit codes documented in CLAUDE.md. |
