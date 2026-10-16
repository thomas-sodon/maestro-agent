# 086: Crash-Durable In-Flight Task Recovery — Surviving a Sudden Halt

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | proposed |
| **Affects** | `maestro/task_queue.py`, `maestro/loop/task_generation.py`, `maestro/agent.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro/types.py`, `maestro.toml`, `docs/loop-lifecycle.md`, `CLAUDE.md`, tests |
| **Depends on** | _(none)_ |
| **Derived from** | private issue #2249 — follows the 057→085 lineage; closes the **unclean-exit** path 085 left out of scope |

## Description

085 made `run_task` finish its in-flight task before a *graceful* shutdown, so a within-grace SIGTERM now records a real outcome instead of discarding work. That is the right behavior for the graceful path — but it deliberately leaves the **unclean-exit** path uncovered (085 lists "re-queue on the transient-error / crash path" as out of scope). A sudden halt — SIGKILL, OOM-kill, power loss, kernel panic, or a task that exceeds `stop_grace_period` and is SIGKILLed — still loses the popped work item with no recovery.

The retired 057 re-queue could never have covered this. Its restore ran *in-process* on graceful SIGTERM; a sudden halt kills the process before that code executes, and the work item is already popped-and-persisted off the queue by then, so re-queue was structurally incapable of helping the sudden-halt case. Reviving it would not close the gap. The only durable fix is one whose write happens **before/during** the run rather than at exit — write-ahead, not restore-on-exit.

