# 054: Task → Evolution Branch Bridge — `EvolutionPlan.source_task_id`

| Field | Value |
|-------|-------|
| **Impact** | LOW (zero new SDK cost on the bridge itself; saves the duplicated implement-phase cost when used) |
| **Status** | shipped |
| **Affects** | `maestro/memory.py`, `maestro/evolution.py`, `maestro/prompts/defaults.py`, `CLAUDE.md`, `docs/self-evolution.md` |
| **Depends on** | 044 (shipped), 046 (shipped) — same architectural pattern: surface a soft signal into the loop, give the LLM a single-field handle, let existing closure paths do the rest |

## Description

> Status updated to shipped on 2026-06-01 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

When `generate_task` produces a self-directed task and `agent.run()` executes it, the task runs with full Read/Write/Edit/Bash permissions and any code changes are committed to a `maestro/task/{task_id}` branch. On `success` / `partial`, that branch is *kept* and its name is persisted in `TaskRecord.branch`. So Maestro genuinely produces shippable work in the act phase.

But the next iteration's `evolve()` re-does that work from scratch. Two seams:

1. **`evolution.reflect()` ignores branches.** The task-history block reads `t.key_findings`, `t.result_summary`, and artifact filenames, but never `t.branch`. The reflecting LLM has no signal that any prior task produced committed code worth reusing.
2. **`evolution.implement()` always starts fresh.** It calls `evolution_branch(plan.plan_id)` and `checkout_new_branch(...)` from current HEAD (typically `main`), then asks the LLM to write the code from scratch and *requires* `has_changes()` — duplicating the task's work and orphaning the task branch as local-only dead weight.

This proposal lets the LLM, during `propose_plan()`, declare that an evolution plan should be implemented by reusing a prior task's branch — and has `implement()` honour that by basing the evolution branch on the task branch, allowing optional refinement, and shipping the result through the normal PR pipeline.

## Design — simplest viable

- **LLM-elective via a single field.** Add `EvolutionPlan.source_task_id: str | None = None` (default preserves every existing call-site, JSONL row, and test fixture). No auto-promotion; the LLM picks. `Memory._parse_plan_line` already filters JSON keys against the dataclass field set, so old rows simply load with `source_task_id=None` — no schema-fingerprint bump required.
- **Reflection signal.** `reflect()` appends a `branch: ...` line per kept task whose `branch` is set, with an inline hint about `source_task_id`. Single source of truth for the LLM's awareness that the field exists.
- **Validation.** `propose_plan._validate_source_task_id(raw)` accepts the field only when (a) it matches a recent `TaskRecord.task_id` in `get_recent_tasks(30)`, (b) outcome is `"success"` or `"partial"`, (c) `record.branch` is non-empty. Otherwise drop with `Plan proposed with invalid source_task_id %r — falling back to fresh-from-HEAD`. The LLM cannot invent IDs that survive review.
- **Implement-phase rebase.** `implement()` resolves a `base_ref`. Default `"main"`. When `plan.source_task_id` is set, look up the record via the new `Memory.get_task_record(task_id)` helper, confirm `vcs.rev_parse(f"refs/heads/{task_branch}")` returns a hash, `checkout(task_branch)` + `fetch("origin")` + `merge_from("origin/main")`. On clean merge, `base_ref = task_branch`. On conflict (or any `VCSError` during the rebase): `abort_merge`, `checkout(_orig_branch)`, log demote, fall back to fresh-from-`main`. Position HEAD on `base_ref`, then `checkout_new_branch(branch)`. The stale-branch retry path also re-positions on `base_ref` after the delete.
- **Empty-refinement guard.** Replace `if not has_changes(): raise` with: `stage_all`; if `has_changes()`, commit; elif `base_ref == "main"`, raise. On the source-task path, refinement may add nothing — we ship the existing task commits as the entire change. `base_hash` (captured before the LLM step) still anchors `_self_review` correctly: when no refinement runs, the diff vs `base_hash` shows the original task commit(s), which is exactly what the reviewer should see.
- **PR body lineage.** When `plan.source_task_id` is set, append one line: `Builds on task {source_task_id} (branch reused from prior self-directed work).` Title and base branch unchanged.
- **Prompt update.** `EVOLUTION_PROMPT` gains a `TASK BRANCH REUSE:` paragraph; the in-prompt JSON schema example in `propose_plan` adds the optional field with explicit `null` default.

## Self-referential proof-of-concept

The same architectural pattern as proposals 044 (audit-stall actuator) and 046 (limitation→evolution bridge): a soft surface (kept task branches) is mirrored into a hard surface (the `propose_plan` JSON schema and `implement` rebase logic) by deterministic code. The LLM cannot route around the surfacing — every `reflect()` cycle pushes every kept task branch into the proposal context. The same fuzzy-match-style validation that anchors `linked_question` to `open_questions` here anchors `source_task_id` to a real `TaskRecord`. Soft self-direction acquires a mechanical loop pathway from act → evolve → ship.

## Risks & edge cases

1. **Stale task branch vs `main`.** Always `fetch origin` and `merge_from("origin/main")` into the task branch *before* basing the evolution branch on it. This puts the local branch in the same state `tend_evolution_prs` would later force, so post-implement quality gates start from a clean tree. Conflict → abort merge → fall back to fresh-from-`main` and log the demote.
2. **Retry path** (`failed` plan with `failure_count < 3`). `implement()` is re-entered; the existing stale-branch handler already deletes a leftover `maestro/evolve/{plan_id}` and recreates it. The new resolution block re-reads `record.branch` and re-`rev_parse`s each retry, so a task branch that disappears between attempts cleanly demotes to fresh-from-`main`. Idempotent.
3. **Branch existence check.** `vcs.rev_parse(f"refs/heads/{task_branch}")` returns `None` on missing. No new VCS helper needed.
4. **`/prune-branches` skill** (in `~/.claude/skills/`) deletes local branches whose origin counterpart is gone. Task branches are never pushed, so they are exactly what `/prune-branches` deletes. After a prune run, every then-existing kept task branch becomes unreachable and the reuse path silently demotes for those plans. Documented in `CLAUDE.md` and `docs/self-evolution.md`.
5. **Empty refinement.** Already handled — only raise on the fresh-from-`main` path; on the source-task path, accept a no-op refinement and ship the task commits as the entire change.
6. **Multiple relevant task branches.** `source_task_id` is single-valued by design. The LLM picks one; others can be referenced in `rationale` text. Avoids combinatorial explosion in the rebase logic and keeps the lineage hint unambiguous in the PR body.
7. **Non-`self_directed` tasks** (`source="external"`). No restriction; if `record.branch` is set, reuse is allowed. External tasks are how Thomas drives Maestro through the CLI; if such a task left a branch, the LLM may legitimately want to ship it.
8. **LLM hallucinated id.** Validation in `propose_plan` rejects any value that doesn't anchor to a real recent record with the right shape, with a warning log; the plan still saves with `source_task_id=None`.

## Future bridge candidates (out-of-scope)

- **Multi-branch reuse.** Cherry-pick or merge multiple task branches into one evolution. Avoided here for the same reason `linked_question` is single-valued: combinatorial state.
- **Task-branch promotion without an evolution plan.** A "ship this task directly" path that skips `propose_plan` entirely. Proposed separately if the bridge here proves Maestro's task work is consistently PR-ready.
- **Automatic `source_task_id` suggestion.** A pre-`propose_plan` heuristic (most recent task with branch, or task whose `key_findings` overlaps the open issue) that pre-fills the field. Avoided here to keep the surface LLM-elective and observable.
