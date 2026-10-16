# 074: Reconcile Dead Code and Add Doc/Code-Drift Guards

| Field | Value |
|-------|-------|
| **Impact** | LOWER |
| **Status** | shipped |
| **Affects** | `maestro/stats.py`, `maestro/health_check.py`, `docs/modules.md`, `.gitignore`, new `tests/test_module_doc_coverage.py`, new `tests/test_sdk_backends_retired.py`, `tests/test_stats.py`, `tests/test_health_check.py` |
| **Derived from** | Exhaustive architecture review, 2026-06-11 |
| **What landed** | The health-predicate duplication is collapsed: `maestro/stats.py` is now a backward-compatible shim whose `is_health_noteworthy` / `summarize_iteration_health` delegate to `maestro/health_check.py` (`summarize_health`), so a single definition backs both call paths; `docs/modules.md` already documents `stats.py` as a re-exporting shim. Step 2's module-doc coverage guard (`tests/test_module_doc_coverage.py`) has landed — it asserts every top-level `maestro/*.py` owns a `docs/modules.md` row and that the table cites no module absent from disk, with the drifted rows backfilled in the same change. Step 3's retired-package guard has landed too: `tests/test_sdk_backends_retired.py` is an AST no-importer scan asserting no `maestro/` module imports the retired `maestro.sdk_backends` package (the docstring mentions in `maestro/providers/cli_paced.py` / `maestro/providers/registry.py` are ignored because it matches on the syntax tree, not raw text) and that the package directory holds no live `.py` source, and `.gitignore` now ignores `maestro/sdk_backends/` so stale bytecode can't masquerade as live code. Step 1's final increment — removing the now-redundant `TestNoteworthyFlagConsistency` sync test from `tests/test_health_check.py` — has landed (PR #3620), so exactly one definition of the noteworthy predicate remains and the sync test is gone because divergence is structurally impossible. |
| **What is missing** | Nothing — the proposal has fully shipped. Step 1 settled on the single-definition shim route rather than deleting `maestro/stats.py` (now a live module with many production importers), which satisfies its acceptance without the preferred deletion. |

## Description

> Status updated to shipped on 2026-08-30 by /plan-proposal — see [README.md](../README.md#shipped) for the landing record.

Two strands of cruft accumulated as the codebase evolved, neither caught by an
existing gate. First, `maestro/stats.py` is dead production code: no production
module imports it — `is_health_noteworthy` and `summarize_iteration_health` are
referenced only by `tests/test_stats.py` and a docstring `:func:` link in
`health_check.py` — while the same noteworthy-detection logic was
re-implemented as `health_check.py:is_noteworthy`, with
`tests/test_health_check.py` carrying a test whose stated purpose is "Verify
`is_noteworthy` matches the logic of `stats.is_health_noteworthy`." Two copies of
one rule, kept in sync by hand, one of them unreachable from the running system.

Second, there is no guard tying `docs/modules.md` to the actual module list, and
the retired `maestro/sdk_backends/` package (source removed by PR #2358) still
leaves stale `__pycache__/*.pyc` on disk that Python could import if anything
referenced the old path. This proposal collapses the duplicated health logic to
one definition, makes the module-doc table self-checking, and adds a small
`.gitignore`/guard so retired-package bytecode can't masquerade as live code.
The end state is that "is this module dead?" and "does the module doc match
reality?" become CI signals instead of manual archaeology.

## Relevant Findings

- `maestro/stats.py` (`is_health_noteworthy`, `summarize_iteration_health`) —
  imported by no production module; the only importer is `tests/test_stats.py`.
- `maestro/health_check.py` (`is_noteworthy`) — a parallel implementation of the
  same predicate; its docstring points back at `maestro.stats.is_health_noteworthy`
  as the spec, and `tests/test_health_check.py` pins the two in sync by hand.
- `maestro/sdk_backends/` — source retired by PR #2358 (confirmed: only stale
  `__pycache__/cli_paced.cpython-312.pyc` and `__init__.cpython-312.pyc` remain;
  the directory is untracked by git but present on disk).
- `docs/modules.md` — a module-by-module reference table with no test binding it
  to the `maestro/*.py` file list; a new module can land undocumented and a
  deleted one can linger in the table with nothing to flag either.
- CLAUDE.md "Fail loud — no warning-silencing exception handlers" / the
  AST-enforcement test family (`test_no_silenced_exceptions`,
  `test_structural_guards`, `test_identity_prompt_slabs`) — establishes the
  precedent that structural invariants are enforced by source-walking tests,
  which this proposal extends to module-doc coverage and dead modules.

## Implementation Plan

1. [x] **Collapse the duplicated health predicate.** Decide one home: either
   delete `maestro/stats.py` and fold its (already-mirrored) logic and tests into
   `health_check.py`, or re-wire `health_check.py:is_noteworthy` to call
   `stats.is_health_noteworthy` so there is a single definition. Deletion is
   preferred — `health_check.py` is the live consumer.
   - *Depends on:* none.
   - *Acceptance:* exactly one definition of the noteworthy predicate remains;
     the "verify the two match" test in `tests/test_health_check.py` is removed
     because divergence is now structurally impossible.
2. [x] **Module-doc coverage guard.** `tests/test_module_doc_coverage.py` —
   assert every top-level `maestro/*.py` (excluding `__init__`, `__pycache__`)
   appears in the `docs/modules.md` table and that the table cites no module
   absent from disk. Follows the `test_no_line_citations_in_docs` source-scan
   pattern.
   - *Depends on:* none.
   - *Acceptance:* the test passes against the reconciled tree (adding any
     missing/stale rows in the same PR) and fails when a module is added without
     a doc row.
3. [x] **Retired-package bytecode guard.** Add `maestro/sdk_backends/` to
   `.gitignore` cleanup and a one-line test asserting the package has no live
   importer (it has none today), so the retired seam can't silently come back via
   stale bytecode.
   - *Depends on:* none.
   - *Acceptance:* a test asserts no `maestro/` source imports `sdk_backends`;
     the stale `__pycache__` is removed from working trees by the ignore rule.

## Testing Strategy

Steps 2 and 3 are pure source-scan tests in the established AST/grep-guard style,
with negative controls (seed an undocumented module name → assert step 2 fires;
seed a `sdk_backends` import string → assert step 3 fires). Step 1's correctness
is proven by the surviving health tests continuing to pass against the single
remaining predicate; the deleted "verify they match" test is replaced by the
absence of a second copy. All static or `mem_fs`; no SDK, no real I/O beyond
reading source files.

## Risk Assessment

The only behavioural risk is in step 1 — collapsing two predicates that might
have *silently drifted* despite the sync test. Mitigation: keep the existing
health-check behavioural tests as the regression oracle and verify the predicate
outputs are identical across the test battery before deleting either copy; if
they have drifted, that is itself a latent bug the consolidation surfaces and
fixes. Steps 2 and 3 are CI-only with no runtime effect. No state-file, config,
or trust-chain surface is touched. Each step is independently mergeable and a
straight revert restores the prior state. Under the 100%-coverage gate, deleting
`stats.py` requires removing `tests/test_stats.py` in the same PR — accounted for
in step 1.
