# 076: Cross-Process Launcher for In-Process MCP Tools on the CLI-Paced Provider

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | partial |
| **Affects** | `maestro/cli.py`, `maestro/providers/cli_paced_tool_adapter.py`, `maestro/providers/cli_paced.py`, `maestro/inproc_mcp.py` (consumed), `docs/sdk-internals.md`, `CLAUDE.md`, tests |
| **Depends on** | 061 (shipped), 066 (shipped — hidden-subcommand pattern) |
| **What landed** | Step 1 — `MCP_SERVE_SUBCOMMAND`, `_build_mcp_serve_registry`, `_resolve_mcp_serve_server`, `run_mcp_serve` in `maestro/cli.py`; exits non-zero on unknown server name; subcommand parser wired into the CLI entry point (PR #2621). Step 2 — `CliPacedToolAdapter.materialize` (`maestro/providers/cli_paced_tool_adapter.py`) emits command-shaped `--mcp-config` launcher entries (`{command: "maestro", args: ["_mcp-serve", <server>], env}`) via `_LAUNCHER_COMMAND` + `launcher_config_env()`, so `_external_mcp_servers` selects them in; qualified-name parity with the Anthropic adapter preserved (PR #2633). Step 3 — `_external_mcp_servers` (`maestro/providers/cli_paced.py`) logs a WARNING naming dropped servers when non-command-shaped entries remain after the adapter's Step 2 coverage; the signal recurs per spawn. Step 4 — the cross-process state-safety fault-seeding tests `test_interleaved_writers_no_lost_update_with_locking` / `test_interleaved_writers_lose_update_without_locking` in `tests/test_fault_seeding.py` verify `file_lock` holds across the launcher/loop write seam (passes with locking; demonstrably loses an update with the lock patched out). |
| **What is missing** | Step 5 — documentation pass (`docs/sdk-internals.md`'s capability matrix still reads `launcher: rollout`; the `CLAUDE.md` sibling-process line is absent). |

## Description

> Status updated to partial on 2026-06-13 by /update-proposals — see [README.md](../README.md#open-proposals) for the landing record.

`cli_paced` declares `in_process_tools=True`, but the capability is
config-only: proposal 061 Step 4 materialised the `--mcp-config` payload and
the `mcp__<server>__<tool>` allow-list, then deliberately deferred "the
cross-process launcher entrypoint (`cli_paced_mcp_serve`)" to the rollout
proposal — and neither 066 (action-gate) nor 067 (canary/runbook) picked it
up. The consequence in production today is a silent capability hole: Maestro's
in-process introspection and messaging servers (journal, self-awareness,
memory, issues, plus messaging/email/telegram tools) are SDK-instance specs a
separate `claude` process cannot host, so `_external_mcp_servers` selects them
out of the spawn command. A phase routed to `cli_paced` that passes the same
`mcp_servers` mapping it passes the Anthropic provider simply runs **without
its introspection tools** — no error, no warning, the model just cannot see
its own journal.

The fix is the deferred launcher: a hidden `maestro _mcp-serve <server>`
subcommand (the exact pattern proposal 066 established with
`maestro _gate-bash-hook`) that hosts one logical server's `InprocTool`
handlers over stdio MCP in a fresh Maestro process. The adapter then emits
*command-shaped* entries pointing at that subcommand, which the existing
`_external_mcp_servers` / `_build_cli_command` path forwards without
modification — the spawned `claude` reaches Maestro's tools the same way it
reaches GitHub or Context7.

The semantic shift to be explicit about: "in-process" becomes "in a sibling
Maestro process" on this provider. The handlers are unchanged Python
functions, but they execute against the data directory from a second process,
so the file-persistence discipline (`atomic_write`, `file_lock`, append-only
JSONL) is promoted from in-process convention to cross-process contract. End
state: the qualified tool names a `cli_paced`-routed phase sees are identical
to the Anthropic adapter's, a dispatched call reaches the same handler logic,
and the interim silent drop is replaced by a loud signal until/unless the
launcher covers it.

## Relevant Findings

- `maestro/providers/cli_paced_tool_adapter.py` (`CliPacedToolAdapter.materialize`,
  `build_servers`) — already emits the CLI `--mcp-config` JSON and
  `mcp__<server>__<tool>` allow-list and can build the in-process hosts via
  `create_inproc_mcp_server`; proposal 061's landing note records that "the
  config is materialised and tested but never executed."
- `maestro/providers/cli_paced.py` (`_external_mcp_servers`) — forwards only
  `{command, args, env}`-shaped entries; the docstring names the in-process
  servers as "SDK-instance specs a *separate* `claude` process cannot host
  (their cross-process launcher is proposal 061's deferred rollout) — so they
  are selected out." This is the silent-drop site this proposal eliminates.
- `maestro/providers/cli_paced.py` (`_build_cli_command`, `_MCP_CONFIG_FLAG`)
  — inline string-encoded `--mcp-config` on the spawn argv; command-shaped
  launcher entries ride this existing path with no provider change.
- `maestro/providers/cli_paced_gate.py` + `maestro/cli.py` — the proposal 066
  precedent this mirrors: a hidden `maestro _gate-bash-hook` subcommand
  spawned by the `claude` child, parameterised via env
  (`GATE_BRANCH_ENV`), delegating to the same core logic as the in-process
  path so the two cannot drift.
- `maestro/cli.py` (`_build_agent_tools`) — assembles the four in-process
  introspection servers plus messaging tools from `maestro/tools.py` handlers;
  the canonical `ToolRegistry` the launcher must reproduce per server.
- `maestro/inproc_mcp.py` (`InprocTool`, `create_inproc_mcp_server`) — the
  neutral tool shape; handlers are plain async Python functions over the data
  directory.
- `maestro/fileutil.py` (`atomic_write`, `file_lock`) — the persistence
  primitives the handlers already use; the CLAUDE.md file-persistence
  invariant ("read-modify-write uses `file_lock()`") is what makes a sibling
  writer process safe, and the audit in Step 4 verifies the lock is genuinely
  cross-process (OS-level, not in-process only) for every handler the launcher
  exposes.
- `maestro/providers/base.py` (`Capabilities.in_process_tools` docstring) —
  the flag is defined as "can the provider host Maestro's own Python tool
  handlers"; today's `cli_paced` answer is honest only against test doubles.

## Implementation Plan

Steps are ordered so the launcher exists and is proven before the adapter
points live config at it, and the silent drop becomes loud in the same change
that makes it rare. Everything is inert under the default `anthropic`
provider.

1. [x] **Hidden launcher subcommand.** `maestro/cli.py` — add
   `maestro _mcp-serve <server>` (underscore-hidden like `_gate-bash-hook`):
   resolves the named logical server from the same registry construction
   `_build_agent_tools` uses, builds it via `create_inproc_mcp_server`, and
   serves stdio MCP until EOF. The data directory and environment resolve
   exactly as a normal `maestro` invocation (TOML + env vars), so the sibling
   process sees the same state files; an unknown server name exits non-zero
   with a one-line error (fail loud, never an empty server).
   - *Depends on:* none.
   - *Acceptance:* invoking the subcommand entry function with a known server
     name yields a server advertising exactly that server's registry tools
     (driven in-process via the stdio seam, no real subprocess); an unknown
     name exits non-zero.
2. [x] **Adapter emits launcher-shaped config.**
   `maestro/providers/cli_paced_tool_adapter.py` — `materialize` switches the
   per-server `--mcp-config` entries from SDK-instance specs to
   `{command: "maestro", args: ["_mcp-serve", <server>], env: {…}}`, threading
   the resolved data-dir / config env so the child serves the same state. The
   qualified `mcp__<server>__<tool>` allow-list is unchanged. Because the
   entries are now command-shaped, `_external_mcp_servers` forwards them with
   zero provider change.
   - *Depends on:* 1.
   - *Acceptance:* a registry round-trips into a `--mcp-config` whose entries
     are command-shaped launcher invocations, the advertised tool names equal
     the Anthropic adapter's qualified names, and `_external_mcp_servers` now
     selects them in.
3. [x] **Loud interim signal for unforwardable specs.**
   `maestro/providers/cli_paced.py` (`_external_mcp_servers` call sites) —
   when `options.mcp_servers` still contains non-command-shaped entries after
   Step 2 (a future SDK-instance spec the adapter does not cover), log a
   WARNING naming the dropped servers instead of selecting them out silently.
   A silent tool-surface narrowing is the same hazard the tool-policy slice
   called "the one thing the fail-loud doctrine forbids most"; WARNING (not
   raise) because a missing introspection server degrades a phase rather than
   corrupting it.
   - *Depends on:* 2.
   - *Acceptance:* a mapping containing one command-shaped and one
     instance-shaped entry forwards the first and logs a WARNING naming the
     second; an all-forwardable mapping logs nothing.
4. [x] **Cross-process state-safety audit + adversarial test.**
   `maestro/fileutil.py` review + `tests/` — verify `file_lock` is an OS-level
   lock effective across processes, and add a fault-seeding test (per the
   `tests/test_fault_seeding.py` discipline) that interleaves a launcher-side
   handler write with a loop-side write to the same state file at the seam
   level (mocked processes, real locking semantics), asserting no lost update.
   Any handler found doing un-locked read-modify-write is fixed here, behind
   the existing invariant rather than a new mechanism.
   - *Depends on:* 1.
   - *Acceptance:* the interleaving test passes with locking on and is
     demonstrated to fail with locking patched out (ground-truth that the test
     detects the fault class).
5. [ ] **Documentation.** `docs/sdk-internals.md` + `CLAUDE.md` — document the
   launcher mechanism, the "in-process means sibling-process on this provider"
   semantics, the env/data-dir threading, and the WARNING contract for
   unforwardable specs; keep the CLAUDE.md MCP invariant line at one line.
   - *Depends on:* 2, 3, 4.
   - *Acceptance:* the capability matrix's `in_process_tools` caveat for
     `cli_paced` points at the launcher instead of a deferral; CLAUDE.md line
     count unchanged.

## Testing Strategy

Unit tests honour `mem_fs` + no-real-I/O + no-real-network + no-real-`sleep`:
the launcher is exercised through its entry function and the stdio seam, never
by spawning a real `maestro` or `claude` process; locking semantics are tested
at the primitive level.

- `tests/test_cli.py` (extend) — `_mcp-serve` resolves each known logical
  server to its registry tools; unknown name exits non-zero; data-dir/env
  resolution matches a normal invocation.
- `tests/test_tool_adapter.py` (extend) — launcher-shaped `--mcp-config`
  round-trip; qualified-name parity with `AnthropicToolAdapter` (the existing
  parity assertion, now over command-shaped entries); a dispatched call still
  reaches the unchanged Python handler via `build_servers`.
- `tests/test_provider_cli_paced.py` (extend) — Step 3 WARNING on residual
  instance-shaped specs; command-shaped launcher entries appear in the spawn
  argv's `--mcp-config`.
- `tests/test_fault_seeding.py` (extend) — Step 4 interleaved-writer test with
  the patched-out-lock failure demonstration.

## Risk Assessment

- **Split-brain state.** Two Maestro processes writing the same JSONL/state
  files is the new hazard; an un-locked handler could lose updates.
  Mitigation: Step 4's audit + adversarial test promote the existing
  `file_lock`/`atomic_write` invariant to a verified cross-process contract
  before any live config points at the launcher; append-only JSONL files are
  already safe under concurrent appenders with locking.
- **Tool surface drift between adapters.** The launcher could expose a
  different tool set than the in-process path. Mitigation: both build from
  the same registry construction (`_build_agent_tools` → `create_inproc_mcp_server`),
  and the qualified-name parity test pins the two adapters to identical names.
- **Launcher startup cost per spawn.** Each `claude` child spawns up to four
  sibling Maestro processes. Mitigation: the launcher imports lazily like the
  provider itself and serves a single server each; if measured startup cost
  matters it is a follow-up optimisation, not a correctness risk.
- **Trust surface.** The launcher executes tool handlers on behalf of an
  outside process, but the stdio transport is parent-spawned (not a listening
  socket), the subcommand is hidden, and the action-gate question is
  orthogonal — these are read/introspection/messaging tools, and 066's gate
  governs Bash, not MCP. Documented rather than re-gated here.

**Rollback posture.** Default `[sdk] provider = "anthropic"` keeps everything
inert; the adapter change only affects `cli_paced` spawns. Reverting Step 2
returns to the (now-loud) selected-out behaviour with no state impact.

**Data migration.** None.

## Out of scope

- **Pooling / keeping launcher processes resident across turns** — spawn-per-
  child is the simple correct baseline; pooling is an optimisation with its
  own lifecycle hazards.
- **Replicating the Bash action-gate** — proposal 066 owns it; MCP tools are
  not Bash.
- **Exposing new tools** — the launcher reproduces the existing registry
  exactly; any tool-surface change is its own proposal.
