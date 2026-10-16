# 085: `run_task` Completes the In-Flight Task Before Shutdown

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Supersedes** | [057](057-requeue-interrupted-task.md) — retires the mid-task interrupt + re-queue |
| **Derived from** | private issue #2249 option **(A)** — a pre-SIGTERM *drain* that lets the current task finish before shutting down |
| **Affects** | `maestro/agent.py`, `maestro/task_queue.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro/types.py`, `maestro/remediation_tracker.py`, `maestro.toml`, `docs/loop-lifecycle.md`, `CLAUDE.md`, tests |
| **Depends on** | _(none)_ |

## Description

> Status updated to shipped on 2026-06-21 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

A graceful shutdown during a deploy should wait until the current phase is complete before
the restart — including the `run_task` phase. Every other loop phase already runs to
completion: the loop checks the shutdown event only at the phase boundary (after the phase
body), and the deploy waits up to the container's `stop_grace_period` (1800 s). `run_task`
was the lone exception — proposal 057 made `run()`'s step-group loop break mid-task on
SIGTERM, discard the partial branch, and re-queue the popped work item.

This proposal removes that exception. `run()` no longer consults the shutdown event between
step groups, so a SIGTERM mid-task lets the task finish, classify a real outcome, and write
its `TaskRecord`; the loop then stops at the phase boundary like any other phase. This is
issue #2249's option **(A)** (drain) in place of 057's option (E) (re-queue): finishing the
task records a real outcome instead of throwing away in-progress work and re-running it from
scratch.

Because the whole proposal-057 machinery existed only to support the mid-task re-queue, it
is removed in full (the repo forbids dead code — uncalled-symbol gate + 100 % coverage):
the `_run_shutdown_interrupted` flag, the `interrupted` `TaskOutcome` literal,
`TaskQueue.requeue_head` / `QueuedTask.requeue_count`, `[loop] max_task_requeues`, and the
`interrupted_requeue` admission channel. The end state is uniform: one shutdown rule for all
phases, gated by the grace period.

## Relevant Findings

_Anchors verified against `main` at HEAD (2026-06-18); cite symbols, not line numbers._

- `maestro/agent.py` (`run`) — the step-group loop breaks when the shutdown event is set
  and sets `self._run_shutdown_interrupted`; a following short-circuit returns a
  non-persisted `interrupted` `TaskRecord` and discards the partial branch.
- `maestro/agent.py` (`run_loop`) — after each phase body the loop persists the next-phase
  cursor and only then checks the shutdown event, breaking at the boundary. The in-code
  comment already states "The current phase always runs to completion before we exit"; this
  is the rule `run_task` will now also follow.
- `maestro/agent.py` (`_run_task_phase`, `_requeue_interrupted_task`) — captures the popped
  work item per origin and, on interruption, restores it to the front of its source.
- `maestro/types.py` (`TaskOutcome`) — carries `"interrupted"` solely as a `run()` return
  value; it is never persisted via `record_task`, and no outcome-classification code
  branches on it.
- `maestro/task_queue.py` (`TaskQueue.requeue_head`, `QueuedTask.requeue_count`) — exist
  only for the 057 re-queue; `load()` builds each `QueuedTask` from a known-field allowlist,
  so dropping `requeue_count` loads pre-existing `task_queue.json` unchanged.
- `maestro/remediation_tracker.py` (`PR_ADMISSION_CHANNEL_REQUEUE`,
  `measure_pr_readmission_channel`) — the `interrupted_requeue` channel is one of two
  admission seams, but the readmission sensor aggregates by PR number regardless of channel,
  so removing the seam leaves the sensor working on the surviving `tend_route` seam.
  `pr_number_from_remediation_goal` has another caller (`escalation_bridge`) and stays.
- `docker-compose.yml` — loop uses `stop_signal: SIGTERM` with `stop_grace_period: 1800s`;
  the container exits as soon as `run_loop` returns.

## Implementation Plan

_(All steps land together in the PR that adds this proposal.)_

1. [x] **Stop interrupting `run()` mid-task.** `maestro/agent.py` — remove the step-group
   shutdown check and the `_run_shutdown_interrupted` short-circuit so `run()` always flows
   into normal outcome classification + `record_task`; drop the `_run_shutdown_interrupted`
   field and `_run_task_phase`'s re-queue capture/branch (and `_requeue_interrupted_task`),
   plus the now-unused `PR_ADMISSION_CHANNEL_REQUEUE` / `log_pending_pr_provenance` /
   `pr_number_from_remediation_goal` imports.
   - *Depends on:* none.
   - *Acceptance:* with the shutdown event set before a step-group boundary, `run()` runs
     every group to completion and returns a `success`/`partial`/`failure` `TaskRecord`;
     nothing is re-queued.
