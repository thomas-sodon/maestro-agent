# 021: Plan Content-Hash Binding

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/memory.py`, `maestro/migration.py`, `maestro/dashboard/routes.py`, `maestro/notifications/telegram.py`, `maestro/agent.py` |

## Description

> Status updated to shipped on 2026-08-07 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Today there is no cryptographic binding between the `EvolutionPlan` a reviewer sees in the dashboard / Telegram notification and the plan the loop implements. `evolution_plans.jsonl` is a mutable JSONL file on a shared Docker volume; any process with write access can edit `proposed_changes` after approval, and the loop will implement the tampered version. Add a SHA-256 `plan_content_hash` over the plan's immutable fields. The hash is stamped at creation, surfaced in every UI and Telegram path, and echoed back on approval so mismatches can be rejected before `_implement_approved_plans()` runs. Proposal 023 (signed approvals) consumes this hash as part of its HMAC payload; standing alone, 021 already closes the race where a user approves plan version A and the loop implements plan version B.

## Relevant Findings

- `EvolutionPlan` at `maestro/memory.py` has no integrity field — serialization is `json.dumps(asdict(plan))` at `maestro/memory.py`.
- `update_plan_status()` at `maestro/memory.py` rewrites the plan in place under `file_lock` but performs no content check.
- `save_evolution_plan()` at `maestro/memory.py` is the sole creation path.
- Dashboard approval handler `approve_plan_action()` at `maestro/dashboard/routes.py` takes no request body and performs no content validation.
- Telegram approval `_on_approve` / `_on_reject` in `maestro/agent.py` mirror the same gap.
- Schema migrations run through `ensure_schema()` in `maestro/migration.py` via SHA-256 fingerprints, which is the hook point for the one-shot hash backfill.

## Implementation Plan

1. Extend `EvolutionPlan` with `plan_content_hash: str = ""` and `notified_content_hash: str = ""` (sentinel `""`, not `Optional`, keeps mypy usage flat).
2. Add `compute_plan_content_hash(plan) -> str` in `maestro/memory.py`: `hashlib.sha256` over a canonicalised tuple `(title.strip(), rationale.strip(), tuple(proposed_changes), tuple(files_affected), expected_stage_impact.strip(), category, priority)` serialised via `json.dumps(..., sort_keys=True, separators=(",", ":"))`. Exclude `status`, `created_at`, `reviewed_at`, `pr_url`, `rejection_reason`, `rejection_learned`, `last_pr_reminder_at`, `pr_reminder_count`, `last_conflict_resolution_attempt_at`.
3. `save_evolution_plan()` computes and stamps the hash before persistence.
4. `_load_all_plans()` computes in-memory when the stored hash is empty (forward-compat for legacy rows); never overwrites a non-empty stored hash on load.
5. `ensure_schema()` in `migration.py` gains a one-shot `migrate_backfill_hashes()` that rewrites `evolution_plans.jsonl` once under `file_lock` to persist every backfilled hash. Idempotent (no-op if all plans already carry a hash).
6. Dashboard `GET /api/plans/{plan_id}` response already serialises the full plan dict, so the hash travels to the UI for free. Render it in the plan detail view and inject it as a hidden form field on approve/reject.
7. `POST /api/plans/{plan_id}/approve` and `/reject` gain a Pydantic body `{content_hash: str}` with `model_config = ConfigDict(extra="forbid")`. `approve_plan_action()` and `reject_plan_action()` compare against the currently stored hash and raise `PlanContentHashMismatchError` (maps to HTTP 409).
8. `notifications/telegram.py:notify_plan_for_review` persists the hash at notify-time via `update_plan_status(..., notified_content_hash=h)` on the same plan row. Telegram callback handlers in `agent.py` re-read the plan, verify `plan_content_hash == notified_content_hash`, and refuse the transition on drift.

## Testing Strategy

1. `compute_plan_content_hash` is insensitive to leading/trailing whitespace on `title` and `rationale`.
2. Two plans differing only in `status` / `reviewed_at` / `pr_url` produce identical hashes.
3. `save_evolution_plan` round-trips: load → hash matches stored hash.
4. Backfill migration populates empty hashes, leaves non-empty hashes untouched, and is a no-op on the second invocation.
5. Dashboard approve with stale hash → 409 and the plan row is not mutated.
6. Telegram callback with drifted `notified_content_hash` → no status transition and the failure path edits the message with an error banner.
7. Pydantic `extra="forbid"` rejects unexpected fields.

Run: `python3.12 -m pytest tests/test_memory_plans.py tests/test_dashboard_routes.py tests/test_telegram_approval.py -v`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Canonicalisation differs between write and verify | Medium | Centralise in `compute_plan_content_hash`; one function used everywhere |
| Backfill rewrite corrupts `evolution_plans.jsonl` on crash | Low | `atomic_write` + `file_lock`; migration is re-runnable |
| UI approval form drops the hidden hash field | Low | Pydantic `extra="forbid"` + required field fails closed |
| Notify-time hash collides with approve-time content edit | Accepted | That is exactly the attack we detect; 409 is the desired outcome |
