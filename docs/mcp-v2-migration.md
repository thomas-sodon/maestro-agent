# MCP `mcp` 1.x → 2.x migration audit

> **Historical record — the migration this audit planned has since shipped
> (proposal 121, complete).** The shim now runs on a single `mcp` 2.x path (the
> `_MCP_MAJOR` version branch and its dormant-2.x scaffolding are deleted), the
> emergency cap is lifted (`pyproject.toml` → `mcp>=2.0.0,<3`, `constraints.txt`
> → `mcp==2.1.1`), and the guard test flipped `test_mcp_dependency_excludes_2x`
> → `test_mcp_dependency_requires_2x`. The audit below is preserved as the
> grounding record: its present-tense wording and the "what remains" framing
> describe the pre-migration tree as it stood when the audit was written, not
> the current one.

This document is the grounding audit for migrating Maestro's in-process MCP shim
from the `mcp` library's 1.x low-level `Server` API to the rebuilt 2.x API. It
changes **no runtime code**: it is a reviewable map and a recorded go/no-go so
the code work that follows lands against a known target instead of discovering
the compatibility surface mid-migration. Because it touches only documentation,
it lands safely under the still-standing `mcp>=1.27.0,<2` cap and leaves the tree
fully working.

## The shim's role and the 1.x → 2.x break

`create_inproc_mcp_server` (in `maestro/inproc_mcp.py`) hosts Maestro's
introspection tools by building an `mcp.server.Server` and handing its
`{"type": "sdk", "name": str, "instance": Server}` wire shape to
`claude-agent-sdk`. The shim is written against the `mcp` library's 1.x
low-level API:

- it registers handlers with the `@server.list_tools()` / `@server.call_tool()`
  decorators,
- it builds `Tool(inputSchema=...)` in camelCase,
- and it leans on the library to auto-convert a raised exception into an error
  result — the unknown-tool branch simply `raise`s a `ValueError`.

`mcp` 2.0 rebuilt the low-level `Server` and **removed the decorators**, so any
environment that resolves `mcp` 2.x crashes at agent construction with
`AttributeError: 'Server' object has no attribute 'list_tools'` — before the
loop reaches iteration 1.

Two halves of the durable fix already stand in the tree:

- **An executable wire contract** — `tests/test_mcp_wire_contract.py` stands up
  the shim from sample tools and dispatches *real* `list_tools` / `call_tool`
  requests through an in-memory connected client session (the way the SDK
  client drives it in production), pinning today's catalog shape, success text,
  unknown-tool error, and handler-raise error as a red-on-break contract the
  migration can move against.
- **Reproducible resolution** — a committed `constraints.txt` (threaded through
  the Docker build, the container entrypoint, and CI, guarded by
  `tests/test_dependency_constraints.py`) pins `mcp==1.27.0` /
  `claude-agent-sdk==0.2.149` so all three install paths resolve identically.

What remains is the forward migration, currently held below the emergency
`mcp>=1.27.0,<2` cap (guarded by `test_mcp_dependency_excludes_2x`). This audit
is the increment before that code work: a complete 1.x→2.x symbol map, a
per-file touchpoint inventory, and the `claude-agent-sdk` compatibility
determination that decides whether the migration is even possible.

## 1.x → 2.x symbol map

Every 1.x symbol / pattern the shim (and the stdio serve path) uses, mapped to
its 2.x replacement. Grounded in the official `mcp` v2 low-level `Server`
migration guide.

| 1.x symbol / pattern | 2.x replacement |
|---|---|
| `Server(name, version=...)` **+** `@server.list_tools()` / `@server.call_tool()` decorators | `Server(name, version=..., on_list_tools=..., on_call_tool=...)` — handlers passed as constructor kwargs; the decorators are gone. |
| list-tools handler `() -> list[Tool]` | `(ctx: ServerRequestContext, params: PaginatedRequestParams \| None) -> ListToolsResult` — wrap the catalog in `ListToolsResult(tools=[...])`. |
| call-tool handler `(tool_name: str, arguments: dict) -> CallToolResult` | `(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult` — read the name/args off `params.name` / `params.arguments` instead of positional parameters. |
| `Tool(inputSchema=...)` (camelCase) | `Tool(input_schema=...)` (snake_case). |
| `CallToolResult(isError=...)` (camelCase) | `CallToolResult(is_error=...)` (snake_case) — the same camelCase→snake_case rename as `inputSchema`, applied to the result the shim already builds by hand. |
| Auto JSON-schema validation of tool arguments against `inputSchema` | Removed. The shim already reads `arguments` without relying on pre-validation, so no behaviour is lost, but nothing validates for us either. |
| Auto dict/list wrapping of a handler return into a `CallToolResult` | Removed. Not load-bearing for the shim's **success** path — `_call_tool` already constructs `CallToolResult(content=..., isError=...)` explicitly rather than returning a bare dict/list. |
| Auto exception→error conversion (a raised handler exception becomes an error result) | **Removed — load-bearing.** The unknown-tool `raise ValueError(...)` branch, and any handler-raise, must instead build an explicit `CallToolResult(content=[TextContent(type="text", text=...)], is_error=True)`; a bare `raise` no longer becomes an error result the client can read. |
| stdio serve path — `mcp.server.stdio.stdio_server`, `server.run(...)`, `server.create_initialization_options()` (`_serve_mcp_over_stdio` in `maestro/cli.py`) | **Unchanged** — import paths and call shapes are stable across 1.x and 2.x, so this path needs re-verification only, not a rewrite. (Corroborated below: `claude-agent-sdk` 0.2.149 itself drives an SDK MCP server instance via `server.run(...)` over in-memory streams.) |

