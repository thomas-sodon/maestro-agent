# 132: Grounded Contemplation — A Bounded Read-Only Retrieval Surface for the Priced Reflection (Pull, Not Only Push)

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:observing |
| **Affects** | new `maestro/reflection_retrieval.py`, `maestro/consciousness.py` (`introspect`, `_introspect_complete`, `_introspect_complete_after_verdict`, `_introspect_complete_impl`), `maestro/agent.py` (`_phase_contemplate`, `contemplate`, `_get_or_create_session`), `maestro/prompts/reflection.py` (`PREDICTION_REQUIREMENT`, `format_prediction_requirement`, `has_prediction_markers`), `maestro/cli.py` (`_build_agent_tools`), `maestro/tools.py`, `maestro/sdk.py` (`QueryOptions`, `query_on_session`), `maestro/config.py`, `maestro/config_settings.py`, `maestro/toml_config.py`, `maestro.toml`, `maestro/flag_lifecycle.py`, `tests/test_structural_guards.py`, tests |
| **What landed** | Steps 1–6. **Step 1:** `maestro/reflection_retrieval.py` is the single owner of the read-only reflection retrieval surface's policy — the `READ_ONLY_REFLECTION_TOOLS` allowlist (the eleven read-only members of the five `_build_agent_tools` introspection servers), the `MUTATING_INTROSPECTION_TOOLS` exclusion set, the hard `MAX_REFLECTION_TOOL_CALLS` budget, the `REFLECTION_TOOL_RESULT_CHAR_CAP` per-result cap, the pure `build_reflection_tool_set()` inclusion-by-allowlist filter, and the bounded `build_bounded_reflection_tool_set()` constructor that wraps each tool with an in-process shared call counter and the per-result cap; covered by `tests/test_reflection_retrieval.py` and the sole-owner guard in `tests/test_structural_guards.py`. **Step 2:** the default-off `[loop] grounded_reflection_enabled` flag is registered end-to-end (`config.py`, `config_settings.py`, `maestro.toml`, `flag_lifecycle.py`'s `EXPERIMENTAL_FLAGS`). **Step 3:** `MaestroAgent._build_contemplation_session_options` builds the slot-A session with the bounded read-only surface (materialised through `AnthropicToolAdapter().materialize`) and `max_turns=REFLECTION_SESSION_MAX_TURNS` when active; flag-off keeps `tools=[]` byte-identical. **Step 4:** the completion contract is tool-loop-safe — `sdk.message_has_tool_use` keeps the early-exit predicates (`_introspect_complete`, `_introspect_complete_after_verdict`, `_introspect_complete_unpriced`) from firing while a tool-use turn is pending, the `_MAX_REFLECTION_CHARS` ceiling counts assistant text only, and the in-process budget returns a graceful `is_error` once `MAX_REFLECTION_TOOL_CALLS` is exhausted. **Step 5:** `format_prediction_requirement` gains a `grounded_reflection_active` kwarg (threaded through `Consciousness.introspect`, fed by `MaestroAgent._grounded_reflection_active()` in `contemplate` so the prompt steering and the attached tools share one predicate per cycle) that prepends `_grounded_reflection_steering()` — a preamble enumerating the allowlisted `READ_ONLY_REFLECTION_TOOLS` and stating plainly there is no `Bash`/shell this turn — **before** the trailing prediction markers, leaving the streaming early-exit contract untouched; flag-off/inactive the rendered prompt is byte-identical (`tests/test_prompts.py`, `tests/test_reflection_prediction.py`). **Step 6:** `MaestroAgent._grounded_reflection_active` degrades the session to `tools=[]` under `budget_throttle_active`, so the most expensive phase is never widened while the loop is slowing. |
| **What is missing** | Step 7. The redundant verify-steering prose is not yet folded into the pull capability (Phase C, sequenced behind the flag's production observation window). |
| **Depends on** | 129, 130 |
| **Derived from** | live production data via /propose, 2026-09-12; motivating incident RCA issue #3842 |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

The priced reflection (`Consciousness.introspect`, driven each cycle by `agent.contemplate`) is the one place Maestro forms and grades beliefs about itself and its own code. It runs on a **deliberately tool-free** session and is grounded only by whatever the loop **pushes** into its prompt — the diagnostic snapshot, the health summary, the measurement blind spots, the falsification hint, the measured-behavioral-data block, and (behind 130) the conscious-moment broadcast. It can never **pull** the one specific fact its current thought hinges on. When a reflection confronts its prior bet and needs to check whether `_wip_owns_slot` does X, or whether PR #N actually landed, it has no way to look — so it either narrates around an unverified premise or, acting on an emergent "verify before you judge" instinct, reaches for a tool the tool-free session cannot serve.

That reach is not rare and it is not harmless. In the trailing 400 journal entries, 34 reflections use verify-before-narrate language, and post-restart contemplations emit `grep` / `Read` / `Bash` invocations as prose. On 2026-09-12 the instinct produced a native-looking `Bash` tool call the tool-free session could not parse; the CLI exhausted its tool-parse retry and returned a terminal `is_error` result, which `maestro/sdk.py` correctly classified non-transient (`UNKNOWN`) and `run_loop` crashed on — the loop stayed down ~4h17m until an operator restarted it (RCA #3842). The same instinct, when it happens to emit a fenced code block instead of a parseable call, merely wastes a priced turn narrating a read it cannot perform.

This proposal gives the priced reflection a single, bounded, **read-only retrieval surface** so it can verify a premise against reality before it commits it to the ledger. The surface reuses infrastructure that already exists — a **curated read-only subset** of the tools the five in-process introspection servers `cli._build_agent_tools` assembles (`journal`, `self_awareness`, `memory`, `issues`, `diagnostics`), plus read-only code reading. Those servers are *not* wholly read-only today — `journal` exposes `write_journal_entry`, `self_awareness` exposes `add_purpose_evidence`, and `issues` exposes `create_issue`/`close_issue` (an external GitHub mutation) — so the surface takes only their read-only members and drops the mutating ones. It is governed by one owner that fixes the allowlist (no `Bash`, no mutating tool), caps the tool-call budget, caps each tool result like Bash output, keeps the completion contract tool-loop-safe, and steers the prompt to the granted tools so the model reaches for those instead of `Bash`. It resolves the crash class at its source rather than by silencing the error, and it converts the push-only grounding architecture that 129 and 130 built into a push **and** pull one.

End state: a reflection that judges a premise reads it first, on a surface that is structurally read-only and cost-bounded; the tool-free-session tool-call crash class is eliminated because the session is no longer tool-free and the prompt no longer invites an unservable tool; and the standing pressure to keep bolting another push-side injection onto contemplate is relieved because reflection can fetch what it needs.

## Production Evidence

All figures read strictly read-only from the live data dir resolved via the `maestro-loop` bind-mount at `/opt/maestro/data` (`$DATA_DIR`).

**The acute failure — one loop-killing crash (RCA #3842).** `journal.jsonl` carries the crash milestone; `logs/maestro.log` carries the traceback; the SDK transcript is authoritative.

```
-- $DATA_DIR/logs/maestro.log (2026-09-12 05:58:46 UTC) --
[engine] turn 1 end (turns=2 usage in=4805 out=1039 cost=$— api=16850ms stop=stop_sequence resultLen=0)
SDK text: The model's tool call could not be parsed (retry also failed).
[WARNING] SDK returned empty completion (terminal error result is_error status=None, caller=introspect, ...) — raising UNKNOWN
[ERROR] run_loop crashing on unhandled SDKError: SDK returned terminal error result (is_error status=None, ...)
```

```
-- $DATA_DIR/logs/sdk/2026-09-12/05-58-30_introspect_a2f09f.jsonl (assistant turn) --
"I'll settle the bet by reading, not re-vowing. Let me look at the actual code before I judge.
 ... Verifying the premise before I narrate it ...
 <invoke name=\"Bash\"><parameter name=\"command\">cd /workspace && grep -n \"...\" maestro/agent.py | head -50</parameter></invoke>"
```

- `journal.jsonl` crash milestone (2026-09-12T10:15:52): *"I crashed. Last known phase: contemplate at iteration 4280."* Loop down 05:58:46 → operator restart 10:15:49 (~4h17m).

**The chronic condition — the verify instinct fires constantly.** Over the trailing 400 `journal.jsonl` entries, 34 reflections use verify-before-narrate language; three post-restart contemplations on 2026-09-12 emit tool calls as text rather than thought:

```
-- $DATA_DIR/journal.jsonl (contemplation, 2026-09-12) --
11:41:55  "```bash grep -rn \"_wip_owns_slot\" maestro/ | head -30 ```"
12:12:40  "I'll settle the prior bet by actually reading the symbol, not narrating around it. - **Bash**: `grep -n \"_wip_owns_slot\" /workspace/maestro/*.py | head` - **Read**: ..."
12:28:12  "I'll confront the bet by doing the read I promised last cycle — not narrating around it. ... Let me check whether last cycle's work touched the gate at all."
```

**The classification asymmetry that makes the reach fatal.** Over the log window (2026-09-08 19:19 → 2026-09-12 11:00) there were 31 empty completions: 30 from `auto_fix` / `implement_plan` shaped `"(no result message)"` → `ErrorCategory.STREAM_STALL` (transient, resumed in place); the **1** from `introspect` shaped `"(terminal error result is_error)"` → `ErrorCategory.UNKNOWN` (hard crash). The executor callers recover from an empty turn; the tool-free reflection caller does not.

**Cost profile.** Over the trailing 200 iterations of `usage.jsonl`, `introspect` ran in 37 and completed in ≤2 turns each; the crash turn alone billed a full priced turn for a *truncated* reflection. `agent.contemplate` is annotated in code as "the loop's most expensive phase." Any retrieval loop must therefore be hard-capped, not open-ended.

## Prior Point Fixes (whack-a-mole ledger)

The priced-reflection subsystem has been patched repeatedly, and contemplate's grounding has grown by accretion of **push-side** injections — each a point response to "reflection got something wrong; push it more context," none of which gave reflection the ability to look something up.

| # | Identifier | What it patched | Why the class survived |
|---|---|---|---|
| 1 | `d3bbd069` | Introduced the priced reflection (falsifiable self-prediction each cycle) | Created the confront-your-bet pressure with no way to verify the bet |
| 2 | `1ac8f211` | Fixed a 600-char early-exit truncating the prediction markers | Completion contract patched for text-only; never made tool-loop-safe |
| 3 | `c4cc6f16` | Fixed early-exit truncation on echoed prior-bet markers (#3474762297) | Same contract, another edge; still text-only |
| 4 | `852606f3` | Raised the reflection char ceiling so long high-effort bets keep markers | Ceiling counts all streamed text; a tool loop would trip it |
| 5 | `dd10d6a6` | Fixed verdict-flip / predicate-parser mismatch | Grading fix; grounding untouched |
| 6 | `29c200dd` | Constrained priced-reflection bets to the self-calibration grammar (129) | Intensified "prove your bet" pressure on a tool-free session |
| 7 | `f1cfb2a3` / `ccaf2243` | Broadcast the conscious moment into contemplation (130) | More **push**; reflection still cannot pull |
| 8 | contemplate injections | Operational-status snapshot, health summary, measurement blind spots, falsification hint, measured-behavioral-data block | Each a push-side point addition compensating for the missing pull |

Rows 1–5 keep patching a text-only completion contract; rows 6–8 keep pushing more context in. The class behind all of them is the same: **reflection can only see what was pre-selected for it.** This proposal closes that class; it does not add a ninth push injection.

## Relevant Findings

- `maestro/agent.py` (`_phase_contemplate`) — builds the slot-A contemplation session with `QueryOptions(..., tools=[])`; `maestro/sdk.py` (`QueryOptions.tools` docstring) confirms `tools=[]` is a genuinely tool-less session. This is the deliberate tool-free contract the model collides with.
- `maestro/consciousness.py` (`introspect`) — the session path calls `query_on_session(session, prompt, caller="introspect", complete_on=...)` with no per-call turn cap; the bare-`query` path uses `max_turns=1`. The retrieval budget must live in one place across both.
- `maestro/consciousness.py` (`_introspect_complete_impl`, via `has_prediction_markers`) — early-exits once the accumulated text carries the trailing `PREDICTION:` / `LOSS CONDITION:` markers, with a `_MAX_REFLECTION_CHARS` ceiling backstop. Interleaved tool-use turns must not trip the ceiling or the marker check.
- `maestro/sdk.py` (`_consume_messages`, `_error_result_is_transient`) — a terminal `is_error` result with no `api_error_status` and no quota prose is `UNKNOWN` and crashes; the sibling `"no result message"` shape is `STREAM_STALL` and resumes. This proposal does **not** touch this classification (see Out of scope); it removes the input that reaches the fatal branch.
- `maestro/cli.py` (`_build_agent_tools`) and `maestro/tools.py` (`journal_tools`, `self_awareness_tools`, `memory_tools`, `issues_tools`, `diagnostics_tools`) — an existing in-process tool registry over journal, self-model, task history, evolution plans, dream digest, issues, and diagnostics. `memory_tools` and `diagnostics_tools` are wholly read-only, but the other three each also return a mutating tool — `journal_tools` returns `write_journal_entry`, `self_awareness_tools` returns `add_purpose_evidence`, and `issues_tools` returns `create_issue`/`close_issue` (an external GitHub mutation). The retrieval surface reuses only the **read-only** members of these factories — no new tool authoring, but its owner (Step 1) must exclude those four mutating tools by name.
- `CLAUDE.md` "MCP servers" invariant — read-only Serena is kept retrieval-only via `external_mcp_disallowed_tools`; read-only code reading has precedent. The human-in-the-loop invariant action-gates `Bash`; the reflection surface must exclude it, which read-only tools satisfy without gating.
- `maestro/flag_lifecycle.py` (`EXPERIMENTAL_FLAGS`) — the four-stage flag lifecycle every behavior change here gates behind, mirroring 129/130's `[audit]` / `[loop]` flags.
- `docs/proposals/core/130-conscious-access-workspace.md` (Step 5 `workspace_moment`, Step 8 `read_conscious_moment` / `read_conscious_access_profile`) — 130 pushes the selected moment into `introspect` and (unbuilt) exposes the *attention schema* for self-inspection. That answers "what am I attending to?"; this proposal answers "is the premise I'm about to narrate true?" — the read-only *retrieval* half 130 does not build.
- `tests/test_agent_contemplate.py`, `tests/test_reflection_prediction.py`, `tests/test_sdk.py` — cover contemplate/introspect, the prediction contract, and terminal-error classification. None covers a priced reflection performing a bounded read; that gap is the regression surface.

## Implementation Plan

### Phase A — the owner and the flag (inert)

1. [x] **Single owner of the reflection retrieval surface.** new `maestro/reflection_retrieval.py` — a leaf module owning the closed read-only tool allowlist (the **read-only** members of the five `_build_agent_tools` introspection servers — excluding the mutating `write_journal_entry`, `add_purpose_evidence`, `create_issue`, and `close_issue` — plus the read-only code-reading tools), the hard `MAX_REFLECTION_TOOL_CALLS` budget, the per-result char cap (matching Bash's 2000-char cap), and one `build_reflection_tool_set()` constructor. No mutating tool, no `Bash`, may appear in it.
   - *Depends on:* none.
   - *Acceptance:* a `tests/test_structural_guards.py` guard asserts `reflection_retrieval` is the sole definition of the reflection tool allowlist and that the allowlist's resolved names are a read-only subset of the introspection registry with `Bash`, `write_journal_entry`, `add_purpose_evidence`, `create_issue`, `close_issue`, and every other mutating tool absent.

2. [x] **Register the default-off flag end-to-end.** `maestro/flag_lifecycle.py` (`EXPERIMENTAL_FLAGS` with a dated `review_by`), `maestro/config.py`, `maestro/config_settings.py`, `maestro/toml_config.py`, `maestro.toml` — `[loop] grounded_reflection_enabled`, default off, kill-switch retained.
   - *Depends on:* 1.
   - *Acceptance:* `tests/test_config_flag_lifecycle.py` shows the flag registered and off everywhere; config-parity test passes.

### Phase B — wire the surface (flag-gated)

3. [x] **Build the reflection session with the surface when on.** `maestro/agent.py` (`_phase_contemplate`, `_get_or_create_session`) — when `grounded_reflection_enabled`, construct the slot-A session with `build_reflection_tool_set()` and a bounded `max_turns` derived from `MAX_REFLECTION_TOOL_CALLS`; flag-off keeps `tools=[]` and today's turn shape byte-identical.
   - *Depends on:* 1, 2.
   - *Acceptance:* flag-on the session exposes exactly the allowlisted read-only tools and the bounded `max_turns`; flag-off the session build is byte-identical to today (`tools=[]`).

4. [x] **Make the completion contract tool-loop-safe.** `maestro/consciousness.py` (`_introspect_complete_impl`) and `maestro/prompts/reflection.py` (`has_prediction_markers`) — the char ceiling counts reflection *text* only (not tool-result content), and early-exit fires only on a trailing text turn carrying the markers, so interleaved tool-use turns neither trip the ceiling nor truncate the bet.
   - *Depends on:* 3.
   - *Acceptance:* a priced reflection with N interleaved read-only tool calls completes on the trailing markers and records exactly one prediction row; a tool-result payload exceeding the ceiling does not early-exit the stream.

5. [x] **Steer the prompt to the granted set.** `maestro/prompts/reflection.py` (`format_prediction_requirement`, `PREDICTION_REQUIREMENT`) — when on, the requirement enumerates the available read-only tools and states plainly that this turn has no `Bash` / no shell and the model must ground its bet through those reads. Flag-off prompt is byte-identical.
   - *Depends on:* 3.
   - *Acceptance:* flag-on prompt names the allowlisted tools and forbids `Bash`; flag-off prompt is byte-for-byte unchanged (asserted by `tests/test_prompts.py`).

6. [x] **Degrade to push-only under budget throttle.** `maestro/agent.py` (`_phase_contemplate`) — under `budget_throttle_active`, build the reflection session tool-free regardless of the flag, so grounded reflection never widens the loop's most expensive phase while the loop is deliberately slowing.
   - *Depends on:* 3.
   - *Acceptance:* under throttle the reflection session is built with `tools=[]` even with the flag on; a test drives both throttle states.

### Phase C — retire the growth pattern (after proof)

7. [ ] **Fold redundant verify-steering prose into the pull capability.** `maestro/consciousness.py` (`contemplate`) — where a push-side injection existed only to *tell the model to verify* (e.g. the falsification hint's "propose a replacement grounded in evidence" steering), narrow it now that the model can perform the verification, without deleting the cheap measured-data grounding blocks. Sequenced behind the flag's observation window.
   - *Depends on:* 3, 5.
   - *Acceptance:* the narrowed injection's obsolete branch and its test are removed; the measured-data block's tests are unchanged.

## Testing Strategy

- `tests/test_reflection_retrieval.py` (new) — the allowlist is read-only (no `Bash`, no mutating names), the budget and result-cap constants are enforced by `build_reflection_tool_set()`, and the constructor is deterministic. `mem_fs`, no real I/O.
- `tests/test_structural_guards.py` — net-new guard pinning `reflection_retrieval` as the sole owner of the reflection allowlist and its read-only subset property (deterministic, zero-SDK; increments the guard count).
- `tests/test_agent_contemplate.py` — flag-off session byte-identical (`tools=[]`); flag-on session exposes the bounded tool set and `max_turns`; under `budget_throttle_active` the session is tool-free with the flag on; a mocked SDK returning a terminal `is_error` on a tool-free session is exercised as the pre-fix regression, and the flag-on grounded path completes without reaching that branch.
- `tests/test_consciousness.py` / `tests/test_reflection_prediction.py` — a priced reflection with interleaved read-only tool turns completes on the trailing markers and records one prediction row; the char ceiling counts reflection text only; the prior-verdict confront predicate still anchors after `PRIOR VERDICT:`.
- `tests/test_prompts.py` — flag-on prompt enumerates the allowlisted tools and forbids `Bash`; flag-off prompt byte-identical.
- `tests/test_config_flag_lifecycle.py` / `tests/test_config.py` — flag registered, default off, parity across overlays.
- All externals mocked (SDK via `SDKMock`, tool handlers stubbed); no real network, sleep, or subprocess; 5s unit budget.

## Risk Assessment

- **Cost blow-up in the most expensive phase.** Mitigated by the hard `MAX_REFLECTION_TOOL_CALLS` budget, the bounded `max_turns`, per-result char caps, and the Phase-B throttle degrade to tool-free. Rollback is the default-off flag.
- **Completion-contract regression.** The streaming early-exit and ceiling carry scar tissue (#3474762297 and rows 2–4 of the ledger). Step 4 is the highest-risk change; its acceptance test drives interleaved tool turns explicitly, and the flag lets it dark-launch before it is trusted.
- **The model still emits a malformed tool call.** Granting the tools makes native `tool_use` the natural path and the XML-text fallback unlikely (the fallback is the model reaching for an *absent* tool), but it is not a proof. The falsifier is crash recurrence: if the `introspect` terminal-error crash recurs with the flag on, the design failed and the flag stays off. This proposal deliberately does not make that terminal error non-fatal (see Out of scope).
- **Trust boundary.** The surface is read-only by construction and structurally pinned (Step 1 guard); it exposes no mutating tool and no `Bash`, so it cannot push, merge, or write, and it needs no action-gating. Read-only code reading exposes source the agent already reads in task phases; no new secret surface.
- **Interaction with 130's broadcast.** 130 pushes the conscious moment into `introspect` via `workspace_moment`; this proposal adds pull alongside it. They compose — push selects what to attend to, pull verifies it — and neither's flag depends on the other's being on.

**Falsifiability.** If the design is right: the `introspect` terminal-error crash recurrence goes to 0 with the flag on, and the premise-accuracy of reflection-cited claims improves (tracked against the `measure-premise-accuracy` reading and the reality-refutation sensor, which should see fewer git-refuted reflection premises). If crashes persist, or refuted premises hold steady, the design has not closed the class and the flag does not graduate.

## Out of scope

- **Changing the SDK terminal-error classification.** Making the `"tool call could not be parsed"` terminal `is_error` transient, swallowing it, or retrying it is forbidden (it would hide the class, not close it) and unnecessary once the trigger is removed. The fail-hard-stay-down posture (`restart: "no"`) is intended and stays; a genuinely malformed tool-call dialect quirk remains governed by it.
- **Deleting the push-side grounding injections.** The diagnostic-snapshot / health-summary / blind-spots / measured-data blocks are cheap zero-SDK grounding and remain; consolidating them into pull-on-demand is a later change.
- **130's attention-schema self-inspection tools** (`read_conscious_moment` / `read_conscious_access_profile`) — a different question, owned by 130 Step 8.
- **Retrieval for the unpriced `evolve()` reflection and task-execution.** This proposal scopes pull to the priced `contemplate` reflection, where the confront-your-bet pressure and the crash both live; extending it elsewhere is a follow-on.
