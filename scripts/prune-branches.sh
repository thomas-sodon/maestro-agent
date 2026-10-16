#!/usr/bin/env bash
# Prune local branches whose origin counterpart is gone — candidate detection
# plus the SAFE (`git branch -d`) delete pass.
#
# prune-branches restated, in prose, the same mechanical sequence every run:
# capture the protected set (current branch, main/master/develop, worktree-pinned
# branches), `git fetch --all --prune`, enumerate `[gone]`-upstream branches,
# subtract the protected set, print the plan, and run the safe `git branch -d`
# pass. This script centralises that plumbing — like scripts/with-env.sh (env
# loading), scripts/preflight.sh (preconditions), and scripts/git-push-tracked.sh
# (push plumbing) — so the candidate detection and the SAFE-delete invariant never
# drift, while the force-delete judgment stays in the command file.
#
# Usage (from anywhere in the repo): scripts/prune-branches.sh [--dry-run]
#   --dry-run   print the plan (candidates + merged status) and stop without
#               deleting anything.
#
# SAFETY — these invariants are mechanically enforced here, not just asserted in
# prose:
#   * NEVER escalates the safe delete to a force delete. Unmerged branches are
#     refused by `git branch -d` and printed as the "refused" remainder for the
#     caller (the command) to hand to the user; force-deletion is the command's
#     judgment, never this script's.
#   * NEVER deletes a protected branch: the current branch, main/master/develop,
#     or any branch checked out in a worktree.
#   * The fetch runs through scripts/with-env.sh in SOFT-token mode
#     (--optional-token): GITHUB_TOKEN is loaded when .env provides it, but an
#     origin fetchable without one (SSH, public HTTPS, a credential helper) is
#     not blocked on a missing token — the fetch's own success/failure is the
#     arbiter. A failed fetch still aborts (exit 1) rather than pruning against
#     stale data, so a private-HTTPS origin that genuinely needs a token stops.
#
# Output: a "Prune plan" section listing each candidate (name, last commit,
# merged status) — plus a "protected (excluded …)" line for any [gone] branch the
# protected set kept off the list — then, unless --dry-run, a "Results" section
# listing the safe-deleted and the refused (unmerged) branches so the caller
# drives the force-delete step.
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Argument parsing. Only --dry-run is recognised here; --force governs the
# force-delete judgment that stays in the command, so it never reaches us. ---
dry_run=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=true ;;
    *)
      echo "prune-branches: unknown argument '$arg'" >&2
      echo "usage: scripts/prune-branches.sh [--dry-run]" >&2
      exit 2
      ;;
  esac
done

# --- Protected set: never deletion candidates regardless of [gone] status. ---
if ! current_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"; then
  echo "prune-branches: cannot determine current branch (not a git repository, or no commits yet)" >&2
  exit 1
fi

declare -A protected=()
protected["$current_branch"]=1
for name in main master develop; do
  protected["$name"]=1
done

# Branches checked out in a worktree cannot be deleted — collect every
# `branch refs/heads/...` entry from `git worktree list --porcelain`.
while IFS= read -r line; do
  case "$line" in
    "branch refs/heads/"*)
      protected["${line#branch refs/heads/}"]=1
      ;;
  esac
done < <(git worktree list --porcelain)

# --- Fetch with prune: pull upstream movement and clear local remote-tracking
# refs whose origin branches are gone. The [gone] detection below depends on a
# successful fetch, so a failure aborts rather than pruning against stale data.
# Routed through with-env.sh in SOFT-token mode (--optional-token): a missing
# GITHUB_TOKEN no longer blocks this fetch-only cleanup when the origin is
# fetchable without one; a fetch that actually fails still aborts below. ---
if ! "$script_dir/with-env.sh" --optional-token git fetch --all --prune; then
  echo "prune-branches: 'git fetch --all --prune' failed — refusing to prune against stale data" >&2
  exit 1
fi

# --- Identify [gone]-upstream branches, minus the protected set. A [gone] branch
# that is protected (current / main-master-develop / worktree) is collected into
# protected_gone so the plan surfaces it as excluded rather than dropping it
# silently — the command's report step sources that list. ---
# NOTE: rely on the DEFAULT IFS (whitespace) to split each line into refname +
# track. Do NOT set `IFS=` here: that disables splitting, assigns the whole line
# to refname, and leaves track empty so the [gone] test never matches — the helper
# would then find no candidates and silently prune nothing. Git refnames cannot
# contain spaces, so default-IFS splitting is safe; a multi-token track such as
# `[ahead 1, behind 2]` lands wholesale in track and correctly fails the match.
candidates=()
protected_gone=()
while read -r refname track; do
  [[ "$track" == "[gone]" ]] || continue
  if [[ -n "${protected[$refname]:-}" ]]; then
    protected_gone+=("$refname")
    continue
  fi
  candidates+=("$refname")