**New imports the 2.x shim requires:**

- `ServerRequestContext` — from `mcp.server`.
- `CallToolRequestParams`, `ListToolsResult`, `PaginatedRequestParams` — from
  `mcp.types` (alongside the already-imported `CallToolResult`, `TextContent`,
  `Tool`, `ContentBlock`).

## Per-file `mcp` touchpoint inventory

Every file that must change when the shim migrates, so the later code slices
have a complete work list. Confirmed against the current tree.

- **`maestro/inproc_mcp.py`** (`create_inproc_mcp_server`) — the sole
  handler-registration site and the concentration point of all `mcp`-API
  coupling: the two decorators, the camelCase `Tool(inputSchema=...)`, the
  camelCase `CallToolResult(isError=...)`, and the `raise ValueError` that
  relies on auto exception→error conversion. This is the file the symbol map is
  derived from and the only one the migration *rewrites*.
- **`maestro/cli.py`** (`_serve_mcp_over_stdio`) — the stdio path. Re-verify
  against the 2.x run loop; call shape (`stdio_server`, `server.run`,
  `create_initialization_options`) is unchanged, so no rewrite is expected.
- **`tests/test_inproc_mcp.py`** — the private-handler tests reach into
  `server.request_handlers[ListToolsRequest]` / `[CallToolRequest]` and assert
  on the 1.x `res.root.*` `ServerResult` wrapper. These invocation-by-hand
  assertions must move to the 2.x surface. This file also owns
  `test_mcp_dependency_excludes_2x`, which pins the `<2` cap and **must flip**
  when the pin moves.
- **`tests/test_tool_adapter.py`** — `instance.request_handlers[...]` +
  `res.root.*` assertions in the cli-paced host tests; same 1.x-internal
  coupling as `test_inproc_mcp.py`.
- **`tests/test_tools.py`** — an `isinstance(mcp[...]["instance"], Server)`
  check; the `Server` import survives the migration, but re-verify the shape.
- **`tests/test_mcp_wire_contract.py`** — dispatches through the in-memory
  client wrapper rather than `request_handlers`, so its *dispatch* is
  migration-stable, and its unknown-tool / handler-raise expectations are the
  regression that proves the 2.x shim reproduces the 1.x auto exception→error
  conversion **explicitly** via `CallToolResult(is_error=True)`. It does still
  need one edit, though not until the pin moves: its three `result.isError`
  reads are the client side of the very field the symbol map renames, so they
  become `result.is_error` at step 4, not step 3.
- **`tests/test_cli.py`** — its in-memory-client round-trip
  (`test_mcp_serve_advertises_registry_tools`) asserts only on
  `result.tools[].name`, stable across 1.x and 2.x, and it carries no
  unknown-tool or handler-raise expectation. It stays green *without edits* at
  every step.

**Untouched by the migration.** The wire shape `{"type": "sdk", "name",
"instance"}` and every shim consumer stay as-is: `maestro/tools.py`,
`maestro/telegram_tools.py`, `maestro/email_tools.py`, and the
`maestro/providers/` adapters all pass `InprocTool` lists and never import `mcp`
directly. The `InprocTool` dataclass and the `@tool` decorator are also
unchanged — they carry the provider-neutral `input_schema` the shim translates
into the `mcp` wire type.

## `claude-agent-sdk` compatibility determination and go/no-go

This is the decisive section. Migrating the shim in isolation is not enough: the
**SDK** — not only our shim — consumes the `Server` instance, so if the pinned
SDK cannot run under `mcp` 2.x, a migrated shim would pass its isolated wire
contract yet still break production at agent construction.

### Finding: the pinned deploy SDK (`0.2.87`) is coupled to `mcp` 1.x internals

`claude-agent-sdk 0.2.87` was the version `constraints.txt` pinned and installed
in this tree when this audit ran (the tree now pins `0.2.149` — the SDK bump in
*Sequencing* below). Source inspection confirms it is 1.x-coupled on both
sides of the seam:

- **It registers** handlers with the 1.x decorators: `create_sdk_mcp_server`
  builds `Server(...)` and applies `@server.list_tools()` / `@server.call_tool()`
  (the same decorators the shim uses), and constructs tools with camelCase
  `inputSchema`.
- **It dispatches** against 1.x internals: its `_internal/query.py` reads
  `server.request_handlers.get(ListToolsRequest)` / `.get(CallToolRequest)`,
  pulls the `.root`-wrapped `ServerResult` off the handler result
  (`result.root.tools`, `result.root.content`, `result.root.isError`), and
  reads `tool.inputSchema`.

