# 031: Artifact Review Phase

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/agent.py`, `maestro/cli.py`, `maestro/prompts/defaults.py`, `maestro.toml`, `CLAUDE.md`, new `maestro/artifact_review.py`, new `maestro/prompts/artifact_review.py`, tests |
| **Depends on** | 030 |

## Description

> Status updated to shipped on 2026-04-27 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Second of the 030 → 033 series. Adds the `review_artifacts` loop phase: every `[artifacts] review_every` (default 20) iterations plus first-iteration-after-restart, Maestro selects up to `review_batch_size` (default 3) newly-completed tasks with non-empty artifact directories and runs one bounded LLM call to classify each as `publish`, `improve`, `reuse`, or `skip`. Reviews persist to `artifact_reviews.jsonl` (from 030). `improve` and `reuse` verdicts also write a `realization` journal entry so Thomas sees them in the diary.

`publish` verdicts are **recorded but not emitted as proposals** in this phase — emission lands in 032 behind the `[publication] enabled` flag (still `false` by default). This lets Maestro shadow-run review classification for as long as Thomas wants before the approval surface goes live, generating a real dataset of verdicts auditable via `maestro reviews --verdict publish` before the publication flag flips. End state: the review phase runs on cadence, the verdict dataset accumulates, but nothing leaves the machine.

## Relevant Findings

- `maestro/agent.py` — `_LOOP_PHASES`; phase dispatch is a `for phase_index in range(start_phase_index, len(_LOOP_PHASES))`, each phase a short `elif phase == "<name>":` reading `smoke`, `iteration`, `first_iteration_after_restart`, and config flags.
- `maestro/evolution.py` (`check_merged_plans`) / `maestro/agent.py` / `maestro/evolution.py` (`tend_evolution_prs`) — `check_merged_plans` is sync; `tend_evolution_prs` is async and runs "every N iterations + first-iteration-after-restart". The new phase mirrors the `tend_evolution_prs` cadence and is async (it calls `query()`).
- `maestro/agent.py` — `budget_throttle_active` is an `Agent` property; read it at dispatch and pass into the phase as a kwarg (same shape as `tend_evolution_prs(..., conflict_resolution_enabled=not self.budget_throttle_active)`).
- `sdk.json_complete` / `sdk.chars_complete` early-exit predicates exist; structured-JSON callers use `json_complete` with a `QueryResult.parse_json` fallback (as in `consciousness.update_self_model`, `planner.decompose`).
- `maestro/agent.py` — `TaskRecord.artifacts_dir` is set in `agent.run()`, typed `str | None`; `fileutil.list_dir_safe` is the safe enumeration helper.
- `Journal.write_thought(..., entry_type="realization")` is already the right entry type for "this artifact is worth improving / reusing".

## Implementation Plan

All steps have landed (`[x]`). The prompt and reviewer (1–2) precede the phase wiring (3); the CLI (4), default flip (5), and docs (6) follow once the phase is in place.

1. [x] **Review prompt.** new `maestro/prompts/artifact_review.py` — `ARTIFACT_REVIEW_PROMPT` returns a JSON array per task with `task_id`, `verdict`, `title`, `reasoning`, and for `publish` only `proposed_name` (kebab-case ≤40), `proposed_description` (≤120), `readme_markdown` (≥600, public-facing). Explicit negatives: no "Maestro", workspace paths, internal identifiers, or self-reflective context; prefer `skip` when uncertain.
   - *Depends on:* none.
   - *Acceptance:* the prompt is a single importable constant and a mocked LLM response parses into the documented fields.
2. [x] **Reviewer module.** new `maestro/artifact_review.py` — `ArtifactReviewer` with `select_candidates(batch_size)` (success/partial + artifacts present + non-empty + not already reviewed, newest-first), `_read_artifact_bundle(task)` (suffix allowlist, per-file/file-count/total caps, returns text + included names), and `async review(candidates, *, budget_throttle_active=False)` (one medium-effort `query()`; per-entry runs `has_hard_block` and forces `publish→improve` on a hit; persists `ArtifactReview`; journals `realization` for improve/reuse; logs INFO for publish without emitting a proposal).
   - *Depends on:* 1.
   - *Acceptance:* selection returns the expected newest-first subset; a hard-block bundle demotes a `publish` to `improve`; `budget_throttle_active=True` makes zero SDK calls.
3. [x] **Phase wiring.** `maestro/agent.py` — insert `"review_artifacts"` into `_LOOP_PHASES` between `"check_merged_plans"` and `"tend_evolution_prs"`; dispatch guards on `not smoke and artifact_review_enabled and (first_iteration_after_restart or (review_every > 0 and iteration % review_every == 0))`, builds an `ArtifactReviewer`, selects candidates, and calls `review(..., budget_throttle_active=self.budget_throttle_active)` when any exist.
   - *Depends on:* 2.
   - *Acceptance:* `review_every=1` runs every iteration, `review_every=0` never runs, and `first_iteration_after_restart=True` forces a run.
4. [x] **CLI.** `maestro/cli.py` — add `maestro reviews [-n 20] [--verdict publish] [--json]` tailing `artifact_reviews.jsonl` with optional verdict filter and JSON mode, mirroring `maestro history`.
   - *Depends on:* 2.
   - *Acceptance:* `maestro reviews --json` emits the tail and `--verdict publish` filters correctly.
5. [x] **Default flip.** `maestro.toml` — flip `[artifacts] review_enabled = true` (030's default was `false`); keep `[publication] enabled = false`.
   - *Depends on:* 3.
   - *Acceptance:* the shipped `maestro.toml` has `review_enabled = true` and `publication.enabled = false`.
6. [x] **Docs.** `CLAUDE.md` — add `artifact_review.py` to the modules table, `review_artifacts` to the phase list, and a bullet on review cadence, bundle caps, and the inert-until-032 `publish` behaviour.
   - *Depends on:* 3.
   - *Acceptance:* CLAUDE.md names the phase and the module.

## Testing Strategy

All tests use `mem_fs` and mock externals.

- **Selection** (`tests/test_artifact_review.py`): 5 tasks (3 success+artifacts newest-first, 1 success no-artifacts, 1 failure+artifacts) + 1 already-reviewed → `select_candidates(3)` returns exactly 2 newest-first.
- **Bundle bounds**: 50 files × 10KB exercise the suffix allowlist, file-count cap, per-file head-truncate, and total cap; returned `file_names` is exactly the included set; binaries dropped.
- **Happy parse**: a valid 3-entry array → 3 reviews; 1 improve journals a `realization`; 1 publish logs INFO and emits no proposal.
- **Malformed LLM**: garbage → no writes, one WARNING.
- **Pre-classification hard-block**: `publish` + `sk-ant-…` bundle → forced `improve` with `[scrub_blocked:…]`, no publish log.
- **Budget throttle**: `budget_throttle_active=True` → zero SDK calls; `select_candidates` still runs.
- **Cadence** (`tests/component/test_artifact_review_phase.py`): `review_every=1` every iteration; `review_every=0` disabled; `first_iteration_after_restart=True` forces run.
- **CLI** (`tests/test_cli.py`): `maestro reviews --json` tail; `--verdict publish` filter.

Run: `python3.12 -m pytest tests/test_artifact_review.py tests/component/test_artifact_review_phase.py tests/test_cli.py -v`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| LLM over-votes `publish` early in prod | Medium | 031 gates emission behind 032. Thomas audits `maestro reviews --verdict publish --json` for weeks before flipping `[publication] enabled`. The verdict distribution is explicit evidence before any approval surface exists. |
| Review burns tokens on low-value tasks | Low | Batch=3 × cadence=20 caps burn; budget-throttle gate respected. One medium-effort call per cycle ≈ cheap. |
| Prompt drift produces inconsistent verdicts | Low | Prompt is a single versioned constant in `maestro/prompts/artifact_review.py`. Proposal 012 (prompt versioning) would formalise this. |
| `_load_all_tasks` is O(total tasks) | Low | Cost already incurred by `get_recent_tasks` in the loop. Proposal 002 addresses scaling if volume warrants. |
| Candidate enumeration crashes the loop on a malformed `artifacts_dir` | Medium | Selection uses `list_dir_safe`; nonexistent or unreadable dirs drop the task with a WARNING, no raise. |

## What landed

`maestro/artifact_review.py:ArtifactReviewer.review()` + the `review_artifacts` loop phase + the `secret_scan.has_hard_block` `publish→improve` downgrade + the `maestro reviews` CLI (PR #1483). `[artifacts] review_enabled` flipped to `true`; `[publication] enabled` stayed `false`.
