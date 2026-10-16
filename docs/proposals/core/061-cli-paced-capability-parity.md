# 061: Capability Parity for the CLI-Paced Provider

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/providers/cli_paced.py`, `maestro/providers/cli_paced_tool_adapter.py`, `maestro_sdk/src/maestro_sdk/{client,types,session}.py`, `maestro_sdk/src/maestro_sdk/mcp_config.py`, `maestro/providers/capability_gate.py` (tests only), `docs/sdk-internals.md`, `docs/deployment.md`, `CLAUDE.md`, tests |
| **Depends on** | 056 (shipped), 058 (shipped) |
| **What landed** | Step 1 (persistent pty session): `maestro_sdk/src/maestro_sdk/session.py` `Session` keeps one `PtyRunner` alive across turns — reusing the child on the `ClientMarkers.idle` redraw and respawning it on EOF, reaped once at `aclose()`; `AsyncClient.create_session()` returns it and `AsyncClient._drive_turn` reports `ended_on_eof` so a session can tell a reusable child from a spent one. Step 2 (`--resume` / initial-prompt command-building, `maestro_sdk`-side): `AsyncClient.create_session(initial_prompt=…, resume=…)` spawns `claude --resume <id>` (via `_spawn` / `_RESUME_FLAG`), `Session` seeds the `initial_prompt` as its first turn and refreshes its resume id from each turn's CLI-reported `QueryResult.session_id` — now populated via `ClientMarkers.session_id` + `_extract_session_id`. Tests in `maestro_sdk/tests/test_session.py`. Step 3 (`complete_on` in-stream early-exit, `maestro_sdk`-side): `QueryOptions.complete_on` is evaluated against `parser.response` inside `client.py`'s `_stream` loop and breaks with `exited_early=True` on the first truthy non-idle snapshot; `Session.query` reaps-and-respawns on `exited_early` exactly as on EOF. Tests in `maestro_sdk/tests/test_client.py` + `test_session.py`. Step 4 (in-process-tools adapter): `maestro/providers/cli_paced_tool_adapter.py` `CliPacedToolAdapter.materialize` emits the CLI `--mcp-config` JSON payload + the `mcp__<server>__<tool>` allow-list, and `build_servers` builds the in-process hosts via `create_inproc_mcp_server`; the pure-JSON `--mcp-config` seam is `maestro_sdk/src/maestro_sdk/mcp_config.py` (`stdio_server` / `build_mcp_config` / `serialize_mcp_config`). Tests in `tests/test_tool_adapter.py` + `maestro_sdk/tests/test_mcp_config.py`. The cross-process launcher entrypoint (`cli_paced_mcp_serve`) is deliberately deferred to the rollout proposal, so the config is materialised and tested but never executed. Step 5 (external MCP forwarding): `maestro/providers/cli_paced.py` `_external_mcp_servers` selects the stdio-shaped (`{command, args, env}`) entries from `options.mcp_servers` and `_build_cli_command` folds them into an inline string-encoded `--mcp-config` payload on the spawn argv (via `mcp_config.py`'s `build_mcp_config` / `serialize_mcp_config`); both `query` and `create_session` now call `_build_cli_command`, and `_reject_unsupported_options` no longer rejects `mcp_servers` (the `_MCP_UNSUPPORTED` rejection is gone). The `remote_mcp` capability flag is still held `False` until Step 11 flips all eight together. Tests in `tests/test_provider_cli_paced.py`. Step 6 (tool-policy + `max_turns` forwarding): `maestro/providers/cli_paced.py` `_tool_policy_flags` folds `tools` → `--tools` (an empty list emits `--tools ""`, the CLI's "no tools" form, kept distinct from `tools=None`), `allowed_tools` → `--allowedTools`, and `disallowed_tools` → `--disallowedTools` onto the spawn argv built by `_build_cli_command` (alongside `--mcp-config`); `max_turns` is enforced as a per-session prompt-turn cap on `_CLISession` (captured at `create_session`, checked in `query_on_session` before the `turn_count` bump so the `N+1`-th turn raises a `RuntimeError` with no side effect). `_reject_unsupported_options` no longer rejects either (the `_TOOL_POLICY_UNSUPPORTED` / `_MAX_TURNS_UNSUPPORTED` rejections are gone). Tool-policy forwarding rides behind the still-`False` capability flags until Step 11. Tests in `tests/test_provider_cli_paced.py`. Step 7 (long-context beta via the CLI child environment): `maestro/providers/cli_paced.py` `_beta_env` maps a requested `maestro.sdk.LONG_CONTEXT_BETA` (the lone member of `_SUPPORTED_BETAS`) onto an `anthropic-beta` header carried via the spawned `claude` child's `ANTHROPIC_CUSTOM_HEADERS` env var, returning `{}` for the no-beta path; any *other* requested beta has no CLI-side mechanism and still raises `_BETAS_UNSUPPORTED` (a silently dropped beta could change the model's behaviour). `_default_client_factory` gains an `env` parameter merged **over** `os.environ` (a bare `env` dict would replace the subprocess environment wholesale, stripping `PATH` / `ANTHROPIC_API_KEY`); a non-beta call passes `env=None` so the child inherits this process's environment unchanged. The env bakes into both `query` and `create_session` spawns, so every turn a session drives carries the same header; `_reject_unsupported_options` no longer rejects `betas` (it defers to `_beta_env`). The `long_context` capability flag is still held `False` until Step 11 flips all eight together. Tests in `tests/test_provider_cli_paced.py`. Step 8 (extended-thinking budget via the CLI child environment): `maestro/providers/cli_paced.py` `_thinking_env` maps an effort-bearing explicit `thinking_budget_tokens` onto the spawned `claude` child's `MAX_THINKING_TOKENS` env var (`_MAX_THINKING_TOKENS_ENV`), gated on `options.effort is not None` to mirror the Anthropic path (a budget without effort stays inert on both providers, and a falsy `0` leaves the child env untouched); `_child_env` unions it with the Step 7 beta header (the two contribute disjoint keys), and the merge in `_default_client_factory` is last-wins so the Maestro-supplied budget overrides the CLI's own `~/.claude.json` thinking config for that child. `_reject_unsupported_options` no longer rejects `effort` / `thinking_budget_tokens`; the env bakes into both `query` and `create_session` spawns. The `extended_thinking` capability flag is still held `False` until Step 11 flips all eight together. Tests in `tests/test_provider_cli_paced.py`. Step 9 (structured-output validate-and-reask): `maestro/providers/cli_paced.py` `_query_structured` enforces `output_format` client-side on the one-shot `query` path — `_augment_prompt_for_schema` appends the JSON Schema (and a correction note on the single re-ask), `_parse_and_validate` parses via `QueryResult.parse_json` + `jsonschema.validate`, and a first-attempt parse/validation miss re-asks exactly once before failing loud with `_OUTPUT_FORMAT_VALIDATION_FAILED`; the validated object rides back on `QueryResult.structured_output`. The schema is well-formedness-checked up front (`check_schema`) so a malformed schema fails loud with no CLI turn. `query_on_session` does no validation and rejects `output_format` with `_OUTPUT_FORMAT_SESSION_UNSUPPORTED` (a session's structured output would silently be `None`); `_reject_unsupported_options` no longer raises a blanket `_OUTPUT_FORMAT_UNSUPPORTED`. The `structured_output` capability flag is still held `False` until Step 11 flips all eight together. Tests in `tests/test_provider_cli_paced.py`. Step 10 (token/cost metering from the CLI transcript sidecar): `maestro/providers/cli_paced.py` `_to_query_result` reads `_read_sidecar_usage(result.session_id, cwd)` — the per-session transcript JSONL located via `_effective_cwd` — and re-packs the real summed token counts into a `QueryUsage` with `unmetered=False` and the sidecar-reported model, falling back to the zero-filled `unmetered=True` shape only when the sidecar is missing/unreadable/ambiguous (never a fabricated count); `total_cost_usd` stays `None` on both branches because the sidecar carries token counts, not a dollar figure (cost is derived downstream). The `usage_metering` capability flag was held `False` until Step 11. Tests in `tests/test_provider_cli_paced.py`. Step 11 (capability flip + provider wiring): `CliPacedProvider.capabilities` is all-`True`; `create_session` forwards `initial_prompt` (composed with the system prompt) and `resume_session_id` into `AsyncClient.create_session`; `query` / `query_on_session` thread an adapted text-only `complete_on` (`_adapt_complete_on` passes `None` for the message — the CLI path has no message object); the `_RESUME_UNSUPPORTED` / `_INITIAL_PROMPT_UNSUPPORTED` / `_COMPLETE_ON_UNSUPPORTED` rejections and the second-turn continuity WARNING are gone; `hooks` (shared check) and session-path `output_format` are the only remaining rejections. Tests in `tests/test_provider_cli_paced.py` + `tests/test_capability_gating.py` (`cli_paced` satisfies every phase). Step 12 (docs): `docs/sdk-internals.md` capability matrix shows `cli_paced` fully qualified with per-capability mechanisms and the action-gate / marker-pack rollout caveats. |
| **What is missing** | Nothing — Steps 11–12 landed: the provider-side wiring of Step 2's resume/initial-prompt into `cli_paced.create_session` (forwarded into `AsyncClient.create_session`; the `_RESUME_UNSUPPORTED` / `_INITIAL_PROMPT_UNSUPPORTED` rejections are gone), the provider-side `complete_on` wiring (`_adapt_complete_on` re-shapes Maestro's two-arg predicate onto the driver's text-only `QueryOptions.complete_on`; `_COMPLETE_ON_UNSUPPORTED` is gone, as is the second-turn continuity WARNING), the eight-flag capability flip (`CliPacedProvider.capabilities` all `True`; `hooks` and session-path `output_format` remain the only rejections), and the Step 12 docs. The production rollout (canary phase, per-CLI-version marker packs, the `cli_paced_mcp_serve` launcher entrypoint, action-gate replication) stays a separate proposal per *Out of scope*. |

## Description

> Status updated to shipped on 2026-06-11 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

The `cli_paced` provider shipped (proposals 056 + 058) as the registry's most
capability-limited member: every flag on its `Capabilities` descriptor is
`False`, so `maestro/providers/capability_gate.py` keeps it off every phase
that needs sessions, in-process tools, remote MCP, long context, structured
output, extended thinking, usage metering, or streaming — which is *every*
load-bearing loop phase. It is selectable but can host almost nothing. This
proposal closes those eight gaps so `cli_paced` becomes a **fully qualified
provider**: one the capability gate will route to any phase.

The design constraint is that the human-paced pty driver stays the driving
discipline — the whole reason `maestro_sdk` exists is to exercise the CLI at
human cadence and surface TUI/UX issues a bulk-write SDK hides. So none of the
eight capabilities is reached by abandoning the pty; each is reached through a
mechanism the `claude` CLI *already* exposes around its TUI: a persistent pty
session for transcript continuity, the CLI's per-session transcript JSONL
sidecar for token/cost metering, a Maestro-hosted local MCP server the CLI
connects to for in-process tools, the CLI's own `--mcp-config` for external
MCP, the spawned child's environment for the long-context beta and
thinking budget, the existing incremental parser for in-stream early-exit, and
client-side schema validation for structured output. The pty stays in charge;
the capabilities ride the CLI's own side channels.

End state: `CliPacedProvider.capabilities` is `Capabilities(...)` with all
eight flags `True`, the now-supported option fields stop raising
`NotImplementedError`, and `tests/test_capability_gating.py` shows `cli_paced`
satisfies every phase's requirements. **This is capability parity, not a
production rollout.** Flipping the eight flags says what the provider *can do*;
it does not assert the loop's trust controls hold over a subprocess — the
in-process `PreToolUse` action-gate (`maestro/tool_gating.py`) cannot reach a
separate `claude` process, so routing `cli_paced` to a mutating phase in
production is deliberately left to a later rollout proposal (see *Out of
scope*). The default stays `[sdk] provider = "anthropic"`; this change is inert
until an operator opts in.

## Relevant Findings

- `maestro/providers/cli_paced.py` — `capabilities` declares all eight
  flags `False`; each docstring line states what the CLI path
  "cannot do in this slice," i.e. the gaps this proposal closes.
- `maestro/providers/cli_paced.py` — `_reject_unsupported_options`
  raises `NotImplementedError` for `mcp_servers`, `betas`, `output_format`,
  tool policy (`tools` / `allowed_tools` / `disallowed_tools`), `max_turns`,
  and `hooks`. Seven of these become supported here; `hooks` stays rejected
  (it is not a `Capabilities` flag — see Risk Assessment).
- `maestro/providers/cli_paced.py` — `_to_query_result` re-packs into a
  zero-count `QueryUsage(unmetered=True)` because "the CLI surfaces no token
  counts." The usage-metering step replaces this with counts read from the
  CLI's transcript sidecar, falling back to `unmetered=True` only when the
  sidecar is unreadable.
- `maestro/providers/cli_paced.py` — `create_session` raises
  `NotImplementedError(_RESUME_UNSUPPORTED)` and
  `NotImplementedError(_INITIAL_PROMPT_UNSUPPORTED)`; the
  persistent-session step removes both rejections.
- `maestro/providers/cli_paced.py` — `query_on_session` spawns a fresh
  `claude` process per turn with no resume, so each turn is
  single-turn-equivalent and `_FIRST_CONTINUITY_TURN` logs a one-shot WARNING.
  The persistent-session step removes this warning.
- `maestro/providers/base.py` — the eight `Capabilities` fields, frozen;
  the exact set this proposal must flip.
- `maestro/providers/base.py` — the `ToolAdapter` protocol
  (`materialize(registry) -> (qualified_tool_names, mcp_servers_dict)`). The
  Anthropic and OpenAI adapters already implement it; the in-process-tools step
  adds a third for `cli_paced` that emits a CLI `--mcp-config` instead.
- `maestro_sdk/src/maestro_sdk/client.py` + `maestro_sdk/src/maestro_sdk/session.py` — `AsyncClient`
  "owns no process between calls — each `query` spawns, drives, and reaps its
  own runner"; `Session.query` keeps one runner alive across turns by calling
  `_drive_turn` directly, reusing the child on the idle redraw, respawning on
  EOF or any mid-turn error, and reaping once at `aclose()`. This is what
  the persistent-session step implemented.
- `maestro_sdk/src/maestro_sdk/client.py` — `ClientMarkers.idle`
  already exists and `_stream` already breaks the read loop when the idle marker
  redraws; persistent interactive turns need only keep the runner alive between
  `query` calls and configure `idle`.
- `maestro_sdk/src/maestro_sdk/client.py` — `env` is
  already threaded from `AsyncClient` into `PtyRunner`, so setting the
  long-context beta / thinking budget on the child process is plumbing that
  already exists, not new transport.
- `maestro_sdk/src/maestro_sdk/client.py` — the `_stream` read loop has
  no early-exit hook; the streaming step evaluates a `complete_on` predicate
  against `parser.response` here and tears the child down early via the existing
  `finally` ladder.
- `maestro_sdk/src/maestro_sdk/types.py` — `QueryOptions` carries only
  `cwd` / `system` / two timeouts; it has no `command` / `mcp_config` /
  `resume` / `extra_args` field, so resume + MCP-config + beta flags are
  command-building this proposal adds.
- `maestro_sdk/src/maestro_sdk/types.py` — `QueryResult.usage` is
  `QueryUsage | None` and `QueryUsage` requires all four token counts;
  recovering metering means producing real counts or honestly leaving `None`.
- `maestro/sdk.py` — `LONG_CONTEXT_BETA = "context-1m-2025-08-07"` is injected
  as a `betas` entry on the Anthropic path; the long-context step maps a
  `betas` membership test to the CLI child's beta-enabling environment.
- `tests/test_capability_gating.py` — pins that a phase needing a capability a
  provider lacks raises at config-load; this proposal's end state is the
  inverse assertion for `cli_paced` (every phase now satisfied).

## Implementation Plan

Steps are ordered so an interrupted run leaves the codebase working: each
capability lands behind the still-`False` flag for that capability, and the
single step that flips all eight flags (Step 11) lands last, after every
mechanism it vouches for is in place. Until Step 11, the provider keeps
rejecting/​degrading exactly as today, so intermediate merges change nothing for
the default `anthropic` path. Steps split between `maestro_sdk/` (the driving
mechanism) and `maestro/providers/cli_paced.py` (the option threading + the
flag flip).

### Phase 1 — Persistent session + streaming (maestro_sdk core)

1. [x] **Persistent pty session.** `maestro_sdk/src/maestro_sdk/client.py` +
   new `session.py` — keep one `PtyRunner` alive across `Session.query` turns
   instead of spawning per call. Drive each follow-up turn by typing into the
   live pty and breaking on the `ClientMarkers.idle` redraw (the mechanism at
   `client.py` already exists); `aclose` walks the existing SIGTERM→SIGKILL
   ladder once. The one-shot `AsyncClient.query` path is unchanged.
   - *Depends on:* none.
   - *Acceptance:* with `InstantCadence` + a fake runner configured with an
     `idle` marker, two sequential `Session.query` calls drive the *same*
     runner (one spawn, one reap) and the second turn sees the first turn's
     context in the fake transcript; the one-shot `query` path still spawns and
     reaps per call.
2. [x] **`--resume` / initial-prompt command-building.**
   `maestro_sdk/src/maestro_sdk/types.py` + `client.py` — add a `resume` (CLI
   session id) and `initial_prompt` surface so `create_session` can spawn
   `claude --resume <id>` and optionally send a first turn. Captures the CLI's
   reported session id into `QueryResult.session_id` so a later call can resume.
   - *Depends on:* 1.
   - *Acceptance:* `create_session(resume="abc")` builds a command containing
     `--resume abc` (asserted against the fake runner's recorded argv), an
     `initial_prompt` is sent as the first turn, and `QueryResult.session_id`
     carries the CLI-reported id.
3. [x] **`complete_on` in-stream early-exit.**
   `maestro_sdk/src/maestro_sdk/client.py` — evaluate an optional
   `complete_on(response_text)` predicate against `parser.response` inside the
   `_stream` loop; when it fires, stop reading and tear the child down via the
   existing `finally`. This is the streaming capability — the repaint is
   irrelevant because the parser already diffs snapshots to text.
   - *Depends on:* none.
   - *Acceptance:* a fake runner emitting incremental answer chunks stops at the
     chunk that first satisfies `complete_on`, returns the accumulated text, and
     reaps the child without waiting for EOF or the idle marker.

### Phase 2 — Tools and MCP

4. [x] **In-process tools via a Maestro-hosted local MCP server.** New
   `maestro/providers/cli_paced_tool_adapter.py` (implements
   `providers.base.ToolAdapter`) + `maestro_sdk/src/maestro_sdk/mcp_config.py`
   — materialise the `ToolRegistry` into (a) a local stdio MCP server that hosts
   Maestro's `InprocTool` handlers in-process and (b) a CLI `--mcp-config` JSON
   pointing the spawned `claude` at it, plus the `mcp__<server>__<tool>`
   allow-list. The handlers run in Maestro's process; the CLI reaches them over
   MCP. No handler logic in `maestro/tools.py` changes.
   - *Depends on:* 1.
   - *Acceptance:* an `InprocTool` registry round-trips through the adapter —
     the emitted `--mcp-config` registers a server whose advertised tools match
     the registry, a dispatched tool call reaches the unchanged Python handler,
     and the qualified names equal the Anthropic adapter's `mcp__server__tool`
     form.
5. [x] **External MCP forwarding.** `maestro/providers/cli_paced.py` +
   `mcp_config.py` — when `options.mcp_servers` is non-empty, fold Maestro's
   `[mcp]` server config (GitHub/Fetch/Context7, built by
   `config.mcp_servers_config()`) into the CLI `--mcp-config` payload, passed
   inline as string-encoded JSON on the spawn argv (no temp file — keeps the
   path within the no-real-IO unit-test discipline). Remove the
   `_MCP_UNSUPPORTED` rejection. The CLI speaks MCP natively, so this is config
   generation, not transport.
   - *Depends on:* 4.
   - *Acceptance:* a `query` with `options.mcp_servers` set carries a
     `--mcp-config` containing those server entries (asserted against the fake
     runner argv) instead of raising `NotImplementedError`.
6. [x] **Tool-policy + `max_turns` forwarding.**
   `maestro/providers/cli_paced.py` — map `allowed_tools` / `disallowed_tools`
   to the CLI's `--allowedTools` / `--disallowedTools`, and enforce `max_turns`
   in the session-driving loop (parallel to the OpenAI adapter's turn cap).
   Remove the `_TOOL_POLICY_UNSUPPORTED` and `_MAX_TURNS_UNSUPPORTED`
   rejections.
   - *Depends on:* 4.
   - *Acceptance:* tool-policy fields appear as the corresponding CLI flags and
     a `max_turns=N` session stops after N turns; neither field raises.

### Phase 3 — Model knobs + structured output

7. [x] **Long-context beta via child environment.**
   `maestro/providers/cli_paced.py` — when `options.betas` contains
   `LONG_CONTEXT_BETA`, set the beta-enabling variable on the spawned child's
   `env` (already threaded at `client.py`) instead of raising
   `_BETAS_UNSUPPORTED`. Unknown betas still raise (fail-loud on the un-mappable).
   - *Depends on:* none.
   - *Acceptance:* a `query` whose options request the long-context beta spawns
     the CLI with the beta enabled in its environment (asserted against the fake
     runner's recorded env); an unrecognised beta still raises.
8. [x] **Extended-thinking budget via child environment.**
   `maestro/providers/cli_paced.py` — thread `effort` / `thinking_budget_tokens`
   into the CLI child's thinking configuration (env/flag) rather than silently
   delegating them to the operator's `~/.claude.json`. Document the precedence
   (Maestro-supplied budget overrides CLI config for the spawned child).
   - *Depends on:* 7.
   - *Acceptance:* a `query` with a thinking budget spawns the CLI with the
     mapped thinking configuration set on its child env (asserted against the
     fake runner), and the default (no budget) leaves the child env untouched.
9. [x] **Structured output via client-side validate-and-reask.**
   `maestro/providers/cli_paced.py` — when `options.output_format` is set,
   append the JSON schema to the prompt, parse the answer region, validate
   against the schema, and re-ask once on a parse/validation miss before failing
   loud. Carry the validated object on `QueryResult`. Remove
   `_OUTPUT_FORMAT_UNSUPPORTED`. Enforcement is client-side (documented
   caveat) — operators needing a server-side guarantee use `anthropic`.
   - *Depends on:* 1.
   - *Acceptance:* a `query` with `output_format` returns a validated object for
     a conforming answer, re-asks exactly once for a non-conforming first answer,
     and raises a clear error (not a silent `None`) when the re-ask also fails.

### Phase 4 — Usage metering

10. [x] **Token/cost metering from the CLI transcript sidecar.**
    `maestro/providers/cli_paced.py` + `maestro_sdk` — after each turn, read the
    CLI's per-session transcript JSONL (`~/.claude/projects/<slug>/<session>.jsonl`,
    located via the session id from Step 2) and re-pack the per-turn `usage`
    into a real `QueryUsage`. Fall back to today's `unmetered=True` zero-fill
    **only** when the sidecar is missing/unreadable/ambiguous — never fake a
    count. This is the only pty/TUI-compatible way to recover metering.
    - *Depends on:* 2.
    - *Acceptance:* a turn whose transcript sidecar (a test fixture file via
      `mem_fs`) carries usage produces a `QueryUsage` with those counts and
      `unmetered=False`; a turn with no readable sidecar stays `unmetered=True`
      with zero counts (no fabricated measurement).

### Phase 5 — Flip capabilities + docs

11. [x] **Flip the eight capability flags.**
    `maestro/providers/cli_paced.py` — set `capabilities = Capabilities(...)`
    with all eight flags `True`, and delete the option-rejection branches made
    obsolete by Steps 5–9 (keeping the `hooks` rejection — see Risk Assessment).
    The `cli_paced` factory in `registry.py` is unchanged. `cli.main`'s
    capability gate now routes `cli_paced` to any phase.
    - *Depends on:* 1–10.
    - *Acceptance:* `CliPacedProvider.capabilities` has all eight flags `True`;
      `tests/test_capability_gating.py` shows `cli_paced` satisfies every phase's
      `required_capabilities_for_phase`; the only field
      `_reject_unsupported_options` still raises on is `hooks`.
12. [x] **Documentation.** `docs/sdk-internals.md`, `docs/deployment.md`,
    `CLAUDE.md` — update the provider capability matrix to show `cli_paced` at
    full parity, document each capability's CLI-side mechanism (persistent pty
    session, transcript-sidecar metering, Maestro-hosted MCP server, child-env
    betas/thinking, client-side structured-output enforcement), and state the
    action-gate caveat: the in-process `PreToolUse` gate does not reach the CLI
    subprocess, so production routing to mutating phases awaits the rollout
    proposal.
    - *Depends on:* 11.
    - *Acceptance:* the capability matrix in `docs/sdk-internals.md` shows
      `cli_paced` fully qualified with the per-capability mechanism noted and the
      action-gate caveat called out; `CLAUDE.md`'s SDK invariant line is
      unchanged in count (one line) but accurate.

## Testing Strategy

Unit tests honour `mem_fs` + no-real-I/O + no-real-network + no-real-`sleep`;
the `claude` CLI and the pty are mocked via the existing fake-runner seam
(`RunnerFactory`) and `InstantCadence`. Integration against the real CLI stays
opt-in behind `RUN_CLAUDE_TESTS=1` per `maestro_sdk/README.md`.

- `maestro_sdk/tests/test_session.py` (new) — persistent session reuses one
  runner across turns; the one-shot path still spawns per call; `--resume`
  argv and captured `session_id` round-trip; a closed session refuses queries.
- `maestro_sdk/tests/test_client.py` (extend) — `complete_on` early-exit stops
  mid-stream and reaps the child; an unmet predicate still ends on idle/EOF.
- `tests/test_tool_adapter.py` (extend) — the `cli_paced` adapter round-trips
  the `InprocTool` registry into a `--mcp-config` whose advertised tools and
  `mcp__server__tool` names match, and a dispatched call reaches the unchanged
  handler.
- `tests/test_provider_cli_paced.py` (extend) — external MCP forwarding, tool
  policy, `max_turns`, long-context-beta env, thinking-budget env, and
  structured-output validate-and-reask each take the supported branch instead
  of raising; `hooks` still raises; transcript-sidecar metering produces real
  counts with `unmetered=False` and falls back to `unmetered=True` when the
  sidecar is unreadable.
- `tests/test_capability_gating.py` (extend) — with all eight flags `True`,
  routing `cli_paced` to `implementation` / `run_task` / `_self_review` no
  longer raises at config-load; a hand-rolled provider missing one flag still
  does (guarding the gate itself).
- `tests/test_check_secrets.py` — unchanged: the `claude`-logged-in precondition
  (056 Step 13) already gates `cli_paced` selection.

## Risk Assessment

**What could regress.**

- **Trust chain — the action-gate cannot reach a subprocess.** Maestro's
  `PreToolUse` action-gate (`maestro/tool_gating.py`) that blocks PR/push bypass
  runs in-process and cannot intercept a separate `claude` process; `hooks`
  therefore stays an unsupported option even after this proposal. Flipping
  `in_process_tools` / tool-policy to supported means the CLI *can* run tools,
  but the loop's mutation guard does not fire over them. Mitigation: this is
  capability parity only — the eight flags describe capability, not trust — and
  production routing to action-gated phases (`implementation`, `run_task`) is
  explicitly deferred to a rollout proposal that must first replicate the gate
  in the CLI's own permission settings. Default stays `anthropic`; nothing
  routes to `cli_paced` when this lands.
- **Metering fragility.** The transcript-sidecar path depends on the CLI's
  on-disk JSONL layout, which is not a stable public contract. Mitigation: the
  fallback is honest `unmetered=True` (never a fabricated count), so a layout
  change degrades to "unknown," exactly today's behaviour, rather than to a
  wrong number that pollutes the burn-rate gate.
- **Structured-output enforcement is client-side.** Validate-and-reask cannot
  guarantee conformance the way a server-side schema does, and a pathological
  model could fail both attempts. Mitigation: the second failure raises (no
  silent `None`), and the docs steer hard-guarantee callers to `anthropic`.
- **Persistent-session liveness.** Keeping a pty alive across turns introduces a
  wedged-subprocess hazard the per-call spawn avoided. Mitigation: the existing
  `stream_idle_timeout_seconds` + SIGTERM→SIGKILL `finally` ladder already bound
  every read and teardown; the session test asserts no leaked child/fd after a
  cancelled mid-turn read.

**Rollback posture.** Default `[sdk] provider = "anthropic"` makes the whole
change inert. Each step is additive behind its still-`False` flag until Step 11;
reverting is setting the flag back or reverting the Step 11 PR — every earlier
step is a behaviour-preserving mechanism addition under the unchanged flags.

**Data migration.** None. `QueryUsage` / `IterationUsage` already tolerate the
`unmetered` flag (058 Step 13); `usage.jsonl` is forward-compatible whether a
`cli_paced` row carries real counts or stays unmetered.

## Out of scope

- **Production rollout of `cli_paced`** — the canary phase selection, the
  `claude login` operational precondition on the loop host, observability under
  a non-default provider, and the rollback trigger. A separate, lighter rollout
  proposal owns turning it on (this proposal is engineering-only).
- **Replicating the in-process action-gate in the CLI's permission config** —
  the prerequisite for routing `cli_paced` to mutating phases; it belongs with
  the rollout proposal so the trust chain stays explicit.
- **A per-tool auto-approval policy** for the CLI's approval modals — deferred
  to 056's approval-policy follow-up (deny-by-default stays the posture).
- **Removing `maestro/sdk.py` or the Anthropic provider** — it remains the
  default indefinitely; this proposal makes `cli_paced` a peer, not a
  replacement.
