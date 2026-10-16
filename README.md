# Maestro

> **Archived experiment.** Maestro is no longer developed. The code, docs and proposals are kept as a record of what was tried and what was learned.

## What Maestro is

Maestro is an autonomous agent built on the Claude Agent SDK. It runs an **act → reflect → evolve** loop: it executes tasks, reflects on its own record, and proposes changes to its own code. Every self-change goes through human approval (dashboard or Telegram) before it lands. It keeps a journal of its own inner thought and a self-model that is injected into every call.

For the full architecture and its invariants, see [CLAUDE.md](CLAUDE.md); the module reference is [docs/modules.md](docs/modules.md).

## What we learned

- **Without an external objective, self-directed evolution collapses into self-reference.** Over its life Maestro's evolution loop produced 35 merged plans, 190 rejected and 655 obsolete. Most rejections and prunes were "already done", "duplicate" or "false premise": the planner kept proposing changes to its own machinery, and the human reviewer ended up as the grounding oracle.
- **The revenue thesis was dropped.** The later charter had Maestro pay for itself by running revenue streams. That lineage (charter, net burn, scouting, income, intake, quotes, streams) was retired before any stream earned.
- **Human-steered Claude Code workflows deliver the same pipeline.** The plan → impl → review → PR pipeline in [`.claude/workflows/`](.claude/workflows/), run by a person on demand, does the useful part of what the autonomous loop did, without the loop's cost and drift.

## Architecture highlights

- **Action-gated Bash** — `maestro/tool_gating.py` installs a `PreToolUse` gate that refuses PR merge/ready/open bypasses, foreign pushes and outward acts, in-process and across `cli_paced` subprocesses.
- **Chokepoints** — merge truth (`is_pr_settled`), CI truth (`ci_diagnosis`) and the owed act (`pr_owed_act.classify_pr_owed_act`) each have one owner, so no scope re-derives them privately.
- **Structural AST guards** — `tests/structural_guards.py` and the tests around it enforce sole owners, retired concepts and banned patterns at CI time.
- **Taint fencing** — `maestro/taint.py` is the only way external text enters a prompt, as a fenced `UntrustedText`.

## Getting Started

### Prerequisites

- Python 3.14+
- A Claude API key **or** an OAuth token from a Claude Max/Pro subscription

### Installation

```bash
git clone <repo-url> && cd maestro
# Base install. Add the email extra — ".[dev,email]" — to use MAESTRO_EMAIL_* or run the full test suite.
pip install -e ".[dev]"
```

### Authentication

Maestro needs one of two credential types. Set the chosen variable in your `.env` file or shell environment.

**Option 1 — API key (pay-per-token)**

