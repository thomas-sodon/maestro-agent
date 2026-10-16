# 067: Turning On the CLI-Paced Provider — Canary, Observability, and Rollback

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/config.py`, `maestro/cost_tracking.py`, dashboard usage surface, `docs/deployment.md`, `docs/sdk-internals.md`, tests |
| **Depends on** | 056 (shipped), 058 (shipped), 061 (shipped), 066 (shipped) |

## Description

> Status updated to shipped on 2026-06-12 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Once proposal 061 gives `cli_paced` full capability parity and proposal 066
replicates the action-gate across the CLI subprocess boundary, the provider is
*safe to route* — but nobody has actually turned it on, and turning on a
non-default provider in an autonomous loop has its own operational hazards
distinct from the trust question. This proposal is the operate-it layer: the
runbook and guard-rails for promoting `cli_paced` from "selectable" to "serving
real loop phases," one phase at a time, with a way to see what it is doing and a
one-line path back.

The work is deliberately small and free of new enforcement logic — 066 owns the
trust control. Here we make a logged-out CLI a load-blocking precondition the
moment `cli_paced` is routed to a phase that must execute, surface the
provider's unmetered usage so an operator never misreads unknown spend as zero,
write the canary→promote→rollback runbook, and start the canary on a read-only
phase so the first production exposure carries no mutation risk at all.

End state: an operator can follow a documented procedure to route a single
non-mutating phase to `cli_paced`, watch attributed unmetered iterations confirm
it is healthy, promote to an action-gated phase under 066's guard, and revert to
`anthropic` in one config line if anything looks wrong — with preflight refusing
to start a mutating-phase route against a logged-out host.

## Relevant Findings

- `maestro/config.py` (`check_secrets`, `_check_provider_credential`,
  `_claude_cli_logged_in`) — `cli_paced` *selection* already requires a
  logged-in `claude` CLI, probed read-only via `claude auth status --json`.
  Today the credential check fires only for the *global* provider: both
  `_build_agent()` and `cmd_preflight()` call
  `check_secrets(provider=resolved_sdk_provider())`, which inspects only
  `sdk_provider` and never iterates `sdk_phase_providers`. So two distinct
  behaviours exist today: (1) global `[sdk] provider = "cli_paced"` with a
  logged-out CLI raises `SystemExit` via `_check_provider_credential` — a hard,
  startup-blocking error; (2) per-phase `[sdk] <phase>_provider = "cli_paced"`
  (the canary route Step 3 prescribes) bypasses the check entirely — no
  `SystemExit`, no warning, no login probe. This proposal *adds* the per-phase
  login precondition that does not exist today and then makes it phase-aware: a
  hard error when a phase that must execute is routed to `cli_paced`, and a
  warning when only non-mutating phases are; the existing global-provider hard
  error is preserved.
- `maestro/config.py` (`provider_for_phase`, `sdk_provider`,
  `sdk_phase_providers`) — the phase→provider routing surface an operator uses to
  canary a single phase via `[sdk] <phase>_provider`, and the surface preflight
  must read to decide whether a mutating phase is routed to `cli_paced`.
- `maestro/sdk.py` (`IterationUsage.unmetered`) + `maestro/cost_tracking.py` —
  `cli_paced` declares `usage_metering=True`: it normally extracts token counts
  from the transcript sidecar and emits `unmetered=False` rows, degrading to
  `unmetered=True` zero-fill only when the sidecar is missing, unreadable, or
  malformed — which the burn-rate gate then treats as unknown spend rather than
  measured-as-zero. Canary routing puts `cli_paced` into production for the first
  time, giving the operator their first look at both the metered sidecar path and
  the `unmetered=True` fallback; neither row currently carries which provider
  produced it.
- `maestro/providers/capability_gate.py` (`validate_provider_capabilities`) +
  proposal 066's action-gate coverage check — together these already fail loud
  at config-load for an unsafe route. This proposal adds the *operational*
  precondition (login) on top of the *trust* precondition (gate), not a second
  trust gate.
- `maestro_sdk/src/maestro_sdk/approvals.py` (`ApprovalHandler`) — deny-by-default
  means a wedged or unexpectedly-prompting CLI fails loud rather than
  auto-approving; the canary runbook leans on this so the first exposure is
  observable, not silently stuck.
- `docs/deployment.md` — the existing operator runbook surface (secrets matrix,
  container entrypoint, deploy procedure) where the canary→promote→rollback
  procedure belongs.

## Implementation Plan

Steps are ordered so the safety precondition lands before the canary procedure
that depends on it, and so nothing actually routes to `cli_paced` until an
operator chooses to. All steps are inert under the default `anthropic` provider.

1. [ ] **Login precondition for `cli_paced` phase routing.**
   `maestro/config.py` (`check_secrets` / `_check_provider_credential` and the
   `_build_agent()` / `cmd_preflight()` call sites) — today
   `check_secrets(provider=resolved_sdk_provider())` checks only the global
   provider, so per-phase `cli_paced` routing has no login gate at all. Extend
   the check to also cover every provider named in `sdk_phase_providers`, then
   make the verdict phase-aware: a hard `SystemExit` (keeping the
   `claude auth login` remediation) when `cli_paced` is routed to a phase that
   must execute (`run_task` / `implementation`) — where a logged-out CLI cannot
   run the task at all — and a warning when `cli_paced` routes only non-mutating
   phases. The existing global `[sdk] provider = "cli_paced"` hard error is
   preserved.
   - *Depends on:* none.
   - *Acceptance:* `cli_paced` routed to a mutating phase (`run_task` /
     `implementation`) with no CLI login — whether selected globally or
     per-phase — fails preflight with a `claude auth login` remediation;
     `cli_paced` routing only a non-mutating phase produces a warning (newly
     added — no per-phase credential check exists today) rather than blocking
     load; the existing global-provider hard error and the default `anthropic`
     path are unchanged.
2. [ ] **Provider attribution on usage rows.** `maestro/cost_tracking.py` +
   dashboard usage surface — carry the producing provider name alongside
   `IterationUsage.unmetered` so an operator can see, per iteration, that spend
   is unknown-by-design under `cli_paced` rather than silently zero, and confirm
   the burn-rate gate is treating it correctly.
   - *Depends on:* none.
   - *Acceptance:* iterations served by `cli_paced` render with the provider
     attributed and spend shown as unknown (not `$0`); `anthropic` rows and the
     burn-rate throttle input are unchanged.
3. [ ] **Canary runbook on a non-mutating phase.** `docs/deployment.md` —
   document routing a single read-only phase (e.g. `contemplation`) to
   `cli_paced` first via `[sdk] contemplation_provider`, the validation signal
   (clean iterations, expected attributed unmetered rows, no `ApprovalRequiredError`),
   and the go/no-go criterion before promoting to an action-gated phase under
   066's guard.
   - *Depends on:* 1, 2.
   - *Acceptance:* the runbook describes the canary phase, the health signal,
     and the explicit go/no-go gate before any mutating-phase promotion.
4. [ ] **Promote + rollback runbook.** `docs/deployment.md`,
   `docs/sdk-internals.md` — document promoting from the canary phase to an
   action-gated phase (citing 066's gate as the precondition), and the one-line
   rollback: revert `[sdk] provider` / `[sdk] <phase>_provider` to `anthropic`,
   inert on the next iteration with no state migration.
   - *Depends on:* 3.
   - *Acceptance:* `docs/deployment.md` carries the full
     canary→promote→rollback procedure with the rollback shown as a single
     config revert effective next iteration.

## Testing Strategy

Unit tests honour `mem_fs` + no-real-IO + no-real-network + no-real-`sleep`; the
`claude` CLI login probe and any subprocess stay mocked.

- `tests/test_config.py` (extend) — Step 1: `cli_paced` on a mutating
  phase with no CLI login fails preflight; `cli_paced` routing only a
  non-mutating phase emits a warning (newly added — no per-phase credential
  check exists today); the global-provider hard error and `anthropic` are
  unaffected.
- Cost-tracking / dashboard tests (extend) — Step 2: `unmetered` rows render with
  provider attribution and unknown spend; metered `anthropic` rows are unchanged
  and the burn-rate input is untouched.
- Steps 3–4 are documentation; their correctness is the runbook review, not a
  code test.

## Risk Assessment

The failure mode of concern here is operational, not trust (066 owns trust): an
operator turning the provider on without a clear picture of its health, or a
wedged subprocess stalling the loop.

- **Logged-out / wedged CLI on a mutating phase.** A logged-out or hung CLI
  subprocess could stall task execution. Mitigation: Step 1 makes login a
  load-blocking precondition for mutating-phase routing, the existing
  `stream_idle_timeout_seconds` + SIGTERM→SIGKILL teardown ladder bounds a wedged
  child, and deny-by-default `ApprovalHandler` fails loud rather than hanging on a
  modal.
- **Unmetered spend read as zero.** Canary routing can put `unmetered=True` rows
  into production when the sidecar fallback fires; an operator could misread
  unknown spend as no spend. Mitigation:
  Step 2 attributes the provider and renders spend as unknown, and the canary
  starts on a read-only phase so the first exposure carries no mutation risk while
  the operator calibrates on the new telemetry.
- **Premature promotion to a mutating phase.** Skipping the canary and routing
  `run_task` directly. Mitigation: the runbook's explicit go/no-go gate, and
  066's startup coverage check, which refuses an un-gated mutating route
  regardless of this runbook.

**Rollback posture.** Default `[sdk] provider = "anthropic"` makes the whole
change inert; the documented rollback is a single config revert effective on the
next iteration, with no state migration. Steps 1–2 only change behaviour for an
operator who has already opted into `cli_paced`.

**Data migration.** None. `usage.jsonl` already tolerates `unmetered` rows
(058 Step 13); adding provider attribution is forward-compatible.

## Out of scope

- **The subprocess action-gate itself** — replicating the in-process mutation
  guard across the CLI boundary is proposal 066, this proposal's hard
  prerequisite. Here we only turn on the already-gated provider.
- **Routing `cli_paced` as the global default** — this proposal enables
  per-phase opt-in and a canary; making it the default indefinitely is a separate
  decision once the canary has soaked.
- **Capability parity itself** (061) and **slash-command metrics** (062) — the
  provider's capabilities and richer observability land on their own tracks.
