# 068: Decompose the `agent.py` God-Module Along Phase Seams

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/agent.py`, new `maestro/loop/` package (`task_generation.py`, `pr_remediation.py`, `lifecycle.py`), `tests/test_agent*.py` |
| **Derived from** | Exhaustive architecture review, 2026-06-11 |

## Description

> Status updated to shipped on 2026-06-12 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

`maestro/agent.py` is 8,765 lines and holds nearly the entire engine: the
`MaestroAgent` class alone carries ~81 methods, and three of its functions are
individually enormous — `run_loop()` (~1,455 lines), `generate_task()` (~931
lines), and `run()` (~583 lines). One module mixes loop orchestration, task
generation with its dozen gates, PR-remediation dequeue logic, budget
throttling, shutdown/requeue, version snapshots, audit bridging, and
notification fan-out. The file is the single largest in the tree and the single
largest source of merge-conflict surface for every evolution PR.

The cost is concrete and already visible in the test suite: `tests/test_agent.py`
is 15,563 lines and `tests/test_agent_lifecycle.py` is 8,467 — together a third
of the entire 127k-line test corpus. Under the 100%-coverage gate, any change
inside `agent.py` forces a walk through that test mass, and the
`task/{task_id}` branch isolation means concurrent evolution work on different
phases collides in one file. The end state is a thin `MaestroAgent` that wires
collaborators together, with each phase cluster living in its own module behind
a narrow, separately-testable surface — no behaviour change, pure structural
extraction gated so each step ships independently.

## Relevant Findings

- `maestro/agent.py` (`MaestroAgent`) — one class, ~81 methods, spanning
  `run`, `contemplate`, `evolve`, `generate_task`, `_run_task_phase`,
  `_audit_self_model`, `run_loop`, plus all the gate and PR-remediation helpers.
- `maestro/agent.py` (`run_loop`) — ~1,455 lines interleaving phase dispatch,
  `_phase_will_run` cadence dedup, budget-throttle gating, standby/peak-throttle,
  resume-in-place, SIGTERM requeue, and heartbeat/status emission in one body.
- `maestro/agent.py` (`generate_task`) — ~931 lines holding the entire task-gate
  stack (quiet-skip, audit-stall, cost-risk, plus the directive bridges) inline.
- `maestro/agent.py` (`_gate_pr_remediation_merged`, `_gate_pr_remediation_merge_block`,
  `_gate_pr_remediation_branch_resolved`, `_preflight_pr_remediation`,
  `_route_pr_feedback_to_pending_requests`, `_PRRemediationAccumulator`) — a
  self-contained PR-remediation cluster sharing only `self.memory`,
  `self.config`, `self.vcs`, and the self-model queues.
- `tests/test_agent.py` (15,563 lines), `tests/test_agent_lifecycle.py` (8,467
  lines) — the coverage gate makes the file's size a direct tax on every edit.
- `maestro/agent.py` imports and directly mutates `self.memory`,
  `self.consciousness`, `self.journal`, `self.task_queue`, `self.config` —
  the shared state each extracted seam would need passed in.

## Implementation Plan

1. [x] **Carve the PR-remediation cluster into `maestro/loop/pr_remediation.py`.**
   Move `_gate_pr_remediation_*`, `_preflight_pr_remediation`,
   `_route_pr_feedback_to_pending_requests`, and `_PRRemediationAccumulator`
   into a `PrRemediationController` constructed with `(memory, config, vcs,
   consciousness)`; `MaestroAgent` holds an instance and delegates. This cluster
   is the most self-contained (least shared mutable state) so it is the safest
   first cut.
   - *Depends on:* none.
   - *Acceptance:* `_run_task_phase` calls `self._pr_remediation.dequeue_gates(...)`
     and no `_gate_pr_remediation_*` method remains defined on `MaestroAgent`;
     existing PR-remediation tests pass unchanged against the delegating shim.
2. [x] **Extract task generation into `maestro/loop/task_generation.py`.**
   Move `generate_task` and its gate/bridge bodies behind a `TaskGenerator`
   collaborator (depends on 069's registry if landed; otherwise lift verbatim).
   `MaestroAgent.generate_task` becomes a one-line delegation.
   - *Depends on:* none (composes with 069).
   - *Acceptance:* `generate_task` body lives in `task_generation.py`;
     `tests/test_agent.py` task-gen cases import the collaborator directly.
3. [x] **Extract lifecycle/throttle into `maestro/loop/lifecycle.py`.**
   Move `_phase_will_run`, `_read_lifecycle_progress`, `_send_status_update`,
   throttle multiplier and peak/standby helpers into a `LifecycleController`;
   `run_loop` keeps only the phase-dispatch skeleton.
   - *Depends on:* 1, 2 (do the easy cuts first to shrink the blast radius).
   - *Acceptance:* `run_loop` is under ~400 lines and contains only phase
     ordering + dispatch; cadence/throttle logic is unit-tested without
     constructing a full `MaestroAgent`.

## Testing Strategy

Each step is a pure move-and-delegate, so the proof is *behavioural
equivalence*: the existing `tests/test_agent*.py` cases must pass unchanged
against the delegating `MaestroAgent` shim before any test is relocated. After
each extraction, add a focused `tests/test_loop_<cluster>.py` that constructs
the new collaborator directly with `mem_fs` and mocked `vcs`/SDK, exercising the
happy path plus each guard branch — replacing the heaviest `MaestroAgent`-level
setup with a narrow fixture. No new behaviour, so no new happy/fallback paths
beyond what the moved code already had.

## Risk Assessment

The dominant risk is a silent behaviour change during the move (an implicit
read of `self.` state that the new constructor doesn't receive). Mitigation:
each step keeps a thin delegating method on `MaestroAgent` so call sites and the
public surface are byte-for-byte unchanged, and the pre-existing test suite is
the regression oracle — no test is rewritten in the same PR that moves the code.
Steps are independently mergeable and ordered easiest-first, so an interrupted
run leaves a working tree. No state-file, schema, or trust-chain surface is
touched. Rollback is a straight revert of the delegating shim.

## Out of scope

The gate/bridge *abstraction* (proposal 069) and the PR-remediation *state
machine* (proposal 070) are separate concerns; this proposal only relocates the
existing code into seams. Doing the abstraction work first inside the 8.7k-line
file would be harder to review than moving first and abstracting in place after.
