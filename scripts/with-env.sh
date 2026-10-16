#!/usr/bin/env bash
# Run a command with the project's .env loaded into the environment.
#
# Replaces the `set -a && source .env && set +a && <cmd>` prefix that the
# slash commands previously inlined before every `gh` / `git push` call.
# Shell state does not persist between separate tool invocations, so the
# prefix had to be repeated dozens of times across the command files; this
# wrapper centralises it in one place.
#
# Usage (from the repo root): scripts/with-env.sh <command> [args...]
#   e.g. scripts/with-env.sh git push -u origin my-branch
#        scripts/with-env.sh gh pr view --json url --jq .url
#
# .env is read from the repo root regardless of the caller's CWD, so the
# wrapper composes with command substitution and heredocs unchanged.
#
# An optional leading --optional-token flag opts into SOFT-token mode: a missing
# .env or an empty GITHUB_TOKEN no longer aborts — the wrapper skips the export
# and execs the wrapped command with whatever ambient auth exists (SSH keys, a
# git credential helper, public access), leaving the wrapped command itself to be
# the arbiter of failure. This is for fetch-only callers (scripts/prune-branches.sh)
# that succeed with zero credentials. Without the flag the wrapper stays
# fail-closed — the default — so commit-push / create-pr / merge-main keep their
# hard token requirement. Least privilege holds either way: only GITHUB_TOKEN is
# ever exported, and only when .env provides a non-empty value for it.
set -euo pipefail

optional_token=false
if [[ "${1:-}" == "--optional-token" ]]; then
  optional_token=true
  shift
fi

root="$(git rev-parse --show-toplevel)"
env_file="$root/.env"

# Load only GITHUB_TOKEN, not every variable in .env, and read it *literally* —
# never source or otherwise evaluate the file. Docker Compose's `env_file`
# directive parses .env as literal KEY=VALUE lines; sourcing it under bash is
# strictly more aggressive — it expands `$…`, runs `$(…)` / backticks, and under
# `set -euo pipefail` an unbalanced quote aborts the wrapper — so a value Compose
# accepts could be mangled or execute code during the load. A single-line read of
# the last `GITHUB_TOKEN=` entry — with surrounding whitespace and one matched
# pair of surrounding quotes stripped — matches Compose's `env_file` parse for the
# common authoring styles (unquoted, `"quoted"`, `'quoted'`, or padded); GitHub
# tokens are `[A-Za-z0-9_]`, so escape / `$`-expansion inside quotes need not be
# replicated. `|| true` keeps an absent entry from tripping `set -e`: the grep
# then yields an empty line.
# The read is skipped when .env is absent so soft mode can proceed token-less.
# The extraction is gated on the `GITHUB_TOKEN=` line *existing* in .env, so the
# three cases stay distinct: a non-empty value is exported; an explicit empty
# `GITHUB_TOKEN=` is dropped via `unset` (exporting GITHUB_TOKEN="" would clobber
# the ambient auth — a credential helper / SSH — soft mode relies on); and a .env
# that omits the line entirely leaves any ambient GITHUB_TOKEN untouched, matching
# the absent-.env path so a soft-mode caller (prune-branches --optional-token) can
# still fall through to ambient auth. The pre-#1636 `source` capture preserved that
# ambient token by inheriting the exported value inside its subshell; the literal
# read sees only the file, so without this line-presence gate an absent entry would
# silently `unset` the ambient token instead.
if [[ -f "$env_file" ]]; then
  _github_token_line="$(grep -E '^[[:space:]]*GITHUB_TOKEN=' "$env_file" | tail -n1 || true)"
  if [[ -n "$_github_token_line" ]]; then
    GITHUB_TOKEN="${_github_token_line#*=}"
    # Normalize the value the way Docker Compose's env_file parser does for the
    # common authoring styles: trim surrounding whitespace, then strip one matched
    # pair of surrounding quotes. So `GITHUB_TOKEN="ghp_…"` / `' ghp_… '` exports
    # the same token the container gets via Compose, instead of the literal
    # quotes/padding reaching gh/git and failing auth (the host-vs-Compose drift
    # this fix removes). Escape / `$`-expansion inside quotes are deliberately not
    # replicated — GitHub tokens are `[A-Za-z0-9_]`, so the pair-strip suffices.
    GITHUB_TOKEN="${GITHUB_TOKEN#"${GITHUB_TOKEN%%[![:space:]]*}"}"
    GITHUB_TOKEN="${GITHUB_TOKEN%"${GITHUB_TOKEN##*[![:space:]]}"}"
    case "$GITHUB_TOKEN" in
      \"*\") GITHUB_TOKEN="${GITHUB_TOKEN#\"}"; GITHUB_TOKEN="${GITHUB_TOKEN%\"}" ;;
      \'*\') GITHUB_TOKEN="${GITHUB_TOKEN#\'}"; GITHUB_TOKEN="${GITHUB_TOKEN%\'}" ;;
    esac
    if [[ -n "$GITHUB_TOKEN" ]]; then
      export GITHUB_TOKEN
    else
      unset GITHUB_TOKEN
    fi
  fi
fi

# Fail closed unless the caller opted into soft-token mode. Default mode keeps
# the original hard contract unchanged: .env MUST exist and yield a non-empty
# GITHUB_TOKEN — a missing .env aborts even when an ambient GITHUB_TOKEN is set,
# so commit-push / create-pr / merge-main keep their hard token requirement.
# Under --optional-token this whole guard is skipped, so the wrapped command runs
# with ambient auth and its own success/failure becomes the arbiter.
if [[ "$optional_token" == false ]]; then
  if [[ ! -f "$env_file" ]]; then
    echo "with-env: $env_file not found" >&2
    exit 1
  fi
  if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    echo "with-env: GITHUB_TOKEN is empty after reading $env_file" >&2
    exit 1
  fi
fi

if [[ $# -eq 0 ]]; then
  echo "with-env: no command given" >&2
  echo "usage: scripts/with-env.sh [--optional-token] <command> [args...]" >&2
  exit 1
fi

exec "$@"
