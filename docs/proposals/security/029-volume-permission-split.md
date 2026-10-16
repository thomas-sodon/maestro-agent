# 029: Volume Permission Split for `evolution_plans.jsonl`

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | proposed |
| **Affects** | `docker-compose.yml`, `Dockerfile`, `maestro/dashboard/routes.py`, `maestro/agent.py` |

## Description

Both containers mount the shared data volume RW. The dashboard only needs to write `approvals.jsonl` and `messages.jsonl`; the loop is the only legitimate writer of `evolution_plans.jsonl`, `lifecycle.json`, journal files, usage.jsonl, etc. Split the volume mount so the dashboard sees `evolution_plans.jsonl` read-only and the loop sees `approvals.jsonl` read-only. Any intrusion on one container cannot falsify the other's data.

## Relevant Findings

- `docker-compose.yml` mounts `./data` as a single RW bind on both services.
- Dashboard writes today: `messages.jsonl`, and (after proposal 023) `approvals.jsonl`. Reads everything else.
- Loop writes today: every other file. Reads `messages.jsonl` and (after 023) `approvals.jsonl`.

## Implementation Plan

1. Subdivide the data directory into `./data/loop-owned/` (plans, journal, lifecycle, usage, dreams, memory) and `./data/dashboard-owned/` (messages, approvals).
2. `docker-compose.yml`: dashboard mounts `loop-owned:ro` and `dashboard-owned:rw`; loop mounts `loop-owned:rw` and `dashboard-owned:ro`.
3. Update every path constant in `maestro/config.py` / `maestro/memory.py` to live under the correct subdir. Provide a one-shot migration that moves files on first boot if legacy layout is detected.
4. Audit dashboard routes for any write to a loop-owned path; if found, refactor through the loop (either via a message or by appending an approval-like signed request that the loop consumes).

## Testing Strategy

1. With the split in place, attempt to write `evolution_plans.jsonl` from the dashboard container → `PermissionError` at the OS level.
2. Loop path-constants all resolve under `loop-owned/`.
3. Migration: with legacy layout, first boot moves files, leaves a breadcrumb, and does not double-move on second boot.
4. End-to-end smoke: signed approval in dashboard → loop picks up → implementation PR.

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Path refactor misses a callsite | Medium | `ruff` + `mypy` + integration smoke will surface stragglers |
| UID drift between containers breaks permission | Medium | Explicit `user: 1002:1002` in compose + documented data ownership |
| Migration moves files and crashes mid-move | Low | `atomic_write` + idempotent migration |
