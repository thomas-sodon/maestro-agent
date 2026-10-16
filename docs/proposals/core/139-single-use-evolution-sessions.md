# 139: Call-Owned SDK Policy — Retire Session B into Single-Use, Registry-Declared Calls

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | burn-in:observing |
| **What landed** | Step 1 — `maestro/call_policy.py` (`SessionScope`, `CallPolicy`, `CALL_POLICIES`) registering every live `caller` tag, with the ten evolution-lineage callers declared `SINGLE_USE`; `Consciousness.introspect` stamps `evolution_introspect` on the unpriced arm, grouped under `Contemplation` in `cost_tracking.CALLER_GROUPS`; `tests/test_call_policy.py` pins registry completeness against the tree, the effort vocabulary, and stateless-arm agreement. Step 2 — `single_use_call` in `maestro/call_policy.py`, the registry's first reader: it opens a session with the caller's policy, sends one `query_on_session` turn, and disconnects in `finally`, refusing `SHARED` and unregistered callers before any session opens; the ten `SINGLE_USE` entries are re-seeded to slot B's `tools=()` and `evolution` phase, pinned against `agent._SESSION_SLOT_PHASES`. Step 3 — `[sdk] single_use_evolution_calls_enabled` (default-off; `Config`, `_SdkSettings`, `maestro.toml`, `EXPERIMENTAL_FLAGS`) threaded into `Consciousness` / `PurposeEngine` / `Dreamer` via `_build_agent` and read by `EvolutionEngine` from config; flag-on, the ten `SINGLE_USE` callers ignore a passed session and run their stateless request (same system, schema — except `dream` / `dream_validate`, which keep the session arm's schema-free text-JSON contract (#4286) — and `complete_on`) through `single_use_call`; flag-off arms unchanged (`tests/test_single_use_evolution_calls.py`, PR #4238). Step 4 — flag-on, `_phase_evolve` passes `session=None` and `_curate_pending_plans` skips `_get_or_create_session`, so the loop never opens slot B and `_phase_dream` inherits `_session_b=None`; flag-off unchanged. Step 5 — `tests/test_call_policy.py` refuses a `query_on_session` attachment of any `SINGLE_USE` caller outside `maestro/call_policy.py` except a shrink-only allowlist of the ten flag-off slot-B arms (`_SINGLE_USE_SESSION_ARMS`, keyed module/qualname/tag), and checks `QueryOptions` `effort`/`thinking_budget_tokens` literals (including `**`-splatted dicts) against the registry for every registered caller, with seeded in-memory fault controls. |
| **What is missing** | Step 6 (burn-in observation window running via the `maestro.production.toml` `[sdk]` canary — criteria in the "Single-use evolution calls canary (proposal 139)" subsection of `docs/self-evolution.md` — then the default flip), step 7 (retire slot B and its workarounds). No behaviour-code remains to build. |
| **Affects** | new `maestro/call_policy.py`, `maestro/agent.py` (`_phase_evolve`, `_phase_curate_pending_plans`, `_phase_dream`, `_curate_pending_plans`, `evolve`, `_get_or_create_session`, `_SESSION_SLOT_PHASES`), `maestro/evolution.py` (`reflect`, `propose_plan`, `propose_plan_chain`, `curate_pending_plans`), `maestro/consciousness.py` (`introspect`, `assess_stage_readiness`), `maestro/purpose.py` (`generate_hypothesis`, `evaluate_hypothesis`), `maestro/dreaming.py` (`_run_dream_query`, `_validate_digest`), `maestro/sdk.py` (`_note_session_effort_override`), `maestro/cost_tracking.py` (`CALLER_GROUPS`), `maestro/config.py`, `maestro.toml`, tests |
| **Derived from** | live production data via /propose, 2026-09-24 |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

Session B is the loop's persistent "evolution" session. `_phase_evolve` opens it once per iteration at `effort="high"` with a 128K thinking budget, and every later phase in that iteration keeps using it: `introspect` (unpriced) → `reflect` → `propose_plan` → `curate_pending_plans` → `dream` (→ `dream_validate` → re-`dream` when validation is on). The `purpose_engine` and `stage_progression` calls, which are off in production, would join the same chain. Two things are fixed when a session is created: its effort, and every earlier turn's transcript. As a result, each of these calls runs with an effort someone else picked and reads a conversation someone else wrote. For example, `curate_pending_plans` is meant to act as an independent reviewer "making the human reviewer's decision easier". In practice it often reviews the plan it proposed about a minute earlier, with that plan's full authoring reasoning still in its context.

This is the same failure that has been patched one caller at a time since shared sessions were introduced in `169d8115`. The failure class: **a call's execution policy (effort, thinking budget, framing, and conversational context) is owned by whichever phase opened the shared session, not by the call itself. Any call attached to that session silently inherits another caller's effort and transcript.** Every past fix removed a single caller: `generate_task` became Session D, planner and distill moved to a run session "so the evolution transcript does not balloon", message replies moved off Session A (#1239), and rejection learning got its own pair of sessions. `#1238` then added a debug log so a dropped effort is at least visible. Each fix closed one leak. None changed the rule that produced it. Today the effort each call expects is written as a string literal in 17 places (the `_note_session_effort_override` calls) and nothing checks those literals against the session the call actually runs on.

The remedy makes **the call the owner of its policy**. A declarative registry (`maestro/call_policy.py`, `CALL_POLICIES`) maps each SDK `caller` to its effort, thinking budget, tool surface, provider phase and **session scope**. A single seam, `single_use_call`, runs a `SINGLE_USE` caller on a session created for that call alone: `create_session` → `query_on_session` → `disconnect_session`. That keeps the session-attachment invariant and its telemetry, turn-dedupe and provider routing. An AST guard then makes it structurally impossible to attach a `SINGLE_USE` caller to a shared session anywhere else. When the change is complete, Session B no longer exists. Each evolution-lineage call gets exactly the inputs its prompt passes explicitly (all of them already do), at the effort it declares. The remaining shared sessions (A, C, the run session and the rejection pair) each become a one-line registry decision rather than a new point fix.

## Relevant Findings

- `maestro/agent.py` (`_phase_evolve`) opens slot B through `_get_or_create_session(slot="b", …)` with `QueryOptions(effort="high", thinking_budget_tokens=128_000, caller="evolution_session")`. `_phase_curate_pending_plans` and `_phase_dream` then receive the same `self._session_b`.
- `maestro/agent.py` (`_curate_pending_plans`) opens slot B *itself*, with separate options (`caller="curate_session"`), when evolve did not run that iteration. So B's configuration depends on which phase reached it first.
- `maestro/agent.py` (`evolve`) passes the B session to `consciousness.introspect(…, price_reflection=False)`, `purpose.evaluate_hypothesis`, `purpose.generate_hypothesis`, `consciousness.assess_stage_readiness`, `evolution.reflect`, and `evolution.propose_plan` / `propose_plan_chain`.
- `maestro/sdk.py` (`query_on_session`) accepts no effort. The session's creation-time effort applies to every turn. `_note_session_effort_override` only logs at DEBUG when a caller's literal and a session are both present, and never compares the two values. It is called with a hand-written literal at 17 sites, including `introspect` ("medium") and `dream_validate` (docstring: "the session branch inherits the dream session's effort").
- Every B caller already receives its inputs **explicitly**, so the shared transcript only duplicates them. `reflect` reads `Journal.get_recent_narrative`, and `evolve` has already journaled the introspection. `propose_plan` embeds the reflection text verbatim ("Based on this reflection:"). `curate_pending_plans` formats `CURATE_PLAN_PROMPT` with the target plan's full body. `dream` builds its own raw-experience prompt. `_validate_digest` formats both the source summary and the digest.
- `maestro/dreaming.py` (`_run_dream_query`) and `maestro/evolution.py` (`reflect`) prepend their specialised prompt on the session path because the session carries the identity framing it was created with (the CLAUDE.md "specialized-prompt callers prepend it" rule). This is a workaround for a caller not owning its own framing.
- The CLAUDE.md invariant "Main-phase SDK calls must be session-attached" (commit `b41a212a`) exists to preserve prompt caching and session-aware turn dedupe in usage telemetry. A single-use session meets both requirements. The identity system prompt is a byte-stable prefix, so the server-side prompt cache serves it across connections inside its TTL. The rule was never meant to require long-lived sessions.
- `tests/test_session_reuse_phases.py` (`test_run_loop_session_b_reused_by_evolve_and_dream`, `test_run_loop_creates_session_b_in_evolve_phase_when_task_skipped`, `test_dream_session_path_prepends_dream_system_prompt`, `test_reflect_session_path_prepends_evolution_prompt`) pins sharing *as a behaviour*. `tests/test_session_effort_override.py` pins the log-only workaround.
- `maestro/loop/rejection_sessions.py` (`rejection_sessions`) and `maestro/loop/lifecycle.py` (`open_run_session`) show the same fix repeated twice: each hand-builds a phase-scoped session because a shared one carried the wrong effort.

## Production Evidence

Live data was read-only from the loop container's bind mount (`docker inspect maestro-loop` → `/workspace` source `…/maestro`, then `data/`). Window: **2026-08-16 → 2026-09-24** (the full retention of `cost_samples.jsonl` and `logs/sdk/` sidecars, 38 day-directories). Model throughout: `claude-opus-4-8`.

**1. The curator grades its own just-minted plan, and approves it.** In `logs/sdk/*/…curate_pending_plans…jsonl`, a curation was classified as *chained* when a `propose_plan` sidecar started less than 10 minutes earlier (the same B session). Its target plan title (from the `CURATE_PLAN_PROMPT` body) was compared with the title the chained `propose_plan` emitted:

| Curation population | n | `refine` | `obsolete` | `keep` |
|---|---|---|---|---|
| Chained, target = plan proposed moments earlier in the same session | 144 | 74 (51.4%) | 1 (0.7%) | 69 |
| Chained, target = some other pending plan | 516 | 37 (7.2%) | 31 (6.0%) | 447 |
| Standalone (fresh B opened by curate; no authoring transcript) | 503 | 27 (5.4%) | 12 (2.4%) | 463 |

22% of chained curations (144 of 660) target the plan the same conversation had just written. On those, the curator rewrites the plan 7× more often and almost never drops it. Worked instance: iteration 5850 on 2026-09-24. `propose_plan` minted "Score a deed-keyed introspect loss condition against deed_ledger" (`580dfc865323`), and the next turn on the same session, `curate_pending_plans`, targeted `plan_id: 580dfc865323` and returned `{"action": "refine", …}`.

**2. Every downstream call carries the upstream transcript.** From `cost_samples.jsonl`, context = `input + cache_read + cache_creation`:

| Caller | Chained (on B after propose) p50 / p90 | Standalone p50 / p90 | Inherited |
|---|---|---|---|
| `curate_pending_plans` | 44,834 / 54,085 (n=660) | 26,234 / 27,955 (n=501) | +18.6K (+71%) |
| `dream` | 40,761 / 55,575 (n=581) | 22,822 / 23,288 (n=2, thin) | ≈ +18K |
| chain position: `introspect` → `reflect` → `propose_plan` | 19,094 → 29,894 → 40,694 (n≈663) | — | each call adds ≈ 10.8K |

Typical chain (2026-09-24 13:10–13:14): cache_read grows 0 → 23,447 → 36,086 → 52,138 → 56,180 across introspect → reflect → propose_plan → curate → dream. The dream prompt is 8.5K characters, yet the call runs at about 61K tokens.

**3. Sharing does not save money.** Median cost per call: the standalone curate call ran about 10% below the chained one. The standalone call is *cheaper*, because the identity prefix still hits the cache and the smaller context bills less. Across the window, B-lineage spend was $— for reflect + propose_plan + curate + dream, plus $— for the 663 chain-head introspects.

**4. Declared effort is silently overridden.** The chain-head `introspect` declares `effort="medium"` but runs at B's high/128K on 663 chains ($— in the window). `dream_validate` declares `low` but would run at high/128K. It is latent in production only because `dream_validation_enabled = false`.

## Prior Point Fixes (whack-a-mole ledger)

| Identifier | Date | What it patched | Why the class survived |
|---|---|---|---|
| `169d8115` | 2026-04-16 | Introduced Sessions A/B/C; B = generate_task + evolve + dream | Origin: policy bound at session open, inherited by all attached calls |
| `b6bb631f` | 2026-04-16 | Smoke mode's `effort="low"` overridden by session `high` | Fixed smoke only; effort still session-owned |
| `aac22172` (#1238) | 2026-04-17 | Made dropped effort *visible* via `_note_session_effort_override` | Observation only; 17 hand-kept literals, never compared |
| `46986770` | 2026-04-18 | Planner/distill moved to a run session "so the evolution transcript does not balloon" | Moved callers out of B; B itself kept growing (curate added later) |
| `a91f071d` | 2026-04-20 | `generate_task` stalled at inherited high/128K → split to Session D | One caller extracted |
| `99f53293` | 2026-06-10 | Wired `curate_pending_plans` onto B (and a second B-opener) | Added a new caller *into* the class |
| `a6d2ad99` (#1239) | 2026-07-28 | Message reply billed Session A's high/128K → off-session low | One caller extracted (from A) |
| `4999806c` | 2026-09-22 | Rejection self-model update would be demoted to low on the classifier session → separate pair | One caller extracted; new bespoke session factory |

This design retires, for the evolution lineage: slot B and its resume bookkeeping, the second B-opener in `_curate_pending_plans`, nine `_note_session_effort_override` literals, and the four tests that pin B sharing. `a91f071d`, `46986770`, `a6d2ad99` and `4999806c` become registry entries once their lineages migrate (see Out of scope).

## Implementation Plan

1. [x] **Call-policy registry** — new `maestro/call_policy.py`, `tests/test_call_policy.py`.
   Define `SessionScope` (`SINGLE_USE`, `SHARED`), a frozen `CallPolicy(effort, thinking_budget_tokens, tools, phase, scope)`, and `CALL_POLICIES: dict[str, CallPolicy]` with one entry per `caller` literal that reaches `query` / `query_on_session` in `maestro/`. Seed the entries from today's stateless-branch literals. Existing shared lineages are declared `SHARED` and the B lineage is declared `SINGLE_USE`: `evolution_introspect`, `reflect`, `propose_plan`, `propose_plan_chain`, `curate_pending_plans`, `dream`, `dream_validate`, `assess_stage_readiness`, `generate_hypothesis`, `evaluate_hypothesis`. The session-opener tags (`evolution_session`, `contemplation_session`, `run_session`, …) are registered too, as `SHARED`. One caller tag is ambiguous today: `introspect` serves both the priced Session-A arm and the unpriced evolve arm. So `Consciousness.introspect` stamps `evolution_introspect` when `price_reflection=False` (a telemetry-label change only, visible in `per_caller` usage rows), and `introspect` stays `SHARED`. `cost_tracking.CALLER_GROUPS` gains `evolution_introspect` under the same group as `introspect`, so the relabelled spend does not fall through to `Other` in `cost-report`. Pure data otherwise; nothing reads the registry yet.
   Depends on: —
   Acceptance: every `caller=` string literal passed to `query`/`query_on_session` in `maestro/` is a key of `CALL_POLICIES`, and every policy's effort is one of `low`/`medium`/`high`.

2. [x] **Single-use seam** — `maestro/call_policy.py` (`single_use_call`), `tests/test_call_policy.py`.
   `single_use_call(caller, prompt, *, system, output_format=None, complete_on=None, stream_idle_timeout_seconds=None)` looks up the policy, calls `create_session(system=…, options=QueryOptions(effort=…, thinking_budget_tokens=…, tools=…, output_format=output_format, caller=caller), phase=policy.phase)`, then `query_on_session`, and runs `disconnect_session` in a `finally`. The disconnect failure is propagated per the fail-loud invariant, with no swallowing. `output_format` is a per-call argument, not a registry field, because it is set when the session is created (`query_on_session` accepts none) and `propose_plan`'s schema varies by flag. It raises on a `SHARED` caller. It is session-attached by construction, so the existing `session_attachment` guard in `tests/test_structural_guards.py` needs no allowlist change.
   Depends on: 1
   Acceptance: with a mocked provider, one call creates exactly one session with the registry's options, sends exactly one turn, and disconnects that session even when the turn raises.

3. [x] **Flag and migrate the evolution-lineage call sites** — `maestro/config.py`, `maestro.toml` (`[sdk] single_use_evolution_calls_enabled = false`), `maestro/evolution.py`, `maestro/consciousness.py`, `maestro/purpose.py`, `maestro/dreaming.py`, and the matching `tests/test_session_reuse_phases.py` cases.
   With the flag on, each of the ten `SINGLE_USE` callers (`evolution_introspect` in place of `introspect`) routes through `single_use_call` and ignores any `session` argument. Flag-on uses the **stateless** arm's structured-output mode: a caller whose stateless arm passes an `output_format` (`propose_plan`, `propose_plan_chain`, `assess_stage_readiness`, `generate_hypothesis`, `evaluate_hypothesis`) passes the same schema to `single_use_call`, together with the same `complete_on`. These schema callers do not fall back to the session arm's `complete_on`-only parsing, which would change `ProposalParseError` for a reason unrelated to the transcript. The exception is `dream` / `dream_validate`: they keep the session arm's text-JSON contract, with no `output_format`, so a malformed reply falls back tolerantly and cannot exhaust the CLI's `StructuredOutput` retries into a terminal error (#4286). With the flag off, today's `session is not None` / stateless arms are byte-identical. The specialised-prompt prepend (`DREAM_SYSTEM_PROMPT`, the evolution prompt in `reflect`) stays in the prompt, so prompt text does not change between modes.
   Depends on: 2
   Acceptance: flag-on, each migrated caller issues its turn on a session created for it alone, at its registry effort (`evolution_introspect` → medium, `dream_validate` → low). Flag-off, the recorded `create_session`/`query_on_session` sequence equals today's.

4. [x] **Stop opening slot B under the flag** — `maestro/agent.py` (`_phase_evolve`, `_phase_curate_pending_plans`, `_phase_dream`, `_curate_pending_plans`).
   With the flag on, the three phase runners pass `session=None` and `_curate_pending_plans` does not open slot B. `self._session_b` stays `None` for the whole iteration.
   Depends on: 3
   Acceptance: flag-on, a full `run_loop` iteration that runs evolve + curate + dream makes zero `create_session` calls with `caller` in {`evolution_session`, `curate_session`}, and exactly one session per SDK turn in the B lineage.

5. [x] **AST guard: single-use callers cannot be shared** — `tests/test_call_policy.py`.
   Walk `maestro/`. Fail when `query_on_session(…, caller=<X>)` appears outside `maestro/call_policy.py` for any `X` whose policy is `SINGLE_USE`, except for a pinned, shrink-only allowlist holding the flag-off session arms that step 3 keeps (one entry per migrated caller, keyed by enclosing function). Step 7 empties it. Fail when a `QueryOptions(effort=<literal>, caller=<X>)` for a registered `X` appears outside `call_policy.py` without matching the registry value. Also fail on any `caller=` literal missing from `CALL_POLICIES`. Include seeded-fault controls: an in-memory source that attaches `reflect` to a passed session must be flagged.
   Depends on: 3
   Acceptance: the guard passes on the migrated tree with only the allowlisted flag-off arms exempt, flags each seeded violation, and fails when a caller outside the allowlist attaches to a passed session.

6. [ ] **Burn-in and flip default** — `maestro.toml`, `maestro/flag_lifecycle.py` (experimental manifest entry with a dated `review_by`).
   Enable in production and observe the falsifiability metrics below for at least 14 days, then flip the default to `true`.
   Depends on: 4, 5
   Acceptance: the metrics in Risk Assessment meet their thresholds over the burn-in window.

7. [ ] **Retire slot B and its workarounds** — `maestro/agent.py` (`_session_b`, slot `"b"` in `_SESSION_SLOT_PHASES`, `_capture_session_id("b", …)` resume bookkeeping, the second B-opener), the nine B-lineage `_note_session_effort_override` literals, the flag, the flag-off arms of the migrated callers, `tests/test_session_reuse_phases.py` B-sharing pins, and the CLAUDE.md "Up to four persistent sessions per iteration (A–D)" line.
   Depends on: 6
   Acceptance: `grep` finds no `_session_b` / `"evolution_session"` / `"curate_session"` in `maestro/` outside `cost_tracking.CALLER_GROUPS`, whose entries stay so that pre-flip usage rows keep their group. The step-5 guard passes with the flag removed and its allowlist empty.

## Testing Strategy

- `tests/test_call_policy.py`: registry completeness (every `caller=` literal is registered, via `ast` over `maestro/`), effort vocabulary, and `single_use_call` lifecycle (create → one turn → disconnect, including disconnect-on-raise and propagation of a disconnect failure) against a mocked provider. No real network, no sleep.
- `tests/test_session_reuse_phases.py`: flag-off byte-identity for the ten migrated callers; flag-on "one session per turn" with registry effort; `run_loop` flag-on iteration creates no slot-B session (mocked `create_session`, `make_agent`, `mem_fs`).
- `tests/test_call_policy.py`: the step-5 guard is the deterministic enforcement of the class. It uses in-memory seeded faults detected by `ast`, per `tests/test_fault_seeding.py` conventions.
- Coverage stays at 100%. Every flag branch and the `SHARED`-caller refusal in `single_use_call` is exercised.

## Risk Assessment

- **Cache regression.** Single-use sessions lose the transcript prefix, but that prefix is exactly the bloat being removed. The identity slab stays a stable cached prefix. The measured standalone curate is already cheaper (about 10% below the chained call at p50). *Failure signal:* the mean cost of a B-lineage chain (introspect + reflect + propose_plan + curate + dream, from `cost_samples.jsonl`) rises more than 10% over the burn-in window.
- **Lost continuity.** A downstream call could have been depending on transcript context that is missing from its prompt. Every B caller already receives its inputs explicitly (Relevant Findings), and `reflect` reads the journaled introspection. *Failure signal:* `propose_plan` parse failures (`ProposalParseError`) or dream digest scores worsen against the pre-flip window.
- **Connect overhead.** Each call pays one extra connect (about 1–3 seconds). That is negligible against a ~4-minute chain, and the calls are cadence-gated.
- **Rollback.** Steps 1–5 are inert with the flag off. Before step 7, rollback is a config flip.

**Falsifiability.** The design is right if, over 14 days flag-on:
1. The chained-curate refine rate on the just-minted plan falls from **51.4% toward the other-plan baseline (5–7%)**, and its obsolete rate becomes non-zero.
2. Curate and dream context p50 drop from **44.8K / 40.8K to ≤ 28K**.
3. The chain-head introspect runs at `medium`.

If (2) holds while (1) does not, the bias came from plan *selection*, not the transcript. That would refute this design's premise for (1), and the remedy would be the selector's existing grace period: raise `[loop] curate_min_plan_age_hours` (shipped at `0.0`, so `select_curation_target` may pick a plan minted moments earlier) instead of adding a new rule.

## Out of scope

- **Session A** (priced `introspect` → `update_self_model`, with grounded-reflection tool output feeding the self-model update), **Session C** (`implement` → `self_review` → `auto_fix` × N, where the reviewer shares the implementer's transcript), the **run session** (`decompose` / `replan` / `_distill_findings` / `_assess_criteria_outcome`, the last two declaring `low` but running `medium`), and the **rejection pair** (cross-plan bleed on the classifier; one self-model JSON per plan accumulating on the self-model session). These are sibling manifestations of the same class. After this proposal each is a `CallPolicy.scope` edit plus call-site migration, guarded by the same step-5 test. They are left for follow-ups because Session C's `auto_fix` continuity and grounded reflection's tool output are real design trade-offs that need their own evidence.
- **The executor step session** (`execute_step` across sequential single-step tiers). Continuity there is load-bearing by design.
- **Curation target selection** (`select_curation_target` picking the plan minted this iteration, which the shipped `[loop] curate_min_plan_age_hours = 0.0` permits). This proposal only removes the authoring transcript. See falsifiability (1).