done < <(git for-each-ref --format='%(refname:short) %(upstream:track)' refs/heads)

# --- Show the plan: name, last commit, and merged-against-trunk status. The
# protected-excluded line surfaces [gone] branches the protected set kept off the
# candidate list, so the command's report can attribute them as skipped. ---
echo "Prune plan:"
if [[ ${#protected_gone[@]} -gt 0 ]]; then
  echo "  protected (excluded — [gone] upstream but never a deletion candidate): ${protected_gone[*]}"
fi
if [[ ${#candidates[@]} -eq 0 ]]; then
  echo "  nothing to prune (no [gone]-upstream branches outside the protected set)"
  exit 0
fi

# --- Resolve the trunk reference the merged status is measured against. The
# question that actually informs the force-delete judgment is "is this work on
# the trunk?", NOT "is it on the branch I happen to have checked out" — so the
# merged set and the status labels below are computed against the default
# branch's freshly-fetched remote-tracking ref, never the checked-out HEAD. Run
# from a feature branch, a HEAD-relative probe falsely reports a candidate
# already merged into the trunk as unmerged, misleading the caller toward an
# unnecessary force-delete. Fail-closed fallback chain mirroring the protected
# long-lived set: prefer the recorded origin/HEAD, then the first existing of
# origin/{main,master,develop}, and only in a trunk-less checkout fall back to
# HEAD — surfaced in the plan below so that degrade is visible, not silent. The
# safe-delete pass itself stays git's own `git branch -d` decision (merged into
# HEAD/upstream); this trunk-relative status is advisory display only.
#
# NOTE: `git symbolic-ref` only reads the symref's CONTENT — it succeeds even when
# the branch it points at no longer exists. Right after this script's own
# `git fetch --all --prune`, a renamed remote default leaves origin/HEAD dangling
# (the prune drops the old origin/<name> but does not repoint origin/HEAD), so the
# origin/HEAD path MUST existence-validate its target with the same
# `git rev-parse --verify --quiet` guard the origin/{main,master,develop} probes
# use; otherwise `trunk` is set to a missing ref, the fallback chain is skipped,
# `git branch --merged "$trunk"` fails, and every candidate is mislabelled 'NOT
# merged' — the very false-unmerged bug (#1535) this resolution exists to prevent. ---
if trunk="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)" && git rev-parse --verify --quiet "$trunk" >/dev/null 2>&1; then
  trunk_display="$trunk"
else
  trunk=""
  for ref in origin/main origin/master origin/develop; do
    if git rev-parse --verify --quiet "$ref" >/dev/null 2>&1; then
      trunk="$ref"
      break
    fi
  done
  if [[ -n "$trunk" ]]; then
    trunk_display="$trunk"
  else
    trunk="HEAD"
    trunk_display="HEAD (no trunk ref resolved)"
  fi
fi
echo "  merged status measured against: $trunk_display"

# Merged-against-trunk set, queried once: a candidate is "merged" iff it appears
# in `git branch --merged "$trunk"` — measured against the trunk, not the
# checked-out HEAD, so a candidate already merged into the trunk but not into the
# current feature branch is not falsely reported as unmerged.
declare -A merged=()
while IFS= read -r branch; do
  [[ -n "$branch" ]] && merged["$branch"]=1
done < <(git branch --merged "$trunk" --format='%(refname:short)')

for branch in "${candidates[@]}"; do
  last_commit="$(git log -1 --format='%h %s' "$branch" 2>/dev/null)"
  if [[ -n "${merged[$branch]:-}" ]]; then
    status="merged into $trunk"
  else
    status="NOT merged into $trunk"
  fi
  echo "  $branch — $last_commit — $status"
done

if [[ "$dry_run" == true ]]; then
  echo
  echo "--dry-run: stopping before the safe-delete pass (${#candidates[@]} candidate(s))."
  exit 0
fi

# --- Safe-delete pass. `git branch -d` REFUSES unmerged branches by design;
# collect successes and refusals separately and NEVER escalate to a force
# delete — the refused remainder is the command's judgment to hand to the user. ---
deleted=()
refused=()
for branch in "${candidates[@]}"; do
  if git branch -d "$branch"; then
    deleted+=("$branch")
  else
    refused+=("$branch")
  fi
done

echo
echo "Results:"
if [[ ${#deleted[@]} -gt 0 ]]; then
  echo "  safe-deleted (${#deleted[@]}): ${deleted[*]}"
else
  echo "  safe-deleted (0): none"
fi
if [[ ${#refused[@]} -gt 0 ]]; then
  echo "  refused — unmerged, NOT deleted (${#refused[@]}): ${refused[*]}"
  echo "  (force-deletion of these is the command's judgment; this script never escalates)"
fi
exit 0
