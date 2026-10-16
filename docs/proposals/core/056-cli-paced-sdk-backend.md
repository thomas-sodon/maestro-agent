# 056: CLI-Paced SDK Provider Under the 058 Registry

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped (all 15 steps landed: `maestro_sdk` slices 1–9; the cli_paced **provider** re-home that retired the `[sdk] backend` seam (Step 11, PR #2358); `[sdk.cli_paced]` Cadence + stream-idle config (Step 12); the `claude auth status` login dry-probe in `check_secrets` (Step 13); `usage_metering=False` honesty via `unmetered=True` (Step 14); and the cli_paced provider documentation refresh (Step 15, PR #2386)) |
| **Affects** | `maestro_sdk/src/maestro_sdk/*`, `maestro_sdk/tests/*`, new `maestro/providers/cli_paced.py` (registered via `maestro/providers/registry.py`), retire `maestro/sdk_backends/` + the `[sdk] backend` field, `maestro/config.py` / `maestro/toml_config.py` / `maestro.toml` (`[sdk.cli_paced]` sub-section + `claude login` probe), `docs/sdk-internals.md`, `docs/deployment.md`, `CLAUDE.md`, tests |
| **Relates to** | 058 _(provides the `Provider` registry, `[sdk] provider` config seam, startup capability gate, and provider-aware `check_secrets` this proposal plugs into; cli_paced is 058's Phase 4 provider — see Relationship to #058)_ |

## Description

> Status updated to shipped on 2026-06-02 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

`maestro_sdk/` was scaffolded in commit `10fa5ee5` as a standalone package that
drives the `claude` CLI like a human user (paced keystrokes, jittered pty
winsize, post-stream heartbeat). The foundation layer — `Cadence`, errors,
data contracts, logging shim — has landed. The package's stated purpose is
to surface TUI/UX issues that bulk-write SDKs hide, support automation on
OAuth-only CLI installs with no `ANTHROPIC_API_KEY`, and exercise rhythms
that downstream rate-limiters and UI state machines assume between
keystrokes. The deferred slices are pty runner, screen emulator, parser,
approvals, and the public `AsyncClient`.

This proposal enumerates what is missing before `maestro_sdk` can stand
beside the Anthropic provider as a **selectable provider** under #058's
registry — not just functionally ("query returns a `QueryResult`") but in
**contract parity** with the existing wrapper, which the rest of Maestro
depends on heavily: session attachment is AST-enforced, error
categorization drives resume-in-place, `IterationUsage` flushes to
`usage.jsonl` per iteration, MCP servers are injected per call, and
long-context betas are conditionally enabled.

End state once this lands: `[sdk] provider = "cli_paced"` (default
`"anthropic"`) selects the CLI-paced provider via #058's registry, with
optional per-phase `[sdk] <phase>_provider` overrides. The provider
implements the `Provider` protocol (`query`, `create_session`,
`query_on_session`, `disconnect_session`), honours the same `ErrorCategory`
taxonomy, and declares honest `Capabilities`: `usage_metering=False`
(`None`s where the CLI cannot measure), `in_process_tools=False`,
`remote_mcp=False`, `long_context=False`, `structured_output=False`. #058's
startup capability gate then keeps it off the phases it cannot serve. The
shipping shape is per-phase opt-in rather than a global swap — observability
regresses under the CLI path, so the loop's heaviest LLM phases keep the
Anthropic provider by default.

## Relationship to #058

Proposal **058** (Provider-Agnostic SDK Layer) generalized 056's narrow
`[sdk] backend = "agent" | "cli_paced"` switch into an open-ended `[sdk]
provider` registry, and that generalization has now **landed**: 058's
Phases 1–2 are complete (the `Provider` protocol + `Capabilities` descriptor
in `maestro/providers/base.py`, the registry in `maestro/providers/registry.py`,
the `[sdk] provider` config + per-phase override map + `resolved_sdk_provider`
/ `provider_for_phase` resolvers, the startup capability gate in
`maestro/providers/capability_gate.py`, and provider-aware `check_secrets`
dispatch), and Phase 3 has landed the OpenAI provider's query path + tool
adapter. The four public functions in `maestro/sdk.py` now delegate through
`get_active_provider()`.

This re-frames 056. Two facts drive the change:

1. **The standalone `[sdk] backend` seam is superseded and inert.** Step 10
   landed `maestro/sdk_backends/{__init__,cli_paced}.py` plus the
   `configure_backend` global, and `cli.main` still calls it — but `sdk.py`
   routes through the **provider registry**, not `sdk_backends`. Nothing in
   production reads the `[sdk] backend` selection, so `[sdk] backend =
   "cli_paced"` is a dead switch today. 058 Step 6 already aliases the
   deprecated `[sdk] backend` onto `[sdk] provider` (`agent → anthropic`,
   `cli_paced → cli_paced`) via `resolved_sdk_provider()`.
2. **cli_paced now lands as a third concrete provider** — 058's Phase 4 /
   Step 12 — registered under `"cli_paced"` alongside `anthropic` and
   `openai`. The adapter logic that already lives in
   `maestro/sdk_backends/cli_paced.py` (type re-pack, `MaestroSDKError →
   SDKError` translation, fail-loud on unsupported options) moves verbatim
   into a `CliPacedProvider` in `maestro/providers/cli_paced.py`; the
   `sdk_backends` package and the `[sdk] backend` field are then retired.

058 owns the **infrastructure** (registry, config schema, capability gate,
`check_secrets` dispatch — all landed). 056 owns **building `maestro_sdk`**
(slices 1–9, done) and the **cli_paced provider implementation itself** (the
re-home, its `Capabilities`, the `[sdk.cli_paced]` Cadence sub-section, the
`claude login` dry-probe, and the cli_paced-specific docs). 058 Step 12 and
056's re-home step describe the same `maestro/providers/cli_paced.py`
deliverable; this proposal is its home and 058 Step 12 points here.

## Relevant Findings

- `maestro_sdk/src/maestro_sdk/__init__.py` — current export surface is
  `Cadence`, `InstantCadence`, the seven error classes, and the six data
  contracts; no `AsyncClient`, no session APIs.
- `maestro_sdk/README.md` — "Status: Early. The foundation layer …
  ships first; pty plumbing, screen emulation, parser, approvals, and the
  public client follow in subsequent slices."
- `maestro_sdk/src/maestro_sdk/cadence.py` — `Cadence` is the only
  timing surface; every wait in the package must route through
  `delay_for` / `heartbeat_for` / `winsize`. Frozen post-construction;
  `InstantCadence` zeros all delays for tests.
- `maestro_sdk/src/maestro_sdk/types.py` — `QueryOptions`,
  `QueryResult`, `QueryUsage`, `QueryTiming`, `StreamDelta`, `ToolCall`.
  Shapes mirror `maestro/sdk.py` so a future adapter is a re-pack.
- `maestro_sdk/src/maestro_sdk/types.py` — `QueryUsage` requires all
  four counts; constructing one is "an assertion that you measured
  something." The CLI path will therefore set `usage = None` when the CLI
  does not surface counts, not zero-fill.
- `maestro/providers/cli_paced.py` — the **CLI adapter** (the file the
  original `maestro/sdk_backends/cli_paced.py` was re-homed into by Step 11).
  It already re-packs `maestro_sdk.QueryResult →
  maestro.sdk.QueryResult`, translates `MaestroSDKError → SDKError(category=…)`,
  fails loud on unsupported options (MCP, betas, `output_format`, tool
  policy, `max_turns`, in-stream `complete_on`, resume, connect-time prompt),
  and emits the once-per-handle transcript-continuity WARNING. The re-home
  wraps these four functions as a `CliPacedProvider`'s methods; the bodies
  move verbatim.
- `maestro/sdk_backends/__init__.py` <!-- TODO: retired in Step 11; this path and `configure_backend` / `current_backend` / `config.sdk_backend` no longer exist --> — the superseded `[sdk] backend`
  seam (`configure_backend` / `current_backend`). `cli.main` still calls
  `configure_backend(config.sdk_backend)`, but `sdk.py` delegates to the
  **provider registry**, not this package — so the selection is inert. The
  re-home retires this package.
- `maestro/providers/registry.py` — `register(name, factory)` is the
  hook the re-homed provider uses: `register("cli_paced", CliPacedProvider)`
  makes `[sdk] provider = "cli_paced"` resolve. `get_provider` already
  fail-loud-rejects unknown names, so until the provider registers, selecting
  it raises at startup (the same transitional posture as `openai`).
- `maestro/providers/base.py` — the frozen `Capabilities` descriptor
  the provider must declare. cli_paced is the one genuinely
  `in_process_tools=False` case: a separate `claude` process cannot host
  Maestro's Python tool handlers. `remote_mcp=False` (the CLI uses its own
  `~/.claude.json`), `long_context=False`, `usage_metering=False`,
  `structured_output=False`.
- `maestro/providers/capability_gate.py` — `required_capabilities_for_phase`
  already gates `in_process_tools` / `remote_mcp` for `implementation` /
  `run_task` and `long_context` when configured. Routing cli_paced to one of
  those phases raises at `cli.main` time — no new gating logic needed, the
  provider just declares honest capabilities.
- `maestro/sdk.py` (four public functions, now thin delegators) — `query`,
  `create_session`, `query_on_session`, `disconnect_session` route through
  `get_active_provider()` (058 Step 5). The session triplet now traffics in
  the opaque `providers.base.Session` handle. Per CLAUDE.md, main-phase calls
  must be session-attached; the AST guard in `tests/test_session_attachment.py`
  recognizes the registry delegation.
- `maestro.providers.base` re-exports `ErrorCategory` / `QueryUsage` /
  `IterationUsage`'s contract from `maestro/sdk.py`. `ErrorCategory`
  (`RATE_LIMIT`, `STALE_SESSION`, `CLI_CRASH`, `STREAM_STALL`, `AUTH`,
  `TIMEOUT`, `NETWORK`, `UNKNOWN`) is provider-neutral; the per-provider
  *mapping* lives in each provider. The cli_paced provider keeps the
  string-`category`-by-name translation already in the adapter.
- `maestro/sdk.py` — `LONG_CONTEXT_BETA = "context-1m-2025-08-07"` has no CLI
  analogue, so `long_context=False`; `IterationUsage`'s `None`-tolerant
  fields back the `usage_metering=False` honesty (058 Phase 5).
- `maestro/config.py` — `check_secrets` already dispatches on `[sdk]
  provider` (058 Step 8); the `cli_paced` branch requires `claude` on PATH
  via `shutil.which`. 058 Step 8 explicitly **deferred the full `claude
  login` dry-probe to this proposal** (056 Step 13) — that upgrade is the
  remaining credential work.
- `maestro/config.py` <!-- TODO: VALID_SDK_BACKENDS retired in Step 11; only VALID_SDK_PROVIDERS remains --> — `VALID_SDK_BACKENDS = {"agent", "cli_paced"}`
  (deprecated) and `VALID_SDK_PROVIDERS = {"anthropic", "openai",
  "cli_paced"}`. Retiring `[sdk] backend` removes the former; the latter
  already accepts `"cli_paced"` at config-schema level (it fails loud in
  `configure_provider` until the provider registers).
- `tests/test_no_silenced_exceptions.py` (AST guard) — forbids
  warning-silencing exception handlers outside the allowlist. The CLI
  pty plumbing (subprocess + pty fds) is exactly the kind of place
  where a tempting `except Exception: pass` would slip in.
- `docs/proposals/technical-plan/055-sdk-orphan-elimination.md` — open
  proposal addressing orphan-task drainage at the agent-SDK boundary.
  The CLI provider has its own cancel-containment surface (child process
  + pty fds + reader task) and must not regress the property that
  cancellation does not leak into later awaits.

## Implementation Plan

Steps are ordered so an interrupted run leaves the codebase working: each
slice ships behind the existing scaffold (no production caller switches
providers) and registration lands last. Steps 1–9 are confined to
`maestro_sdk/` and are **done**. Step 10 (the standalone `[sdk] backend`
seam) landed and is now **superseded** by #058's registry. Steps 11 onward —
the provider re-home and the cli_paced-specific config/credential/doc work —
plug into #058's already-landed infrastructure (registry, config schema,
capability gate, `check_secrets` dispatch) rather than re-building it.

1. [x] **Pty runner.** `maestro_sdk/src/maestro_sdk/pty_runner.py` (new) —
   spawn `claude` in a pty with `Cadence.winsize()`; expose async
   `send_char()` that awaits `Cadence.delay_for(...)` and `aread_chunk()`
   that yields raw bytes. Cancel discipline: on cancel, close the master
   fd, send `SIGTERM`/`SIGKILL` ladder to the child, await the reader task
   to completion. No `except Exception: pass`.
   - *Depends on:* none.
   - *Acceptance:* an `InstantCadence`-driven pty runner test sends N
     characters in order, and a cancel during read closes the master fd,
     reaps the child, and awaits the reader task without leaking fds or
     orphan tasks.
2. [x] **Screen emulator.** `maestro_sdk/src/maestro_sdk/screen.py` (new) —
   vt100 subset (cursor, ED/EL, SGR, scroll region) sufficient to
   disambiguate incremental streaming from re-renders. Pure function over
   bytes → screen state; no I/O. Reuse an existing minimal emulator (e.g.
   `pyte`) only if license and dependency footprint are acceptable;
   otherwise vendor the small subset.
   - *Depends on:* none.
   - *Acceptance:* table tests of byte streams → expected screen state pass
     for cursor moves, ED/EL clears, SGR ignores, and scroll-region writes,
     with the module performing zero I/O.
3. [x] **Output parser.** `maestro_sdk/src/maestro_sdk/parser.py` (new) —
   given a screen snapshot stream, emit
   `StreamDelta(text, kind, screen_snapshot)` and accumulate the final
   `response` plus any `ToolCall`s. Kind is `"thinking"` for `claude`'s
   reasoning region and `"answer"` for the final answer region.
   - *Depends on:* 2.
   - *Acceptance:* a golden screen-snapshot sequence parses into the
     expected `StreamDelta` list, accumulated `response` text, and
     `ToolCall` list, with `kind` correctly assigned per region.
4. [x] **Approval handler.** `maestro_sdk/src/maestro_sdk/approvals.py`
   (new) — detect the CLI's tool-approval prompts in screen state; if a
   caller has not supplied an `auto_approve` predicate, raise
   `ApprovalRequiredError` (already defined in `errors.py`). Default
   posture is deny-by-raise; "approve everything" is opt-in per session.
   - *Depends on:* 2.
   - *Acceptance:* a synthetic approval-prompt screen with no
     `auto_approve` raises `ApprovalRequiredError`; with a permissive
     predicate returns the typed approval response.
5. [x] **Public `AsyncClient`.** `maestro_sdk/src/maestro_sdk/client.py`
   (new) — `AsyncClient(cadence, options)` with
   `query(prompt) -> QueryResult`, `create_session() -> Session`,
   `Session.query(prompt) -> QueryResult`, `Session.aclose()`. `query()`
   composes runner + screen + parser + approvals +
   `Cadence.heartbeat_for(answer_chars)`.
   - *Depends on:* 1, 2, 3, 4.
   - *Acceptance:* with `InstantCadence` and a fake runner, `AsyncClient`
     drives a happy-path query to a `QueryResult` and supports
     session-create / `Session.query` / `Session.aclose` round-trips
     without leaking the underlying runner.
6. [x] **Stream-idle and timeout safety nets.**
   `maestro_sdk/src/maestro_sdk/client.py` — wrap parser reads in
   `asyncio.wait_for(..., options.stream_idle_timeout_seconds)` and overall
   startup in `options.startup_timeout_seconds`; on either timeout, kill
   the child and raise `CLITimeoutError`.
   - *Depends on:* 5.
   - *Acceptance:* a fake runner that stalls past `stream_idle_timeout_seconds`
     causes `query()` to raise `CLITimeoutError` and reap the child; a
     stall during startup raises the same error tagged as startup.
7. [x] **`ErrorCategory` mapping.** `maestro_sdk/src/maestro_sdk/errors.py`
   — add `category` field to `MaestroSDKError` mirroring
   `maestro.sdk.ErrorCategory` *by name* (string), not by import (to keep
   `maestro_sdk` standalone). Map: `RateLimitError → "rate_limit"`,
   `SessionExpiredError → "stale_session"`,
   `CLIExitedError → "cli_crash"`,
   `CLITimeoutError → "stream_stall"` (post-startup) or `"timeout"`
   (during startup), `CLINotFoundError → "auth"`.
   - *Depends on:* none.
   - *Acceptance:* every error subclass exposes a non-empty `category`
     string, and a parametrized test asserts the documented
     subclass→category mapping is exhaustive.
8. [x] **`QueryTiming` and best-effort `QueryUsage`.**
   `maestro_sdk/src/maestro_sdk/client.py` — populate
   `QueryTiming(send_ms, stream_ms, heartbeat_ms)` from monotonic clocks;
   set `QueryUsage = None` when the CLI does not surface counts. Do not
   zero-fill.
   - *Depends on:* 5.
   - *Acceptance:* `QueryResult.timing` is populated on every successful
     `query()`; `QueryResult.usage` is `None` when the CLI does not surface
     counts, and no field is ever zero-filled as a stand-in for "unknown."
9. [x] **MCP forwarding decision.** `maestro_sdk/README.md` and
   `maestro_sdk/src/maestro_sdk/client.py` — document the decision and
   raise `NotImplementedError("CLI-paced backend does not forward MCP
   server config; configure the CLI via its own settings")` if MCP servers
   are passed in `QueryOptions`. The `claude` CLI honours its own
   `~/.claude.json`; bridging Python-built MCP servers is a separate
   proposal.
   - *Depends on:* 5.
   - *Acceptance:* passing a non-empty `mcp_servers` in `QueryOptions`
     raises `NotImplementedError` with the documented message; the README
     calls out the constraint.
10. [x] **(Superseded) Standalone `[sdk] backend` seam.**
    `maestro/sdk_backends/{__init__,cli_paced.py}` landed: a
    `configure_backend` global selecting between the agent SDK and the CLI
    adapter, and the adapter itself (type re-pack, `MaestroSDKError →
    SDKError(category=…)`, fail-loud on unsupported options). This shipped
    before #058's registry and is now **superseded** — `sdk.py` delegates to
    `get_active_provider()`, not this seam, so `[sdk] backend = "cli_paced"`
    is a dead switch in production. Step 11 re-homes the adapter as a provider
    and retires this package. The adapter *bodies* are reused verbatim, so no
    work is thrown away.
    - *Status:* landed, superseded. Retained only until Step 11 deletes it.
11. [x] **Re-home the CLI adapter as a provider.** New
    `maestro/providers/cli_paced.py` — a `CliPacedProvider` implementing the
    `Provider` protocol (`maestro/providers/base.py`), wrapping
    `maestro_sdk.AsyncClient`. Move the four function bodies from
    `maestro/sdk_backends/cli_paced.py` onto the provider's methods unchanged
    (re-pack, error translation, fail-loud-on-unsupported, the
    transcript-continuity WARNING). Declare honest `Capabilities` — every flag
    `False`, the most capability-limited provider in the registry:
    `in_process_tools=False`, `remote_mcp=False`, `long_context=False`,
    `usage_metering=False`, `structured_output=False`, `extended_thinking=False`,
    `sessions=False` (no server-side transcript continuity yet — each
    `query_on_session` turn is single-turn-equivalent), `streaming=False` (the
    repainting TUI offers no in-stream early-exit hook). Register via
    `registry.register("cli_paced",
    CliPacedProvider)`. Then **retire** `maestro/sdk_backends/` and the
    `[sdk] backend` field (the deprecated alias in `resolved_sdk_provider()`
    keeps `[sdk] backend = "cli_paced"` working for one release, or is dropped
    if no operator depends on it). This realises #058's Phase 4 / Step 12.
    - *Depends on:* 5, 7; #058 Phase 2 (landed).
    - *Acceptance:* `CliPacedProvider` satisfies the `Provider` protocol
      structurally; `[sdk] provider = "cli_paced"` resolves to it via the
      registry; a parametrized table asserts the `MaestroSDKError →
      SDKError(category=…)` translation is exhaustive over every
      `ErrorCategory`; `maestro/sdk_backends/` no longer exists and no module
      imports `configure_backend`.
12. [x] **`[sdk.cli_paced]` Cadence + stream-idle config.** `maestro/config.py`,
    `maestro/toml_config.py`, `maestro.toml` — add a `[sdk.cli_paced]`
    sub-section for `Cadence` overrides (`mean_key_ms`,
    `heartbeat_per_answer_char_ms`, etc.) and `stream_idle_timeout_seconds`,
    which the provider reads instead of inheriting the agent-side
    `[sdk] stream_idle_timeout_seconds` global (the adapter docstring flags
    that 18× divergence today). Provider selection, per-phase override
    routing, and unknown-value validation already landed in #058 Steps 5–7,
    so this step is **only** the cli_paced-specific sub-section.
    - *Depends on:* 11.
    - *Acceptance:* `[sdk.cli_paced]` `Cadence` + `stream_idle_timeout_seconds`
      fields round-trip through `Config`; the provider applies them to its
      `maestro_sdk.AsyncClient` / `QueryOptions`; absent the sub-section the
      provider falls back to documented `maestro_sdk` defaults.
13. [x] **`claude login` dry-probe in `check_secrets`.** `maestro/config.py` —
    #058 Step 8 landed provider-aware dispatch with a bare `shutil.which("claude")`
    PATH check for `cli_paced` and **explicitly deferred the full login probe
    here**. Upgrade the `cli_paced` branch to also confirm the CLI is logged
    in (`claude --print-config` or an equivalent non-mutating dry probe), with
    a remediation message ("run `claude login`") when the binary is present
    but unauthenticated.
    - *Depends on:* 11.
    - *Acceptance:* with `cli_paced` selected, `check_secrets` passes when
      `claude` is on PATH **and** the dry probe succeeds, and raises with the
      documented remediation when either is missing — exercised with a mocked
      probe in `tests/test_config.py`.
14. [x] **`usage_metering=False` honesty.** This is **#058's Phase 5 / Step 13**
    (generic over any `usage_metering=False` provider): emit `IterationUsage`
    rows with explicit `None`, gate `cost_tracking.py` so a `None` row
    contributes neither cost nor revenue, keep the burn-rate throttle from
    dividing by `None`, and render the missing count as "—". 056 does not
    duplicate this; it only ensures the re-homed provider returns honest
    `None`s (the current adapter re-packs into a zero-filled `QueryUsage` with
    `total_cost_usd=None` as a dataclass-shape concession — that concession
    is removed once #058 Step 13's `None`-tolerant path lands).
    - *Depends on:* 11; #058 Step 13.
    - *Acceptance:* a `query` through `CliPacedProvider` propagates honest
      `usage = None` (not zero-fill) once #058 Step 13 lands; until then the
      documented zero-with-`None`-cost concession stands.
15. [x] **Documentation.** `docs/sdk-internals.md`, `docs/deployment.md`,
    `CLAUDE.md` — document the cli_paced **provider** under the #058 provider
    abstraction: its capability descriptor, its degradation policy
    (`usage_metering=False`, no in-process tools / remote MCP / long context),
    how to select it (`[sdk] provider = "cli_paced"` globally or per-phase),
    and the `claude login` precondition. Fold the existing "SDK Backend
    Selection Seam (proposal 056)" section in `docs/sdk-internals.md` into
    #058's "Provider abstraction" section (Step 14 of #058) rather than
    leaving a stale parallel description. `CLAUDE.md` keeps one line under the
    SDK invariant pointing at the provider abstraction.
    - *Depends on:* 11, 12, 13, 14; #058 Step 14.
    - *Acceptance:* the docs describe the cli_paced provider (capabilities,
      `usage = None` degradation, `claude login` precondition, selection) with
      no stale `[sdk] backend` seam description left behind; `CLAUDE.md`
      carries a one-line pointer.

## Testing Strategy

Unit tests for the new files honour `mem_fs` + no-real-I/O + no-real-sleep:

- `maestro_sdk/tests/test_pty_runner.py` — `pty_runner` against a fake pty
  (`os.openpty` is fine inside the test process; the *child* is replaced
  by a fixture echo loop). Asserts cadence-driven send order,
  cancel-cleans-fds, kill-on-timeout.
- `maestro_sdk/tests/test_screen.py` — pure-function table tests on byte
  streams → expected screen state (cursor, scroll, SGR ignored,
  ED/EL handled).
- `maestro_sdk/tests/test_parser.py` — golden screen streams → expected
  `StreamDelta` sequence and `QueryResult.response`/`tool_calls`.
- `maestro_sdk/tests/test_approvals.py` — synthetic screen showing a tool
  approval prompt with no `auto_approve` raises `ApprovalRequiredError`;
  with a permissive predicate returns the typed response.
- `maestro_sdk/tests/test_client.py` — `InstantCadence` + fake runner +
  fake parser exercise happy path, stream-idle timeout
  → `CLITimeoutError`, startup timeout, premature exit
  → `CLIExitedError`, rate-limit screen → `RateLimitError`.
- `maestro_sdk/tests/test_errors.py` — extend to verify the `category`
  field is set on every raised error and matches the documented mapping.
- `tests/test_provider_cli_paced.py` (new, in the main package; this is
  #058's `test_provider_cli_paced.py`) — `CliPacedProvider` satisfies the
  `Provider` protocol structurally; `[sdk] provider = "cli_paced"` resolves
  through the registry; the `MaestroSDKError → SDKError(category=…)`
  translation table is exhaustive (one parametrized case per
  `ErrorCategory`); `usage = None` propagates without zero-fill; the
  declared `Capabilities` match the proposal.
- `tests/test_capability_gating.py` (extend) — routing `cli_paced`
  (`in_process_tools=False` / `remote_mcp=False` / `long_context=False`) to
  a phase that needs one of those capabilities raises the documented
  remediation at config-load time.
- `tests/test_config.py` — retiring `[sdk] backend` leaves only
  `[sdk] provider` validation; `[sdk.cli_paced]` `Cadence` +
  `stream_idle_timeout_seconds` overrides round-trip through `Config`.
- `tests/test_config.py` — `cli_paced` requires `claude` on PATH
  **and** a successful login dry-probe (mocked); a present-but-unauthenticated
  CLI raises the documented `claude login` remediation.
- `tests/test_iteration_usage.py` — `IterationUsage` rows with `None`
  counts round-trip through `write_iteration_usage` /
  `read_iteration_usage`; `cost_tracking.py` aggregator skips them
  cleanly (shared with #058 Step 13).
- `tests/test_session_attachment.py` — the AST guard already recognizes the
  `providers/registry.py` delegation as session-attached (#058 Step 5); the
  re-home adds no bare-`query` site, so no guard change is needed beyond
  confirming the retired `maestro/sdk_backends/` path is gone.

Integration tests against the real `claude` CLI remain opt-in behind
`RUN_CLAUDE_TESTS=1` per `maestro_sdk/README.md`.

## Risk Assessment

**What could regress.**

- **Observability.** The CLI path returns `usage = None` for some or all
  fields. Anything that aggregates over `usage.jsonl` (burn-rate gate,
  cost dashboard, `cost_per_task`) must treat `None` as "did not
  measure," not as zero. The `usage_metering=False` capability makes this
  explicit; mitigation is #058 Step 13 plus documentation. Default provider
  stays `"anthropic"`.
- **Trust chain.** CLI tool approvals interleave with `maestro`'s
  existing dashboard / Telegram approval flow. Step 4 lands a deny-by-
  default `ApprovalRequiredError`; callers must opt in explicitly. No
  auto-approval ships in v1.
- **Cancel containment.** Pty + child process is its own orphan-task
  hazard, parallel to the agent-SDK orphan class addressed in #055.
  Mitigation: step 1's cancel discipline + a dedicated cancel test in
  `test_pty_runner.py` that asserts no leaked tasks/fds after
  `asyncio.CancelledError`.
- **In-process tools + MCP gap.** A separate `claude` process cannot host
  Maestro's Python tool handlers (`in_process_tools=False`) and the CLI
  uses its own `~/.claude.json` for external MCP (`remote_mcp=False`). The
  adapter raises rather than silently dropping MCP config; #058's startup
  capability gate keeps cli_paced off the `implementation` / `run_task`
  phases that need either capability, at config-load time, not at first
  query.
- **Long-context absence.** No CLI flag exposes `context-1m-2025-08-07`
  today (`long_context=False`). Phases that depend on long context
  (`_self_review`, optionally Session C / Executor) cannot use `cli_paced`
  until a CLI flag exists; the capability gate enforces this.

**Rollback posture.** Default `[sdk] provider = "anthropic"` makes the whole
change inert in production. Reverting amounts to setting the flag back (or,
if registration itself regresses, reverting the re-home PR — the provider is
additive behind the flag, and #058's registry is unaffected). The
re-home/retire PR is the only step that touches the production path; the
`maestro_sdk` slices are confined to `maestro_sdk/`.

**Data migration.** None for the contract. `IterationUsage`'s
`None`-tolerant fields are already on the dataclass; `usage.jsonl` is
forward-compatible. The deprecated `[sdk] backend` field aliases onto
`[sdk] provider` for one release (#058 Step 6's `resolved_sdk_provider()`)
before retirement.

## Out of scope

- MCP bridging from Python-built servers into the `claude` CLI process
  (cli_paced declares `remote_mcp=False`; reaching external MCP from
  non-Claude providers is #058's out-of-scope item too).
- Hosting Maestro's in-process introspection tools on the CLI process
  (`in_process_tools=False` — a separate CLI process is the one genuinely
  transport-bound case).
- Long-context beta exposure on the CLI surface.
- Auto-approval predicate for `ApprovalRequiredError` (will land with
  its own approval-policy proposal so the trust chain stays explicit).
- The provider **infrastructure** (registry, `[sdk] provider` config,
  capability gate, `check_secrets` dispatch) — owned by #058, already
  landed; this proposal plugs into it rather than re-building it.
- Removing `maestro/sdk.py` or the Anthropic provider. It remains the
  default indefinitely; this proposal adds an *alternative* provider, not a
  replacement.
