# Deployment

> **Archived — no active deployment.** Maestro is no longer developed or deployed. This page records how the production host was set up while the experiment ran; read present-tense descriptions of the host as historical.

Maestro ran on a **Hetzner Ubuntu VPS** in Docker containers with host bind mounts.

## Environment Prerequisites

**IMPORTANT: Verify the environment is fully set up before starting any implementation work.**

| Requirement | Version | Verify with |
|---|---|---|
| Python | 3.14+ | `python3 --version` |
| pip | latest | `pip --version` |
| gh (GitHub CLI) | latest | `gh --version` |
| git | latest | `git --version` |
| Node.js (browser tests) | 18+ | `node --version` |

```bash
pip install -e "maestro_sdk/"                   # Sibling SDK — cli_paced provider imports it (mypy maestro/ + test suite need it)
pip install -e ".[dev]"                         # Install project + dev dependencies
pip install -e ".[browser]" && playwright install chromium  # Browser test deps (optional)
pip install -e ".[email]"                       # Email surface deps (aiosmtplib/aioimaplib) — required by any deployment using MAESTRO_EMAIL_*
ruff --version && mypy --version && pytest --version        # Verify tools
gh auth status                                  # Verify GitHub CLI authenticated
```

When the separate `maestro_sdk` editable install is absent (the loop devbox), the test suite still resolves the sibling from the in-repo `maestro_sdk/src` tree via `[tool.pytest.ini_options] pythonpath` — the pytest twin of `[tool.mypy] mypy_path` — so collection does not abort. Installing it is still the supported setup.

Confirm `ruff check maestro/ tests/ && mypy maestro/ && pytest tests/ -v` runs (even if there are pre-existing issues to fix) before writing code.

## Environment Variables (Secrets Only)

Only secrets remain as environment variables. All configuration values have moved to TOML.

