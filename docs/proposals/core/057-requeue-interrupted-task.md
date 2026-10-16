# 057: Re-queue a Task Interrupted by Graceful Shutdown

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | superseded |
| **Superseded by** | [085](085-run-task-completes-before-shutdown.md) — `run_task` now finishes the in-flight task before shutdown (issue #2249 option **(A)**, drain), so the re-queue mechanism below was removed. |
| **Derived from** | private issue #2249 — "Deployments interrupt in-progress tasks without drain or checkpoint" (auto-filed) |
| **Affects** | `maestro/agent.py`, `maestro/task_queue.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro/types.py`, `maestro.toml`, `docs/loop-lifecycle.md`, `CLAUDE.md`, tests |
| **Depends on** | _(none — adjacent to 006, see below)_ |

## Description

> **Superseded by [085](085-run-task-completes-before-shutdown.md).** This proposal's
> mid-task interrupt + re-queue (the `_run_shutdown_interrupted` flag, the non-persisted
> `interrupted` `TaskOutcome`, `TaskQueue.requeue_head` / `QueuedTask.requeue_count`, and
> `[loop] max_task_requeues`) was retired: `run_task` now runs to completion on graceful
> shutdown like every other phase. The mechanism described below no longer exists in the
> tree; the text is retained as the historical decision record.

> Status updated to shipped on 2026-05-28 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

When a deploy (or any `docker compose stop`) sends SIGTERM mid-task, the `run_task`
phase's executor loop breaks between step groups. The work item was already popped
from its source queue *before* `run()` was called, so the interrupted task is **lost**:
it is recorded as a `partial`/`failure` outcome, the lifecycle cursor advances past
`run_task`, and the task never runs again. A routine deploy silently drops whatever
self-directed, user-, or PR-remediation task happened to be in flight.

This proposal makes a graceful-shutdown interruption a *non-event* for the task's
verdict: the partial branch is discarded, no permanent `TaskRecord` is written, the
interruption is journaled, and the original work item is restored to the **front** of
its source so the next iteration's `run_task` re-runs it cleanly. It also stops `run()`
from spending SDK budget on `_distill_findings` / `_assess_criteria_outcome` *after*
shutdown was requested — work that currently runs inside the stop-grace window and
delays container exit.

This is distinct from [006 Task Checkpointing](006-task-checkpointing.md), which
proposes *per-step* resume within a task. Here the unit is the whole task: on graceful
interruption it is re-run from scratch, not resumed mid-step. The two compose — 006
would later let the re-run skip already-completed steps — but this change stands alone.

### Relation to issue #2249's options

Issue #2249 enumerates four candidate approaches — **(A)** a pre-SIGTERM *drain* signal
that lets the current task finish before shutting down, **(B)** per-step
*checkpoint/resume*, **(C)** a *deploy-script* that polls `lifecycle.json` `current_phase`
before sending SIGTERM, and **(D)** *preserve the branch* on interruption with a new
`interrupted` status. This proposal is a fifth, narrower option: re-queue the whole work
item and re-run it cleanly on the next iteration. It is preferred over **(A)**/**(C)**
because it needs no bounded drain timeout and no deploy-script coupling — `run_task` runs
every live iteration gated only by `_has_runnable_work()` (see findings), so the restored
item is picked up immediately on restart — and over **(B)** because it requires no
per-step state machine. It reaches **(D)**'s goal (no lost work) by clean re-run rather
than branch preservation, so no orphan branch survives. It does borrow **(D)**'s
`interrupted` `TaskOutcome` literal, but only as a non-persisted return value.

## Relevant Findings

_Anchors verified against `main` at HEAD (2026-05-24, the day #2249 was filed); all citations below resolve cleanly with no drift._

- `maestro/agent.py` — inside `run()`, the step-group loop breaks when the
  shutdown event is set: `while group_idx < len(groups): if self._ensure_shutdown_event().is_set(): break`. The in-flight step's SDK call is never cancelled; remaining steps are left `pending`.
- `maestro/agent.py` — `_run_task_phase` pops the work item from its source
  (`pending_user_requests` → `pending_pr_requests` → `task_queue.pop_head()`) and saves
  the model/queue *before* awaiting `run()`. The return value of `run()` is discarded
  (`await self.run(...)`).
- `maestro/agent.py` — after the loop, outcome is classified from step
  statuses; a shutdown break yields `partial` (some done) or `failure` (none done).
- `maestro/agent.py` — `_assess_criteria_outcome` and
  `_distill_findings` are SDK calls that still run after a shutdown break, inside the
  grace window.
- `maestro/agent.py` — on `partial`/`success` the branch is committed and
  *kept* (`kept_branch = branch_name`) **only when `self._vcs.has_changes()` is true**
  (uncommitted edits at the break). If the tree is clean at the break — e.g. completed
  steps were already committed — `kept_branch` stays `None` and the outer `finally`'s
  `_teardown_task_branch()` *deletes* the branch (`kept_branch is None and
  not preserve_branch`). So today's branch fate on a shutdown break is split: uncommitted
  work is committed onto an orphan branch, while already-committed intermediate work can
  be deleted — exactly #2249's "loses committed intermediate work." Re-queue side-steps
  both: it always discards the partial branch (plan step 2) and re-runs cleanly.
- `maestro/agent.py` — the loop checks shutdown only *after* the phase body
  returns and `_persist_progress(iteration, next_phase)` writes the cursor to the phase
  *after* `run_task`; on restart the loop resumes past `run_task`, so an interrupted
  task is not retried that iteration.
- `maestro/agent.py` — `run_task` runs every live iteration
  gated only by `_has_runnable_work()` (no `task_every` cadence). A re-queued item is
  therefore guaranteed pickup on the next iteration's `run_task`.
- `maestro/agent.py` — `_on_shutdown_signal` only sets the event and writes
  `lifecycle.json`; it never cancels the running task. Shutdown is cooperative.
- `maestro/task_queue.py` — `QueuedTask` dataclass and `pop_head()`;
  `load()` filters unknown keys and defaults missing ones, so a new field is
  backward-compatible without migration. No front-insert helper exists today.
- `maestro/consciousness.py` — `PendingPRRequest` dataclass;
  `pending_user_requests: list[str]` and `pending_pr_requests: list[PendingPRRequest]`.
- `maestro/types.py` — `TaskOutcome = Literal["success", "partial", "failure"]`.
- `docker-compose.yml` — loop uses `stop_signal: SIGTERM` with
  `stop_grace_period: 1800s`; the container exits as soon as `run_loop` returns.

## Implementation Plan

1. [x] **Interruption flag on `run()`.** `maestro/agent.py` — add `self._run_shutdown_interrupted: bool` (init `False` in `__init__` and at `run()` entry). Set it `True` at the step-loop break. This is the unambiguous "loop broke due to shutdown" signal, distinct from a malformed-dependency-graph `pending` step.
   - *Depends on:* none.
   - *Acceptance:* `self._run_shutdown_interrupted` is `True` after `run()`'s executor loop breaks on a set shutdown event, and reset to `False` on entry to every `run()` call.
2. [x] **Short-circuit the SDK-spending tail.** `maestro/agent.py` — after the executor `finally`, when `_run_shutdown_interrupted`: skip outcome classification, `_assess_criteria_outcome`, `_distill_findings`, `record_task`, and the success/partial notify; write one journal entry (`entry_type="reflection"`, `triggered_by=f"shutdown_interrupt:{task_id}"`); leave `kept_branch = None` so the outer `finally`'s `_teardown_task_branch()` deletes the partial branch; return a `TaskRecord` with outcome `"interrupted"`.
   - *Depends on:* 1, 3.
   - *Acceptance:* when `_run_shutdown_interrupted` is `True`, `run()` returns a `TaskRecord` with outcome `"interrupted"`, none of `_assess_criteria_outcome` / `_distill_findings` / `record_task` is called, and the partial branch is torn down by the outer `finally`.
3. [x] **Add the `interrupted` outcome.** `maestro/types.py` — extend `TaskOutcome` to `Literal["success", "partial", "failure", "interrupted"]`. It is only ever a *return* value here — never persisted via `record_task` — so historical-record load validation (`tests/test_agent.py`) and downstream persisted-history consumers (`purpose.investigation_rate`, health/audit no-op-rate) are unaffected. Adding the literal lets any future consumer exclude it explicitly rather than miscount a deploy as a failure.
   - *Depends on:* none.
   - *Acceptance:* `TaskOutcome` includes `"interrupted"` as a valid literal, and no `record_task` call writes that value.
4. [x] **Front-insert + bounded retry on the queue.** `maestro/task_queue.py` — add `requeue_count: int = 0` to `QueuedTask`; add `requeue_head(self, task: QueuedTask, *, max_requeues: int) -> bool` that increments `requeue_count`, refreshes `last_touched_at`, and — if `requeue_count > max_requeues` — returns `False` without enqueuing; otherwise `load() → insert(0) → save()` and returns `True`.
   - *Depends on:* none.
   - *Acceptance:* `requeue_head` front-inserts and returns `True` while `requeue_count <= max_requeues`, returns `False` without enqueuing once over the cap, and increments `requeue_count` on every call.
5. [x] **Re-queue in `_run_task_phase`.** `maestro/agent.py` — keep a reference to the popped source object (the `QueuedTask`, the `goal` string, or the `PendingPRRequest`) and its origin; capture `record = await self.run(...)`. When `self._run_shutdown_interrupted`, restore to the front of the source: self_directed → `task_queue.requeue_head(head, max_requeues=...)` (log + drop if capped); user request → reload model, `pending_user_requests.insert(0, goal)`, save; PR request → reload model, `pending_pr_requests.insert(0, entry)`, save. Log the re-queue at INFO.
   - *Depends on:* 1, 4.
   - *Acceptance:* after an interrupted `run()`, the popped item is restored to index 0 of its source (self_directed queue, `pending_user_requests`, or `pending_pr_requests`); self_directed re-queues are bounded by `max_task_requeues`.
6. [x] **Config.** `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml` — add `[loop] max_task_requeues` (default `1`). One re-queue covers a single deploy mid-task; the cap prevents a deterministic re-interrupt loop. External requests carry no counter field and are re-queued unconditionally (human/PR-visible, rare) — documented asymmetry.
   - *Depends on:* 5.
   - *Acceptance:* `[loop] max_task_requeues` loads from `maestro.toml` with default `1`, is exposed on `Config`, and is consumed by the re-queue path in `_run_task_phase`.
7. [x] **Docs.** Update `docs/loop-lifecycle.md` (shutdown / `run_task` section) and add a one-line invariant to `CLAUDE.md` self-evolution bullets; document `max_task_requeues` in `maestro.toml`.
   - *Depends on:* 5, 6.
   - *Acceptance:* `docs/loop-lifecycle.md` and `CLAUDE.md` describe the graceful-shutdown re-queue invariant, and `maestro.toml` carries an inline comment for `max_task_requeues`.

## Testing Strategy

All tests use `mem_fs` and mocked SDK/executor per CLAUDE.md (no real I/O, network, or `sleep`).

- **`run()` interruption** — mock executor so the shutdown event is set between groups; assert `_run_shutdown_interrupted` is `True`, the returned outcome is `"interrupted"`, `_distill_findings` / `_assess_criteria_outcome` / `record_task` are **not** called, and the branch is **not** kept (teardown deletes it).
- **`_run_task_phase` re-queue per source** — self_directed head returns to index 0; user-request string returns to `pending_user_requests[0]`; `PendingPRRequest` returns to `pending_pr_requests[0]`.
- **No re-queue on clean completion** — all steps `done`; flag stays `False`; nothing re-queued even if the event is set after the loop.
- **No re-queue on genuine failure** — steps `failed` without shutdown; flag `False`; not re-queued.
- **Re-queue cap** — a `QueuedTask` at `requeue_count == max_task_requeues` is dropped (not enqueued) and logged; `requeue_head` returns `False`.
- **`TaskQueue.requeue_head`** — front-insert ordering, `requeue_count` bump, `last_touched_at` refresh, and the cap return value.

## Risk Assessment

- **Double-run / orphan branches** — mitigated: the interrupted run writes no `TaskRecord` and discards its branch, so the clean re-run is the only persisted attempt and leaves no orphan.
- **Infinite re-queue loop** — mitigated by `max_task_requeues` (default 1) for self_directed tasks. External requests lack a counter; the residual risk is a user/PR item re-queued more than once across repeated deploys mid-task — bounded by deploy frequency and human-visible.
- **`run()` raises during shutdown** (in-flight step throws a transient `SDKError`) — control never reaches the re-queue line and the item is lost, then `run_loop`'s resume-in-place re-enters `run_task` against the next item. This is pre-existing behavior and **out of scope**; a follow-up could move the re-queue into a `try/finally` in `_run_task_phase`.
- **Rollback posture** — `max_task_requeues = 0` disables re-queuing (every interrupted task is dropped, today's behavior) without code revert. The `interrupted` literal and short-circuit are inert when no interruption occurs.

## Migration

`QueuedTask.requeue_count` is additive and defaults to `0`; `TaskQueue.load()` already
filters unknown keys and defaults missing ones, so pre-existing `task_queue.json`
entries load unchanged with no schema-fingerprint rewrite required. The new
`interrupted` `TaskOutcome` value is never written to `task_history.jsonl`, so no
historical-record migration is needed.

## Out of scope

Per-step resume within a task (that is [006](006-task-checkpointing.md)); re-queue on
the transient-error / crash path (only graceful, cooperative shutdown is handled here).
