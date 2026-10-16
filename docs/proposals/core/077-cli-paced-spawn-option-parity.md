# 077: Spawn-Surface Option Parity — Model, System Prompt, Agent Default-Deny, Task Budget

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | proposed |
| **Affects** | `maestro/providers/cli_paced.py`, `maestro/sdk.py` (read-only accessors), `maestro_sdk/src/maestro_sdk/{client,types}.py`, `docs/sdk-internals.md`, tests |
| **Depends on** | 061 (shipped) |

## Description

Proposal 061 closed the eight `Capabilities` gaps, but a second tier of
`QueryOptions` fields still behaves differently on `cli_paced` than on
`anthropic` — not as fail-loud rejections but as *documented silent
divergences*: `model` / `fallback_model` are delegated to the operator's
`~/.claude.json`, so `[models] default` / `[models] fallback` simply do not
apply to a `cli_paced`-routed phase (the same `QueryOptions` can run a
different model per provider); the `system` prompt is prepended into the user
prompt instead of being a real system prompt; the Anthropic path's implicit
`disallowed_tools=["Agent"]` default is not replicated, so a spawned `claude`
can launch subagents that the Anthropic path denies by default on every call;
and `task_budget_tokens` is silently dropped, leaving per-call cost unbounded.

Each of these has a concrete CLI-side mechanism, so the fix is forwarding, not
redesign. The `claude` CLI exposes `--model` and `--fallback-model` flags —
mapping them restores the invariant that the `[models]` table governs which
model serves a phase regardless of provider routing. It exposes
`--append-system-prompt`, which restores genuine system-prompt semantics for
the identity prompt (CLAUDE.md's "identity context in every call" invariant
currently rides in user-message position on this provider) and stops sessions
re-paying the full system slab inside every turn's user message. The Agent
default-deny is one branch in the existing tool-policy fold. Only
`task_budget_tokens` has no CLI flag; for it the honest mechanism is
client-side enforcement from the same sidecar counts Step 10 of 061 already
reads — explicitly post-hoc (a turn that overshoots completes, then the
session refuses further turns), a documented weaker guarantee than the SDK's
native budget, mirroring how 061 handled `max_turns` semantics divergence.

End state: a phase routed to `cli_paced` runs the configured model with the
configured fallback, carries its identity prompt as a system prompt, denies
the Agent tool by default exactly like the Anthropic path, and stops a
runaway session at the configured token budget — with each divergence that
remains (budget post-hoc-ness) documented in the capability matrix instead of
discoverable only by reading provider docstrings.

## Relevant Findings