Every one of those touchpoints is exactly what `mcp` 2.0 removed. So `mcp` 2.x
would break `claude-agent-sdk 0.2.87` **independently of the shim** — migrating
`maestro/inproc_mcp.py` alone would not unblock the cap.

### The SDK relaxes its `mcp` bound across releases, and a 2.x-capable release exists

`claude-agent-sdk` metadata (`Requires-Dist` for `mcp`, read from PyPI):

| `claude-agent-sdk` | `mcp` bound | Reading |
|---|---|---|
| `0.2.87` (audited baseline; was pinned / installed) | `mcp>=1.23.0` (unbounded above) | 1.x-coupled dispatch; unbounded metadata is what let a clean-room resolver pick 2.x and crash — the reason the `<2` cap exists. |
| `0.2.100` | `mcp<2.0.0,>=1.23.0` | Adds the `<2` cap. |
| `0.2.120` | `mcp<2.0.0,>=1.23.0` | Still `<2`. |
| `0.2.149` (now pinned / installed; latest at audit time) | `mcp<3.0.0,>=1.23.0` | Relaxes to `<3` — the first named release whose metadata **permits** `mcp` 2.x. |

The cap was relaxed somewhere in the `(0.2.120, 0.2.149]` window; `0.2.149` is
the confirmed-safe target this audit inspected. A metadata relaxation alone is
not proof of 2.x compatibility, so the target release was source-inspected.

### Confirmation: `0.2.149` dispatches against the 2.x `Server` surface

Source inspection of `claude-agent-sdk 0.2.149` confirms it dropped the
1.x-internal coupling that `0.2.87` had:

- **Server construction is version-branched.** `create_sdk_mcp_server` delegates
  to `_internal._mcp_compat.build_tool_server`, which branches on
  `if MCP_MAJOR >= 2:` — the 2.x path constructs
  `Server(name, version=..., on_list_tools=on_list_tools, on_call_tool=on_call_tool)`
  with handler signatures `on_list_tools(ctx, params) -> ListToolsResult` /
  `on_call_tool(ctx, params) -> CallToolResult`, exactly the 2.x constructor-kwarg
  surface in the symbol map; the 1.x path keeps the decorators for backward
  compatibility.
- **Dispatch no longer reaches into `request_handlers` / `.root`.** The
  `SdkMcpBridge` that replaced the inline `_internal/query.py` dispatch drives
  the wrapped `Server` instance via `server.run(...)` over in-memory streams —
  no `request_handlers`, no `.root`. That is transport-based and identical
  across `mcp` 1.x and 2.x, which is *why* the SDK can safely widen its `mcp`
  bound. It also corroborates that the shim's own `server.run(...)` stdio path is
  migration-stable.

**Determination: GO.** The shim migration is feasible. `claude-agent-sdk`
`0.2.149` runs against `mcp` 2.x, so bumping the SDK to it (or any release with
the same `<3.0.0` metadata and 2.x-branched construction) is the **hard
prerequisite** that unblocks both the shim rewrite and the cap lift. No blocker
survives once the SDK bump lands.

### Sequencing

1. **This audit** — the map + go/no-go (documentation only; lands under the
   `<2` cap). ← *this increment*
2. **Bump `claude-agent-sdk`** to the 2.x-capable release (`0.2.149`,
   confirmed). Its own compatibility surface is a separate change.
3. **Migrate the shim** — move `create_inproc_mcp_server` onto the 2.x `Server`
   constructor-kwarg API per the symbol map, reproducing the auto
   exception→error behaviour explicitly. To land as a code slice *while the
   `<2` cap still stands*, this has to be **version-branched** (`MCP_MAJOR >= 2`)
   — the same shape `claude-agent-sdk` 0.2.149 uses — because 1.x
   `Server.__init__` accepts no `on_list_tools` / `on_call_tool` kwargs, so an
   unbranched 2.x-only rewrite cannot run, let alone go green, against the
   pinned `mcp==1.27.0`. Proven green by `test_mcp_wire_contract.py` on the 1.x
   branch. (The alternative is to fold this step into step 4 and move the pin
   and the shim together, giving up the under-the-cap landing.)
4. **Lift the `mcp>=1.27.0,<2` cap** to a pinned 2.x range — `pyproject.toml`,
   `constraints.txt`, and the 1.x-coupled test updates
   (`test_mcp_dependency_excludes_2x` flips), gated on steps 2 and 3 both green.

## Out of scope for this audit

- Migrating the shim (the `maestro/inproc_mcp.py` rewrite) — step 3 above.
- Bumping `claude-agent-sdk` — step 2 above; a separate change with its own
  compatibility surface.
- Lifting the cap and pinning a 2.x range — step 4 above.
- The 2.x high-level `MCPServer` (formerly `FastMCP`) API — the shim deliberately
  uses the low-level `Server` to control the wire shape `claude-agent-sdk`
  consumes.
- Pinning the transitive dependency closure or the sibling `maestro_sdk` /
  `maestro_review` packages — the constraints file pins the direct set only.
