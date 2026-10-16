# 070: Make the PR-Remediation Lifecycle an Explicit State Machine

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/loop/pr_remediation.py` (`PrRemediationController.dequeue_gates`, `_gate_pr_remediation_*`), `maestro/agent.py` (`_run_task_phase`), `maestro/remediation_tracker.py`, new `maestro/pr_remediation_state.py`, `tests/test_agent_pr_gates.py` |
| **Depends on** | 068 (shipped — step 1 moved the cluster into `maestro/loop/pr_remediation.py`) |
| **What landed** | Steps 1–3 (all shipped). Step 1 — `maestro/pr_remediation_state.py` now defines the `PrRemediationState` enum (`MERGED`, `LANDED`, `STOOD_DOWN`, `MERGE_BLOCKED`, `BRANCH_RESOLVED`, `PREFLIGHT_CLEAN`, `REMEDIABLE`, `REPETITION_EXHAUSTED` — `LANDED` and `STOOD_DOWN` were added by 080 and are named into step 2's table) and the `Marker` `StrEnum` (`MERGE_BLOCK = "[merge-block]"`, `LANDED = "[landed]"`, `PREFLIGHT = "[preflight]"`, `BRANCH_RESOLVED = "[branch-resolved]"`, `AWAITING_TEND_PUSH = "awaiting tend push"`) as the single definition site. The marker-matching and marker-writing code paths in `maestro/loop/pr_remediation.py`, `maestro/agent.py`, `maestro/audit_checks.py`, and `maestro/remediation_tracker.py` now reference `Marker.*` rather than raw literals (the literals survive only in explanatory comments and docstrings). Step 2 — the dequeue chain is now the ordered `(state, gate_fn)` transition table `_DEQUEUE_GATE_TABLE` in `maestro/loop/pr_remediation.py`, walked by the `PrRemediationController.classify_dequeue` runner that returns the first matching `PrRemediationState` (or `REMEDIABLE` when none fire); `dequeue_gates` delegates to it and `_run_task_phase` consumes the result instead of falling through `if gate(...): return` blocks. The order-invariant guard test `test_dequeue_gate_table_order_is_pinned` (`tests/test_loop_pr_remediation.py`) fails on a reorder. Step 3 — `classify_remediation_outcome_state` (`maestro/remediation_tracker.py`) is the single typed classifier for the post-task lifecycle (`STOOD_DOWN` / `BRANCH_RESOLVED` / `REMEDIABLE`); `_record_remediation_outcome` (`maestro/agent.py`) calls it once, stores the result on `_last_remediation_state`, and switches the stand-down-persist and host-no-op/escalation decisions on that enum member instead of re-parsing the free-text summary. The `test_branch_resolved_lap_trips_gate_and_escalates` (`tests/test_run_task_phase.py`) lap seeds a `BRANCH_RESOLVED` outcome and asserts escalation with no marker substring in the summary. |
| **What is missing** | Nothing — step 3 (post-task downgrade/escalation routed through the typed `PrRemediationState`) landed via PR #2715, so all three steps have shipped. |
| **Derived from** | Exhaustive architecture review, 2026-06-11 |

## Description

> Status updated to shipped on 2026-06-20 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

A `pending_pr_requests` item passes through a chain of dequeue gates —
merged-exclusion, merge-block, branch-resolved (costume-signature), preflight,
and the remediation-repetition gate — before it becomes a task, and after the
task runs its outcome is downgraded or escalated based on more checks. The
sequencing is correct today, but it is encoded **only** as an ordered run of
`if gate(...): return` statements whose required order survives purely in prose
comments ("ordering: *before* the merge-block gate and preflight"). The "states"
a PR can be in are not named anywhere; they are inferred at read time from a mix
of string-literal protocol markers — `[merge-block]`, `[preflight]`,
`[branch-resolved]`, `awaiting tend push` — scattered across `agent.py`,
`audit_checks.py`, and `remediation_tracker.py` and matched by substring.

This emergent design is fragile in two specific ways. First, the ordering
constraint is unenforced: a future edit can reorder two gates and break the
documented "merged-exclusion must precede merge-block" invariant with no test
failure. Second, the string markers are a private protocol matched by `in`
checks in multiple modules, so renaming one ("awaiting tend push") silently
breaks the counters that gate escalation. This proposal names the states and
transitions explicitly — a small `PrRemediationState` enum and a transition
table — so ordering is data the runner enforces and the markers become enum
members with one definition site.

## Relevant Findings

- `maestro/loop/pr_remediation.py` (`PrRemediationController.dequeue_gates`,
  invoked from `maestro/agent.py`'s `_run_task_phase`) — runs
  `_gate_pr_remediation_merged` → `_gate_pr_remediation_merge_block` →
  `_gate_pr_remediation_branch_resolved` → `_preflight_pr_remediation` as bare
  sequential `if ...: return` calls; the required order is asserted only in
  comments, not in code.
- `maestro/loop/pr_remediation.py` / `maestro/agent.py` /
  `maestro/remediation_tracker.py` / `maestro/audit_checks.py` — the markers
  `[merge-block]`, `[preflight]`, `[branch-resolved]`, and `awaiting tend push`
  are written and substring-matched across these modules with no shared constant.
- `maestro/remediation_tracker.py` (`measure_unlanded_remediations`,
  `*_readmission_channel`, `*_readmission_rate`) — escalation/repetition
  sensors depend on parsing these exact strings out of `result_summary`/
  `key_findings`, so the marker vocabulary is load-bearing protocol, not logging.
- `maestro/loop/pr_remediation.py` (`_PRRemediationAccumulator`) — already
  aggregates per-PR remediation state for one iteration, a natural home for an
  explicit state field instead of re-deriving state from markers each lap.
- CLAUDE.md "Tend PR is sole pusher" / dequeue-face / merge-block / costume-
  signature invariants — five one-line rules describe what is really one state
  machine; today a reader must reconstruct it from five separate bullets.

## Implementation Plan

1. [x] **Name the states and markers.** `maestro/pr_remediation_state.py` —
   a `PrRemediationState` enum (`MERGED`, `MERGE_BLOCKED`, `BRANCH_RESOLVED`,
   `PREFLIGHT_CLEAN`, `REMEDIABLE`, `REPETITION_EXHAUSTED`) and a `Marker`
   `StrEnum` holding the `[merge-block]` / `[preflight]` / `[branch-resolved]` /
   `awaiting tend push` literals as the single definition site. Replace the
   scattered string literals with `Marker.*` references across those modules.
   - *Depends on:* none.
   - *Acceptance:* grep finds the literal strings only inside
     `pr_remediation_state.py`; all other matches go through `Marker`.
2. [x] **Make the gate order a transition table.** Express the dequeue chain as
   an ordered tuple of `(state, gate_fn)` and a `classify_dequeue(entry)` runner
   that returns the first matching `PrRemediationState`; `_run_task_phase` acts
   on the returned state instead of falling through `if ...: return` blocks.
   - *Depends on:* 1.
   - *Acceptance:* a test asserts `classify_dequeue` evaluates merged-exclusion
     before merge-block before branch-resolved before preflight, and reordering
     the tuple fails a guard test that pins the required order.
3. [x] **Route outcome downgrade/escalation through the state.** Have the
   post-task downgrade and the repetition-gate escalation read the
   `PrRemediationState` recorded on the `_PRRemediationAccumulator` rather than
   re-parsing markers from the task summary.
   - *Depends on:* 1, 2.
   - *Acceptance:* escalation counters increment off the enum state; a test
     seeds a `BRANCH_RESOLVED` lap and asserts escalation without any substring
     match on `awaiting tend push`.

## Testing Strategy

`tests/test_agent_pr_gates.py` gains: an order-invariant guard test (step 2)
that fails if the transition tuple is reordered; per-state classification tests
feeding synthetic `PendingPRRequest` + git-merge-truth fixtures and asserting the
returned `PrRemediationState`; and a marker-centralization test asserting the
literal strings appear only in `pr_remediation_state.py`. The remediation_tracker
sensor tests are re-pointed at `Marker.*` to prove the centralization didn't
change the on-disk vocabulary. All git/merge-truth oracles mocked; `mem_fs` for
state.

## Risk Assessment

The markers are an *on-disk* protocol — they appear in already-persisted
`task_history.jsonl` records — so the `Marker` values must stay
byte-identical to the current literals; the centralization test enforces that,
making this a rename with zero wire-format change. The ordering-table refactor
risks a behavioural change if a gate's side effects (it both classifies *and*
escalates today) are split incorrectly; step 2 keeps the gate functions intact
and only changes how their order is expressed, deferring the side-effect split to
step 3 behind its own test. No config or trust-chain surface changes; rollback is
a revert. Steps are independently mergeable.