This proposal adds a **leased pop**: `run_task` marks the head item in-progress in durable state instead of deleting it, and clears the lease only after the task records a real outcome. On the next startup, `run_loop` re-admits any still-leased item to the front of its source — the durable analogue of the 057 re-queue, but one that survives SIGKILL because the durability is write-ahead. Re-admission re-runs the task from a clean branch (matching 085's "record a real outcome, don't half-apply"), is bounded by a per-item counter to prevent a poison-task re-interrupt loop, and is guarded by merge-truth so a task that actually landed just before the crash is not blindly re-run. The end state: every in-flight task survives any unclean exit, recovered automatically at startup, with no human `resume-task` step.

## Relevant Findings

_Anchors verified against this branch at HEAD (2026-06-20); cite symbols, not line numbers._

- `maestro/task_queue.py` (`TaskQueue.pop_head`) — `load()` → `save(items[1:])` → return head. The pop is persisted off the queue **before** the task runs, so from the instant a task starts the work item exists only in an in-process variable; an unclean exit loses it.
- `maestro/loop/task_generation.py` / `maestro/agent.py` (`_run_task_phase`) — pops the self-directed head, or `pending_user_requests[0]`, or a `pending_pr_requests[0]` entry (each `pop` immediately `save`d), then mints a **fresh** `run()` with a new uuid; it never reuses a prior `task_id`, so the autonomous loop never re-enters a crashed task.
- `maestro/agent.py` (`run_loop` startup orphan sweep) — calls `Memory.cleanup_stale_checkpoints(checkpoint_max_age_days)`, which only **deletes** checkpoints older than the age bound. It never resumes a still-fresh checkpoint, so an orphan left by a sudden halt is silently abandoned until it ages out.
- `maestro/agent.py` (`run`, `resume_task`) and `maestro/cli.py` (`cmd_resume_task`, `maestro resume-task`) — proposal 006 checkpoint-resume exists, but the only trigger is the **human-driven** `maestro resume-task <id>` CLI; `run`'s load-on-entry only fires when something calls it with the same `task_id`, which the loop does not do after a crash.
- `maestro/agent.py` (`resume_task` docstring) — checkpoint recovery is **forward-only**: completed steps are *skipped*, not replayed, and their working-tree edits are **not** restored (steps commit only at end-of-run). So checkpoint-resume recovers step *decisions*, not file state — which is why this proposal re-runs from a clean branch rather than auto-resuming a checkpoint (see Alternatives).
- `docs/proposals/core/085-run-task-completes-before-shutdown.md` / `057-requeue-interrupted-task.md` — 085 retired the 057 re-queue (`requeue_head`, `QueuedTask.requeue_count`, `[loop] max_task_requeues`, the `interrupted` outcome) and lists the crash path as out of scope; 057's own risk note already flagged that a raise during shutdown loses the item. The re-queue only ever ran on the graceful path.
- `docker-compose.yml` — loop uses `stop_signal: SIGTERM` with `stop_grace_period: 1800s`; a task that exceeds the grace window is SIGKILLed — the same unclean-exit class as a crash, and the case 085's "Over-grace SIGKILL data loss" risk note accepted.

## Implementation Plan

_Phase 1 (lease + re-admission) is the load-bearing change; later phases harden it. All steps gate behind `[loop] max_task_readmissions`, default `0` (today's behavior), so intermediate steps are safe to ship._

### Phase 1 — Leased pop and startup re-admission

1. [ ] **Lease the head instead of deleting it.** `maestro/task_queue.py` — add a write-ahead lease: replace the destructive `pop_head` save with a `lease_head()` that marks the head item in-progress (a `leased_at` stamp + monotonically bumped `readmission_count`) and persists the queue with the item still present-but-leased, plus `clear_lease(task_id)` (drop a recorded outcome's leased item) and `leased_items()` (read still-leased entries). Keep the existing `pop_head` semantics available, or have `_run_task_phase` consult the lease — whichever keeps `load()`'s known-field allowlist the single migration seam.
   - *Depends on:* none.
   - *Acceptance:* `lease_head` returns the head and leaves a still-present leased entry in persisted JSON; `clear_lease` removes it; `leased_items` returns only leased entries; a pre-086 `task_queue.json` with no lease fields loads unchanged.
2. [ ] **Clear the lease only on recorded outcome.** `maestro/loop/task_generation.py` / `maestro/agent.py` (`_run_task_phase`) — lease the item at dequeue, and call `clear_lease` only after `run()` returns and `record_task` has written the `TaskRecord`. A crash anywhere in between leaves the lease set. Mirror the lease for the `pending_user_requests` / `pending_pr_requests` channels (a durable in-progress marker on the self-model side).
   - *Depends on:* 1.
   - *Acceptance:* a `run()` that records an outcome clears the lease; a `run()` interrupted before `record_task` leaves the lease set; the external/PR channels carry the same marker.
3. [ ] **Re-admit leased items at startup.** `maestro/agent.py` (`run_loop`) — after the existing `cleanup_stale_checkpoints` sweep, re-admit any still-leased item whose `readmission_count <= max_task_readmissions` to the front of its source (the durable analogue of the 057 re-queue), incrementing the counter; drop and log (escalate) one that exceeds the bound, so a deterministic re-interrupt loop cannot spin forever.
   - *Depends on:* 2, 5.
   - *Acceptance:* with `max_task_readmissions >= 1`, a leased item is restored to its source head on the next `run_loop` start; an item already at the bound is dropped (not re-admitted) and surfaced via `_notify_best_effort`.

### Phase 2 — Don't re-run what already landed

4. [ ] **Merge-truth guard on re-admission.** `maestro/agent.py` (`run_loop` re-admission) — before re-admitting, consult the existing merge-truth oracle (`_branch_merged_into_main` / `_host_or_git_pr_merged`) for the leased item's branch/PR reference; if the work already landed, clear the lease and skip re-admission rather than re-running it. This gives at-least-once admission with a landed-work exclusion, matching the dequeue-face merged-PR guards.
   - *Depends on:* 3.
   - *Acceptance:* a leased item whose branch is merged into `origin/main` is lease-cleared and **not** re-admitted; an unlanded leased item is re-admitted.

### Phase 3 — Config, observability, docs

5. [ ] **Config knob.** `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml` — add `[loop] max_task_readmissions` (default `0` → feature off, today's behavior; `1` covers a single crash mid-task; the cap prevents a poison-task loop, the same concern 057's `max_task_requeues` addressed).
   - *Depends on:* none.
   - *Acceptance:* `[loop] max_task_readmissions` loads with default `0`, is exposed on `Config`, and gates the re-admission path.
6. [ ] **Docs.** Update `docs/loop-lifecycle.md` (shutdown / crash-recovery section) and the `CLAUDE.md` shutdown bullet (extend the 085 line to note write-ahead leasing covers the unclean-exit path); cross-link 085 ⇄ 086.
   - *Depends on:* 1–5.
   - *Acceptance:* docs describe leased-pop crash recovery and the at-least-once/idempotency tradeoff; `tests/test_claude_md_brevity.py` and `tests/test_no_line_citations_in_docs.py` pass.

## Testing Strategy

All tests use `mem_fs` and mocked SDK/executor/VCS per CLAUDE.md (no real I/O, network, `sleep`, or subprocess).

- **Lease round-trip** — `lease_head` leaves a present-but-leased entry; `clear_lease` removes it; `leased_items` returns only leased entries; backward-compat load of a pre-086 queue.
- **Lease cleared on recorded outcome** — `_run_task_phase` clears the lease after `record_task`; a `run()` raising before `record_task` leaves it set (each source channel).
- **Startup re-admission** — a leased item is restored to its source head with `readmission_count` bumped; an at-bound item is dropped and escalated; `max_task_readmissions = 0` re-admits nothing.
- **Merge-truth exclusion** — a leased item whose branch the merge-truth oracle reports merged is lease-cleared and not re-admitted; an unlanded one is re-admitted (mock the oracle both ways).
- **Config** — `max_task_readmissions` loads, defaults to `0`, and gates the path.
- **Migration** — a `task_queue.json` lacking lease fields loads unchanged (allowlist drops nothing it does not know).

## Risk Assessment

- **At-least-once re-run (duplicate work).** A task that finished `run()` but crashed *between* `record_task` and `clear_lease` re-runs once on restart. Mitigated by the Phase 2 merge-truth guard (landed work is excluded) and the `max_task_readmissions` cap; residual exposure is a re-run of unlanded, non-idempotent work — the standard at-least-once tradeoff, and strictly better than today's silent loss. Document the task-level idempotency expectation.
- **Poison task re-interrupt loop.** A task that deterministically crashes the process would re-admit forever; bounded by `max_task_readmissions` (default `0`, recommended `1`), which drops-and-escalates at the cap — the same defense `057` gave `max_task_requeues`.
- **Re-introducing a per-item counter 085 removed.** `readmission_count` resembles the retired `requeue_count`, but the mechanism differs in the way that matters: it is a write-ahead lease that survives SIGKILL, not an exit-time restore that only ran on graceful SIGTERM. Called out so the lineage is honest.
- **Rollback posture.** `max_task_readmissions = 0` disables re-admission (leases are still written but never acted on — inert), so the behavior is gated without a revert. Full revert is the PR.

## Alternatives considered

- **Auto-resume the orphaned 006 checkpoint at startup** (instead of re-running from scratch). Cheaper — it reuses `resume_task`/`run(task_id=...)` and the existing `checkpoints/` machinery — but checkpoint recovery is **forward-only**: completed steps are skipped, not replayed, and their working-tree edits are not restored. Resuming therefore runs later steps against a tree missing the earlier steps' file changes, which is only safe for idempotent/independent steps. Re-running from a clean branch (this proposal) avoids that incoherence at the cost of redoing work. The two **compose**: a future step could prefer checkpoint-resume when steps are marked idempotent and fall back to clean re-run otherwise.
- **Revive the 057 re-queue.** Rejected: structurally incapable of covering a sudden halt (its restore is in-process, exit-time), so it would not close the gap this proposal targets.

## Out of scope

Per-step working-tree restoration / replaying committed intermediate edits ([006](006-task-checkpointing.md)); changing the container `stop_grace_period` ([085](085-run-task-completes-before-shutdown.md)); leasing across multiple concurrent loop processes (single-process loop assumed).
