# 058: Provider-Agnostic SDK Layer

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/sdk.py`, `maestro/providers/` (`base.py`, `anthropic.py`, `anthropic_tool_adapter.py`, `registry.py`, `capability_gate.py`, `openai.py` query + sessions + error mapping + `openai_tool_adapter.py` + `cli_paced.py` landed), `maestro/{agent,evolution,executor,planner,purpose,consciousness,dreaming,artifact_review,doc_proposals}.py` (session type only), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `docs/sdk-internals.md`, `docs/deployment.md`, `CLAUDE.md`, tests |
| **Relates to** | 056 _(generalizes its selection seam; 058 Phases 1–2 land first — see Relationship)_ |
| **What landed** | **Phase 1 complete.** `maestro/providers/base.py` — `Provider` protocol with the four async ops, frozen `Capabilities` dataclass with the eight named flags, opaque `Session` newtype (aliased to `Any`), and re-exports of `QueryOptions` / `QueryResult` / `QueryUsage` / `ToolCall` / `ErrorCategory` / `SDKError` (Step 1). Provider-neutral `ToolRegistry` from `_build_agent_tools`, materialised by `maestro/providers/anthropic_tool_adapter.py` (Step 2). `maestro/providers/anthropic.py::AnthropicProvider` delegating wrapper (Step 3). Nine consumer modules now type their session slot via the opaque `maestro.providers.base.Session` handle instead of `ClaudeSDKClient`, pinned by `tests/test_provider_session_handle.py` (Step 4). `tests/test_providers_base.py` / `test_provider_anthropic.py` pin the contract surface. |
| **What is missing** | Phase 3 nearly complete — Step 9 (`OpenAIProvider` query path), Step 10 (`OpenAIToolAdapter`), and Step 11 (OpenAI sessions over `previous_response_id` + `openai`→`ErrorCategory` mapping wrapping both the one-shot and session paths into `SDKError`) shipped. The provider remains deliberately **not** registered in `registry.py`: the Step 10 tool adapter is still not wired into `OpenAIProvider.query` (the loop's call sites pass Anthropic-materialised MCP servers through `QueryOptions`, so threading the neutral registry to OpenAI is a separate plumbing concern), so registering now would let the capability gate vouch for `in_process_tools=True` while queries run toolless — a silent degradation the fail-loud doctrine forbids. Selecting `[sdk] provider = "openai"` therefore still fails loud at startup; OpenAI stays a standalone, test-pinned seam (like Steps 2/10) until that wiring + registration land. Phase 4 is complete: Step 12 re-homed the `cli_paced` backend as the registered `CliPacedProvider` (owning the adapter bodies directly in `maestro/providers/cli_paced.py` — the `maestro/sdk_backends/` package was retired — behind a lazy registry factory), so `[sdk] provider = "cli_paced"` now selects through the registry. Phase 5 Step 13 (usage-metering honesty) landed — `QueryUsage`/`IterationUsage` carry an `unmetered` flag that keeps `cost_tracking` from reading the zero-count rows as spend and renders the dashboard cost as `—`. Step 14 (docs) has now landed too — `docs/sdk-internals.md` gained the Provider abstraction + capability-matrix sections, `docs/deployment.md` the per-provider credential table, and `CLAUDE.md` the SDK provider-registry / capability-gate invariant lines — so all fourteen numbered steps are shipped. The sole residual is the deliberate, documented deferral noted under Steps 9–11: the OpenAI provider stays unregistered (its Step 10 tool adapter is not yet wired into `OpenAIProvider.query`), so `[sdk] provider = "openai"` still fails loud — a follow-up plumbing concern outside this proposal's numbered scope, not a missing step. Phase 2 is complete: the provider registry + `sdk.py` delegation (5), `[sdk] provider` / per-phase config + the `resolved_sdk_provider` / `provider_for_phase` resolvers wired through `cli.main` (6), startup capability validation (`capability_gate.py`, 7), and provider-aware `check_secrets` dispatch (8). Selection stays at the `"anthropic"` default in production; `cli_paced` is now selectable through the registry (Step 12), while `openai` validates at config-schema level but still fails loud in `configure_provider` until it registers (Phase 3 wiring). |

## Description

> Status updated to shipped on 2026-06-02 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.
>
> Status updated to partial on 2026-05-28 by /update-proposals — Phase 1 Step 1 (the `Provider` protocol, `Capabilities` descriptor, opaque `Session` handle, and contract re-exports under `maestro/providers/base.py`) has shipped; Steps 2–14 remain open. See [README.md](../README.md#open-proposals) for the current status row.
>
> Phase 1 completed with the Step 4 PR (this change) — Steps 2–3 had already landed but their checkboxes were stale; they are re-synced here alongside Step 4 (opaque `Session` handle in the nine consumer modules, guarded by `tests/test_provider_session_handle.py`). The next move is Phase 2's selection seam (Steps 5+).

Maestro's entire reasoning surface routes through four functions in
`maestro/sdk.py` — `query`, `create_session`, `query_on_session`,
`disconnect_session` — and the `QueryOptions` / `QueryResult` data
contracts around them. `QueryOptions` already *describes itself* as
"vendor-neutral options … map to universal concepts shared across agent
SDKs (Claude, Codex, Gemini)" (`sdk.py`), but the neutrality is
aspirational: every code path below the contract is hard-bound to
`claude_agent_sdk`. The conversion to `ClaudeAgentOptions`, the
`ClaudeSDKClient` session object, the `ResultMessage`-derived usage, the
`ErrorCategory` mapping from Anthropic exception types, MCP servers, and
the `context-1m` beta are all Anthropic-specific and braided through the
loop. The binding has even leaked *out* of `sdk.py`: nine modules import
`ClaudeSDKClient` directly to type the `session` they pass back in.

This proposal introduces a **provider abstraction** so the LLM vendor
becomes a configuration choice, not an architectural assumption. A
`Provider` protocol declares the four operations plus an explicit
`Capabilities` descriptor (sessions, in-process tools, remote MCP, extended
thinking, long-context, structured output, usage metering, streaming). The current
Anthropic implementation becomes the first registered provider; a second
reference provider on the **OpenAI SDK** proves the seam against a genuinely
different vendor; and a **custom** provider slot (the `maestro_sdk`
CLI-paced backend from #056) lands under the same protocol rather than as a
one-off switch. The whole track is phased so each slice is independently
mergeable and inert until
the selection seam lands.

End state once this lands: `[sdk] provider = "anthropic" | "openai" |
"cli_paced"` (default `"anthropic"`) selects which implementation backs the
four public functions, with optional per-phase overrides. Phases declare
the capabilities they require; the loop validates the selected provider at
startup and **fails loud** when a phase needs a capability the provider
lacks (e.g. a phase that consumes the external GitHub/Fetch/Context7 MCP
servers on a provider that cannot reach remote MCP). Crucially, Maestro's
own introspection tools are *not* one of those gaps: they are plain Python
handlers that any function-calling provider can host, so the abstraction
treats them as a portable capability rather than a Claude-only one. The
loop's call sites stop importing `claude_agent_sdk` entirely — they hold an
opaque `Session` handle and never name a vendor type.

## Relationship to #056

Proposal **056** (CLI-Paced SDK Backend) introduces a narrower switch —
`[sdk] backend = "agent" | "cli_paced"` — to select between the official
agent SDK and the `maestro_sdk` pty-driven CLI. This proposal **generalizes
that seam**: the binary `backend` flag becomes the open-ended `[sdk]
provider` registry, and 056's `cli_paced` implementation becomes one
provider registered under the `Provider` protocol (Phase 4 here). The two
proposals should land in this order — 058 Phases 1–2 first (protocol +
seam), then 056's pty/screen/parser/approvals slices, then 058 Phase 4
adapts the finished `maestro_sdk.AsyncClient` to the protocol. If 056 ships
first, its `[sdk] backend` flag is migrated to `[sdk] provider` in Phase 2
with a one-release alias.

## Relevant Findings

- `maestro/sdk.py` — `QueryOptions` docstring already claims
  vendor-neutrality and names Claude/Codex/Gemini, but `betas`
  (`sdk.py`) is Anthropic-only and the docstrings describe
  conversion to `ClaudeAgentOptions`. The contract is the right seam; the
  implementation below it is not neutral.
- `maestro/sdk.py` — `QueryResult.session_id` is defined purely
  in terms of `ClaudeAgentOptions(resume=<id>)` server-side transcript
  continuity. Other vendors model conversation state differently (OpenAI
  `previous_response_id` / local transcript replay); the field needs a
  provider-neutral meaning.
- `maestro/sdk.py` — the four public functions
  (`query`, `create_session`, `query_on_session`, `disconnect_session`).
  The last three traffic in `ClaudeSDKClient`. These four signatures are
  the exact surface the `Provider` protocol must reproduce.
- `maestro/sdk.py` — `ClaudeAgentOptions(**sdk_opts)` and
  `ClaudeSDKClient(options=...)` construction. This is the single point
  where vendor binding happens; the Anthropic provider keeps it, every
  other provider supplies its own.
- `maestro/sdk.py` — `ErrorCategory` enum and
  `_classify_error`, which maps `claude_agent_sdk._errors` exception types
  (`CLIConnectionError`, `ProcessError`, …) onto the taxonomy
  resume-in-place switches on (`agent.py`). The taxonomy is provider-neutral;
  the *mapping* is per-provider and must move into each provider.
- `maestro/sdk.py` — `LONG_CONTEXT_BETA = "context-1m-2025-08-07"`
  injected as a `betas` entry. No analogous concept exists on other vendors;
  this is a capability, not a universal option.
- `maestro/sdk.py` — `TaskBudget(total=N)` and the `effort →
  max_thinking_tokens` mapping are Anthropic extended-thinking concepts.
- `maestro/{consciousness,dreaming,agent,doc_proposals,evolution,artifact_review,executor,planner,purpose}.py`
  — nine modules import `ClaudeSDKClient` (under `TYPE_CHECKING`) solely to
  type the `session` handle they receive from `create_session` and pass to
  `query_on_session` (e.g. `agent.py`, `executor.py`,
  `consciousness.py`). This is provider leakage into the loop and the
  single largest mechanical change: replace with an opaque `Session` type.
- `maestro/inproc_mcp.py` — the `@tool` decorator produces an
  `InprocTool(name, description, input_schema, handler)` where `input_schema`
  is plain JSON Schema and `handler` is `async (dict) -> {"type": "text",
  "text": ...}`. **This is already a vendor-neutral tool spec.** Only
  `create_inproc_mcp_server` (`inproc_mcp.py`) is Anthropic-specific —
  it wraps the list into an `mcp.server.Server` and emits the `{"type":
  "sdk", "name", "instance"}` wire shape the Claude SDK consumes. The MCP
  protocol is a *transport* for these tools, not a property of them.
- `maestro/tools.py`, `maestro/telegram_tools.py`,
  `maestro/email_tools.py` — every Python-defined tool server (the four
  introspection servers plus messaging, telegram, email) routes through
  `create_inproc_mcp_server`. None of their handler logic touches MCP; all of
  them are portable to any function-calling provider via the `InprocTool`
  registry. The `mcp__<server>__<tool>` name convention in
  `cli._build_agent_tools` (`cli.py`) is the only other Claude-ism.
- `maestro/config.py` — `mcp_servers_config()` builds the
  *external* MCP-protocol servers (GitHub, Fetch, Context7) from `[mcp]` TOML
  as subprocess `command=` entries. These are genuinely transport-bound:
  hosting them needs a client that speaks MCP. This is the distinction the
  capability descriptor must encode — in-process tools (portable) vs. remote
  MCP servers (MCP-speaking providers only).
- `maestro/config.py` — `check_secrets` accepts `ANTHROPIC_API_KEY` OR
  `CLAUDE_CODE_OAUTH_TOKEN`. A provider switch needs provider-specific
  credential preconditions (`OPENAI_API_KEY` for OpenAI; "`claude` logged
  in" for cli_paced per #056).
- `maestro/sdk.py` — the `configure_sdk_*` module-level globals
  (model, fallback, concurrency, pricing, stream-idle, warning throttle).
  These are configuration the *abstraction* owns; providers consume them or
  declare them unsupported.
- `tests/test_session_attachment.py` (AST guard) — bare `query()` only in
  the documented `else` branch and tests. The seam must preserve this:
  session attachment is a protocol-level guarantee, not Anthropic-specific.
- `tests/test_no_silenced_exceptions.py` (AST guard) — forbids
  warning-silencing handlers. New provider adapters (HTTP clients, retries)
  must classify-and-raise, not swallow.

## Implementation Plan

Steps are ordered so an interrupted run leaves the codebase working. Phases
1 and the early part of 2 are pure refactors with zero behaviour change
(Anthropic stays the only provider); the selection seam and the new
providers land behind a default-`anthropic` flag.

### Phase 1 — Protocol extraction (no behaviour change)

1. [x] **`Provider` protocol + `Capabilities` descriptor.** New
   `maestro/providers/base.py` — a `typing.Protocol` declaring the four
   async operations with the *current* signatures (`query`,
   `create_session`, `query_on_session`, `disconnect_session`), plus a
   frozen `Capabilities` dataclass (`sessions: bool`, `in_process_tools:
   bool`, `remote_mcp: bool`, `extended_thinking: bool`, `long_context:
   bool`, `structured_output: bool`, `usage_metering: bool`, `streaming:
   bool`). The single `mcp` flag is deliberately split into
   `in_process_tools` (can the provider host Maestro's Python tool
   handlers?) and `remote_mcp` (can it reach external MCP-protocol
   servers?). Define an opaque `Session = Any`-backed newtype (a thin
   handle) and re-export `QueryOptions` / `QueryResult` / `QueryUsage` /
   `ToolCall` / `ErrorCategory` / `SDKError` from here so providers and
   callers import the contract from one neutral module.
   - *Depends on:* none.
   - *Acceptance:* `maestro/providers/base.py` exports a `Provider` protocol
     with the four async ops, a frozen `Capabilities` dataclass with the
     eight named flags, an opaque `Session` newtype, and the six re-exported
     contract symbols importable from one module.
2. [x] **Provider-neutral tool registry.** `maestro/inproc_mcp.py` +
   `maestro/cli.py` — make the `InprocTool` list (not the MCP-wrapped
   server) the canonical surface. `_build_agent_tools` returns a registry of
   `InprocTool`s keyed by logical server name; `create_inproc_mcp_server`
   stays but is called *by the Anthropic provider's tool adapter*, not by
   `cli`. Add a `ToolAdapter` seam on the provider: given the registry,
   each provider materializes whatever its API needs (Phase 2 wires this).
   Zero behaviour change — the Anthropic adapter reproduces today's
   `{"type": "sdk", ...}` servers and `mcp__server__tool` names exactly.
   - *Depends on:* 1.
   - *Acceptance:* `_build_agent_tools` returns an `InprocTool` registry
     keyed by logical server name; `create_inproc_mcp_server` is reached
     only via the Anthropic provider's `ToolAdapter`; the `mcp__server__tool`
     names produced by the Anthropic adapter are byte-identical to today's.
3. [x] **Anthropic provider.** New `maestro/providers/anthropic.py` — move the
   `claude_agent_sdk`-specific bodies (the `ClaudeAgentOptions` build at
   `sdk.py`, `_classify_error`, the `betas`/`TaskBudget`/
   `effort` mapping, and the `create_inproc_mcp_server` tool adapter) behind
   an `AnthropicProvider` implementing the protocol, with `capabilities =
   Capabilities(sessions=True, in_process_tools=True, remote_mcp=True,
   extended_thinking=True, long_context=True, structured_output=True,
   usage_metering=True, streaming=True)`. `maestro/sdk.py` keeps the four
   public functions but delegates to the active provider (Phase 2);
   transitionally it imports `AnthropicProvider` directly so this step is a
   no-op move.
   - *Depends on:* 1, 2.
   - *Acceptance:* `AnthropicProvider` satisfies the `Provider` protocol
     structurally and a golden-dict regression pin shows the resulting
     `ClaudeAgentOptions` dict equals the pre-move build for the same
     `QueryOptions` input.
4. [x] **Opaque session handle in consumers.** The nine modules importing
   `ClaudeSDKClient` switch their `TYPE_CHECKING` import to
   `from maestro.providers.base import Session`. Mechanical; no runtime
   change. After this, no loop module names `claude_agent_sdk` (the hooks
   surface in `tool_gating.py` is a separate Anthropic API, out of scope).
   - *Depends on:* 3.
   - *Acceptance:* no module under `maestro/` outside `providers/` and
     `sdk.py`'s Anthropic-glue imports `claude_agent_sdk` for the session
     type, and only `sdk.py` imports the name `ClaudeSDKClient` (AST guard
     `tests/test_provider_session_handle.py`); the nine consumer modules type
     their `session` argument via `providers.base.Session`.

### Phase 2 — Selection seam + config

5. [x] **Provider registry + tool-adapter wiring.** New
   `maestro/providers/registry.py` — a name→factory map (`{"anthropic":
   AnthropicProvider}`) with a `get_active_provider()` resolved from config
   and a `register(name, factory)` hook. `maestro/sdk.py`'s four functions
   delegate to `get_active_provider()`; the bare-`query` AST invariant is
   preserved because the delegate is still reached only through these
   wrappers. The active provider's `ToolAdapter` (step 2) consumes the
   `InprocTool` registry — Anthropic wraps it in MCP servers; other providers
   materialize their own tool form.
   - *Depends on:* 3.
   - *Acceptance:* the four `sdk.py` public functions route through
     `get_active_provider()`; `test_session_attachment.py` recognises the
     registry delegation as session-attached and still passes.
   - *Status:* landed. `registry.py` exposes `register` / `configure_provider`
     / `current_provider_name` / `get_active_provider` (cached singleton per
     name); the four `sdk.py` public functions are now thin delegators over
     `_query_impl` / `_create_session_impl` / `_query_on_session_impl` /
     `_disconnect_session_impl` (real bodies unchanged, ~30 module globals
     co-located), each lazily importing `get_active_provider` to dodge the
     `sdk → registry → anthropic → sdk` cycle. `AnthropicProvider` retargets the
     private impls to avoid `sdk.query → provider.query → sdk.query` recursion.
     The active name stays the hardcoded `"anthropic"` default — *config
     resolution* (`[sdk] provider` + `cli.main` wiring) is deferred to Step 6,
     so `configure_provider` is not yet called from startup. The 056
     `[sdk] backend` seam and this registry coexist until Step 6 unifies them.
     Pinned by new `tests/test_provider_registry.py`; the tool-adapter wiring
     itself already landed with Step 2.
6. [x] **Config wiring.** `maestro/config.py` + `maestro/toml_config.py` +
   `maestro.toml` — add `[sdk] provider: Literal["anthropic", "openai",
   "cli_paced"] = "anthropic"` and an optional per-phase
   `[sdk] <phase>_provider` override map. Validate unknown values at load.
   If #056 has shipped, alias its `[sdk] backend` for one release with a
   deprecation log line.
   - *Depends on:* 5.
   - *Acceptance:* `[sdk] provider` validates against the registered names at
     load; unknown values raise with a remediation message; the per-phase
     override map routes only the named phases.
   - *Status:* landed. `Config` gains `sdk_provider` (validated against
     `VALID_SDK_PROVIDERS` at load), `sdk_phase_providers` (validated against
     `SDK_PHASE_NAMES` + the provider names), `resolved_sdk_provider()` (applies
     the deprecated `[sdk] backend` → provider alias with a one-time warning),
     and `provider_for_phase()`. `cli.main` calls `configure_provider(
     config.resolved_sdk_provider())`. The `[sdk] <phase>_provider` keys parse
     into `sdk_phase_providers` via `toml_config`. Pinned by `test_config.py`,
     `test_cli.py`, and `tests/component/test_config_integration.py`.
7. [x] **Capability validation at startup.** `maestro/cli.py` (or `_build_agent`)
   — for each phase, assert the resolved provider's `capabilities` satisfy
   what it needs: `in_process_tools` for any introspection-tool phase,
   `remote_mcp` only for phases that consume the external GitHub/Fetch/
   Context7 servers, `long_context` for `_self_review` (and optionally
   Session C / Executor when enabled). Raise a clear remediation error at
   config-load time, never at first query.
   - *Depends on:* 6.
   - *Acceptance:* starting up with a `remote_mcp=False` provider routed to
     a phase that consumes the external MCP servers (or `long_context=False`
     to a long-context phase) raises a remediation error at config-load
     time, before any query.
   - *Status:* landed. New `maestro/providers/capability_gate.py` exposes
     `required_capabilities_for_phase(phase, config)` (config-aware: `remote_mcp`
     only when an external MCP server is enabled, `long_context` only when
     `sdk_model_long_context` is set for a phase that uses it) and
     `validate_provider_capabilities(config)`, which `cli.main` calls right after
     `configure_provider`. `registry.get_provider(name)` fetches an arbitrary
     registered provider's capabilities (fail-loud on unknown per-phase names).
     Inert under the default `anthropic` (every capability `True`). Pinned by
     new `tests/test_capability_gating.py`.
8. [x] **Provider-aware `check_secrets`.** `maestro/config.py` — dispatch on
   `[sdk] provider`: `anthropic` keeps the existing OR check; `openai`
   requires `OPENAI_API_KEY`; `cli_paced` requires "`claude` on PATH and
   logged in" (per #056). Fail loud with the per-provider remediation.
   - *Depends on:* 6.
   - *Acceptance:* `check_secrets` dispatches on `[sdk] provider`; each
     provider's missing precondition raises its own documented remediation
     message in `test_check_secrets.py`.
   - *Status:* landed. `check_secrets` gains a keyword-only `provider`
     argument (default `"anthropic"`) and delegates the credential
     precondition to the new `Config._check_provider_credential(provider)`
     helper: `anthropic` keeps the `check_auth` OR-check + prefix validation
     verbatim; `openai` requires `OPENAI_API_KEY` (warns on a non-`sk-`
     prefix); `cli_paced` requires `claude` on PATH (`shutil.which`); an
     unknown name fails loud against `VALID_SDK_PROVIDERS`. The GitHub-token
     requirement and the Telegram/dashboard/publication checks stay
     provider-neutral. `cli.main`'s two call sites pass
     `provider=config.resolved_sdk_provider()`; under the default `anthropic`
     behaviour is unchanged. The full `claude login` dry-probe (vs. bare PATH
     presence) defers to #056 Step 13 when the `cli_paced` provider lands.
     Pinned by new provider-dispatch cases in `tests/test_config.py`.

### Phase 3 — OpenAI reference provider

9. [x] **OpenAI provider.** New `maestro/providers/openai.py` — `OpenAIProvider`
   over the official `openai` SDK (Responses API). Map `QueryOptions` →
   request (`effort` → `reasoning.effort`; `model`/`fallback_model`;
   `output_format` → structured outputs; `max_turns` enforced in the adapter
   loop). Map response `usage` → `QueryUsage` (token fields populate;
   `total_cost_usd` via the existing `_estimate_cost_usd` pricing table).
   Capabilities: `in_process_tools=True` (via step 10's adapter),
   `remote_mcp=False` (v1; OpenAI's hosted-MCP tool type is a follow-up),
   `long_context=False`, `extended_thinking=False` (reasoning models expose
   effort, not a token budget), `usage_metering=True`.
   - *Depends on:* 5.
   - *Acceptance:* `OpenAIProvider` satisfies the `Provider` protocol
     structurally, and per-field `QueryOptions → request` mapping (including
     `effort → reasoning.effort`) plus `response.usage → QueryUsage` are
     covered by parametrized tests against a mocked `openai` client.
   - *Status:* landed. `maestro/providers/openai.py` lands the one-shot query
     path: `_build_request` maps `prompt → input`, `system → instructions`,
     `model`, `effort → reasoning.effort`, `output_format → text.format` (tool /
     beta / budget fields dropped — they ride Step 10 or have no Responses API
     analogue); `_usage_from_response` maps token counts (incl.
     `input_tokens_details.cached_tokens → cache_read_tokens`) and estimates cost
     via the existing `sdk._estimate_cost_usd` pricing table (`estimated=True`);
     `_result_from_response` carries `output_text`, `output_parsed`, and
     `response.id → session_id`. `openai` is an optional dep imported lazily in
     `_default_client`; tests inject a mocked client. Capabilities are honest for
     this slice (`sessions=False`/`streaming=False` until Steps 11/follow-up;
     `in_process_tools=True` declared ahead of the Step 10 adapter). The three
     session ops fail loud (`NotImplementedError`, deferred to Step 11) and the
     provider is **not** registered in `registry.py` yet, so selection still
     fails loud (the Step 6 transitional posture holds). Pinned by new
     `tests/test_provider_openai.py`.
10. [x] **OpenAI tool adapter.** Same file — implement the `ToolAdapter` (step 2)
    for OpenAI: map each `InprocTool` to a function-tool spec
    (`{"type": "function", "name": t.name, "parameters": t.input_schema}` —
    the JSON Schema maps 1:1) and run a tool-call dispatch loop that, on each
    `tool_call`, awaits `registry[name].handler(arguments)` and feeds the
    `{"type": "text", "text": ...}` return back as the tool-result message.
    No handler logic in the seven tool servers changes. This makes the four
    introspection servers (journal, self-awareness, memory, issues) plus
    messaging/email/telegram run natively on OpenAI — only the external
    GitHub/Fetch/Context7 servers fall back to an `anthropic` phase via the
    `remote_mcp` gate.
    - *Depends on:* 9.
    - *Acceptance:* an `InprocTool` round-trips through the OpenAI adapter —
      the emitted function-tool spec's `parameters` equals `t.input_schema`
      and the dispatch loop calls the unchanged handler with the decoded
      arguments and feeds the `{"type":"text","text":...}` return back as
      a tool-result message.
    - *Status:* landed. New `maestro/providers/openai_tool_adapter.py`
      (`OpenAIToolAdapter`, paralleling the Step 2 `anthropic_tool_adapter.py`
      rather than living inside `openai.py`, to match the established
      one-file-per-adapter convention). `materialize` emits flat Responses API
      function-tool specs `{"type": "function", "name", "description",
      "parameters"}` keyed by a collision-free qualified `<server>__<tool>`
      name (OpenAI's function namespace is flat, unlike Anthropic's
      `mcp__<server>__<tool>`), with `parameters` the tool's `input_schema`
      verbatim; `handler_map` keys the unchanged handlers by the same name;
      `dispatch` decodes one `function_call` item's JSON arguments, awaits the
      handler, and packs the `{"type": "text", "text": ...}` return into the
      `function_call_output` message OpenAI consumes next turn (unknown tool
      fails loud); `run_tool_calls` is the per-turn loop over `response.output`.
      The adapter is stateless, never imports `openai`, and is not yet wired
      into `OpenAIProvider.query` (the provider stays unregistered until Step 11
      lands sessions) — a standalone test-pinned seam exactly like the Anthropic
      adapter. Pinned by new OpenAI cases in `tests/test_tool_adapter.py`.
11. [x] **OpenAI sessions + errors.** Same file — model `create_session` /
    `query_on_session` via OpenAI conversation state (`previous_response_id`)
    or, where unsupported, a local transcript-replay shim that re-sends prior
    turns; `QueryResult.session_id` carries whichever continuity token the
    provider uses. Map `openai` exceptions
    (`RateLimitError`/`APITimeoutError`/`AuthenticationError`/`APIConnectionError`)
    onto `ErrorCategory` so resume-in-place keeps working unchanged.
    - *Depends on:* 9.
    - *Acceptance:* `create_session`/`query_on_session` round-trips
      continuity via `previous_response_id` (or transcript replay) in test,
      and each mocked `openai` exception class maps to the expected
      `ErrorCategory` in a parametrized table.
    - *Status:* landed. `_OpenAISession` (a dataclass holding the baked
      `options` / `instructions` and a rolling `previous_response_id`) is the
      opaque `Session`. The Responses API exposes native continuity via
      `previous_response_id`, so **no transcript-replay shim is needed**:
      `create_session` seeds the id from `resume_session_id` (and sends
      `initial_prompt` as a first turn only when one is given — Maestro's usual
      call passes `None`), `query_on_session._turn` threads the id and advances
      it from `response.id`, and `disconnect_session` is a clean no-op (server-side
      stored state, no live connection). `_classify_openai_error` maps `openai`
      exception *class names* (walking the MRO, so it works without the optional
      `openai` package installed — `APITimeoutError` wins over its
      `APIConnectionError` base) onto `ErrorCategory`, with an HTTP-`status_code`
      fallback for unmapped status errors; `_create_response` wraps recognised
      failures into `SDKError(category=…)` on **both** the one-shot `query` and
      the session path, and re-raises non-`openai` exceptions unwrapped.
      `capabilities.sessions` flips to `True`. The provider stays unregistered —
      registration waits on wiring the Step 10 tool adapter into `query` (see
      *What is missing*). Pinned by new `TestSessions` / `TestErrorMapping`
      cases in `tests/test_provider_openai.py`.

### Phase 4 — Custom (cli_paced) provider

12. [x] **Adapt `maestro_sdk` to the protocol.** New
    `maestro/providers/cli_paced.py` — wrap `maestro_sdk.AsyncClient` (from
    #056) as a `Provider`, translating `MaestroSDKError → SDKError(category=…)`
    and setting `usage = None` honestly where the CLI cannot measure.
    Capabilities: `in_process_tools=False` (a separate CLI process cannot
    host Maestro's Python handlers — this is the one genuinely transport-bound
    case), `remote_mcp=False` (the CLI uses its own `~/.claude.json`),
    `long_context=False`, `usage_metering=False`. Register under
    `"cli_paced"`. This replaces 056's standalone `[sdk] backend` seam with
    the unified registry.
    - *Depends on:* 5.
    - *Acceptance:* `CliPacedProvider` registers under `"cli_paced"`; the
      `MaestroSDKError → SDKError(category)` table is exhaustive in test;
      a query returning `usage = None` propagates through `QueryResult`
      without zero-fill.
    - *Status:* landed. New `maestro/providers/cli_paced.py::CliPacedProvider`
      owns the four ops directly, importing `maestro_sdk` and re-packing its
      types into `maestro.sdk` shapes — the #056 adapter bodies, moved verbatim
      when the `maestro/sdk_backends/` package was retired.
      `capabilities` declares every flag `False` — the most capability-limited
      provider, each flag an honest statement of what a separate human-paced
      `claude` CLI process cannot do (`in_process_tools` is the one genuinely
      transport-bound gap). `registry.py` registers it under `"cli_paced"`
      behind a *lazy* `_cli_paced_factory`, so `maestro_sdk` loads only when an
      operator selects or per-phase-routes `cli_paced`; the default `anthropic`
      path never imports it. `MaestroSDKError → SDKError(category)` translation
      and the fail-loud rejection of unsupported option fields live in the
      provider itself (test-pinned). The honest end-to-end `usage = None`
      propagation (vs. this slice's zero-count `QueryUsage` with
      `total_cost_usd=None`, the #056 precedent) landed in Step 13 as the
      `unmetered=True` flag. Pinned by new
      `tests/test_provider_cli_paced.py`. Merged in PR #2352.

### Phase 5 — Honest degradation + docs

13. [x] **Usage-metering honesty.** `maestro/cost_tracking.py` + the burn-rate
    gate — ensure a provider with `usage_metering=False` emits
    `IterationUsage` rows with explicit `None` (already supported by the
    dataclass) that contribute neither cost nor revenue and never divide the
    throttle by `None`; the dashboard renders missing counts as "—".
    - *Depends on:* 5.
    - *Acceptance:* an `IterationUsage` row with `usage = None` round-trips
      through `cost_tracking` contributing zero to the aggregate, the
      burn-rate gate skips it cleanly, and the dashboard renders the missing
      counts as `—`.
    - *Status:* landed. `QueryUsage` and `IterationUsage` carry an `unmetered`
      flag (`maestro/sdk.py`); `_log_sdk_call` coerces an unmetered call's
      `total_cost_usd=None` to `0.0` and sets the flag, and
      `maestro/cost_tracking.py::_build_cost_rates_uncached` drops unmetered
      samples from the rolling-window rate aggregation so the burn-rate gate
      never reads the zeros as spend; the dashboard's `formatCost` renders an
      unmetered value as `—`. Pinned by `tests/test_sdk_usage.py` (SDK-side
      `unmetered` flag) and `tests/test_cost_tracking.py` (the
      `_build_cost_rates_uncached` burn-rate exclusion).
14. [x] **Documentation.** `docs/sdk-internals.md` gains a "Provider abstraction"
    section (protocol, capability matrix, the in-process-tools vs remote-MCP
    split, selection, degradation); `docs/deployment.md` documents
    per-provider credentials and capability-gating; `CLAUDE.md` gets one new
    line under the SDK invariants pointing here, and the "MCP servers" /
    long-context invariants note they are Anthropic-provider capabilities
    (with in-process tools portable across providers).
    - *Depends on:* 12, 13.
    - *Acceptance:* `docs/sdk-internals.md` contains a "Provider abstraction"
      section with the capability matrix; `docs/deployment.md` lists
      per-provider credential requirements; `CLAUDE.md` carries one new
      invariant line pointing at the new doc section.
    - *Status:* landed. `docs/sdk-internals.md` gained the "Provider abstraction"
      section (protocol, capability matrix, in-process-tools vs remote-MCP split,
      selection, degradation); `docs/deployment.md` gained the "Per-provider
      credentials" table + the capability-gating note; `CLAUDE.md` carries the
      "SDK provider registry" / "Provider capability gate" invariant lines
      pointing at `docs/sdk-internals.md#provider-abstraction`. Merged in PR #2385.

