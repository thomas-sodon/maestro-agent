# maestro_review

An AI code reviewer for GitHub pull requests, built on the Claude Agent SDK. `maestro-review` fetches a PR's diff through the `gh` CLI, asks Claude for a structured review, and posts the findings back as inline review comments (findings that cannot be anchored to a diff line go in the review body). Before posting it drops findings already tracked by an open issue, skips findings that match an existing review thread, and escalates disputed findings to GitHub issues. It does not depend on the main `maestro` package. A standalone copy lives at [thomas-sodon/maestro-review](https://github.com/thomas-sodon/maestro-review).

## Install

From the repository root (Python 3.11+):

```bash
pip install -e "maestro_review/"
```

## Credentials

- **Claude:** `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` in the environment. The package never reads either itself; the Claude Agent SDK runs the Claude Code CLI, which picks the credential up from the environment. The `--require-comment-replies` audit makes no Claude call and needs neither.
- **GitHub:** an authenticated `gh` CLI (`gh auth login`, or `GH_TOKEN` / `GITHUB_TOKEN`). Posting reviews needs `pull-requests: write`; the disputed-finding escalation needs `issues: write`.

## Usage

```bash
maestro-review --repo owner/repo --pr 42                            # review and post
maestro-review --repo owner/repo --pr 42 --dry-run                  # print findings, post nothing
maestro-review --repo owner/repo --pr 42 --require-comment-replies  # read-only reply-coverage audit
```

`--max-diff-chars`, `--max-files` and `--gh-timeout` bound the diff and the `gh` calls; defaults can also come from a `[review]` section in `maestro.toml`.

## Exit codes

- **Review (default):** exits `1` when it freshly produces a comment, meaning any finding of any severity newly posted or reopened. Findings filtered by an open issue, escalated to an issue, or matching an existing thread do not count. It also exits `1` on an infrastructure failure (API error, SDK crash, failed post), and `0` otherwise.
- **`--dry-run`:** applies the same filtering and exits `1` if any finding would be posted or reopened, and `1` on an infrastructure failure (the review itself still calls Claude); if filtering fails it falls back to the raw findings. It exits `0` otherwise.
- **`--require-comment-replies`:** exits `1` if any of the reviewer's own findings (inline or overflow) has no reply, or if the check itself fails; `0` otherwise.

## More

- [docs/review-process.md](../docs/review-process.md) — the review process end to end.
- [CLAUDE.md](CLAUDE.md) — module map, configuration, quality gates and CI integration.
