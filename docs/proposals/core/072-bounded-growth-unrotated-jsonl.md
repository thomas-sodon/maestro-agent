# 072: Bounded Growth for Unrotated JSONL State Files

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | proposed |
| **Affects** | `maestro/memory.py` (`archive`), `maestro/fileutil.py`, new `maestro/fileutil.py:rotate_jsonl()`, `maestro/agent.py` (`run_loop` dream-cycle archive call), `tests/test_memory.py`, `tests/test_fileutil.py` |
| **Derived from** | Exhaustive architecture review, 2026-06-11 |

## Description

The tiered-memory invariant promises hot/cold rotation on dream cycles, but
`Memory.archive()` rotates exactly two files — `task_history.jsonl` and
`evolution_plans.jsonl` (its `self._tasks_path` and `self._plans_path`). Every
other append-only JSONL the system writes grows forever with no rotation,
truncation, or cold-tier split: `version_archive.jsonl`, `usage.jsonl`,
`audit_history.jsonl`, `gate_activations.jsonl`, `question_resolutions.jsonl`,
`rejection_history.jsonl`, `predictions.jsonl`, `document_records.jsonl`,
`artifact_reviews.jsonl`, `purpose_archive.jsonl`, `messages.jsonl`,
`cost_samples.jsonl`, `novelty_blocks.jsonl`, `pending_pr_provenance.jsonl`, and
`dream_history.jsonl`.

This is a slow-burn reliability problem. Most readers take a bounded tail
(`get_recent_*`, `measure_gate_triggers(..., n)`), so growth is invisible until a
file is large enough that even the tail read pays a full-file scan, or until disk
pressure hits the bind-mounted volume in the deployment. The fix is to generalize
the rotation `Memory.archive()` already performs into a reusable
`rotate_jsonl(path, threshold_days)` in `fileutil.py`, and drive every
append-only operational file through it on the same dream-cycle cadence — turning
"rotates two files" into "rotates every unbounded file, by policy."

## Relevant Findings

- `maestro/memory.py` (`archive`) — iterates only `(self._tasks_path,
  self._plans_path)`, writing each to a sibling `<path>.archive.jsonl`; no other
  file is reached.
- `maestro/memory.py` (`record_version`, writing `version_archive.jsonl`) and the
  writers of `usage.jsonl`, `audit_history.jsonl`, `gate_activations.jsonl`,
  `question_resolutions.jsonl`, `rejection_history.jsonl`, `predictions.jsonl`,
  `document_records.jsonl`, `artifact_reviews.jsonl`, `purpose_archive.jsonl`,
  `messages.jsonl`, `cost_samples.jsonl` — all append unconditionally with no
  size or age bound.
- `maestro/audit_checks.py` (`measure_gate_triggers`), `maestro/memory.py`
  (`get_recent_*`) — readers take a bounded tail, so unbounded files are
  *latent* cost: correctness is fine, scan cost is not.
- CLAUDE.md "Tiered-memory archive — Dream cycles split `task_history.jsonl` /
  `evolution_plans.jsonl`" — the invariant explicitly names only two files,
  confirming the gap is by-omission, not accidental.
- CLAUDE.md "File persistence — JSONL is append-only" — rotation must preserve
  append-only semantics on the hot file; the existing `archive()` pattern
  (move-old-to-`.archive.jsonl`, keep-recent-hot) already does this.

## Implementation Plan

1. [ ] **Extract a reusable rotator.** `maestro/fileutil.py:rotate_jsonl(path,
   keep_predicate)` — generalize the move-cold-rows-to-`<path>.archive.jsonl`
   logic from `Memory.archive()` into one tested function taking a
   keep-predicate (age- or count-based). `Memory.archive()` calls it for its two
   files with no behaviour change.
   - *Depends on:* none.
   - *Acceptance:* `rotate_jsonl` splits a fixture file into hot+`.archive.jsonl`
     exactly as `Memory.archive()` did; `Memory.archive()` delegates to it and
     existing memory tests pass unchanged.
2. [ ] **Add a rotation policy table and drive the unbounded files.** A
   `ROTATION_POLICY` mapping (filename → keep-predicate, e.g. age for
   `audit_history`/`gate_activations`, count for `usage`/`cost_samples`) consumed
   by a `rotate_all(memory_dir, config)` call added to the dream-cycle block in
   `run_loop`. Gate behind `[loop] rotate_operational_jsonl_enabled` (default on,
   revertible).
   - *Depends on:* 1.
   - *Acceptance:* after a simulated dream cycle on an oversized fixture set,
     each policy-covered file is bounded and its `.archive.jsonl` sibling holds
     the overflow; a test enumerates the policy table against the on-disk JSONL
     inventory and fails if a known unbounded file is absent from the policy.
3. [ ] **Coverage guard.** `tests/test_memory.py::test_all_jsonl_files_have_a_
   rotation_policy_or_explicit_exemption` — assert every `*.jsonl` filename the
   codebase writes is either in `ROTATION_POLICY` or on a small documented
   exemption list (e.g. `schema_version.json` is not JSONL), so a new unbounded
   file can't be added without a conscious choice.
   - *Depends on:* 2.
   - *Acceptance:* the test passes with every current writer classified.

## Migration

No schema change: rotation only moves already-written rows from `<f>.jsonl` to
`<f>.archive.jsonl`, both in the existing append-only format. The `.archive.jsonl`
siblings are operator-managed cold storage, never read by hot paths — identical
to today's `task_history.jsonl.archive.jsonl`. First run after deploy rotates the
accumulated backlog in one pass; the policy predicates bound how much stays hot.

## Testing Strategy

`tests/test_fileutil.py` covers `rotate_jsonl` directly: empty file, all-hot (no
split), all-cold (full move), mixed, and a corrupt trailing line (must not lose
the hot rows). `tests/test_memory.py` keeps the `Memory.archive()` equivalence
test and adds the policy-coverage guard (step 3). The `run_loop` wiring is tested
with a mocked dream cycle asserting `rotate_all` fires on the cadence and not
every iteration. All `mem_fs`; no real filesystem.

## Risk Assessment

The danger in any rotation is data loss on the hot file — dropping rows that a
tail reader still needs. Mitigation: the keep-predicates are sized at or above
the largest tail any reader requests (e.g. `measure_gate_triggers`'s `n`), and
the corrupt-line test proves rotation never truncates surviving hot rows. The
feature is flag-gated default-on so a single config flip disables it; rollback is
a revert plus leaving the `.archive.jsonl` siblings in place (harmless). No
trust-chain or approval surface is touched. `messages.jsonl` carries
`reply_to` threading, so its predicate must keep enough recent history for
thread resolution — covered by an explicit threading-preservation test.