1. Create an account at [console.anthropic.com](https://console.anthropic.com)
2. Generate an API key
3. Add to `.env`:
   ```
   ANTHROPIC_API_KEY=sk-ant-api03-...
   ```

**Option 2 — OAuth token (Max/Pro subscription, personal use)**

1. Install the Claude CLI:
   ```bash
   npm install -g @anthropic-ai/claude-code
   ```
2. Generate a long-lived token (~1 year):
   ```bash
   claude setup-token
   ```
   This opens a browser for authentication and prints a long-lived token.
3. Copy the token and add to `.env`:
   ```
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
   ```
4. When the token expires, re-run `claude setup-token` to generate a new one.

> If both are set, Maestro's auth check reports `CLAUDE_CODE_OAUTH_TOKEN`, but the Claude CLI bills `ANTHROPIC_API_KEY` first, so cost samples are stamped `metered` and a WARNING is logged. Set only one.

### Configuration

Maestro uses TOML files for configuration and environment variables for secrets only.

**Secrets** (set in `.env` or your shell):

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | One of these | Claude API key (pay-per-token) |
| `CLAUDE_CODE_OAUTH_TOKEN` | is required | OAuth token from Max/Pro subscription |
| `GITHUB_TOKEN` | Yes | GitHub PAT (`contents:write`, `pull_requests:write`, `issues:write`) |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token for notifications |
| `TELEGRAM_CHAT_ID` | No | Telegram chat ID for notifications |
| `MAESTRO_DASHBOARD_TOKEN` | No | Bearer token for dashboard auth (`openssl rand -base64 32`) |
| `MAESTRO_ENV` | No | Environment name — loads `maestro.{env}.toml` overlay |

**TOML configuration** (in `maestro.toml` with per-environment overrides):

| Section | Key | Default | Purpose |
|---|---|---|---|
| `[paths]` | `workspace` | `"."` | Git repo root (~ expanded, relative to cwd) |
| `[paths]` | `memory_dir` | `"data"` | Persistent state directory (relative to cwd / repo root) |
| `[github]` | `repo` | `""` | GitHub repo for evolution PRs (e.g. `owner/repo`) |
| `[dashboard]` | `port` | `8420` | Dashboard port |
| `[dashboard]` | `host` | `"0.0.0.0"` | Dashboard bind address |
| `[cli]` | `verbose` | `false` | Enable debug logging |

See `maestro.toml` for all settings (timeouts, loop intervals, limits). Per-environment overrides are in `maestro.{env}.toml` files, selected by `MAESTRO_ENV`. 

### Try it: `maestro loop --smoke`

`maestro loop --smoke --max-iters 1` runs one fast iteration of the loop (without `--max-iters` the loop is unbounded): only `contemplate` runs with the production SDK settings, and every phase that does external work or downstream evolution is skipped. See [docs/cli.md](docs/cli.md) for every other subcommand.

## Packages

- [`maestro_sdk/`](maestro_sdk/README.md) — a CLI-driving SDK with human-paced keystrokes.
- [`maestro_review/`](maestro_review/CLAUDE.md) — the standalone AI code-review package.

## Web Dashboard

Start it with `maestro serve`, then open `http://localhost:8420`.

From the dashboard you can:

- View Maestro's identity and self-model
- Browse journal entries
- Review evolution plans — **approve** or **reject** proposed code changes
- Monitor task history and metrics
- View live logs with App/SDK source filtering and per-level filtering
- Browse SDK session logs — see the full Claude interaction for each query

## Docker Deployment

Maestro runs in Docker with two services: `loop` (autonomous agent) and `dashboard` (web UI on port 8420). Credentials are read from a `.env` file.

```bash
docker compose build
docker compose up -d              # Start loop + dashboard
docker compose logs -f loop       # Follow loop output
docker compose stop               # Graceful shutdown
```

**Note:** The `maestro shutdown` and `maestro kill` CLI commands use PID files and only work within the same process namespace. In Docker, use `docker compose stop` for graceful shutdown and `docker compose down` to remove containers.

For full deployment details (volumes, backup, restore), see [docs/deployment.md](docs/deployment.md).

## Development

```bash
# Install in dev mode (the email extra keeps the full test suite resolvable)
pip install -e ".[dev,email]"

# Run all quality gates
ruff check maestro/ tests/ && mypy maestro/ && pytest tests/ -v

# Browser tests (dashboard UI in headless Chromium)
pip install -e ".[dev,browser]" && playwright install chromium
pytest tests/browser/ -v -m browser --no-cov --browser chromium
```

All code must pass lint (ruff), type checking (mypy), tests (pytest), and maintain 100% coverage before merging. Browser tests run as a separate CI job.

### AI Code Review

This repository's CI does not run an AI code review. The standalone `maestro_review` package (see [`maestro_review/CLAUDE.md`](maestro_review/CLAUDE.md)) can post inline pull-request comments identifying bugs and providing fix plans or code suggestions, and an operator can add a workflow that runs it on pull requests; such a workflow needs `CLAUDE_CODE_OAUTH_TOKEN` as a repository secret. `maestro-review` exits 1 when it freshly produces any comment or hits an infrastructure failure (API errors, SDK crashes), and exits 0 otherwise.

Install and run independently:

```bash
pip install -e "maestro_review/"
maestro-review --repo owner/repo --pr 42
```

## License

MIT — see [LICENSE](LICENSE).
