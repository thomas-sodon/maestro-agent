# 026: Sandboxed Sibling Container for Implement Runtime

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | proposed |
| **Affects** | `docker-compose.yml`, `Dockerfile`, `maestro/evolution.py`, `maestro/cli.py`, new `ops/sandbox/` |

## Description

Even with proposals 024 and 025, `implement()` runs in the same container as `maestro loop`, which holds all secrets (`GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, email creds) and has unrestricted network egress. A stray `curl` in an LLM tool call still reaches arbitrary destinations. Move the implement phase into a sibling Docker container (`maestro-implement`) with a read-write bind mount of the workspace, a read-only bind mount of the approvals volume, a minimal set of env vars (`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `GITHUB_TOKEN` scoped to the evolution branch), and egress restricted to `api.anthropic.com` + `api.github.com`.

## Relevant Findings

- `docker-compose.yml` shows the current single-loop container with full volumes and env.
- `Dockerfile` runs as UID 1002 `maestro` — already non-root, good starting point for sandbox reuse.
- `implement()` at `maestro/evolution.py` is a single async method; the hand-off point is natural.
- No existing RPC infrastructure — hand-off is most simply a shell exec (`docker run --rm …`) or a compose sidecar started on demand.

## Implementation Plan

1. New `ops/sandbox/Dockerfile.implement` layered on the existing Dockerfile with a reduced entrypoint (`maestro implement-worker`).
2. New CLI `maestro implement-worker --plan-id <id>` that loads the plan, runs only the current body of `implement()` / `_self_review()` / `auto_fix()`, and exits with a structured JSON status on stdout.
   - The worker builds its agent tools (`maestro/cli.py::_build_agent_tools`) with the three external MCP servers **disabled**, so the implement-phase tool configuration **intentionally diverges** from the loop-phase configuration (which may enable them). This divergence must be documented where `_build_agent_tools` is invoked on the worker path, so a future operator who enables an external MCP server does not assume it is reachable inside the restricted-egress sandbox.
3. `MaestroAgent.implement_plan()` in the main container spawns the worker via `docker run` (or a pre-declared compose service with `docker compose run`) and consumes the stdout JSON.
4. `docker-compose.yml` adds a `maestro-implement` service with:
   - `network_mode: "bridge"` plus egress rules via iptables / a sidecar proxy (document the simpler path: Docker networks + no outbound DNS except for `api.anthropic.com` and `api.github.com` — implement as a networks/pkgs overlay or Cloudflare tunnel).
   - **External MCP egress.** Maestro runs three external (`remote_mcp`) MCP servers — GitHub (`api.github.com`), Context7 (`context7.com` / `api.context7.com`), and Fetch (arbitrary URLs by design). Only GitHub falls inside the `api.anthropic.com` + `api.github.com` allowlist; Context7 and Fetch would be silently blocked. **Default: the implement worker runs with all three external MCP servers disabled** (the four in-process introspection servers — journal, self-awareness, memory, issues — make no outbound calls and are unaffected); GitHub MCP, if needed, is still reachable via the existing `api.github.com` allowlist entry. *Alternative:* if a phase genuinely needs Context7 mid-implement, widen the allowlist to `context7.com` / `api.context7.com` — but Fetch's unbounded arbitrary-URL surface cannot be safely allowlisted in a restricted-egress sandbox and stays disabled regardless.
   - `read_only: true` root filesystem, with writable tmpfs and the workspace bind mount.
   - Minimal env allowlist.
5. `maestro` CLI remains the single user-facing entrypoint; sandbox usage is transparent.
6. Feature flag `[evolution] implement_in_sandbox` (default `false`) until the CI smoke test validates the whole path.

## Testing Strategy

1. Integration smoke in CI: start the compose stack, submit a signed approval for a trivial plan, observe the worker container run and exit zero.
2. Negative: the worker cannot reach a non-allowlisted host (`curl https://example.com`) — command fails or hangs until timeout.
3. Negative: the worker's env lacks `TELEGRAM_BOT_TOKEN` and cannot exfiltrate via Telegram.
4. Parity: all existing `tests/test_evolution*.py` pass against the feature-flag-on path.

Run: `python3.12 -m pytest tests/test_evolution*.py -v && docker compose run --rm maestro-implement maestro implement-worker --plan-id <test-id>`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Container startup latency inflates iteration time | Medium | Measure; if unacceptable, keep the worker alive as a long-running service and dispatch plans over a UNIX socket |
| Docker-in-docker permission model on hosts | Medium | Document supported host configurations; fall back to bare-metal implement when not available via the feature flag |
| GitHub PAT in the worker has the same scope as before | Accepted | GitHub itself is the shared trust boundary; a future proposal can issue branch-scoped deploy keys |
| Egress restrictions break `pip install` during implement | High | Pre-install everything at image build time; disallow new deps in implement without a follow-up review cycle |
| External MCP servers (Context7, Fetch) unreachable under restricted egress | Medium | Disable external MCP in the implement worker and document the loop-vs-implement tool-config divergence; if a phase genuinely needs Context7, widen the allowlist to `context7.com`/`api.context7.com` — Fetch's arbitrary-URL surface is incompatible with a restricted-egress sandbox |
