# 069: Declarative Registry for `generate_task` Gates and Directive Bridges

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/agent.py` (`generate_task`), new `maestro/task_gates.py`, `maestro/audit_checks.py`, `tests/test_agent.py`, new `tests/test_task_gates.py` |
| **Depends on** | _(composes with 068)_ |
| **Derived from** | Exhaustive architecture review, 2026-06-11 |

## Description

> Status updated to shipped on 2026-06-12 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

The directive-bridge stack inside `generate_task()` has grown to seven
near-identical blocks — instrument-consumption, integration-cadence,
investigation-streak, investigation-rate, partial-construction,
promotable-backlog, self-directed-success-rate, and forced-read — each repeating
the same shape: guard on `if not model.next_task_directive`, call a
`check_*` function, set the directive, `save_self_model`, write a journal
thought, and `log_gate_activation`. Precedence between bridges is encoded
**only** by their physical order in the function body, and the single shared
`next_task_directive` slot means the first block to fire silently wins — a fact
documented today purely in prose comments ("Low precedence: only fires when no
higher gate set a directive"). Adding a bridge means hand-copying ~25 lines and
hoping it lands in the right spot.

The same copy-paste tax applies to the blocking gates (quiet-skip, audit-stall,
cost-risk) that `return` early. There is no shared abstraction: `generate_task`
is ~931 lines largely *because* every gate is inlined. This proposal replaces
the inline stack with a declarative registry — an ordered list of gate objects,
each declaring its config flag, its predicate, and whether it *blocks* or
*sets a directive* — so precedence is data, `log_gate_activation` is emitted
once by the runner, and a new gate is a registry entry plus a focused test.

## Relevant Findings

- `maestro/agent.py` (`generate_task`) — six literal `if not
  model.next_task_directive:` guards plus the unguarded instrument-consumption
  block, each followed by the identical set/`save_self_model`/journal/
  `log_gate_activation` sequence.
- `maestro/audit_checks.py` (`check_integration_cadence`,
  `check_investigation_streak`, `check_partial_construction_gap`,
  `check_promotable_backlog_directive`, `check_self_directed_success_rate`,
  `check_forced_read_directive`) and `maestro/agent.py`
  (`find_unconsumed_instruments`, `compute_shape_investigation_rate`) — the
  bridge predicates already exist as standalone pure functions; only the
  surrounding plumbing is duplicated.
- `maestro/agent.py` (`log_gate_activation`) — invoked 16 times across the file
  with hand-written `gate=`/`action=`/`detail=` arguments; `action` is an
  ad-hoc string (`"blocked"` / `"directive_set"`) with no enum.
- `maestro/audit_checks.py` (`measure_gate_triggers`) — already reads the tail
  of `gate_activations.jsonl`, so a registry that emits consistent records
  strengthens an existing sensor rather than adding a new one.
- CLAUDE.md "Task gates in `generate_task`" — the taxonomy (quiet-skip,
  audit-stall, cost-risk blocking; the directive bridges; novelty/
  terrain-exhaustion/compound-scope post-call) is maintained by hand because
  the code offers no single place to read the gate order from.

## Implementation Plan

1. [x] **Introduce the gate abstraction.** `maestro/task_gates.py` —
   a `TaskGate` dataclass (`name`, `enabled: Callable[[Config], bool]`,
   `kind: Literal["block", "directive"]`, `run: Callable[..., GateOutcome]`)
   and a `run_gate_chain(gates, ctx)` runner that evaluates in list order,
   short-circuits on the first `block`, applies the first `directive` to the
   empty slot, and emits exactly one `log_gate_activation` per firing.
   - *Depends on:* none.
   - *Acceptance:* `run_gate_chain` over a synthetic two-gate list blocks on the
     first blocker and ignores later directives; unit-tested without `MaestroAgent`.
2. [x] **Port the directive bridges to registry entries.** Wrap each existing
   `check_*` predicate as a `TaskGate(kind="directive")`; keep behaviour and
   ordering byte-identical to today's source order. Replace the inline blocks in
   `generate_task` with one `run_gate_chain` call.
   - *Depends on:* 1.
   - *Acceptance:* the gate-activation records produced for a fixed task history
     match the pre-refactor records one-for-one (golden test); `generate_task`
     no longer contains any `if not model.next_task_directive:` block.
3. [x] **Port the blocking gates and expose the order.** Move quiet-skip,
   audit-stall, cost-risk into `TaskGate(kind="block")` entries at the head of
   the list; add a `gate_order()` accessor the CLAUDE.md-brevity-style docs and a
   `maestro list-gates` CLI can read, retiring the hand-maintained taxonomy.
   - *Depends on:* 2.
   - *Acceptance:* `gate_order()` returns the documented precedence and a test
     asserts blocking gates precede directive gates.

## Testing Strategy

`tests/test_task_gates.py` covers the runner in isolation: empty chain, a
blocker short-circuiting later gates, a directive filling an empty slot, a
second directive being ignored once the slot is full, and `enabled=False`
skipping a gate. A golden-equivalence test in `tests/test_agent.py` pins that
the registry produces the same `gate_activations.jsonl` records and the same
`next_task_directive` outcome as the pre-refactor code for a battery of task
histories — this is the behavioural-equivalence proof for the port. All I/O
through `mem_fs`; no real SDK calls (the gates are zero-SDK-cost by design).

## Risk Assessment

Precedence is the load-bearing invariant: a reordering during the port would
silently change which directive wins. Mitigation is the golden-equivalence test
in step 2, which freezes today's behaviour before the inline blocks are deleted.
The registry is internal — no config-schema, state-file, or trust-chain change —
so rollback is a revert. A subtle risk is that the one-emit-per-firing rule must
exactly reproduce the current per-block `log_gate_activation` calls; the golden
test on `gate_activations.jsonl` records guards that. Steps are independently
mergeable: step 1 adds dead-but-tested code, step 2 swaps the implementation,
step 3 is additive surfacing.
