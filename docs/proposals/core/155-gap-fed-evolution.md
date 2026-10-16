# 155: Gap-Fed Evolution

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/evolution_fuel.py`, `maestro/gap_store.py`, `maestro/loop/self_health.py`, `maestro/evolution_submission.py`, `maestro/agent.py` (`evolve`), `[evolution_fuel]` in `maestro.toml` |
| **Depends on** | 120 _(the pre-mint gate still applies to fuelled ticks)_ |

## Description

Evolution feeds only on capability gaps, never on Maestro's own narrative. A
self-initiated plan exists to close a gap, and every gap is a GitHub issue
carrying a `gap:*` label. **No fuel, no spend:** `evolve`'s first act is fuel
selection; with no admitted gap the tick makes zero SDK calls and runs no
quality gate. The priced `reflect` call is deleted.

## What a gap is

A gap issue has a door-shaped title naming an owner id from a closed grammar.
`GAP_PREFIXES` in `maestro/evolution_fuel.py` maps each owner-id prefix to its
one label and the openers admitted to open it:

| Prefix | Label | Opener |
|---|---|---|
| `gate` | `gap:maestro` | detector |
| `source_budget` | `gap:maestro` | detector |
| `toolchain` | `gap:maestro` | refusal caller |
| `capability` | `gap:capability` | refusal caller |

`GAP_LABELS` is exactly `gap:maestro` and `gap:capability`.

## The opener door

`open_gap_issue` is the one door that opens a gap. It is idempotent: it reuses
an open issue for the same owner id, reopens a door issue that was closed by a
commit, never reopens one an operator closed by hand, and records each open or
reopen as a `gap_opens` row. `close_gap_issue` is the one closer.

## Trust

`classify_gap_trust` admits a `gap:*` issue only when its label was applied by
a login in `[evolution_fuel] operator_github_logins`, or when a `gap_opens` row
the door wrote re-derives it; anything else is `unverified` and never becomes
fuel. Startup refuses the loop's own login in that list. The
`gap_issue_owner` entry in `tests/structural_guards.py` keeps the labels and
the ledger owned by this module alone.

## Self-health detectors

`run_evolve_prestep` (`maestro/loop/self_health.py`) runs zero-SDK detectors
before fuel selection: the red-main detector opens a `gate` gap once `main` has
been red past `[evolution_fuel] red_main_grace_hours` and closes it on a green
read, and the source-budget detector keeps at most one `source_budget` gap for
the worst over-budget file.

## Fuel-gated evolve

`select_evolution_fuel` reads the open gap issues once, admits them through the
trust check, skips gaps with a live plan, in cooldown, or past their attempt
budget, keeps only a class-1 (red `maestro:gate`) gap while a gap-citing plan
awaits review, and picks the lowest fuel class, oldest first. The pick
is planned through `propose_plan(gap=…)` over a capped deterministic context
block, and the plan is pinned to its gap. A gap fuels at most
`[evolution_fuel] max_plans_per_gap` plans; attempt accounting lives in
`maestro/evolution_submission.py`.

## Stuck-gap tripwire

When a gap reaches `max_plans_per_gap`, it is labelled `needs-decision` and
skipped. `deliver_gap_decision_asks` sends Thomas a plain `ask_thomas`
`[gap-decision #N]` message; the first threaded reply naming build or buy is
applied by `apply_gap_decision` (build clears the label, buy keeps it). Only an
operator close drops a gap.

## The gap ledger

The `gap_opens` and red-main rows live in `gaps.db`, opened only through
`gap_store.build_gap_store`.
