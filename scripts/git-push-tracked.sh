#!/usr/bin/env bash
# Push the current branch, setting upstream tracking on the first push, and
# signal a non-fast-forward rejection with a distinct reserved exit code.
#
# commit-push, create-pr, and merge-main each restated, in prose, the same push
# plumbing: pick `git push` when the branch already tracks a remote, otherwise
# `git push -u origin HEAD` to set tracking on the first push. merge-main also
# detected a non-fast-forward rejection (the remote feature branch advanced) and
# branched into an integrate-and-retry loop. This script centralises the
# mechanical part — like scripts/with-env.sh (env loading) and
# scripts/quality-gates.sh (gate definitions) — so the push form and the
# rejection detection never drift, while the conflict-resolution and retry-loop
# judgment stay in the command files.
#
# Usage (from anywhere in the repo): scripts/git-push-tracked.sh
#   Pushes the CURRENT branch (HEAD); takes no arguments.
#
# Exit codes:
#   0   push succeeded
#   3   push was rejected as non-fast-forward — the remote branch has commits
#       this branch does not; the caller integrates (fetch + merge) and retries
#   1   any other failure (auth, protected branch, hook, missing token) — the
#       caller surfaces the error to the user and does NOT retry blindly
#
# Routed through scripts/with-env.sh so GITHUB_TOKEN is loaded. NEVER passes
# --force or --force-with-lease: a non-fast-forward rejection is signalled (exit
# 3) for the caller to integrate, never overridden by force-pushing.
set -uo pipefail

# Reserved exit code for a non-fast-forward rejection, distinct from git's own
# generic failure (1) so the caller can branch on "integration required" without
# parsing output itself. Chosen clear of 1/2 (git/usage) and bash's reserved
# 126-165/255 range.
NFF_EXIT_CODE=3

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Pick the push form: plain `git push` when an upstream is already configured,
# `git push -u origin HEAD` to set tracking on the first push.
if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  push_cmd=(git push)
else
  push_cmd=(git push -u origin HEAD)
fi

# Run through with-env.sh for the token; capture combined output to inspect for
# a non-fast-forward rejection.
output="$("$script_dir/with-env.sh" "${push_cmd[@]}" 2>&1)"
status=$?

if [[ -n "$output" ]]; then
  printf '%s\n' "$output" >&2
fi

if [[ $status -eq 0 ]]; then
  exit 0
fi

# A non-fast-forward rejection means the remote feature branch advanced; the
# caller integrates and retries rather than force-pushing. Match git's three
# rejection-message forms (same set merge-main.md step 7 keyed on).
if grep -qiE 'Updates were rejected because the remote contains work that you do not have locally|fetch first|non-fast-forward' <<<"$output"; then
  exit "$NFF_EXIT_CODE"
fi

# Any other failure (auth, protected branch, hook, missing token): surface it.
exit 1
