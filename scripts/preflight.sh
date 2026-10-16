#!/usr/bin/env bash
# Shared git preconditions for the git-centric slash commands.
#
# commit-push, create-pr, merge-main, and prune-branches each open with the same
# handful of precondition checks, previously restated in prose in every command
# file. This script centralises them — like scripts/with-env.sh (env loading)
# and scripts/quality-gates.sh (gate definitions) — so the checks never drift.
# The command passes the check tokens it needs; the script runs each in order
# and exits non-zero with a one-line diagnostic on the FIRST failed check.
#
# Usage (from anywhere in the repo): scripts/preflight.sh <check> [check ...]
#   Checks:
#     work-tree     inside a git work tree (git rev-parse --is-inside-work-tree)
#     not-main      current branch is not main
#     attached-head HEAD is attached to a branch (not detached)
#     clean-tree    working tree has no uncommitted changes
#     dirty-tree    working tree HAS changes (staged, unstaged, or untracked)
#     github-token  .env exists and defines a non-empty GITHUB_TOKEN
#
# The command keeps the "stop and surface to the user" instruction in prose;
# this script only performs the mechanical check and signals pass/fail.
# Checks fail CLOSED: when a git probe itself fails (not a work tree, unborn
# HEAD), the check fails with a diagnostic rather than passing on empty output.
set -uo pipefail

check_work_tree() {
  local inside
  if ! inside="$(git rev-parse --is-inside-work-tree 2>/dev/null)"; then
    echo "preflight: not inside a git work tree" >&2
    return 1
  fi
  # rev-parse exits 0 from inside the .git/ directory too, printing "false";
  # evaluate the output (not just the exit code) so that case fails closed.
  if [[ "$inside" != "true" ]]; then
    echo "preflight: not inside a git work tree" >&2
    return 1
  fi
}

check_not_main() {
  local branch
  if ! branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"; then
    echo "preflight: cannot determine current branch (not a git repository, or no commits yet)" >&2
    return 1
  fi
  if [[ "$branch" == "main" ]]; then
    echo "preflight: on 'main' — switch to a feature branch first" >&2
    return 1
  fi
}

check_attached_head() {
  # git symbolic-ref --quiet HEAD exits 0 only when HEAD points at a branch ref;
  # it exits non-zero in detached HEAD (and on any probe failure), so the check
  # fails closed. Use it instead of `git rev-parse --abbrev-ref HEAD`, which
  # returns the literal string "HEAD" in detached state and cannot distinguish
  # "on a branch named HEAD" from "detached".
  if ! git symbolic-ref --quiet HEAD >/dev/null 2>&1; then
    echo "preflight: HEAD is detached — check out a branch first" >&2
    return 1
  fi
}

check_clean_tree() {
  local status
  if ! status="$(git status --porcelain)"; then
    echo "preflight: git status failed — cannot inspect the working tree" >&2
    return 1
  fi
  if [[ -n "$status" ]]; then
    echo "preflight: working tree is dirty — commit changes first" >&2
    return 1
  fi
}

check_dirty_tree() {
  local status
  if ! status="$(git status --porcelain)"; then
    echo "preflight: git status failed — cannot inspect the working tree" >&2
    return 1
  fi
  if [[ -z "$status" ]]; then
    echo "preflight: no changes to work with (working tree is clean)" >&2
    return 1
  fi
}

check_github_token() {
  local root env_file
  if ! root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    echo "preflight: not inside a git work tree — cannot locate .env" >&2
    return 1
  fi
  env_file="$root/.env"
  if [[ ! -f "$env_file" ]]; then
    echo "preflight: $env_file not found" >&2
    return 1
  fi
  # Load only GITHUB_TOKEN, not every variable in .env, and read it *literally* —
  # never source or evaluate the file. Docker Compose's `env_file` directive
  # parses .env as literal KEY=VALUE lines; sourcing under bash is strictly more
  # aggressive (expanding $…, running $(…), aborting on an unbalanced quote), so a
  # value Compose accepts could be mangled or execute code during the load. A
  # single-line read of the last `GITHUB_TOKEN=` entry — with surrounding whitespace
  # and one matched pair of surrounding quotes stripped — matches Compose's parse for
  # the common authoring styles (unquoted, `"quoted"`, `'quoted'`, or padded) and
  # keeps no unrelated .env secret in the preflight process or later checks; GitHub
  # tokens are `[A-Za-z0-9_]`, so escape / `$`-expansion inside quotes need not be
  # replicated, and `|| true` keeps an absent entry from tripping pipefail. Mirrors
  # the sibling scripts/with-env.sh narrowing (including this normalization) so the
  # two do not drift.
  GITHUB_TOKEN="$(grep -E '^[[:space:]]*GITHUB_TOKEN=' "$env_file" | tail -n1 || true)"
  GITHUB_TOKEN="${GITHUB_TOKEN#*=}"
  GITHUB_TOKEN="${GITHUB_TOKEN#"${GITHUB_TOKEN%%[![:space:]]*}"}"
  GITHUB_TOKEN="${GITHUB_TOKEN%"${GITHUB_TOKEN##*[![:space:]]}"}"
  case "$GITHUB_TOKEN" in
    \"*\") GITHUB_TOKEN="${GITHUB_TOKEN#\"}"; GITHUB_TOKEN="${GITHUB_TOKEN%\"}" ;;
    \'*\') GITHUB_TOKEN="${GITHUB_TOKEN#\'}"; GITHUB_TOKEN="${GITHUB_TOKEN%\'}" ;;
  esac
  export GITHUB_TOKEN
  if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    echo "preflight: GITHUB_TOKEN is empty after reading $env_file" >&2
    return 1
  fi
}

if [[ $# -eq 0 ]]; then
  echo "preflight: no checks given" >&2
  echo "usage: scripts/preflight.sh <check> [check ...]" >&2
  exit 2
fi

for check in "$@"; do
  case "$check" in
    work-tree)     check_work_tree     || exit 1 ;;
    not-main)      check_not_main      || exit 1 ;;
    attached-head) check_attached_head || exit 1 ;;
    clean-tree)    check_clean_tree    || exit 1 ;;
    dirty-tree)    check_dirty_tree    || exit 1 ;;
    github-token)  check_github_token  || exit 1 ;;
    *)
      echo "preflight: unknown check '$check'" >&2
      exit 2
      ;;
  esac
done
