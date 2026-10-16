# Maestro Review

Standalone AI code review agent for GitHub pull requests, powered by the Claude Agent SDK.

For the full review process documentation, see [docs/review-process.md](../docs/review-process.md).

## Architecture

| Module | Purpose |
|---|---|
| `maestro_review/sdk.py` | Vendor-isolation layer for the Claude Agent SDK. Defines `QueryOptions`, `QueryResult`, `QueryUsage`, `ToolCall`, and an async `query()` function that wraps the SDK's streaming interface. Slim version without session file logging. Includes retry logic for transient errors (exit code failures, connection errors) with exponential backoff (up to 3 retries), stderr capture from the CLI subprocess, and the stderr-drain monkey-patch for `ProcessError` diagnostics. |
| `maestro_review/github.py` | GitHub client wrapping the `gh` CLI. `GitHubClient` handles PR diffs, PR info, inline review posting, review thread fetching (GraphQL), and issue listing/creation. `CodeHost` protocol enables test doubles. `GitHubError` exception for failures. |
| `maestro_review/review.py` | Core review logic. `PRReviewer` orchestrates: fetch diff → truncate → send to Claude → parse findings → filter findings already tracked by issues (any severity) → escalate disputed findings (threads carrying `_DISAGREEMENT_MARKER`) to GitHub issues → deduplicate by threads (exact bold-marker match, with a normalized token-overlap fallback that absorbs wording drift across runs) → post review. Dataclasses: `ReviewFinding`, `ReviewResult`, `ReviewThread`, `PostReviewResult`. Also hosts the reply-coverage audit behind the `--require-comment-replies` gate: `PRReviewer.find_unreplied_comments` returns `UnrepliedComments(inline, overflow)` — read-only, makes no Claude call — backed by the `_is_unreplied_ai_thread` predicate, the `_union_overflow_findings` helper, and the overflow/ack/AI-finding parsers (`_parse_overflow_findings`, `_parse_overflow_ack`, `_AI_FINDING_RE`). |
| `maestro_review/prompt.py` | `CODE_REVIEW_PROMPT` template with placeholders for PR title, body, and diff. |
| `maestro_review/markers.py` | Shared constant module. Defines `_DISAGREEMENT_MARKER` and `_FIXED_LOCALLY_MARKER`, the terminal phrases the comment-remediation surfaces (the human-paced comment-remediation surface and Maestro's autonomous loop) post on review threads; imported by `review.py` so the reviewer's detection cannot drift from the posted wording. Has no package-internal imports to stay outside any future import cycle. |
| `maestro_review/cli.py` | CLI entry point: `maestro-review`. Parses args, loads optional TOML defaults (including `require_comment_replies` via `_load_review_defaults`), runs the review pipeline. In the default mode it exits `1` whenever the reviewer produces a comment — any finding of any severity newly posted or reopened (`post_result.posted + reopened`); filtered/disputed/skipped findings are not freshly produced and do not block. With `--require-comment-replies` it switches to a check-only mode that skips the diff review and posting, calls `find_unreplied_comments`, and exits `1` on any unreplied finding (infrastructure failure also exits `1`). |

## Quality Gates

All code must pass before merging. These are enforced in CI via the `package-quality` job in `quality-gates.yml` (which runs both the `maestro_review/` and `maestro_sdk/` gate sets in one job):

```bash
python3.14 -m ruff check maestro_review/ tests/test_maestro_review/
python3.14 -m mypy maestro_review/
python3.14 -m pytest tests/test_maestro_review/ -v --no-cov
```

| Gate | Tool | Threshold |
|---|---|---|
| Lint | ruff | Zero errors |
| Types | mypy | Zero errors |
| Tests | pytest | All pass |
| Per-test runtime | pytest-timeout | 5s default |

## CLI Usage

```bash
# AI Code Review
maestro-review --repo owner/repo --pr 42           # Review and post
maestro-review --repo owner/repo --pr 42 --dry-run  # Print findings only
maestro-review --repo owner/repo --pr 42 --max-diff-chars 100000 --max-files 75
maestro-review --repo owner/repo --pr 42 --require-comment-replies  # Read-only reply-coverage audit; exits 1 on any unreplied finding
```

## Configuration

CLI arguments take precedence. Defaults are optionally loaded from `maestro.toml` `[review]` section if the file exists in the current directory or package parent:

```toml
[review]
max_diff_chars = 80000
max_files = 50
gh_timeout = 30
require_comment_replies = 0  # 0 = off; default for the --require-comment-replies gate
```

## CI Integration

This repository's own CI does not run `maestro-review` (it needs a Claude credential as a repository secret); an operator can add a workflow that runs it on every PR. Such a review job requires `issues: write` so the disagreement → issue escalation flow can open tracking issues. The **reply-coverage audit** — the check-only `--require-comment-replies` mode, i.e. the read-only `find_unreplied_comments` audit, with no Claude token — belongs as a step *ahead of* the diff review, so it reads the comment state as of this push *before* the reviewer posts this push's findings. The standalone comment processor has been retired (proposal 063): comment handling now lives in Maestro's core loop (`tend_evolution_prs` routes unresolved review threads of every severity — 🐛 bug, 💡 suggestion, ✏️ nit — plus human inline threads and overflow findings into remediation tasks) and the human-paced comment-remediation surface, which adds the prior session's implementation plan as fixing context. `markers.py` stays shared so the reviewer's escalation path keeps detecting the wording those surfaces post.

See [docs/review-process.md](../docs/review-process.md) for the full process, including:

- The reviewer's disagreement → issue escalation flow (replaces the old
  nit/suggestion → issue linking step)
- The comment-remediation surfaces (autonomous loop and the human-paced surface)
  and their validate → fix → reply discipline

## Testing Principles

Tests use mocking — no real network calls, no real filesystem writes, no real subprocess invocations:

- `PRReviewer` tests mock the `CodeHost` protocol and `maestro_review.sdk.query`
- CLI tests mock `PRReviewer` and `asyncio.run`
- Component tests (marked `component`) use a mock HTTP server for the `gh` CLI
- **Per-test runtime gate** — `pytest-timeout` enforces a 5s default per test (configured in `[tool.pytest.ini_options]`). Override with `@pytest.mark.timeout(N)` only when justified; the preferred fix is to mock the slow dependency. `--durations=10` is in `addopts` so the slowest tests are always visible before they trip the gate.

## Development

```bash
pip install -e "maestro_review/[dev]"
pytest tests/test_maestro_review/ -v
```
