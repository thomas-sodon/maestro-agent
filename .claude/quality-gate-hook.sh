#!/bin/bash
# Quality gate enforcement for Claude Code agents.
# Runs as a Stop hook — gates only the packages with modifications.
# Exit 0 = all clear, Exit 2 = keep working (forces agent to fix issues).
#
# The gate command groups and changed-package detection live in
# scripts/quality-gates.sh (single source of truth, shared with the
# /quality-gates and /merge-main slash commands). This hook only adapts
# that script's exit code to the Stop-hook contract.

set -o pipefail

# Consume stdin (hook JSON input)
cat > /dev/null

# Navigate to repo root
cd "$(git rev-parse --show-toplevel)" || exit 0

# Run gates for the changed packages (never browser; no source changes -> no-op).
# All output goes to stderr so the agent sees it; exit 1 from the runner means a
# gate failed -> exit 2 to keep the agent working.
if ! ./scripts/quality-gates.sh changed >&2; then
  echo "Quality gates FAILED. Fix the issues above before completing." >&2
  exit 2
fi

exit 0
