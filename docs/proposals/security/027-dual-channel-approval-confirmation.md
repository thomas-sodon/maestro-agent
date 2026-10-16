# 027: Dual-Channel Approval Confirmation

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | proposed |
| **Affects** | `maestro/types.py`, `maestro/approvals.py`, `maestro/dashboard/routes.py`, `maestro/notifications/telegram.py`, `maestro/agent.py`, `maestro/config.py` |

## Description

For `priority in {bug_fix, security}` plans, require confirmation on both the dashboard and Telegram before `_implement_approved_plans` will treat the plan as approved. A single compromised channel no longer authorises implementation. Low-priority plans keep the single-channel flow. Builds on proposal 023 (signed approvals) — each channel writes its own signed row; `verify_latest` becomes `verify_quorum(plan_id, hash, required_sources)`.

## Relevant Findings

- `EvolutionPlan.priority` already exists (`"bug_fix"`, `"performance"`, `"security"`, `"feature"`).
- Two ingress paths exist and already share `update_plan_status` via the proposal-023 store: dashboard `approve_plan_action` and Telegram `_on_approve`.
- Telegram state is already persistent in the plan row (`notified_content_hash` from proposal 021).

## Implementation Plan

1. Extend `PlanStatus` in `maestro/types.py` with `"partially_approved"`, then audit every exhaustive status match: `_implement_approved_plans` in `maestro/agent.py` (the `status not in ("approved", "failed")` guard and the `status == "approved"` selection), and the dashboard approval guards in `maestro/dashboard/routes.py` (`approve_plan_action`), so the new transitional state is handled rather than silently treated as un-approved.
2. Extend `AppendOnlyApprovalStore` with `verify_quorum(plan_id, expected_hash, required_sources: set[str]) -> list[ApprovalRecord] | None` — returns the list only when every required source has a valid row matching the expected hash. This source-keyed quorum is sound *only because* proposal 023's signature binds `source` (and `approver_subject`) into the HMAC payload: without that binding a single compromised channel could mint one signed row and replay its signature as the other channel's row, satisfying the quorum from one side and silently defeating the dual-channel requirement.
3. New config `[evolution] dual_channel_priorities: list[str]` (default `["bug_fix", "security"]`). Empty disables the feature.
4. Dashboard approval for a dual-channel plan writes its signed row but does not flip `status` to `approved` yet — flips to a new transitional `partially_approved`. Telegram approval does the same from the other side. `_implement_approved_plans` only implements when the quorum is satisfied.
5. Both channels receive the plan notification immediately on plan creation (the existing proactive `notify_plan_for_review` flow). After the first signed row lands, the dashboard surfaces an "awaiting Telegram confirmation" banner and Telegram emits a one-time reminder ("dashboard already approved — your confirmation still required"); after the second signed row lands, `verify_quorum` returns the row pair and `_implement_approved_plans` proceeds. A Telegram-only or dashboard-only reviewer therefore never has to wait on the other channel to discover the plan.

## Testing Strategy

1. Single-channel approval for a `security` plan → `partially_approved`, no implementation.
2. Both-channel approval in either order → `approved`, implementation proceeds.
3. Hash drift between the two channels → quorum fails, plan demoted.
4. Low-priority plans unaffected by the quorum rule.
5. Dashboard-then-dashboard (i.e. same channel twice) does not satisfy quorum.

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Stuck `partially_approved` plans clutter the queue | Low | TTL via `last_pr_reminder_at` extends naturally; an auto-demotion after 7 days is optional |
| Telegram outage blocks all high-priority approvals | Medium | Config allows overriding the required sources list per deployment |
| UX confusion ("I already approved!") | Medium | Explicit status value (`partially_approved`) + dashboard banner |
