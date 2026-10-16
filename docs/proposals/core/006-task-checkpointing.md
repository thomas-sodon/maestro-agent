# 006: Task Checkpointing

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/agent.py`, `maestro/memory.py`, `maestro/config.py` |

## Description

> Status updated to shipped on 2026-06-17 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

If the process crashes mid-task, all completed step results are lost. The `TaskRecord` is written only after all steps complete. There is no per-step checkpoint mechanism — the existing `lifecycle.json` progress file handles **phase-level** resume (see the subsection below), but a single task running inside one phase still loses every completed step on a crash. For multi-step tasks that take minutes or longer, this means a crash after step 4 of 5 wastes all prior work. This proposal adds per-step checkpointing with crash recovery, complementing (not duplicating) the existing phase-level persistence.

## Relationship to phase-level persistence

Since this proposal was written, `run_loop()` grew phase-level progress persistence backed by `{memory_dir}/lifecycle.json`. `_LOOP_PHASES` in `maestro/agent.py` lists the ordered phases (eighteen as of 2026-06-11, including `review_artifacts`, `execute_approved_publications`, `process_rejected_plans`, `audit_self_model`, and `curate_pending_plans`), and `_persist_progress(iteration, next_phase)` atomically writes `{"iteration": N, "next_phase": "..."}` after each phase completes. On restart, `_read_lifecycle_progress()` returns the saved `(iteration, next_phase)` and the loop resumes at the saved phase without replaying earlier phases.

That mechanism is **orthogonal** to this proposal. Phase-level persistence resumes the loop at the *next phase*, but any work in flight **inside** a phase (notably a task inside `generate_task`/`run()`) is still lost. A task that crashes on step 4 of 5 has:

1. No completed steps persisted (only in the in-memory `steps` list)
2. No `TaskRecord` written (the write only happens at the end of `run()`)
3. `lifecycle.json` still pointing at `next_phase="evolve"` (or whatever's next)

On restart the loop jumps to the next phase and the task's partial work is unrecoverable. Task checkpointing closes this gap: it persists per-step progress so either `run()` can pick up mid-task on the next iteration, or a `resume_task(task_id)` entry point can be invoked after manual recovery.

## Relevant Findings

### TaskRecord written only after all steps complete (`maestro/agent.py`)
`run()` executes all steps, computes outcome, distills findings, then writes a single `TaskRecord`. If the process dies anywhere in this window, nothing is persisted about the task.

### Step loop is a dependency-group execution with no intermediate persistence (`maestro/agent.py`)
```python
# 3. Execute each step (with adaptive re-planning)
replans = 0
step_idx = 0
while step_idx < len(steps):
    step = steps[step_idx]
    if self._ensure_shutdown_event().is_set():
        break
    step.status = "running"
    result = await self.executor.execute(
        step, str(self.config.workspace), identity_context,
        artifacts_dir=str(artifacts_path),
    )
    if result.success:
        step.status = "done"
        step.result = result.output
        all_output.append(result.output)
    else:
        step.status = "failed"
        error_msg = result.error or "Unknown error"
        step.result = error_msg
        errors.append(error_msg)
        # Adaptive re-planning: revise remaining steps
        if replans < self.config.max_replans:
            ...
    step_idx += 1
```
Step results live in the in-memory `steps` list and `all_output` list. The loop is now index-based (to support mid-loop splicing from `replan()`), not a simple `for` loop.

### Task branches provide isolation but not persistence (`maestro/agent.py`)
The branch (`task_branch(task_id)`) and artifacts directory are created before the step loop; commits happen *after* the loop completes, not per-step. No git-level persistence during execution.

### No checkpoint directory exists
There is no `checkpoints/` directory under `memory_dir`, no `TaskCheckpoint` type, no resume logic anywhere in the codebase, and no `task_id` passthrough from `run_loop()` to `run()` that would let a resume pick up by ID.

## Implementation Plan

### Step 1: Define TaskCheckpoint dataclass

In `maestro/memory.py`, add:
```python
@dataclass
class TaskCheckpoint:
    task_id: str
    goal: str
    source: TaskSource
    branch: str | None
    completed_steps: list[PlanStep]  # steps with status="done" and results
    remaining_steps: list[PlanStep]  # steps still pending
    errors: list[str]
    all_output: list[str]
    artifacts_dir: str | None
    replans: int         # so resume honours the max_replans budget
    timestamp: str
```

Track the existing `replans` counter inside the checkpoint so a resumed task doesn't silently get a fresh budget.

### Step 2: Add checkpoint persistence to Memory

In `maestro/memory.py`, add:
```python
def save_checkpoint(self, checkpoint: TaskCheckpoint) -> None:
    """Save task checkpoint for crash recovery."""
    path = self._dir / "checkpoints" / f"{checkpoint.task_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(asdict(checkpoint)))

