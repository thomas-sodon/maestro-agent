# 073: Config-Flag Lifecycle — Dead-Flag Detection and Experimental-Flag Retirement

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, new `tests/test_config_flag_lifecycle.py` |
| **Derived from** | Exhaustive architecture review, 2026-06-11 |
| **What landed** | Step 1 — the dead-flag CI guard. `tests/test_config_flag_lifecycle.py::test_every_config_field_has_a_reader` enumerates every `Config` field and fails on any with no `.<field>` reader anywhere under `maestro/` (excluding the `toml_config.py` mirror layer and `config.py`'s `validate`/`from_env` plumbing spans), backed by the self-cleaning `_KNOWN_READERLESS_FIELDS` allowlist (`test_known_readerless_allowlist_is_current`) and seeded negative controls. Step 2 — the Config↔TOML symmetry guard. `test_config_toml_mapping_matches_maestro_toml_keys` binds `_TOML_TO_CONFIG` bidirectionally to the committed `maestro.toml` scalar defaults (through the pure `maestro_toml_scalar_pairs` helper that replicates the loader's carve-outs), and `test_every_toml_mapping_names_a_real_config_field` pins each mapping row to a declared `Config` field — with two seeded negative controls. Step 3 — the experimental-flag manifest and audit CLI. `maestro/flag_lifecycle.py` declares the `EXPERIMENTAL_FLAGS` manifest of dated opt-in `Config` flags (`ExperimentalFlag` — a `review_by` horizon plus an optional gate trace) with the pure `parse_review_by` / `find_overdue_flags` / `audit_flags_report` helpers; `test_no_experimental_flag_is_overdue` reds the build once a horizon is crossed and `test_every_experimental_flag_gate_names_a_real_gate` binds each manifest gate to a real emit site; the read-only `maestro audit-flags` CLI (`cmd_audit_flags`) joins the manifest against recent `measure_gate_triggers` fire-rates. |
| **What is missing** | Nothing — all three implementation steps have shipped (see the Shipped table). |

## Description

> Status updated to shipped on 2026-08-08 by /plan-proposal — see [README.md](../README.md#shipped) for the landing record.

`Config` carries ~161 fields, ~49 of them in the `[loop]` namespace, and the
count climbs with every gate, bridge, and experiment the self-evolution track
adds. CLAUDE.md marks a large fraction of these "experimental",
"measurement-only", or "default-off", but there is no mechanism to ever retire
one: an experiment that proved out (or didn't) leaves its flag behind forever,
and a flag whose consumer was deleted in a refactor becomes a silent dead field
that still appears in `maestro.toml` and still costs boilerplate to thread
through `toml_config` → `Config`. The namespace is flat — no grouping, no
provenance, no "added in / review by" metadata — so the only way to know whether
a flag is live is to grep for its reader.

This proposal adds two cheap, deterministic guards rather than a heavy config
framework. First, a dead-flag test that fails when a declared `Config` field has
no reader outside `config.py`/`toml_config.py` — catching orphaned flags at CI
time. Second, a lightweight experimental-flag manifest: experimental flags
declare a `review_by` cycle/date so a periodic audit (or a CI staleness check)
surfaces flags overdue for a promote-or-delete decision. The end state is a
config surface that can only grow on purpose, where a retired experiment is
detectable rather than invisible.

## Relevant Findings

- `maestro/config.py` (`Config`) — ~161 dataclass fields in one flat class;
  ~49 `[loop]`-prefixed. Adding a flag touches the dataclass, the
  `toml_config` parse path, the TOML defaults, and the consumer — four edit
  sites with no lint binding them.
- `maestro/toml_config.py` — the parse/overlay layer that mirrors every
  `Config` field; a field added in one and not the other is caught only by a
  hand-written test, not by construction.
- `maestro.toml` — flat section list mirroring the flags; CLAUDE.md notes
  "`Config` field defaults are for tests only", so the TOML is the real default
  surface and drift between them is possible.
- CLAUDE.md subsystem invariants — repeatedly tag features "experimental",
  "measurement-only", "default-off" (construction-directive gates, directive-
  compliance, prediction tracker, integration-backlog, task-mode balance,
  forced-read bridge, …) with no expiry mechanism named.
- `maestro/audit_checks.py` (`measure_gate_triggers`) — already measures whether
  gates *fire*; a flag whose gate never fires across a long window is a
  promote-or-delete candidate, so the data to drive retirement already exists.

## Implementation Plan

1. [x] **Dead-flag CI guard.** `tests/test_config_flag_lifecycle.py::
   test_every_config_field_has_a_reader` — walk `Config`'s fields, grep the
   `maestro/` tree (excluding `config.py`/`toml_config.py`) for each field name,
   and fail on any field with zero readers. Seed a tiny allowlist for fields read
   only via `getattr`/reflection, documented inline.
   - *Depends on:* none.
   - *Acceptance:* the test passes against the current tree (or flags genuine
     orphans for deletion in the same PR) and fails when a reader-less field is
     seeded.
2. [x] **Config↔TOML symmetry guard.** A test asserting every `[loop]`-style
   `Config` field has a corresponding `maestro.toml` key and vice-versa, so the
   "defaults are for tests only" split can't silently drift.
   - *Depends on:* none.
   - *Acceptance:* the test fails when a field is added to `Config` without a
     `maestro.toml` entry.
3. [x] **Experimental-flag manifest.** Introduce an `EXPERIMENTAL_FLAGS`
   mapping (flag name → `review_by` cycle count or absolute date) and a test
   that lists flags past their review horizon. Pair it with a
   `maestro audit-flags` CLI subcommand reporting, per experimental flag, its
   recent fire-rate from `measure_gate_triggers` so the operator has the
   promote-or-delete evidence in one place.
   - *Depends on:* 1.
   - *Acceptance:* `maestro audit-flags` prints each experimental flag with its
     `review_by` and recent activation count; a test flags any entry past
     `review_by`.

## Testing Strategy

All three guards are pure static/CI tests in `tests/test_config_flag_lifecycle.py`
using the repo tree on disk (the same pattern as `test_no_line_citations_in_docs`
and `test_claude_md_brevity`) plus `mem_fs` for the `audit-flags` activation
read. Negative controls: seed a reader-less field and assert step 1 fires; seed a
`Config`-only field and assert step 2 fires; seed a past-horizon entry and assert
step 3 fires. No SDK, no real I/O beyond reading source files.

## Risk Assessment

The dead-flag scan can false-positive on reflective access; the inline allowlist
plus a `getattr`-aware grep mitigates, and the cost of a false positive is a
one-line allowlist entry, not a broken build for users. The guards are
CI-only — no runtime behaviour, no state-file, no trust-chain change — so the
blast radius is the test suite itself. The experimental manifest is advisory
(it reports, it does not auto-delete), keeping retirement a human decision
consistent with the human-in-the-loop evolution invariant. Rollback is a revert
of the test files.

## Out of scope

Grouping `Config` into nested per-subsystem sub-dataclasses is a larger
ergonomic change deferred to a future proposal; this one adds guards around the
flat surface that exists rather than reshaping it.
