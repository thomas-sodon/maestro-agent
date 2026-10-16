# 066: Replicating the Action-Gate Across the CLI-Paced Subprocess Boundary

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/tool_gating.py`, `maestro/providers/cli_paced.py`, `maestro/providers/capability_gate.py`, `maestro/cli.py`, new `maestro/providers/cli_paced_gate.py`, `docs/sdk-internals.md`, `CLAUDE.md`, tests |
| **Depends on** | 056 (shipped), 058 (shipped), 061 (shipped) |

## Description

> Status updated to shipped on 2026-06-12 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Proposal 061 makes `cli_paced` a *capability-qualified* provider: once its eight
`Capabilities` flags flip to `True`, `maestro/providers/capability_gate.py` will
route it to any phase. But 061 explicitly stops at capability and defers the
trust question — "the eight flags describe capability, not trust." The
load-bearing gap is that Maestro's in-process `PreToolUse` action-gate
(`maestro/tool_gating.py`), which blocks PR-mutating and protected-ref-push Bash
commands during task execution, runs *inside Maestro's process* and cannot
intercept the separate `claude` subprocess the CLI-paced provider drives. Flip
the flags without closing this gap and routing `cli_paced` to `run_task` /
`implementation` would run task-execution Bash with no mutation guard — a direct
violation of the CLAUDE.md "Human-in-the-loop evolution" invariant.

This proposal closes exactly that gap and nothing more: it replicates the
in-process gate inside the CLI's own permission system, using a single shared
source of truth for the gate logic so the two enforcement paths cannot drift,
and it makes it a startup error to route a provider to an action-gated phase
without that guard in place. It is the security prerequisite for ever turning
`cli_paced` on for real work; the operational act of *turning it on* — canary
phase selection, login preconditions on the loop host, observability under a
non-default provider, and rollback — is the sibling rollout proposal (067),
which depends on this one.

End state: the loop's mutation guard fires over the CLI subprocess exactly as it
does in-process today — proven by an adversarial test that seeds the same
PR-mutating command set the in-process gate is tested on and asserts the
CLI-side gate denies it with byte-for-byte parity — and `capability_gate`
refuses at config-load to route an un-gated provider to a mutating phase. The
default stays `[sdk] provider = "anthropic"`; every step is inert until an
operator opts in via 067.

## Relevant Findings

- `maestro/tool_gating.py` (`evaluate_bash_command`) — the pure, synchronous
  core of the guard: returns a deny-reason string or `None`, unwraps
  `bash -c` / `sh -c` wrappers, allows commits/pushes to the run's own task
  branch, and denies PR mutations (`gh pr create|merge|ready`, mutating
  `gh api …/pulls`) and protected-ref pushes fail-closed. Because it is pure and
  branch-parameterised, it can back a second enforcement path without
  duplicating logic.
- `maestro/tool_gating.py` (`make_task_pretooluse_hook`, `make_task_tool_hooks`)
  — the in-process enforcement path: an SDK `PreToolUse` hook scoped to `Bash`,
  attached via `QueryOptions(hooks=…)` in the executor. This is the path that
  does **not** reach a subprocess, which is the gap this proposal closes.
- `maestro/providers/cli_paced.py` (`_tool_policy_flags`, `_build_cli_command`,
  `_ALLOWED_TOOLS_FLAG`, `_DISALLOWED_TOOLS_FLAG`) — `cli_paced` already folds
  tool policy onto the spawned `claude`'s own `--allowedTools` /
  `--disallowedTools` flags. The same `_build_cli_command` seam is where a
  gate-bearing settings payload would be injected on spawn via the `claude`
  CLI's `--settings <file-or-json>` flag, which accepts an inline JSON string
  (not only a file path), exactly as `--mcp-config` does — so the gate rides
  the spawn argv with no on-disk settings file.
- `maestro/providers/cli_paced.py` (`_reject_unsupported_options`) — `hooks`
  stays a rejected option even after 061, precisely because the in-process hook
  cannot cross into the subprocess. This proposal supplies the subprocess-native
  substitute rather than making `hooks` forwardable.
- `maestro/providers/capability_gate.py` (`validate_provider_capabilities`,
  `required_capabilities_for_phase`) — startup validation that fails loud when a
  phase's provider lacks a required `Capabilities` flag. It validates capability
  only; there is no equivalent assertion that an action-gated phase's provider
  carries the mutation guard — the coupling this proposal adds.
- `maestro/config.py` (`provider_for_phase`, `sdk_provider`,
  `sdk_phase_providers`) — phase→provider routing via `[sdk] provider` and
  per-phase `[sdk] <phase>_provider`. This is the routing the new gate-coverage
  check must inspect to decide which phases are action-gated.
- `maestro_sdk/src/maestro_sdk/approvals.py` (`ApprovalHandler`, `ApprovalPolicy`,
  `approve_all`) — deny-by-default modal handling that raises
  `ApprovalRequiredError` when no policy approves. For an autonomous loop this is
  the right backstop (fail loud, never auto-approve), so the gate must drive the
  CLI such that *no* approval modal appears for permitted tools — via
  `--allowedTools` plus the inline `--settings` gate — rather than relying on a
  permissive policy.
- `tests/test_fault_seeding.py` — the project's adversarial fault-seeding
  discipline (seed a known fault, assert which gate catches it). The new
  CLI-side gate must be proven the same way, against the same ground-truth
  command set the in-process gate is tested on.

## Implementation Plan

Steps are ordered so the trust control lands and is proven **before** any
routing change becomes possible, and so an interrupted run never leaves a
mutating phase routed to an un-gated subprocess. Phase 1 builds and proves the
gate replication; Phase 2 makes it impossible to route around. Everything stays
inert under the default `anthropic` provider, and no phase is actually switched
to `cli_paced` here — that is 067's job.

### Phase 1 — Replicate the action-gate inside the CLI subprocess

1. [x] **Shared gate entrypoint.** New `maestro/providers/cli_paced_gate.py` +
   `maestro/cli.py` — expose a hidden CLI subcommand (e.g.
   `maestro _gate-bash-hook`) that reads the `claude` CLI's `PreToolUse` hook
   payload on stdin and emits the CLI's allow/deny JSON, delegating the decision
   to the existing `tool_gating.evaluate_bash_command` so there is exactly one
   gate implementation. The allowed branch is passed via an env var the spawning
   provider sets. No change to `evaluate_bash_command` itself.
   - *Depends on:* none.
   - *Acceptance:* invoking the subcommand with a PR-mutating command on stdin
     emits a deny verdict and with an own-branch push emits an allow verdict,
     byte-for-byte matching `evaluate_bash_command`'s decision for the same
     inputs (table-driven over the in-process gate's fixtures).
2. [x] **Materialise a gate-bearing CLI settings payload.**
   `maestro/providers/cli_paced_gate.py` — build the `claude` CLI settings
   payload that registers the Step 1 subcommand as the CLI's own `PreToolUse`
   `Bash` hook, serialised inline as a JSON string passed via `--settings`
   (no temp file, per the no-real-IO unit-test discipline, mirroring the
   `--mcp-config` inline-JSON pattern). The settings carry the run's task
   branch so the subprocess gate is branch-scoped exactly like the in-process
   one.
   - *Depends on:* 1.
   - *Acceptance:* the materialised settings JSON registers a `PreToolUse` Bash
     hook pointing at the gate subcommand and carries the supplied branch;
     asserted against the structure the CLI consumes, with no filesystem write.
3. [x] **Inject the gate on action-gated spawns.**
   `maestro/providers/cli_paced.py` (`_build_cli_command`) — when the provider
   runs an action-gated phase (`run_task` / `implementation`), fold the Step 2
   settings onto the spawn argv alongside the existing tool-policy flags, and set
   the allowed-branch env var on the child. Keep the deny-by-default
   `ApprovalHandler` posture as a backstop: configure `--allowedTools` so
   permitted tools raise no modal, and let any unexpected modal fail loud rather
   than auto-approve.
   - *Depends on:* 2.
   - *Acceptance:* a `run_task` spawn carries the gate settings and branch env
     (asserted against the fake-runner argv/env); a non-action-gated phase spawn
     does not.
4. [x] **Adversarial cross-boundary parity test.**
   `tests/test_fault_seeding.py` + `tests/test_provider_cli_paced.py` — seed the
   same PR-mutating / protected-ref-push command set the in-process gate is
   tested on, drive it through the CLI gate seam, and assert deny parity; seed an
   own-branch commit/push and assert allow parity. This is the test that proves
   the trust chain holds over the subprocess.
   - *Depends on:* 3.
   - *Acceptance:* every command the in-process gate denies, the CLI-side gate
     also denies, and every command it allows, the CLI-side gate also allows —
     one shared fixture table, two enforcement paths, zero divergence.

### Phase 2 — Make un-gated routing impossible

5. [x] **Action-gate coverage check at startup.**
   `maestro/providers/capability_gate.py` + `maestro/cli.py` — add a check,
   sibling to `validate_provider_capabilities`, that raises at config-load when a
   provider routed to an action-gated phase does not supply the mutation guard
   (in-process for `anthropic`, the Phase 1 CLI gate for `cli_paced`). This is a
   trust assertion, deliberately *separate* from the `Capabilities` flags (per
   061's "capability, not trust" boundary).
   - *Depends on:* 3.
   - *Acceptance:* routing `cli_paced` to `run_task` with the gate present
     loads clean; a hand-rolled provider routed to `run_task` with no gate fails
     loud at config-load naming the phase and the missing guard.

### Phase 3 — Document the mechanism

6. [x] **Document the trust-chain mechanism.** `docs/sdk-internals.md`,
   `CLAUDE.md` — document that the CLI-side gate replicates the in-process
   `PreToolUse` guard via the shared `evaluate_bash_command` entrypoint, and
   update the CLAUDE.md "Human-in-the-loop evolution" invariant line to cover
   both enforcement paths without growing past one line. Operational turn-on
   (canary, login precondition, rollback) is documented by 067.
   - *Depends on:* 5.
   - *Acceptance:* `docs/sdk-internals.md` describes the two-path gate with its
     single shared core; the CLAUDE.md invariant accurately covers both paths in
     its existing single line.

## Testing Strategy

Unit tests honour `mem_fs` + no-real-IO + no-real-network + no-real-`sleep`; the
`claude` CLI and pty stay mocked via the existing fake-runner seam and
`InstantCadence`, and the gate subcommand is exercised in-process (stdin/stdout
fixtures), never by spawning a real subprocess.

- `tests/test_tool_gating.py` (extend) — the Step 1 gate subcommand returns the
  same verdict as `evaluate_bash_command` across the full deny/allow fixture
  table, including `bash -c` unwrapping and own-branch allowance.
- `tests/test_provider_cli_paced.py` (extend) — Step 2 settings materialise with
  the `PreToolUse` Bash hook and branch; Step 3 injects gate settings + branch
  env only on action-gated phases; non-mutating phases spawn without them.
- `tests/test_fault_seeding.py` (extend) — Step 4 cross-boundary parity: one
  shared seeded-fault table, asserted denied/allowed identically by the
  in-process hook and the CLI-side gate (the adversarial ground-truth test).
- `tests/test_capability_gating.py` (extend) — Step 5 coverage check: `cli_paced`
  on `run_task` with the gate present loads clean; a gate-less provider routed to
  a mutating phase fails loud; non-mutating phases are unaffected.

## Risk Assessment

This change is squarely on the trust chain, so the failure mode of concern is a
*false sense of coverage* — believing the mutation guard fires over the
subprocess when it does not.

- **Gate divergence between the two paths.** Two enforcement points (in-process
  hook, CLI settings hook) could drift, leaving the subprocess under-guarded.
  Mitigation: both delegate to the single `evaluate_bash_command` core, and the
  Step 4 parity test fails the build on any divergence across the shared fixture
  table. No second gate implementation is written.
- **CLI hook bypass / settings tampering.** The subprocess gate relies on the
  `claude` CLI honouring the injected settings hook; a CLI that ignored or
  mis-scoped it would run un-gated. Mitigation: the gate is injected on the spawn
  argv per call (not from operator-editable on-disk config), `--allowedTools`
  narrows the tool surface independently, and the deny-by-default
  `ApprovalHandler` backstops by failing loud on any unexpected modal rather than
  auto-approving. The Step 5 startup check refuses to route to a mutating phase
  unless the gate is wired.

**Rollback posture.** Default `[sdk] provider = "anthropic"` makes the entire
change inert — no phase routes to `cli_paced` until 067 turns it on. Every step
is additive: Phase 1 adds mechanism, Phase 2 adds a startup assertion that only
fires for a misconfiguration that does not exist under the default, Phase 3 is
docs-only. Reverting is a plain revert of the PRs.

**Data migration.** None. No schema or state-file shape changes.

## Out of scope

- **Turning `cli_paced` on in production** — the canary phase selection, the
  `claude login` precondition on the loop host, observability for the provider's
  unmetered usage rows, and the rollback runbook are proposal 067, which depends
  on this one. This proposal is the security prerequisite only.
- **Sandboxed sibling container for the CLI subprocess** (#026) — a stronger
  isolation ring than the action-gate; complementary, not a prerequisite.
- **Per-tool auto-approval policy for the CLI's approval modals** — deferred to
  056's approval-policy follow-up; deny-by-default stays the posture here.
- **Retiring `maestro/sdk.py` or the Anthropic provider** — `anthropic` remains
  the default and the trust-chain reference implementation indefinitely.