def load_checkpoint(self, task_id: str) -> TaskCheckpoint | None:
    """Load checkpoint if one exists for this task."""
    path = self._dir / "checkpoints" / f"{task_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        # Reconstruct PlanStep lists
        ...
        return TaskCheckpoint(**filtered)
    except (json.JSONDecodeError, OSError, TypeError):
        logger.warning("Corrupted checkpoint for %s, ignoring", task_id)
        return None

def clear_checkpoint(self, task_id: str) -> None:
    """Remove checkpoint after task completion."""
    path = self._dir / "checkpoints" / f"{task_id}.json"
    path.unlink(missing_ok=True)
```

### Step 3: Write checkpoints during step execution

In `maestro/agent.py:run()`, after each successful step (`maestro/agent.py`), persist a snapshot of the current `steps` list (completed + remaining) plus `all_output`, `errors`, and `replans`. This is called **before** `step_idx += 1` so a crash at the next iteration sees the just-completed step as done. Use `atomic_write` (no need for JSONL — one file per task, overwritten).

Also checkpoint after any `steps[step_idx + 1:] = new_steps` splice (post-replan), so the revised plan survives a crash on the replanned path.

### Step 4: Check for checkpoint on task entry

At the beginning of `run()` (or ideally a new `resume_task()` entry point), after the task_id is known:
```python
checkpoint = self.memory.load_checkpoint(task_id)
if checkpoint:
    logger.info(
        "Resuming task %s from checkpoint (%d steps done, %d pending, replans=%d)",
        task_id, len(checkpoint.completed_steps),
        len(checkpoint.remaining_steps), checkpoint.replans,
    )
    steps = checkpoint.completed_steps + checkpoint.remaining_steps
    errors = list(checkpoint.errors)
    all_output = list(checkpoint.all_output)
    branch_name = checkpoint.branch
    replans = checkpoint.replans
    step_idx = len(checkpoint.completed_steps)  # skip to first pending
```

### Step 5: Clean up checkpoint after completion

After the `TaskRecord` is written (`maestro/agent.py`), call:
```python
self.memory.clear_checkpoint(task_id)
```
Successful or partial — either way the task is done, the record is stored, and the checkpoint is garbage.

### Step 6: Add resume_task() entry point and task_id plumbing

`run()` currently mints the task_id internally via `uuid.uuid4().hex[:12]`. Add:

- An optional `task_id` parameter to `run()` (default None → new uuid)
- A `resume_task(task_id)` method that loads the checkpoint, reconstructs the resume context, and delegates to `run(task_id=task_id, ...)`
- A CLI hook (`maestro resume-task <task_id>`) so Thomas can manually resume a known-checkpointed task

Optionally, wire `run_loop()` to automatically resume any orphaned checkpoints at startup — but only for tasks whose branches still exist, to avoid dangling state.

### Step 7: Stale-checkpoint cleanup

Add a grace check in `run_loop()` (e.g., on the first iteration after restart) that enumerates `checkpoints/*.json`, checks their `timestamp`, and deletes anything older than `checkpoint_max_age_days` (new config, default 7). Prevents unbounded growth when checkpoints are orphaned by schema drift or manually deleted branches.

## Testing Strategy

1. **Checkpoint write/read round-trip** — Save checkpoint, load it, verify all fields preserved including `replans`.
2. **Checkpoint written after each step** — Mock executor to succeed twice then crash; verify 2 checkpoint writes, each with the correct `completed_steps` / `remaining_steps` split.
3. **Checkpoint survives replan** — Mock executor to fail on step 2; verify the replan splice produces a new checkpoint with the revised remaining steps.
4. **Resume skips completed steps** — Create checkpoint with 2 done + 2 pending, call `resume_task()`, verify only the 2 pending steps are executed.
5. **Checkpoint cleared on completion** — Verify the file is deleted after `TaskRecord` is written.
6. **Corrupted checkpoint handled** — Write invalid JSON, verify graceful fallback (checkpoint ignored, task starts fresh).
7. **Stale checkpoint with missing branch** — Point `branch_name` at a nonexistent branch, verify resume falls back to fresh start without crashing.
8. **Stale checkpoint cleanup** — Write a checkpoint with an old timestamp, run the loop once, verify the file is deleted.
9. **Phase-level resume still works** — Confirm `lifecycle.json`-based phase resume is unaffected (no test file collisions under `memory_dir`).

Run: `python3.12 -m pytest tests/ -v -k "checkpoint"`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Checkpoint adds I/O per step | Low | `atomic_write` is fast; one file per task; overwritten not appended |
| Resume on stale branch | Medium | Check branch exists before resuming; fall back to fresh start with a warning |
| Checkpoint schema changes | Low | Treat load errors as corrupt; migration via the existing fingerprint mechanism if needed |
| Step results may reference stale state | Medium | Steps should be idempotent; document this requirement in `Executor` docstring |
| Orphaned checkpoint files | Low | Step 7 adds bounded cleanup |
| Interference with phase-level lifecycle.json | None | Different file, different directory semantics — no overlap |
| Double-counting replans across resume | Low | `TaskCheckpoint.replans` carries the counter across resume |
