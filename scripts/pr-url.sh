#!/usr/bin/env bash
# Print the URL of the pull request for the CURRENT branch, or nothing if there
# is none.
#
# commit-push (the "Look up the PR" step) restated, in prose, the same PR-URL
# lookup: after pushing, run `gh pr view --json url --jq .url` and treat the
# "no pull requests found for this branch" exit as "no PR to link" rather than a
# surfaced error. This script centralises that lookup — like scripts/with-env.sh
# (env loading), scripts/preflight.sh (preconditions), and
# scripts/git-push-tracked.sh (push plumbing) — so the query and the no-PR
# fallback never drift, while the report wording stays in the command file.
#
# Usage (from anywhere in the repo): scripts/pr-url.sh
#   Looks up the PR for the CURRENT branch (HEAD); takes no arguments.
#
# Contract:
#   PR exists       -> prints its URL to stdout, exit 0.
#   No PR           -> empty stdout, exit 0. gh exits non-zero and prints "no
#                      pull requests found ..." when the branch has no PR; only
#                      that specific message is swallowed, so the caller captures
#                      an empty string instead of a surfaced failure.
#   Genuine failure -> non-zero exit, gh's stderr re-emitted. A gh/auth/network
#                      breakage, or with-env.sh's own ".env not found" /
#                      "GITHUB_TOKEN is empty" diagnostics, is NOT the no-PR
#                      case: it must NOT be masked as "no PR". The captured
#                      stderr is re-emitted and the script exits non-zero so the
#                      caller surfaces a real error. Mirrors
#                      scripts/git-push-tracked.sh, which likewise distinguishes
#                      an expected condition from a genuine failure rather than
#                      swallowing every non-zero exit.
#
# Routed through scripts/with-env.sh so GITHUB_TOKEN is loaded. No -e: gh's
# non-zero "no PR" exit is expected and handled, not fatal.
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Capture gh's stderr to a temp file rather than discarding it with 2>/dev/null,
# so the URL on stdout stays clean while the diagnostics can be classified: the
# expected "no PR" message is swallowed, any other failure is surfaced.
err_file="$(mktemp)"
trap 'rm -f "$err_file"' EXIT

# Capture the lookup in an `if` condition so gh's non-zero exit does not abort
# the script. On success `url` holds the PR URL and is printed.
if url="$("$script_dir/with-env.sh" gh pr view --json url --jq .url 2>"$err_file")"; then
  printf '%s\n' "$url"
  exit 0
fi

# Lookup failed. gh prints "no pull requests found ..." when the branch simply
# has no PR — swallow only that case (empty stdout, exit 0).
if grep -qi 'no pull requests found' "$err_file"; then
  exit 0
fi

# Any other failure (gh/auth/network breakage, or with-env.sh's own diagnostics):
# re-emit the captured stderr and exit non-zero so the caller surfaces a genuine
# error rather than misreporting it as "no PR".
cat "$err_file" >&2
exit 1