2. [x] **Drop the `interrupted` outcome.** `maestro/types.py` — `TaskOutcome =
   Literal["success", "partial", "failure"]`.
   - *Depends on:* 1.
   - *Acceptance:* `TaskOutcome` has no `"interrupted"` member and `mypy` passes.
3. [x] **Remove the queue re-queue helpers.** `maestro/task_queue.py` — delete
   `requeue_head` and `QueuedTask.requeue_count` (and its deserialization normalization).
   - *Depends on:* 1.
   - *Acceptance:* `TaskQueue.load()` still loads a persisted entry that carries a stale
     `requeue_count` key (the allowlist drops it); no `requeue_head` symbol remains.
4. [x] **Remove the config knob.** `maestro/config.py`, `maestro/toml_config.py`,
   `maestro.toml` — delete `[loop] max_task_requeues` and its validation + mapping.
   - *Depends on:* 1.
   - *Acceptance:* `Config` has no `max_task_requeues` field and config load is unaffected.
5. [x] **Retire the `interrupted_requeue` admission channel.**
   `maestro/remediation_tracker.py` — delete `PR_ADMISSION_CHANNEL_REQUEUE` and reword the
   provenance comment/docstring to the single `tend_route` seam; keep
   `pr_number_from_remediation_goal` (used by `escalation_bridge`).
   - *Depends on:* 1.
   - *Acceptance:* the readmission sensor still counts re-admissions via the `tend_route`
     seam; no `interrupted_requeue` reference remains in production code.
6. [x] **Docs.** Update `docs/loop-lifecycle.md` (shutdown / `run_task` section), retire the
   `CLAUDE.md` re-queue bullet, mark 057 `superseded`, and add this proposal.
   - *Depends on:* 1–5.
   - *Acceptance:* docs describe `run_task` running to completion before shutdown and the
     grace-period tradeoff; `tests/test_claude_md_brevity.py` and
     `tests/test_no_line_citations_in_docs.py` pass.

## Testing Strategy

All tests use `mem_fs` and mocked SDK/executor per CLAUDE.md (no real I/O, network, `sleep`).

- **`run()` runs to completion despite shutdown** — set the shutdown event before/at a
  step-group boundary; assert every group runs, the returned outcome is a normal verdict
  (not `interrupted`), and `record_task` is called once.
- **`_run_task_phase` does not re-queue** — after an interrupted-but-completed run, the
  popped self-directed / user / PR item is **not** restored to the front of its source.
- **Queue backward-compat** — `TaskQueue.load()` ignores a stale `requeue_count` key in
  persisted JSON.
- **Removed-symbol regressions** — the existing 057 tests (interrupted outcome, re-queue per
  source, `requeue_head` cap, `max_task_requeues` config) are deleted, not left dangling.
- **Readmission sensor** — `measure_pr_readmission_channel` still counts re-admissions from
  `tend_route` provenance with the `interrupted_requeue` channel gone.

## Risk Assessment

- **Over-grace SIGKILL data loss** — a task that exceeds `stop_grace_period` (1800 s) is now
  SIGKILLed and its popped work item is lost, the same outcome as any crash mid-`run_task`.
  This reverts 057's protection for the over-grace case; accepted because the grace window is
  30 min and the common (within-grace) case is strictly safer — it records a real outcome
  instead of discarding in-progress work. Mitigation if needed is an ops choice: raise
  `stop_grace_period` in `docker-compose.yml`. Not changed here.
- **Rollback posture** — revert the PR. There is no config flag; the change is a pure
  removal, so the behavior is binary.

## Migration

`QueuedTask.requeue_count` removal needs no migration: `TaskQueue.load()` builds each entry
from a known-field allowlist, so a stale `requeue_count` key in `task_queue.json` is dropped
silently. The `interrupted` `TaskOutcome` was never persisted, so `task_history.jsonl` needs
no migration.

## Out of scope

Per-step resume within a task ([006](006-task-checkpointing.md)); changing the container
`stop_grace_period`; re-queue on the transient-error / crash path.