| Variable | Purpose | Required |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude API key (one of this or OAuth token required) | Yes* |
| `CLAUDE_CODE_OAUTH_TOKEN` | OAuth token from Max/Pro subscription | Yes* |
| `GITHUB_TOKEN` | GitHub PAT (`contents:write`, `pull_requests:write`, `issues:write`, `repo` — see [Publication Track Deployment Prerequisites](#publication-track-deployment-prerequisites)) | Yes |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for notifications | No (warn) |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for notifications | No (warn) |
| `MAESTRO_DASHBOARD_TOKEN` | Bearer token for dashboard auth | No (warn) |
| `MAESTRO_EMAIL_ADDRESS` | Email sender address (e.g. `operator@example.com`) | No |
| `MAESTRO_EMAIL_PASSWORD` | Email account password or app password | No |
| `MAESTRO_EMAIL_THOMAS` | Thomas's email address for direct communication | No |
| `MAESTRO_ENV` | Environment name for TOML overlay selection | No |

\* At least one Claude credential is required.

`check_secrets()` runs on startup (in `_build_agent()`) and during `preflight`.

### Per-provider credentials

The credential rules above cover the default `[sdk] provider = "anthropic"`. `check_secrets()` dispatches the credential precondition on the selected provider (proposal 058), so the requirement changes with the [provider abstraction](sdk-internals.md#provider-abstraction):

| `[sdk] provider` | Required credential | Notes |
|---|---|---|
| `anthropic` (default) | `ANTHROPIC_API_KEY` **or** `CLAUDE_CODE_OAUTH_TOKEN` | Existing OR check; warns on an unexpected key prefix. |
| `openai` | `OPENAI_API_KEY` | Warns on a non-`sk-` prefix. Not yet registered — selecting it fails loud at startup until the Phase 3 tool-adapter wiring lands. |
| `cli_paced` | `claude` on `PATH`, already logged in | Drives the `claude` CLI through a pty (see [CLI-paced provider](sdk-internals.md#cli-paced-provider-sdk-provider--cli_paced-proposal-056)); expects a logged-in CLI rather than an env-var key. Checks PATH presence (`shutil.which`) **and** an active login via a non-mutating `claude auth status --json` dry-probe (proposal 056 Step 13); a present-but-unauthenticated CLI raises a `claude auth login` remediation. |

The `GITHUB_TOKEN` requirement and the Telegram/dashboard/publication checks stay provider-neutral.

**Per-phase routing precondition (proposal 067 Step 1).** `check_secrets()` also covers every provider named in the `[sdk] <phase>_provider` override map, with a phase-aware verdict: a provider routed to a phase that must be able to execute (`implementation` / `run_task`, `MUTATING_SDK_PHASE_NAMES` in `maestro/config.py`) gets the same hard startup error as a global selection — for `cli_paced`, the `claude auth login` remediation, with the failing phase named — while a provider routing only non-mutating phases degrades to a warning: the loop starts, and the routed phases fail loud at their first query. A per-phase route to the already-checked global provider is not re-checked.

**Capability gating.** At startup `validate_provider_capabilities()` asserts the resolved provider can serve every phase routed to it — `remote_mcp` for phases consuming the external Context7/Serena servers, `long_context` for long-context phases, `in_process_tools` for introspection-tool phases. A gap raises a remediation error at config-load time, before any query. See the [capability matrix](sdk-internals.md#capability-matrix).

**cli_paced slash-probe (optional).** With `cli_paced` selected, `[sdk.cli_paced] probe_metrics_enabled = true` (plus `probe_metrics_every`, ≥ 1) turns on the slash-command metrics probe — an observability side channel that scrapes `/cost`, `/usage`, `/context` from the CLI's TUI for context-window composition and plan-headroom visibility. It is a screen-scrape of a non-contractual layout (a mismatch reads as "no reading", never a wrong number) and **never overrides the transcript-sidecar spend counts**; a `/cost`-vs-sidecar drift logs a `metering_divergence` WARNING. Default-off. See the [slash-command metrics probe](sdk-internals.md#cli-paced-provider-sdk-provider--cli_paced-proposal-056).

**cli_paced marker pack (optional).** `[sdk.cli_paced] marker_pack = "auto" | "<pack-name>" | "none"` (default `"none"`) selects the per-CLI-version layout pack that makes four `cli_paced` capabilities live rather than honestly degenerate: session continuity (idle marker keeps the child resident; session-id marker enables `--resume` respawn), sidecar usage metering (the captured id locates the transcript), the rate-limit/expired-session error taxonomy, and the slash-probe parses. `"auto"` probes `claude --version` at startup and applies the newest registered pack covering that version — when none covers, it logs one WARNING naming the version and runs pack-less (EOF-only turns, exactly the `"none"` behaviour); a typo'd pack name fails loud at config validation. **On a CLI upgrade**, a stale pack reads as one-shot, pack-named layout-drift WARNINGs (never a wrong parse): re-pin `marker_pack` to a pack covering the new version, or revert to `"none"`/wait for a covering pack — behaviour meanwhile is the EOF-only degradation, not a crash. See [marker packs](sdk-internals.md#cli-paced-provider-sdk-provider--cli_paced-proposal-056).

### CLI-paced rollout: canary → promote → rollback (proposal 067)

Capability parity (proposal 061) and the subprocess action-gate (proposal 066) make `cli_paced` *safe to route*; this runbook is the operational procedure for actually routing it — one phase at a time, with an observable health signal and a one-line path back. Every stage is a plain config edit in `maestro.{env}.toml` followed by a loop-container restart; there is no state migration at any point.

**Stage 1 — canary on a non-mutating phase.** Route a single read-only phase first, so the first production exposure carries no mutation risk:

```toml
[sdk]
contemplation_provider = "cli_paced"
```

Preconditions: a logged-in `claude` CLI on PATH in the loop container, and `maestro preflight` green. With only a non-mutating phase routed, a logged-out CLI is a preflight *warning*, not a blocker (see the per-phase routing precondition above) — fix the login anyway; the canary phase cannot serve a query without it. For the canary's observability signals to mean anything, a marker pack covering the container's CLI version should be configured (`[sdk.cli_paced] marker_pack`, above): without one, session continuity, sidecar metering, the rate-limit/expired error taxonomy, and the slash probe are all in their honest degenerate state — every turn EOFs, every usage row is unmetered, and a rate-limited session reads as a generic stall — so the soak would be observing the degraded path, not the one being promoted. **Registry status:** no pack currently covers the claude 2.1.x TUI (see the layout findings in [sdk-internals](sdk-internals.md#cli-paced-provider-sdk-provider--cli_paced-proposal-056)); until one ships, run the canary with that caveat in mind.

Watch the health signal over a soak of at least a handful of iterations:

- **Clean iterations** — the canary phase completes in the app log with no `cli_paced` error-taxonomy crashes and no stream-idle teardowns (`stream_idle_timeout_seconds` + the SIGTERM→SIGKILL ladder bound a wedged child; a teardown firing is a signal, not a recovery).
- **No `ApprovalRequiredError`** — `maestro_sdk`'s approval handler is deny-by-default: a wedged or unexpectedly-prompting CLI fails loud rather than auto-approving or hanging on a modal. Any occurrence in the log is a no-go.
- **Attributed usage rows** — rows produced by the canary name their provider (the `providers` field on `usage.jsonl` rows, the `provider` field on `cost_samples.jsonl`, the Provider column on the dashboard Budget tab): metered token counts when the CLI transcript sidecar is readable, an honest `—` (`unmetered=True`, spend unknown-by-design) when not — never `$0`. *Caveat:* until proposal 078 lifts the per-call telemetry spine (`_log_sdk_call` / usage sink) to the provider-neutral seam, `cli_paced` calls do not reach `cost_samples.jsonl` / `usage.jsonl` at all — read missing Budget-tab rows for the canary phase as that known gap, not as canary health, and lean on the two log-based signals above. The burn-rate throttle is correct either way: unmetered samples are excluded from rate aggregation, never read as spend.

**Go/no-go gate.** Promote only when the soak shows **all** of: zero `ApprovalRequiredError`, zero unexplained crashes or idle-teardowns on the canary phase, and the expected attribution on whatever usage rows the canary produced. Any miss → roll back (below), diagnose, re-canary. Do not skip the canary and route a mutating phase directly.

**Stage 2 — promote to an action-gated phase.** With the gate passed, extend the routing to a phase that executes repo-mutating Bash:

```toml
[sdk]
contemplation_provider = "cli_paced"
implementation_provider = "cli_paced"   # or run_task_provider
```

Two startup guards now apply on top of the canary preconditions: `validate_action_gate_coverage()` (proposal 066) refuses any mutating-phase route to a provider without a #2280 Bash mutation guard — `cli_paced` qualifies via its CLI-injected `--settings` PreToolUse hook — and the per-phase login precondition becomes a **hard** preflight failure (`claude auth login` remediation) because the routed phase must be able to execute. Keep watching the same health signal; PR/push attempts from the routed phase must keep arriving as approval requests, never as direct pushes.

**Rollback (any stage, one line per routed phase).** Revert the override to `anthropic` — or delete the line:

```toml
[sdk]
# contemplation_provider = "cli_paced"   # removed (or set to "anthropic")
```

Restart the loop container; the next iteration runs entirely on the reverted provider. No state migration: sessions are rebuilt per iteration, and mixed-provider `usage.jsonl` / `cost_samples.jsonl` histories coexist — attribution names which provider produced each row. The same single-line revert applies to a global `[sdk] provider` selection.

## Publication Track Deployment Prerequisites

Required before enabling [proposal 033](proposals/publication/033-publication-execution-pipeline.md) (`Publisher` phase) in production. The deploy PAT must carry the `repo` scope so `gh repo create` succeeds.

**Rotation procedure:**

1. Regenerate the deploy PAT at https://github.com/settings/tokens with scopes:
   `contents:write`, `pull_requests:write`, `issues:write`, **`repo`**.
2. Update the Docker secret / `.env` value for `GITHUB_TOKEN`.
3. Restart the loop container. The dashboard does **not** need restart — it never calls `gh repo create`.

**Verification (in the loop container):**

```bash
docker exec maestro-loop gh auth status --show-token
# expect 'repo' listed in the scope summary
```

**Container credential helper:**

The entrypoint at `scripts/docker-entrypoint.sh:16-19` runs `gh auth setup-git` on every container start (after `GITHUB_TOKEN` is in env, before the loop process starts). This writes `credential.https://github.com.helper` to `~/.gitconfig` and is idempotent — a base-image refresh cannot silently strip it.

**Startup soft-check:**

When `[publication] enabled = true`, `check_secrets()` runs `gh auth status --show-token` (5s timeout, best-effort) and emits a WARNING if `repo` is not in the output. Non-blocking; swallows on any error. See [proposal 039](proposals/publication/039-github-token-scope-elevation.md).

## Docker Setup

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage build — `base` (Python 3.14, git, gh CLI, dev tools), `production` (`maestro` user UID 1002, gosu for privilege drop) |
| `docker-compose.yml` | Two services: `loop` (autonomous agent) and `dashboard` (web UI on port 8420), both targeting `production` stage |
| `scripts/docker-entrypoint.sh` | Runs as root to fix bind-mount permissions, then drops to `maestro` user via gosu before running the application |

**Volumes:**
- `.:/workspace` — the Maestro repo itself (bind-mounted so code changes are visible; `:ro` for dashboard). Persistent state lives inside at `./data/` and is created by `Config.ensure_dirs()` on first run.
- `./data:/workspace/data` — dashboard-only writable bolt-on so the `:ro` workspace mount can still persist plan status / messages.
- `/dev/null:/workspace/.env:ro` — hides the `.env` secrets file from inside the container

**Gate execution lease slot directory (`[loop] gate_lease_enabled`, default on).** The lease bounds *cross-checkout* gate concurrency by probing a shared set of slot-lock files under `[loop] gate_lease_slot_dir` (default `~/.maestro/gate_slots`). The loop container mounts only `.:/workspace`, so that default `~/.maestro/gate_slots` resolves inside the container and is **container-private** — with the lease on by default, loop-side gate runs create their slot files there, and neither the loop nor host interactive runs count the other's slots. To make loop-side and host runs share one slot set, either bind-mount the host slot directory into the loop container or point `gate_lease_slot_dir` at a path under `/workspace` (e.g. `/workspace/data/gate_slots`). Setting `gate_lease_enabled = false` rolls the lease back: no slot files are created, so no mount change is needed.

**Bound-aware memory reserve (`[loop] gate_lease_cgroup_reserve_enabled`, default off).** The lease's worker grant holds back `SYSTEM_RESERVE_MIB` (~1100 MiB) for the OS and the non-agent baseline — processes that live *outside* a loop cgroup, and whose residency a container's `memory.max - memory.current` headroom has already netted out. With this flag on, that reserve is applied only when the measured headroom was host-bounded, so a gate run inside a small memory-limited container is no longer floored to a single serial worker by a double-counted baseline. Off (the default) the grant is exactly as it is today. Either way every `gate_runs.jsonl` row records the `available_mib` and `bounded_by` measured at admission, so the flip can be made against measured evidence. The cgroup bound is not container-only: the headroom is read as the tightest `memory.max - memory.current` along the process's own cgroup path, so on a bare host whose `user-<uid>.slice` carries a `memory.max` the lease is `cgroup`-bounded too. With the flag off, the reserve is then subtracted from a slice headroom that already excludes the OS baseline (a double count), and `memory.current` also charges reclaimable page cache, so a slice that is mostly cache can floor the grant to one serial worker. That is conservative (anti-OOM) but costs throughput; enabling the flag on such a host removes the double count, while netting reclaimable `inactive_file` out of `memory.current` remains an open option.

**Host-wide gate slots (`scripts/gate-slot.sh`).** When several checkouts or agents gate on one host, wrap each ruff / mypy / test command in `scripts/gate-slot.sh`. It sizes the number of concurrent gate runs, and the CPUs each owns, from the host's usable CPUs (affinity grouped by SMT sibling, capped by the tightest CFS quota on the process's cgroup path) and total memory — the smaller of `MemTotal` and the tightest finite `memory.max` on the process's cgroup path (for example `user-<uid>.slice` on a bare host) (`maestro.gate_capacity.measure_gate_slots`), locks a slot CPU by CPU under `GATE_LOCK_DIR` (default `$HOME/.maestro/gate-slot-locks`; every checkout and agent on the host must share it), and runs the command under `taskset` on that slot, so the lease and `-n auto` size to the slot and concurrent runs never share a CPU. It needs `flock` (util-linux) and, to pin, `taskset`; exit 75 means every slot is busy (re-run), exit 125 that the wrapper itself cannot run; any other code is the gated command's own.

**Usage:**
```bash
docker compose build
docker compose up -d              # Start loop + dashboard
docker compose logs -f loop       # Follow loop output
docker compose stop               # Graceful shutdown (SIGTERM, may wait minutes for in-flight phase)
```

`PYTHONPATH=/workspace` in the container ensures Python imports from the bind-mounted workspace, so code updates (after `git pull` + container restart) are picked up without rebuilding the image.

**Important:** The `maestro shutdown` and `maestro kill` CLI commands use PID files and only work within the same process namespace. In Docker, use `docker compose stop` for graceful shutdown and `docker compose down` to remove containers. Graceful stop respects the loop's `stop_grace_period` (1800 s) so the in-flight phase + SDK call can finish before the container exits; the wait ends as soon as `run_loop` returns, so a deploy typically blocks for the remainder of the current phase only.

**Graceful stop can take several minutes.** The `loop` service has `stop_grace_period: 1800s` so that an in-flight phase (`contemplate` / `evolve` / `run_task`, routinely 5–10 min) and its SDK call complete naturally before the container exits. `docker compose stop loop` (and `scripts/safe-deploy.sh`, which passes `-t 1800` explicitly) will block until the phase returns. In practice the container exits the moment `run_loop` finishes — the 30 min ceiling only fires if the loop is genuinely stuck.

## Restart Policy

| Service | Policy | Behaviour on crash |
|---|---|---|
| `loop` | `restart: "no"` | **Stays exited.** Thomas triages before anything resumes. |
| `dashboard` | `restart: unless-stopped` | Auto-restarts so the UI stays reachable during triage. |

The loop is deliberately **not** auto-restarted on failure. An auto-restart would mutate `lifecycle.json` / `loop.heartbeat` / the journal the moment it came back up, burying evidence and opening duplicate crash issues on every cycle. When the loop exits non-zero, it stays down until Thomas manually brings it back:

```bash
docker compose ps                    # confirm loop is exited
docker compose logs --tail=200 loop  # inspect crash
docker compose start loop            # resume after triage
```

The manual restart triggers the normal "Crash Handling" path below (unclean-shutdown detection → log warning + Telegram + journal milestone), so the crash signal is produced on the restart, not on the original crash.

**Not just crashes — reboots too.** Because `restart: "no"` is unconditional, the loop container also does **not** auto-start after a host reboot or a Docker daemon restart — Docker never starts a `restart: "no"` container on daemon startup, regardless of how it last exited. So a clean host reboot leaves the loop sitting silently `exited` for as long as nobody looks, with no alert and no obvious cause. Bring it back the same way you would after a crash — `docker compose up -d` (or `docker compose start loop`), or a full `scripts/safe-deploy.sh` run.

**Expect a clean-stop restart, usually.** A host reboot or Docker daemon shutdown stops the container with SIGTERM first, and `_on_shutdown_signal` eagerly persists `last_shutdown` to `lifecycle.json` the moment it runs. So even though the daemon's own shutdown timeout is far shorter than the loop's 1800 s `stop_grace_period` — and the loop only breaks at a phase boundary, so the daemon usually SIGKILLs it mid-phase — the marker is already on disk, and the post-reboot startup logs a clean stop plus a "Waking up after ... of downtime" journal milestone (see [Eager Shutdown Marker](loop-lifecycle.md#eager-shutdown-marker-sigterm-during-long-phase-protection)). A crash report after a reboot is the exception: the post-reboot startup takes the "Crash Handling" path below (and fires the `Maestro crashed (...)` ping) only when no SIGTERM arrived at all (hard power loss, a forced reboot) or the kill landed while the event loop was blocked in synchronous work (e.g. a synchronous quality-gate run) before the signal handler could run. Confirm such a ping is the reboot, not a fault, by matching the reported phase/iteration against the reboot time in `docker compose logs loop`.

## Editable Runtime Install

The `Dockerfile` bakes a non-editable copy of `maestro/` into `/usr/local/lib/python3.14/site-packages/maestro/` so the image carries all third-party dependencies without a network round-trip on every container start. But the bind-mounted `/workspace` must always win over that baked copy at runtime — otherwise a deploy can roll back a previous deploy's *fix* because the new code never runs.

`scripts/docker-entrypoint.sh` enforces this as root, before `gosu maestro`:

**Writable path** (gated by `[ -w /workspace ]` — the loop container):
1. `pip uninstall -y maestro` to drop the baked copy
2. Remove stale `/workspace/maestro.egg-info` and `/workspace/maestro_review.egg-info` directories (setuptools' `os.utime` can fail on bind-mounted filesystems with different ownership/mount semantics)
3. `pip install --quiet --no-cache-dir -e "/workspace[dev,email]" -c /workspace/constraints.txt` — writes a `.pth` file pointing at `/workspace`, so `import maestro` resolves there directly; `-c /workspace/constraints.txt` pins the production dependency set to the same versions the image build and CI resolved so this runtime re-resolve cannot drift; the `email` extra keeps the optional `aiosmtplib`/`aioimaplib` libs installed so the loop container's `MAESTRO_EMAIL_*` surface keeps functioning

**Read-only path** (the dashboard container mounts `.:/workspace:ro`):
- Skips the reinstall entirely, keeping the baked-in image install in place
- Relies on `PYTHONPATH=/workspace` from the Dockerfile to put the bind-mounted workspace ahead of site-packages on `sys.path`
- `pip uninstall -y maestro` lives *inside* the writable branch because running it on the read-only path would leave the container with no maestro install at all

**Sibling distributions (`maestro_sdk/`, `maestro_review/`)**: both are excluded from maestro's own distribution (`pyproject.toml` `packages.find` exclude), so each must be installed as a *real* distribution in its own right — in two places:

- The `Dockerfile` base stage `COPY`s both trees and runs `pip install --no-cache-dir -e "maestro_sdk/" -e "maestro_review/"` **before** `pip install --no-cache-dir -c constraints.txt ".[dev,email]"`, matching the sibling editable installs `.github/workflows/quality-gates.yml` performs — `maestro_sdk/` in the gates job and `maestro_review/` in the separate sibling-packages job, no single line covering both (pinned across the whole workflow by `tests/test_dockerfile_structure.py::test_sibling_editable_installs_match_ci`).
- The entrypoint's **writable path** editable-reinstalls both from the bind-mount (`pip install -e "/workspace/maestro_sdk" -e "/workspace/maestro_review"`) ahead of the `maestro` reinstall, and that failure is **fatal** exactly like the `maestro` one — a container that starts with a stale or absent sibling would fail gate collection instead.

Why a real distribution matters: `PYTHONPATH=/workspace` puts the bind-mounted repo root on `sys.path`, where `maestro_sdk/` is a src-layout directory with no top-level `__init__.py` and so resolves as a half-empty PEP 420 namespace — `import maestro_sdk` succeeds but every submodule import (e.g. `maestro_sdk.mcp_config`, which `maestro/providers/cli_paced.py` needs) raises, aborting in-container gate runs at collection. A regular or static-`.pth` editable install puts the sibling on `sys.path` as a *path entry*, which beats the `/workspace/maestro_sdk` namespace portion; a finder-hook editable install would sit behind `PathFinder` and lose to it.

**Container smoke checks for the sibling install**: both only mean anything with the repo bind-mounted at `/workspace` under `PYTHONPATH=/workspace` — a bare `docker run` has an empty `/workspace` and passes either way. Both need `--entrypoint` / `exec` because the entrypoint ends in `exec maestro "$@"`.

```bash
# 1. The baked base-stage install (the path the read-only and MAESTRO_SKIP_PIP_SYNC
#    containers run). Exits 0 when both siblings are real distributions.
docker compose run --rm --entrypoint python loop -c "import maestro_sdk.mcp_config, maestro_review"

# 2. On the running loop container: the bind-mount reinstall won over the namespace
#    shadow, and gate collection reaches the modules that import the sibling.
docker compose exec loop python -c "import maestro_sdk; print(maestro_sdk.__file__)"   # a path under /workspace/maestro_sdk/src/
docker compose exec loop python -m pytest tests/test_provider_cli_paced.py tests/test_tool_adapter.py --collect-only -q -p no:cacheprovider   # no ERROR row
```

**Failure handling**: Both `pip install -e` failure and the post-install `python -c "import maestro, maestro.cli"` sanity check are **fatal**: the container exits non-zero, the healthcheck fails, and `safe-deploy.sh` rolls back. The sanity check also prints `maestro.__file__` to container logs so the loaded source path is grep-able.

## Bind-Mount Permissions

`/workspace/data` lives inside the workspace bind-mount from the host and may contain subdirectories left behind by earlier container runs whose ownership drifted from the `maestro` user (UID 1002).

`scripts/docker-entrypoint.sh` runs a root phase before `gosu maestro` that explicitly creates and `chown -R 1002:1002`s the known write-heavy subdirectory `$MEMORY_DIR/logs` (for app + SDK logs) in addition to a non-recursive `chown` on `$MEMORY_DIR` itself. `$MEMORY_DIR` defaults to `/workspace/data` inside the container.

This is the only place in the system with the privilege to fix existing subdirectory ownership: `Config.ensure_dirs()` runs later as the `maestro` user, so its `mkdir(parents=True, exist_ok=True)` silently no-ops on a pre-existing directory and cannot correct a stale owner.

All `chown` lines use `2>/dev/null || true` so a chown failure on an edge-case filesystem never blocks startup. If this list grows in the future (e.g. a new persistent queue directory), the entrypoint must be updated in lock-step.

The same root phase also seeds an empty, writable `~/.claude.json` (`/home/maestro/.claude.json`) when absent and `chown`s it to UID 1002, so the Agent SDK's Claude CLI has a file to persist config into. Without it the CLI's "save config with lock" step aborts with `ENOENT: no such file or directory, lstat '/home/maestro/.claude.json'` the moment it needs to write (auth refresh, a settings change). The `[ ! -f ]` guard keeps the seed idempotent and never clobbers a live-session config.

## Continuous Deployment

Production deployment is automated but runs **on the production host**, not on a CI runner. A deploy starts from a `production` GitHub deployment record — the *intent* — which an operator creates by hand (e.g. `gh api repos/<owner>/<repo>/deployments -f ref=main -f environment=production -F auto_merge=false -F 'required_contexts[]'`) or from a deploy workflow of their own after Quality Gates pass on `main`; this repository ships no such workflow. The host-local systemd deploy agent (`scripts/deploy-agent.sh`, installed and enabled out-of-band — see [deploy/systemd/README.md](../deploy/systemd/README.md#the-deploy-agent-executable-units)) polls that record and, when its target sha is `origin/main`'s tip, runs `scripts/safe-deploy.sh` locally for a blue-green rollout with automatic rollback. Running on the host, the agent owns the ≤30-minute graceful-stop window, so no dropped connection can sever a deploy mid-flight.

**Trigger:** a `production` deployment record whose target sha is `origin/main`'s tip.

**What `safe-deploy.sh` does, once the host agent runs it:**
1. **Git config allowlist and hardened git** — before its first git command, runs `python3 -I scripts/deploy_git_config_check.py`, which refuses the deploy (exit code 4, nothing changed; persistent, so the deploy agent keeps its attempt marker and does not retry) when `.git/config` holds a key outside `DEPLOY_GIT_CONFIG_ALLOWLIST`, `remote.origin.url` is not the expected origin, `.git/info/attributes` is non-empty, or a `.git/commondir` / `.git/config.worktree` redirects what git reads — and runs the same check again before the fetch (exit 4) and once the loop is stopped, right before `reset --hard` (exit 2, loop left stopped, tree untouched); every git command then runs through a shell `git` function adding `-c safe.bareRepository=explicit -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.sshCommand= -c credential.helper=` plus one fixed inline credential helper, scoped to `https://github.com`, that answers with `MAESTRO_DEPLOY_GIT_TOKEN` (set from `GITHUB_TOKEN`), so no planted config key or inherited helper runs on the host through `safe-deploy.sh`'s own git calls. Residual: `deploy-agent.sh`'s pre-flight `git rev-parse HEAD` / `git ls-remote origin` run unhardened in the same checkout before the check, so a planted transport key can still run there; also until the deploy-clone cutover, the checker and `safe-deploy.sh` themselves run from the agent-writable checkout, so the loop could edit them before the stop; the proposal accepts this until the deploy moves to the deploy clone
2. Tags the current Docker images as `rollback` for quick restoration
3. **Fetches and pins the target** — a read-only `git fetch origin main`, pinning `FETCH_HEAD` as the deploy target; the working tree is not touched yet, so source never changes under a running loop
4. **Pre-deploy disk-space guard** — reads free space on the Docker root and, if it is below `MAESTRO_DEPLOY_MIN_FREE_KB` (default 5 GiB), aborts *before* stopping the loop (exit code 3), leaving the loop running rather than stranded `Exited (0)` on a build that would die on `No space left on device`. Override the threshold with the `MAESTRO_DEPLOY_MIN_FREE_KB` env var.
5. **Pre-deploy notify** — runs `maestro notify-pre-deploy` inside the running loop container, so the pending restart is recorded before the loop stops (best-effort — a failure is logged and the deploy continues; skipped when the loop isn't running)
6. Gracefully stops the loop, if running (SIGTERM, waits up to 30 min for the in-flight phase + SDK call to finish; exits as soon as `run_loop` returns)
7. After confirming the loop is stopped (otherwise it refuses to touch the tree and rolls back), `git reset --hard` to the pinned target (then `git clean -fd`); if `scripts/safe-deploy.sh` itself changed, the script re-execs its new version
8. Rebuilds the Docker image only if `pyproject.toml`, `Dockerfile`, `scripts/docker-entrypoint.sh`, or `constraints.txt` changed, or the image's `pyproject.sha256` label no longer matches the workspace
9. Runs two smoke tests against a throwaway snapshot volume of the memory directory (both containers set `MAESTRO_SKIP_PIP_SYNC=1` so the entrypoint skips its workspace-dependency resolve — the image already matches the current `pyproject.toml`, so the resolve would be a no-op taking several seconds per run):
   - **Preflight** — `maestro preflight` validates imports, config, secrets, schema, and state files
   - **Single iteration (smoke mode)** — `maestro loop --smoke --max-iters 1` runs the full loop phase dispatch against the snapshot. In smoke mode `contemplate` runs with the same SDK settings as production (effort, model, session reuse, `update_self_model`), so one real end-to-end Claude SDK round-trip catches deploy-time credential, network, and model drift; the phases that do external work or downstream evolution — `respond_to_messages`, `implement_approved_plans`, `check_merged_plans`, `process_rejected_plans`, `generate_task`, `evolve`, and `dream` — are skipped. Phase orchestration, heartbeat, startup notification, and shutdown paths all still execute.
10. Starts services with the new build (`docker compose up -d`, then force-recreates the dashboard)
11. Waits for health checks to pass (up to 5 minutes)
12. On any failure: **automatically rolls back** to the previous image and code

**Rollback behavior:**
- Code is reset to the pre-deploy commit via `git reset --hard $ROLLBACK_HEAD`
- Docker images are restored from the `rollback` tag
- Services are restarted from the rolled-back state
- Exit code 1 = rollback succeeded, exit code 2 = rollback itself failed, or a git config was refused after the loop stopped (no reset attempted — on re-exec, before the forward reset, or inside rollback before its reset, where services are explicitly stopped; manual intervention needed), exit code 3 = deploy aborted before any change (e.g. low disk) with the loop left running — nothing was deployed or rolled back, and the agent re-drives it; exit code 4 = git config refused before any change, loop left running, not retried until a human fixes `.git/config`

**Failure visibility:** the deploy runs autonomously, so nobody watches for it. A *deploy* failure is the **host agent**'s to surface (guarantees pinned by `tests/test_deploy_agent.py`); a failure to create the record is whoever created it's to surface, since the agent never runs:
- The agent stamps the `production` GitHub deployment status (`in_progress` → `success`/`failure`/`error`) as it drives the deploy, so a failed deploy is visible on the repo's Environments page without opening a log. Whatever creates the record should write no status itself — the agent owns the lifecycle, so nothing else can stamp a premature "success".
- On a non-zero `safe-deploy.sh` exit the agent pushes a best-effort Telegram alert using the same `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` vars `config.py` reads, escalating exit code 2 (rollback-also-failed) to a distinct manual-intervention headline and exit code 3 (aborted before any change, loop left running) to a distinct "nothing deployed" headline. Missing creds or a failed send degrade to a warning, never a hard failure.

**Setup:**
- Install and enable the host deploy agent (its service + timer) per [deploy/systemd/README.md](../deploy/systemd/README.md#the-deploy-agent-executable-units). Set its `WorkingDirectory=` (or a `DEPLOY_DIR` env var, default `$HOME/maestro`) to the repo clone on the VPS, run it as the deploy user with Docker permissions, and give it a `GITHUB_TOKEN` to poll deployment records and stamp their status.
- Optional: set the `MAESTRO_DEPLOY_MIN_FREE_KB` env var in the agent's environment to override the pre-deploy disk-space threshold (default `5242880`, i.e. 5 GiB). When free space on the Docker root drops below it, the deploy aborts *before* stopping the loop (exit code 3), leaving the loop running rather than dying mid-deploy on `No space left on device`.

**Concurrency:** the host agent is single-flighted by `flock` so an activation overlapping a still-running deploy is a no-op. No CI job runs on the production host, so nothing contends with the deploy for the box's Docker daemon or working tree.

### Deploy clone (host trust root — first act)

The deploy clone is the host-side checkout the agent cannot write; every later host script a runbook tells Thomas to run comes from it. Create it once, by hand:

1. `sudo install -d -o <thomas-user> -g <thomas-user> -m 0755 /opt/maestro-deploy`
2. As Thomas: `git clone https://github.com/thomas-sodon/maestro-agent.git /opt/maestro-deploy`. Never copy or clone the loop checkout — its `.git/` is agent-writable.
3. Verify:
   - `stat -c %U /opt/maestro-deploy` prints Thomas's user;
   - `sudo -u maestro test -w /opt/maestro-deploy` fails (non-zero);
   - `find /opt/maestro-deploy ! -user <thomas-user>` prints nothing;
   - `python3 -I /opt/maestro-deploy/scripts/deploy_git_config_check.py --repo /opt/maestro-deploy --expected-origin https://github.com/thomas-sodon/maestro-agent.git` exits 0.
4. These checks do not prove the clone is tamper-proof: when the clone's parent directory belongs to the agent user, the agent can rename or replace `/opt/maestro-deploy` wholesale — a known risk a later owner check closes. No container mounts the clone, and `safe-deploy.sh` does not deploy from it yet; that move is a later increment.

**Pre-merge operator check:** before the first deploy that carries the allowlist, run the checker against `$HOME/maestro` (`python3 -I scripts/deploy_git_config_check.py --repo $HOME/maestro --expected-origin https://github.com/thomas-sodon/maestro-agent.git`) and remove any key it names. This check is mandatory, not advisory: the first deploy carrying this change runs the *old*, unchecked `safe-deploy.sh`, which fetches, stops the loop, runs `reset --hard`, then re-execs the new script. A refused config there takes the re-exec branch and exits 2 ("manual intervention required"), leaving the loop stopped and the tree at the new commit. Recovery: fix the keys the checker names (re-run it until it passes), then re-run the deploy, or restart the loop with `docker compose up -d loop`. Once the new script is live, a refused config at start or before the fetch exits 4 (nothing changed, loop left running; the deploy agent keeps its attempt marker on exit 4, so after fixing the config re-run the deploy by hand or create a new deployment record); one refused after the loop stopped, before the reset, exits 2 with the loop left stopped and the tree untouched — fix the config and re-run the deploy. Also confirm the deploy agent's environment carries `GITHUB_TOKEN`: the inherited `credential.helper=store` is now cleared, so the fixed helper is the fetch's only credential source.

## HTTPS / Reverse Proxy

External access to the dashboard is via `https://maestro.example.com` through an nginx reverse proxy with a Cloudflare origin SSL certificate. The nginx config lives in `nginx/maestro.conf` (symlinked on the server to `/etc/nginx/sites-enabled/`). The dashboard container binds to `127.0.0.1:8420` only — not exposed externally.

Traffic flow: Client → Cloudflare edge → nginx (port 443, origin cert) → localhost:8420 (dashboard)

See [docs/vps-setup.md](vps-setup.md) for full server setup instructions including nginx, SSL certs, firewall, and the host deploy-agent install/enable.

## Runtime Contract

`deploy/runtime_contract.toml` declares what the production container must
provide — binaries, the first-party sibling distributions, writable paths,
cgroup and `/proc` readings, the runtime identity, the shell toolbelt, and the
MCP launcher packages (proposal 141). Check the real image against it with:

```bash
docker compose run --rm loop runtime-contract          # table
docker compose run --rm loop runtime-contract --json   # machine-readable
docker compose run --rm loop runtime-contract --network  # also resolve launcher packages
```

Each entry reads `ok`, `violated` (required and missing — the detail names how
to provision it), `not-required` (its `required_when` does not hold under this
config/provider/`MAESTRO_ENV`), `host-only` (a host-scoped entry seen from the
container), or `unverified` (a launcher package not resolved because
`--network` was not passed). With `--network`, each required launcher package is
resolved against its launcher's public registry (npm for `npx`, PyPI for `uvx`;
a pinned version must exist exactly) and reads `ok` or `violated`. The command
is read-only, offline unless `--network` (which reaches only those public
registries), needs no secrets, and exits `1` when any required entry is
violated.

The image's `base` stage installs the declared shell toolbelt (`procps` for
`ps`/`pgrep`/`free`, and `jq`), and in-container `docker_root_free` reads the
`/workspace` bind mount's filesystem (the same host root filesystem) when the
Docker root is not mounted, so `docker compose run --rm loop runtime-contract`
on a freshly built image is expected to exit `0` on the container-scoped
entries. `tests/test_dockerfile_structure.py` binds each apt-provisioned
toolbelt entry to the Dockerfile's `apt-get install` list.

`maestro preflight` (which `safe-deploy.sh` runs as its smoke test) and every
command that builds an agent (`_build_agent`: `loop`, `serve`, `run`,
`resume-task`, `contemplate`, `evolve`, `workspace-counterfactual`) also
fold the contract, always offline — a launcher package stays `unverified` and never counts. `[loop] runtime_contract_enforced`
decides what a required violation does: on, preflight prints a `FAIL:` line
naming each entry and exits `1`, so safe-deploy rolls the deploy back before the
container serves an iteration, and startup raises `SystemExit`; off (default),
each violation is logged at WARNING (preflight reports
`runtime_contract: N warning(s)`) and startup continues. A missing or malformed
contract (no `deploy/` in the image) counts as a violation under the same rule.
Because `serve` is covered too, turning the flag on with a violated entry also
stops the dashboard container. An enabled MCP launcher missing from `PATH` is
still refused by the earlier launcher check whatever the flag says.

The contract is declare-before-use: `tests/test_runtime_contract_owner.py`
fails when `maestro/` spells an executable, a kernel/host path
(`/sys/fs/cgroup`, `/proc`, `/var/lib/docker`), or a home-directory dependency
(`Path.home()`, an expanded `~` literal) the contract does not declare — via a
`binary`/`toolbelt` entry, a `readable_fact`'s `paths`, or a `writable_path`'s
`used_by` respectively — and on a path or `used_by` declaration the code no
longer uses. `writable_path` templates expand `{memory_dir}` from the config and
`{home}` from the runtime user's passwd home.

## Preflight Validation

The `maestro preflight` command validates that the system can start without actually running the agent loop:

```bash
maestro preflight
```

Checks performed:
- Configuration loads and validates (TOML + env vars)
- Authentication is configured (API key or OAuth token)
- Memory directory exists and is writable
- State files load without error (self_model, journal, tasks, plans)
- All core modules import successfully

Used by the safe deploy script as the first smoke test before bringing the new build online.

## Crash Handling

When the loop dies without a graceful `shutdown()` (SIGKILL, OOM, unhandled exception), Maestro detects it on the next startup by noticing that `lifecycle.json` has a non-zero `iteration` / pending `next_phase` but no `last_shutdown` timestamp.

**Note:** Because the loop container uses `restart: "no"` (see "Restart Policy" above), "the next startup" is manual — the crash-detection sequence only fires after Thomas runs `docker compose start loop`. Until then, the crash is visible via `docker compose ps` (loop in `exited` state), stale `loop.heartbeat` (dashboard `current_phase` goes `null` after 600s), and `docker compose logs loop`.

On detection, Maestro:

1. **Logs a WARNING** ("Crash detected and reported") to the app log so the event is grep-able alongside the rest of the crash context.
2. **Pings Thomas on Telegram** with the phase and iteration (e.g. `Maestro crashed (phase=contemplate, iter=3). Check app logs for details.`).
3. **Records a journal milestone event** (`triggered_by="crash"`, in `events.jsonl` — operational records never enter the inner-thought journal or a prompt), readable with `maestro journal --events` or the dashboard Journal tab's Events toggle.

Triage relies on the app logs — there is no auto-filed GitHub issue and no LLM-driven root-cause analysis. Crash reporting is best-effort throughout: it will never itself crash the startup path.

Manual end-to-end verification:

```bash
# On a scratch workspace with Telegram secrets set:
echo '{"iteration": 3, "next_phase": "contemplate"}' > ./data/lifecycle.json
maestro loop --max-iters 1
# Expect: a "Crash detected and reported" WARNING in the app log and a
# Telegram ping naming the phase + iteration. No GitHub issue is created.
```

## Backup

`scripts/backup-maestro.sh` is a host-level cron script (not a Docker service):
- Creates hourly timestamped `tar.gz` of `$DEPLOY_DIR/data`, excluding PID files and logs
- Rotates local backups (keeps last 48 = 2 days of hourly snapshots)
- Optionally rsyncs to a Hetzner Storage Box if `HETZNER_STORAGE_BOX` is set

```bash
# Cron setup (on the host, as the user that owns the repo clone; see vps-setup.md §9).
# Runs from the checkout so that the default MEMORY_DIR=$(pwd)/data resolves correctly,
# and logs inside it: /var/log/ is root-owned and not writable by that user.
0 * * * * cd /home/maestro/maestro && mkdir -p /home/maestro/maestro/logs && scripts/backup-maestro.sh >> /home/maestro/maestro/logs/backup.log 2>&1
```

## Quarantining Pre-Fence Test Telemetry

`scripts/quarantine_test_telemetry.py` (proposal 138) moves the test telemetry written before the live-state fence out of `cost_samples.jsonl` and `logs/sdk/`. It moves and never deletes anything: classified rows go to `cost_samples.quarantine.jsonl` and test-shaped session logs to `logs/sdk-quarantine/`, keeping their relative paths.

1. Stop the loop (the stop path of `scripts/safe-deploy.sh`). `--apply` refuses while `maestro.pid` names a live process.
2. Run `python3.14 scripts/quarantine_test_telemetry.py --report --since <date> --until <fence deploy time>`. This writes nothing; it prints the derived test-only caller set, a per-rule, per-caller table, and the maximum quarantined spend in any rolling `budget_rate_window_hours` window (override with `--window-hours`). `--until` stops gate runs after the fence deploy from classifying concurrent production rows as test traffic.
3. Have a human review that table.
4. Run the same command with `--apply` in place of `--report`, and record the before/after row counts it prints.

The script refuses when `sqlite_state_enabled` is on, and it does not process the cold `cost_samples.jsonl.archive.jsonl` sibling.

## Restore

To restore from a backup:

1. Stop Maestro: `docker compose stop`
2. Back up current state: `cp -r ./data ./data.before-restore`
3. Extract backup: `tar -xzf ~/backups/maestro/maestro-YYYYMMDD-HHMMSS.tar.gz -C ./`
4. Start Maestro: `docker compose up -d`

**Warning:** Restoring replaces all persistent state (journal, consciousness, purpose, tasks, evolution plans). The running containers must be stopped first to avoid data corruption.
