# 071: A Rule/Sensor Framework for `audit_checks.py`

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | proposed |
| **Affects** | `maestro/audit_checks.py`, `maestro/agent.py` (`_audit_self_model`, `generate_task`), new `maestro/audit_framework.py`, `tests/test_audit_checks.py` |
| **Derived from** | Exhaustive architecture review, 2026-06-11 |

## Description

`maestro/audit_checks.py` is 4,447 lines and houses two distinct families that
have grown by accretion: ~11 deterministic audit *rules* (`check_*`) that
actuate (emit findings, open questions, directives) and ~20 *sensors*
(`measure_*` / `format_*`) that only describe state for prompt injection. Both
families are hand-rolled: each rule repeats the load-history → count → threshold
→ format-finding boilerplate, and each sensor repeats read-tail → window →
aggregate → format-line. There is no shared rule interface, no registration, and
no single place to read "which rules run, in what order, and what each
consumes." The self-extinguish protocol ("rules self-extinguish on INTEGRATE")
is re-implemented per rule rather than provided by a framework.

The accretion has a measurable downside: it is hard to tell which sensors are
*consumed* versus dead weight. Several `measure_*` functions feed only
`format_known_state_summary`, some feed CLI subcommands, and the review could not
confirm a consumer for every one without reading all 4,447 lines. A thin
framework — a `Rule` protocol with `id`, `applies`, `evaluate`, and an
`extinguishes_on` predicate, plus a `Sensor` protocol declaring its consumer —
turns the file into a registry of small, individually-testable units and makes
unconsumed sensors detectable by a test rather than by manual audit.

## Relevant Findings

- `maestro/audit_checks.py` (`check_success_claim_vs_rate`,
  `check_stability_claim_vs_failure_streak`, `check_repeated_error_without_limitation`,
  `check_repeated_target_attempt`, `check_kept_task_not_promoted`,
  `check_stalled_question`, `check_stale_self_model_entry`,
  `check_evolution_impact_reviews`, `check_phantom_remediation`,
  `check_orphaned_cure`, `check_self_model_predictions`) — eleven rule functions,
  each independently loading history and formatting findings with no shared base.
- `maestro/agent.py` (`_audit_self_model`) — invokes the rules as a hand-written
  sequence; adding a rule means editing both `audit_checks.py` and this call site,
  with the run order living only in the call site.
- `maestro/audit_checks.py` (`measure_noop_rate`, `measure_success_by_origin`,
  `measure_false_success_recurrence`, `measure_pr_lineage_recurrence`,
  `measure_outcome_trends`, `measure_directive_compliance`,
  `measure_gate_triggers`, `measure_prediction_accuracy`, plus the `format_*`
  pairs) — ~20 sensors with overlapping windowing logic and no declared consumer.
- `maestro/audit_checks.py` (`format_known_state_summary`) — a ~194-line
  function that pulls many sensors together; the single biggest sink and the
  place where "is this sensor consumed?" is answered today.
- CLAUDE.md "audit_self_model runs ten deterministic rules … Rules self-extinguish
  on INTEGRATE" — the self-extinguish contract is documented but not codified as
  a shared mechanism.

## Implementation Plan

1. [ ] **Define the framework.** `maestro/audit_framework.py` — a `Rule`
   protocol (`id: str`, `enabled(config) -> bool`, `evaluate(ctx) -> Finding |
   None`) and a `RULES` registry tuple; a `run_rules(ctx)` driver that iterates
   the registry and collects findings. Provide a `Sensor` protocol with a
   `consumed_by: str` field so each sensor names its sink.
   - *Depends on:* none.
   - *Acceptance:* `run_rules` over a two-rule registry returns both findings in
     registry order; unit-tested with no `MaestroAgent`.
2. [ ] **Port the rules.** Wrap each existing `check_*` as a `Rule` registry
   entry, preserving behaviour and order. `_audit_self_model` calls `run_rules`
   instead of the hand-written sequence.
   - *Depends on:* 1.
   - *Acceptance:* the findings produced for a fixed history match the
     pre-refactor output one-for-one (golden test); the run order is asserted
     from `RULES` rather than the call site.
3. [ ] **Annotate sensors and add an unconsumed-sensor test.** Give each
   `measure_*`/`format_*` a `consumed_by` annotation and add
   `tests/test_audit_checks.py::test_every_sensor_has_a_consumer`, which fails if
   a registered sensor names no live sink — converting "is this dead?" from a
   manual read into a CI signal.
   - *Depends on:* 1.
   - *Acceptance:* the test passes for all current sensors or flags the
     genuinely-unconsumed ones for deletion in the same PR.

## Testing Strategy

`tests/test_audit_checks.py` gains a registry-order guard, a per-rule golden test
(seed history → assert the `Finding`), and the unconsumed-sensor test from step
3. The existing per-function rule tests are re-pointed at the registry entries
with no behavioural change. Self-extinguish behaviour is tested at the framework
level once (seed an INTEGRATE → assert the rule stops firing) rather than
re-proved per rule. All `mem_fs`; zero SDK cost (the rules are deterministic).

## Risk Assessment

The rules drive real actuation (open questions, directives, escalation), so a
porting error changes agent behaviour. The golden test in step 2 freezes
today's findings before the hand-written sequence is deleted. The
unconsumed-sensor test (step 3) may surface genuinely-dead sensors — deleting
them is in scope but should be done as separate commits with their own coverage
adjustment so the 100%-coverage gate stays green. No state-file or config-schema
change; the framework is internal. Rollback is a revert. Steps are independently
mergeable, with step 1 adding tested-but-unwired code.

## Out of scope

Splitting `audit_checks.py` into separate `audit_rules.py` / `audit_sensors.py`
files is a follow-up once the framework boundary is clear; this proposal
introduces the abstraction in place to keep the diff reviewable.