- `maestro/providers/cli_paced.py` (module docstring, "Delegated to the CLI's
  own config" list) — `model`, `fallback_model`, and `task_budget_tokens` are
  documented silent drops: "a Maestro-side override would either be inert
  (the CLI re-resolves them) or fight the operator's CLI config." That
  rationale predates per-phase provider routing, under which the silent drop
  means `[models]` config quietly stops applying to routed phases.
- `maestro/sdk.py` (`_build_sdk_options`) — the Anthropic path resolves
  `options.model` against the module default `_sdk_model` and
  `options.fallback_model` against `_sdk_fallback_model` (both configured from
  the `[models]` table), and wraps a positive effective `task_budget_tokens`
  in `TaskBudget`. This is the resolution behaviour parity must reproduce.
- `maestro/sdk.py` (`_build_sdk_options`, `disallowed_tools` handling) — when
  `options.disallowed_tools` is `None` the Anthropic path injects
  `["Agent"]`, and a final `setdefault` re-asserts it; the docstring calls the
  hooks additive "never relaxes the `disallowed_tools=["Agent"]` default."
- `maestro/providers/cli_paced.py` (`_tool_policy_flags`) — emits
  `--disallowedTools` only for a truthy `options.disallowed_tools`; a `None`
  (the common case — the executor sets hooks, not disallowed lists) emits no
  flag, so the spawned CLI keeps its own default tool surface including
  subagents. This is a behavioural divergence on every `cli_paced` call today.
- `maestro/providers/cli_paced.py` (`_compose_prompt`) — "fold the system
  prompt into the user prompt (the CLI driver does not thread a separate
  system prompt)"; `_CLISession` re-prepends the baseline system prompt on
  every follow-up turn, so a session pays the full identity slab per turn in
  user-message position.
- `maestro_sdk/src/maestro_sdk/types.py` (`QueryOptions.system`) — documented
  as "**not yet threaded** by `AsyncClient`: turning it into a CLI invocation
  is command-building, which is deferred to the Maestro selection seam."
  The deferred slot this proposal fills.
- `maestro/providers/cli_paced.py` (`_build_cli_command`) — the established
  spawn-argv seam (`--mcp-config`, tool-policy flags, gate `--settings`)
  where `--model` / `--fallback-model` / `--append-system-prompt` belong.
- `maestro/providers/cli_paced.py` (`_read_sidecar_usage`, `_SidecarUsage`)
  — per-turn token totals are already recovered from the transcript sidecar;
  the budget check in Step 4 consumes these, adding no new measurement.
- `maestro/providers/cli_paced.py` (`_CLISession`, `max_turns` cap) — the
  precedent for a per-session, checked-before-the-turn cap raising a terminal
  `RuntimeError` outside the resume taxonomy; the budget cap mirrors it.
- `maestro/consciousness.py` (`Consciousness.get_identity_prompt`) — the
  stable + volatile slab identity prompt that CLAUDE.md requires be injected
  "as `system` prompt"; on `cli_paced` today it is user-message content.

## Implementation Plan

Each step is independently mergeable and inert under the default `anthropic`
provider. Forwarding steps extend `_build_cli_command` (already covered by
fake-runner argv assertions), so an interrupted run leaves earlier flags
working and later ones still delegated-with-documentation.

1. [ ] **Model + fallback forwarding.** `maestro/providers/cli_paced.py` +
   `maestro/sdk.py` — fold the *effective* model and fallback onto the spawn
   argv as `--model` / `--fallback-model`. Effective-value resolution must
   match the Anthropic path (per-call field, else the `[models]`-configured
   module default), so expose small read-only accessors in `maestro.sdk` for
   the configured defaults rather than duplicating the globals. No value
   resolved → no flag → today's delegation to the CLI's own config, now the
   *fallback* rather than the only behaviour. Update the module docstring's
   "delegated" list accordingly.
   - *Depends on:* none.
   - *Acceptance:* a query with `options.model` set spawns with `--model
     <value>` (fake-runner argv); with only the `[models]` default configured,
     the same flag carries that default; with neither, no `--model` appears;
     same matrix for `--fallback-model`.
2. [ ] **System prompt as a real system prompt.**
   `maestro_sdk/src/maestro_sdk/client.py` + `maestro/providers/cli_paced.py`
   — thread `QueryOptions.system` into the spawn command as
   `--append-system-prompt <value>` (append, not replace, so the CLI's own
   baseline behaviour is preserved), and stop `_compose_prompt`-folding when
   the flag path is taken: one-shot queries pass the system text via the flag;
   sessions bake it into the session spawn (and its `--resume` respawn) so
   follow-up turns stop re-prepending the slab into user messages. Keep the
   prepend path as the documented fallback when no system prompt is set.
   - *Depends on:* none.
   - *Acceptance:* a query with `system` set spawns with
     `--append-system-prompt` carrying it and the delivered prompt text equals
     the bare user prompt (no prepended slab); a session's second turn sends
     only the user prompt while the respawn argv still carries the flag.
3. [ ] **Agent default-deny parity.** `maestro/providers/cli_paced.py`
   (`_tool_policy_flags`) — when `options` is present and
   `options.disallowed_tools` is `None`, emit `--disallowedTools Agent`,
   mirroring `_build_sdk_options`; an explicit list (including `[]`) keeps the
   caller's intent exactly as the Anthropic path does. Also apply the default
   on the `options is None` path so a bare call is not the one un-denied
   shape.
   - *Depends on:* none.
   - *Acceptance:* a call with no `disallowed_tools` spawns with
     `--disallowedTools Agent`; an explicit non-empty list emits that list
     verbatim without an injected `Agent`; behaviour matrix matches
     `_build_sdk_options` for the same inputs.
4. [ ] **Client-side task-budget enforcement.**
   `maestro/providers/cli_paced.py` — capture the effective
   `task_budget_tokens` (per-call field, else the configured module default,
   via the Step 1 accessor pattern) on `_CLISession`; after each turn's
   sidecar metering, accumulate the session's total tokens and, once the
   budget is exceeded, make the *next* `query_on_session` raise a terminal
   `RuntimeError` (the `max_turns` posture: a caller-set control-flow stop,
   outside the resume taxonomy). One-shot queries log a WARNING when a single
   turn overshoots the budget (nothing to stop after the fact). Unmetered
   turns cannot count toward the budget — document that enforcement is
   metering-dependent and therefore marker-pack-dependent (proposal 075).
   - *Depends on:* 1 (accessor pattern); enforcement quality depends on 075
     but does not block on it.
   - *Acceptance:* a session whose summed sidecar tokens exceed the budget
     refuses the following turn with a budget-naming error; under-budget and
     unmetered sessions are unaffected; a one-shot overshoot logs exactly one
     WARNING.
5. [ ] **Documentation.** `docs/sdk-internals.md` — capability-matrix caveats:
   model/fallback now forwarded (CLI config is fallback, not owner), system
   prompt via `--append-system-prompt`, Agent denied by default on both
   providers, task budget enforced client-side/post-hoc and
   metering-dependent. Trim the provider module docstring's "delegated" list
   to the remaining truth.
   - *Depends on:* 1–4.
   - *Acceptance:* the matrix and module docstring agree with the shipped
     behaviour; no doc still claims model/fallback/system/budget are
     unconditionally delegated.

## Testing Strategy

Unit tests honour `mem_fs` + no-real-I/O + no-real-network + no-real-`sleep`;
all spawn assertions run against the fake-runner argv/env seam with
`InstantCadence`.

- `tests/test_provider_cli_paced.py` (extend) — the full forwarding matrix per
  step above: per-call vs module-default vs absent for model/fallback;
  system-prompt flag + unprepended prompt text on one-shot and session paths;
  Agent default-deny truth table against `_build_sdk_options` parity; budget
  accumulation, refusal turn, unmetered no-op, one-shot WARNING.
- `tests/test_sdk.py` (extend) — the new read-only accessors return the
  `[models]` / task-budget configured values and track reconfiguration.
- `maestro_sdk/tests/test_client.py` (extend) — `QueryOptions.system` reaches
  the spawn command; absent system leaves the command unchanged.
- Guard branches: explicit `disallowed_tools=[]`, budget of `0`/`None`
  (disabled), session respawn carrying the system flag after `--resume`.

## Risk Assessment

- **Fighting the operator's CLI config.** Forwarding `--model` overrides
  `~/.claude.json` for the spawned child — previously the documented reason
  for the silent drop. Mitigation: the override only fires when Maestro has an
  explicit per-call or `[models]` value, which is exactly when the operator
  has stated intent at the Maestro layer; an unset value still delegates. The
  precedence is documented, mirroring the thinking-budget precedence note from
  061 Step 8.
- **System-prompt flag semantics drift.** `--append-system-prompt` behaviour
  is CLI-owned and could change shape between versions. Mitigation: append
  (not replace) keeps the CLI's defaults intact; the fallback prepend path is
  retained and a single config-free code path switch makes reverting trivial;
  the opt-in `RUN_CLAUDE_TESTS=1` suite is the live check.
- **Budget false stops.** A sidecar mis-read could overcount and stop a
  healthy session. Mitigation: unmetered turns add zero (never fabricate), the
  stop is a clean terminal error the caller already handles for `max_turns`,
  and disabling is the existing `task_budget_tokens` default of unset/0.
- **Agent-deny breaking a caller that wanted subagents.** No Maestro call
  site passes `disallowed_tools` expecting subagents on this provider today,
  and the Anthropic path has always denied them by default — this step removes
  a divergence, not a capability.

**Rollback posture.** Default `[sdk] provider = "anthropic"` keeps all steps
inert. Each step is a self-contained flag/branch addition; reverting any one
restores the previous documented-delegation behaviour without touching the
others.

**Data migration.** None.

## Out of scope

- **An `effort`-level CLI mapping** — the CLI exposes no effort knob beyond
  the thinking-budget env shipped in 061 Step 8; inventing one client-side
  would fake a capability. The partial mapping stays documented.
- **Pre-emptive (mid-turn) budget enforcement** — would require parsing live
  token counts out of the TUI; the post-hoc stop plus the existing
  stream-idle deadline is the honest bound for this provider.
- **`max_turns` semantics unification** — the prompt-turn vs agentic-turn
  divergence was deliberately documented in 061 Step 6 and stands.
