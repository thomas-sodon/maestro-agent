# 023: Append-Only Signed Approval Log

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | proposed |
| **Affects** | `maestro/approvals.py` (new), `maestro/memory.py`, `maestro/agent.py`, `maestro/dashboard/routes.py`, `maestro/notifications/telegram.py`, `maestro/config.py`, `CLAUDE.md` |

## Description

Today the single source of truth for "is this plan approved?" is a mutable field inside `evolution_plans.jsonl`. Any process with volume write access can flip `status` from `pending_review` to `approved`. This proposal replaces the status flip with an append-only, HMAC-signed audit log (`approvals.jsonl`) and teaches `_implement_approved_plans()` to require a valid signed record before running. Tampering with `evolution_plans.jsonl` is therefore observable (hash mismatch) and non-authoritative (no signed row → no implementation). Depends on proposal 021 (the hash is part of the HMAC payload).

## Relevant Findings

- File-persistence conventions documented in CLAUDE.md: "JSONL files are append-only (`cost_samples.jsonl`, `usage.jsonl`, `messages.jsonl`). Read-modify-write operations use `file_lock()`." The existing patterns in `maestro/cost_tracking.py` and `maestro/messaging.py` are the templates to follow.
- Approval handlers `approve_plan_action` / `reject_plan_action` at `routes.py` and Telegram `_on_approve` / `_on_reject` in `agent.py` are the two ingress points.
- `_implement_approved_plans` at `maestro/agent.py` is the consumer.
- `check_secrets()` in `maestro/config.py` validates required env vars at startup — hook point for `MAESTRO_APPROVAL_SIGNING_KEY`.

## Implementation Plan

1. New `maestro/approvals.py` defining `ApprovalRecord` dataclass with fields `plan_id, plan_content_hash, decision ("approved" | "rejected"), source ("dashboard" | "telegram" | "legacy"), approver_subject, timestamp, reason, signature` and class `AppendOnlyApprovalStore` at `<memory_dir>/approvals.jsonl` with:
   - `append(record) -> None` — serialises, HMACs, appends under `file_lock`. The signature MUST cover every verifier-trusted field — `plan_id, plan_content_hash, decision, source, approver_subject, timestamp, reason` (the full record minus `signature` itself, in this canonical order) — so a future implementer cannot quietly reintroduce a replay gap by signing a subset (see step 2 for the exact payload).
   - `verify_latest(plan_id, expected_hash) -> ApprovalRecord | None` — returns the most recent record whose signature verifies over all those trusted fields AND whose stored hash matches `expected_hash`; else `None`.
   - `list_for(plan_id) -> list[ApprovalRecord]` — chronological audit trail.
2. Signing: `hmac.new(secret, f"{plan_id}|{plan_content_hash}|{decision}|{source}|{approver_subject}|{timestamp}|{reason or ''}".encode(), "sha256").hexdigest()`. The payload binds every verifier-trusted field in the canonical `ApprovalRecord` field order (step 1), so the signer and `verify_latest` cannot diverge on payload construction. Binding `source` and `approver_subject` is load-bearing: a signature minted for one row must not validate when copied onto a forged row whose `source` or `approver_subject` differs (e.g. replaying a dashboard signature as a Telegram approval). Secret from new required env var `MAESTRO_APPROVAL_SIGNING_KEY`; validated in `check_secrets()` with a clear error on absence.
3. `update_plan_status()` at `maestro/memory.py` gains an `_approval_store: AppendOnlyApprovalStore | None` param. When transitioning to `approved` or `rejected`, under the file lock, recompute the plan's hash and call `verify_latest(plan_id, hash)`; refuse the transition on `None`. A `_skip_signed_check=True` bypass exists for tests only.
4. Dashboard `approve_plan_action` / `reject_plan_action` — write the signed row via `approval_store.append(...)` first, then `update_plan_status`. Attribute `approver_subject="dashboard"` (until per-user identity lands).
5. Telegram `_on_approve` / `_on_reject` — same flow with `source="telegram"` and `approver_subject="telegram:{chat_id}"`.
6. `_implement_approved_plans` — for every `status == "approved"` plan, `verify_latest(plan.plan_id, plan.plan_content_hash)` before calling `implement_plan()`. On failure, demote to `pending_review`, journal a `realization` entry tagged `security`, open a GitHub issue, skip. See "Orphan-row invariant" below for why an orphan signed row without a status flip is harmless.
7. Migration — plans already past `pending_review` at rollout time are grandfathered: `verify_latest` returns a synthetic `source="legacy"` record with `signature=""` for those plan ids when the store has no row and the plan's `reviewed_at` predates the migration timestamp. New approvals always require a real signature.

### Orphan-row invariant

Steps 4 and 5 append the signed row before flipping plan status, so a crash between `approval_store.append(...)` and `update_plan_status(...)` produces a transient state — a signed row in `approvals.jsonl` for a plan whose status is still `pending_review`. This window is acceptable: `_implement_approved_plans` (step 6) iterates only plans with `status == "approved"` *and* additionally requires `verify_latest(plan_id, plan_content_hash)` to succeed before calling `implement_plan()`. The orphan signed row therefore cannot, on its own, drive an implementation; the reviewer's eventual re-click flips status and supplies a fresh row whose hash matches the (possibly drifted) plan content, and step 6's same AND-predicate then admits that combination. A signed row whose plan content has since drifted is silently filtered out by the `expected_hash` arm of `verify_latest`.

## Testing Strategy

1. Tamper: directly edit `evolution_plans.jsonl` to flip `status="approved"` without writing an approval row → `_implement_approved_plans` skips, demotes, journals (`tests/component/test_signed_approvals.py`).
2. Forgery: append a row with an incorrect signature → `verify_latest` returns `None`.
3. Replay resistance: take a row with a valid signature and copy that signature verbatim onto a second row differing only in `source` (or `approver_subject`) → `verify_latest` returns `None`. Proves the signature binds `source`/`approver_subject`, so a signature minted for one channel cannot be replayed as another channel's row (the gap proposal 027's quorum depends on being closed).
4. Hash drift: approve with hash `H`, then edit the plan's `proposed_changes` so its current hash becomes `H'`. `_implement_approved_plans` rejects (`H != H'`).
5. Grandfathering: a plan created before rollout with `status="approved"` and no row → treated as legacy, implementation proceeds once.
6. Forward-compat: a row with an unknown extra field loads cleanly.
7. `MAESTRO_APPROVAL_SIGNING_KEY` missing on startup → `check_secrets()` raises a clear error.
8. Ingress parity: dashboard and Telegram approvals both produce valid rows with `source` correctly attributed.

Run: `python3.12 -m pytest tests/test_approvals.py tests/component/test_signed_approvals.py tests/test_agent_loop.py -v`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Secret key leaked on host | Accepted | Same trust model as `ANTHROPIC_API_KEY`; keys are env vars |
| `approvals.jsonl` grows unbounded | Low | Append-only JSONL; a future proposal can add compaction with an audit-preserving archive |
| Tests forget the bypass and fail confusingly | Low | `_skip_signed_check` default-False flag; fixture in `tests/conftest.py` sets it on a shared `Memory` builder |
| Legacy grandfathering window is abused | Low | Legacy path triggers only when `reviewed_at < migration_timestamp`; migration timestamp written once to `lifecycle.json` on first boot |