## Testing Strategy

Unit tests honour `mem_fs` + no-real-I/O + no-real-network + no-real-`sleep`;
externals (OpenAI client, `maestro_sdk` runner) are mocked.

- `tests/test_providers_base.py` — `Capabilities` is frozen; the `Provider`
  protocol is satisfied by each concrete provider (structural check); the
  re-exported contract types are importable from `providers.base`.
- `tests/test_provider_registry.py` — `provider = "anthropic"` resolves to
  `AnthropicProvider`; `"openai"` / `"cli_paced"` resolve correctly; unknown
  name raises; per-phase override map routes the named phase only.
- `tests/test_provider_anthropic.py` — the moved bodies still build
  `ClaudeAgentOptions` identically (golden-dict assertion) and
  `_classify_error` maps the same exception classes to the same categories
  as before the move (regression pin).
- `tests/test_provider_openai.py` — `QueryOptions → request` mapping per
  field; `usage → QueryUsage`; each mocked `openai` exception maps to the
  expected `ErrorCategory` (one parametrized case per category); session
  continuity round-trips `previous_response_id`; `effort` → `reasoning.effort`.
- `tests/test_tool_adapter.py` — the `InprocTool` registry round-trips
  through both adapters: the Anthropic adapter reproduces today's `{"type":
  "sdk", ...}` server shape and `mcp__server__tool` names byte-for-byte; the
  OpenAI adapter emits a function-tool spec whose `parameters` equals each
  tool's `input_schema` and whose dispatch loop calls the unchanged handler
  and feeds the `{"type": "text", "text"}` return back as a tool result.
- `tests/test_provider_cli_paced.py` — `MaestroSDKError → SDKError(category)`
  table is exhaustive; `usage = None` propagates without zero-fill.
- `tests/test_capability_gating.py` — startup validation raises with a
  remediation message when a `remote_mcp`-requiring phase (GitHub/Fetch/
  Context7) is pointed at a `remote_mcp=False` provider, and when a
  long-context phase is pointed at a `long_context=False` provider; an
  introspection-tool phase on an `in_process_tools=True` provider passes.
- `tests/test_check_secrets.py` — extended: `openai` requires `OPENAI_API_KEY`;
  `cli_paced` accepts "`claude` logged in"; each missing precondition raises
  the documented remediation.
- `tests/test_session_attachment.py` — extend the AST guard so it recognizes
  the `providers/registry.py` delegation as session-attached, and that the
  nine consumer modules no longer import `claude_agent_sdk`.
- `tests/test_iteration_usage.py` — `None`-count rows round-trip and the
  `cost_tracking` aggregator + burn-rate gate skip them cleanly.

## Risk Assessment

**What could regress.**

- **Refactor blast radius.** Phase 1–3 touch the single most-depended-on
  module. Mitigation: Phase 1/2 are behaviour-preserving moves with golden
  regression pins (`test_provider_anthropic.py`); the default stays
  `anthropic`, so production behaviour is unchanged until an operator opts in.
- **Session-semantics drift.** Vendors model conversation state differently;
  a naive transcript-replay shim could resend stale context or lose
  continuity. Mitigation: continuity tokens are provider-opaque and tested
  per provider; the resume-in-place taxonomy is preserved by mapping each
  vendor's errors onto the existing `ErrorCategory`.
- **Capability gaps shipping silently.** A provider lacking a needed
  capability could degrade a phase invisibly. Mitigation: capabilities are
  explicit (with `in_process_tools` and `remote_mcp` split so the portable
  introspection tools aren't conflated with the external MCP servers) and
  validated at startup (step 7), failing loud rather than at first query —
  the same fail-loud doctrine as the rest of the loop.
- **Observability under non-metering providers.** `usage = None` must not be
  read as zero. Mitigation: step 13's dedicated tests + dashboard "—"
  rendering; default provider meters fully.
- **Trust chain.** Tool execution and approvals differ per vendor. The OpenAI
  adapter runs the *same* in-process handlers as Anthropic (no new tool
  logic, no new trust surface — only a different transport), and cli_paced
  inherits 056's deny-by-default approval posture; no auto-approval crosses
  providers.

**Rollback posture.** Default `[sdk] provider = "anthropic"` makes the
whole track inert. Reverting is setting the flag back, or reverting the
registry-delegation PR (Phase 2) — Phase 1's moves are behaviour-preserving
and safe to leave. Each later provider is additive behind the flag.

**Data migration.** None for the contract. `QueryResult.session_id` gains a
provider-neutral meaning but the field shape is unchanged; `IterationUsage`'s
`None`-tolerant fields already exist. If #056's `[sdk] backend` shipped, a
one-release config alias migrates it to `[sdk] provider`.

## Out of scope

- Reaching the *external* MCP-protocol servers (GitHub, Fetch, Context7) from
  non-Claude providers — e.g. via OpenAI's hosted-MCP tool type. v1 declares
  `remote_mcp=False` for OpenAI; the in-process introspection tools are
  already portable via the registry adapter (Phase 3), so this only defers
  the three external servers.
- Long-context exposure on providers that lack an equivalent beta/flag.
- A cross-provider per-tool auto-approval policy (lands with #056's
  approval-policy follow-up so the trust chain stays explicit).
- Removing `maestro/sdk.py` or the Anthropic provider — it remains the
  default indefinitely; this proposal adds *alternatives*, not a replacement.
