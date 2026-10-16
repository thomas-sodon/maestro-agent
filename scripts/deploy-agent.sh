#!/usr/bin/env bash
# Host-local, pull-based deploy agent.
#
# Watches for a new `production` GitHub deployment record and runs the local
# `scripts/safe-deploy.sh` in place. Because it runs on the host itself it *owns*
# the ≤30-minute graceful-stop window `safe-deploy.sh` opens — no dropped
# controlling connection (as with a remote runner shelling in) can sever a
# deploy mid-flight. It carries `safe-deploy.sh`'s four-outcome exit-code
# contract to a human through the same GitHub deployment-status stamp and
# Telegram alert path the deploy workflow uses today.
#
# Exit codes mirror `safe-deploy.sh` when a deploy actually ran:
#   0 — deploy succeeded, or a no-op/skip: already at target, no deployment yet,
#       the record's sha is not origin/main's tip, or this deployment was already
#       attempted (a prior deploy of it failed or rolled back — not retried)
#   1 — deploy failed, rollback performed
#   2 — deploy failed, rollback also failed (manual intervention required)
#   3 — aborted before any change (e.g. low disk): nothing deployed, loop left
#       running, no rollback needed
#   4 — git config refused before any change (allowlist / origin /
#       info/attributes / commondir): loop left running, persistent — the
#       attempt marker is kept, so it is not auto-retried
#
# SHIPPED DORMANT. This file, its systemd service + timer, and the README note
# land without touching the runner-driven deploy. The agent does nothing until
# its timer is enabled on the host out-of-band — merging this repo does not
# activate it, so the agent and the existing runner path can never race. See
# deploy/systemd/README.md for the (deliberately operator-gated) cutover.
set -euo pipefail

log()  { echo "$(date -Iseconds) [deploy-agent] $*"; }
fail() { log "FAIL: $*" >&2; }

# Deploy directory: `${DEPLOY_DIR:-$HOME/maestro}`, read from a host env var.
DEPLOY_DIR="${DEPLOY_DIR:-$HOME/maestro}"

# Read KEY from the deploy dir's .env tolerating `KEY="val"` / `KEY='val'` and
# CRLF line endings: drop the trailing CR and one layer of surrounding quotes,
# so a quoted or Windows-edited .env still yields a usable value instead of one
# curl/gh rejects. Identical to the deploy workflow's `read_env`, and — like
# scripts/with-env.sh — a literal read that never sources the file.
read_env() {
  sed -n "s/^$1=//p" .env | tail -n1 | tr -d '\r' \
    | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

# Best-effort Telegram alert, mirroring the deploy workflow's alert send. The
# `TELEGRAM_*` creds come from the runner/host env or, failing that, the deploy
# dir's compose .env (the same vars maestro/config.py reads).
send_telegram() {
  local text="$1"
  local tg_token="${TELEGRAM_BOT_TOKEN:-}"
  local tg_chat="${TELEGRAM_CHAT_ID:-}"
  if [ -f .env ]; then
    [ -n "$tg_token" ] || tg_token="$(read_env TELEGRAM_BOT_TOKEN)"
    [ -n "$tg_chat" ]  || tg_chat="$(read_env TELEGRAM_CHAT_ID)"
  fi
  if [ -z "$tg_token" ] || [ -z "$tg_chat" ]; then
    log "WARN: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID unset — no push alert sent"
    return 0
  fi
  # -f: without it curl exits 0 on an HTTP-level API rejection (bad token 401,
  # wrong chat_id 400, blocked bot 403), so the `|| log` fallback never fires and
  # the failure goes fully silent — the exact mode this alert exists to prevent.
  # -f makes such a response a non-zero exit so the warning fallback triggers.
  curl -fsS --max-time 20 \
    "https://api.telegram.org/bot${tg_token}/sendMessage" \
    --data-urlencode "chat_id=${tg_chat}" \
    --data-urlencode "text=${text}" \
    >/dev/null \
    || log "WARN: deploy-agent Telegram alert failed to send"
}

# Any *unexpected* failure (a gh/git/cd error — not the deploy's own 1/2/3, which
# is captured under `set +e` below) trips this trap, so an agent that breaks
# before it can even drive the deploy is never silent. Disarm first so a failure
# inside the handler cannot recurse.
on_err() {
  trap - ERR
  fail "deploy-agent errored unexpectedly (line ${1:-?}) — no deploy driven this run"
  send_telegram "🚨 Maestro deploy-agent errored on $(hostname) before/around a deploy — check the host"
  exit 1
}
trap 'on_err "$LINENO"' ERR
trap 'log "deploy-agent exiting"' EXIT

# Single-flight guard. A short timer interval can fire a second activation while
# the ≤30-min deploy from the first is still running; `flock -n` makes the second
# run a no-op instead of racing two safe-deploy.sh against one Docker daemon and
# one working tree. The lock lives outside the repo so safe-deploy.sh's
# `git clean -fd` cannot remove it mid-deploy; the fd is released automatically
# when this process exits.
LOCK_FILE="${TMPDIR:-/tmp}/maestro-deploy-agent.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "another deploy-agent run holds $LOCK_FILE — exiting (single-flight)"
  exit 0
fi

cd "$DEPLOY_DIR"
log "Deploy directory: $DEPLOY_DIR"

# gh authenticates from GITHUB_TOKEN. Read it literally from .env when the
# environment does not already carry it (mirroring scripts/with-env.sh), never
# sourcing the file.
if [ -z "${GITHUB_TOKEN:-}" ] && [ -f .env ]; then
  GITHUB_TOKEN="$(read_env GITHUB_TOKEN)"
  if [ -n "$GITHUB_TOKEN" ]; then
    export GITHUB_TOKEN
  else
    unset GITHUB_TOKEN
  fi
fi

# Poll the newest `production` deployment. `gh api` fills {owner}/{repo} from the
# CWD's repo clone. The response is a JSON array; parse the newest entry's target
# sha + deployment id with an inline stdlib-`json` block — the same idiom
# safe-deploy.sh uses to read `docker compose ps --format json`. An empty array
# (no production deployment yet) prints nothing, handled as a no-op below.
poll_output="$(gh api \
  "repos/{owner}/{repo}/deployments?environment=production&per_page=1" \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
if not data:
    sys.exit(0)
entry = data[0]
print(entry['sha'], entry['id'])
")"

TARGET_SHA=""
DEPLOYMENT_ID=""
if [ -n "$poll_output" ]; then
  read -r TARGET_SHA DEPLOYMENT_ID <<< "$poll_output"
fi

if [ -z "$TARGET_SHA" ] || [ -z "$DEPLOYMENT_ID" ]; then
  log "No production deployment record found — nothing to do"
  exit 0
fi

# Compare the target against the locally-deployed head. Exiting 0 as a no-op when
# they match is what makes the agent safe to fire on a short interval.
LOCAL_SHA="$(git rev-parse HEAD)"
if [ "$TARGET_SHA" = "$LOCAL_SHA" ]; then
  log "Already at target ${TARGET_SHA:0:12} — no-op"
  exit 0
fi
log "New production target ${TARGET_SHA:0:12} (deployment $DEPLOYMENT_ID); locally at ${LOCAL_SHA:0:12}"

# safe-deploy.sh always lands origin/main's current tip (it fetches and resets to
# FETCH_HEAD), ignoring the deployment record's own sha. Only drive a deploy when
# the record's target IS origin/main's tip: a stale record — a dispatch redeploy
# of an older commit, or main having raced ahead of this record — would otherwise
# make safe-deploy.sh land a *different* commit than the one being reported and,
# because HEAD could never reach TARGET_SHA, redeploy on every tick forever. It
# also refuses to land an origin/main tip that has outrun the newest gate-passed
# `production` record. Skip with a warning; the newer origin/main carries its own
# record for the agent to act on.
#
# Read origin/main's tip with `git ls-remote`, NOT `git fetch` + `git rev-parse
# FETCH_HEAD`. docker-compose.yml bind-mounts this repo into the still-running loop
# container (`.:/workspace`), which issues its own `git fetch origin …` calls; each
# rewrites the single process-global FETCH_HEAD ref — the exact hazard safe-deploy.sh
# documents under issue #2814. A loop fetch landing between our own fetch and the
# FETCH_HEAD read would leave ORIGIN_MAIN_SHA holding whatever the loop last fetched,
# silently defeating this guard (a spurious skip, or worse a mislabeled deploy if the
# stale value happened to equal TARGET_SHA). `git ls-remote` queries the remote and
# prints the tip to its own stdout without touching FETCH_HEAD or any local ref, so
# the read is immune to the concurrent loop. Read-only — moves neither HEAD nor tree.
ORIGIN_MAIN_SHA="$(git ls-remote origin refs/heads/main | cut -f1)"
if [ "$TARGET_SHA" != "$ORIGIN_MAIN_SHA" ]; then
  log "WARN: production target ${TARGET_SHA:0:12} is not origin/main's tip ${ORIGIN_MAIN_SHA:0:12} — safe-deploy.sh would land the wrong commit; skipping until a matching deployment record appears"
  exit 0
fi

# Per-deployment attempt memory. A failed deploy rolls HEAD back (exit 1/2) or
# aborts before touching it (exit 3), so HEAD never advances to TARGET_SHA and the
# HEAD-vs-target no-op above would re-drive the same record every tick — a full
# rebuild + smoke run + loop stop/restart + alert storm, where today's runner path
# is one-shot per push. Remember the deployment id last driven and refuse to
# re-drive it; a genuinely new record — a fresh push, or an operator re-dispatch
# (a new id even for the same sha) — clears the guard and is attempted. Keyed on
# the deployment id, not the sha, so re-dispatching a commit HEAD has not yet
# reached (retrying a deploy that failed or rolled back, so HEAD never advanced) is
# honoured. A re-dispatch of the commit the host is *already at* is a different
# case: the HEAD-vs-target no-op above short-circuits it before this block ever
# runs, so the agent deliberately does not force-rebuild an already-deployed commit
# — that new deployment record is left un-stamped, the accepted cost of the
# idempotent, short-interval-safe no-op above. The exit-3 arm below is the one
# exception to the refuse-to-re-drive rule: that abort is
# transient (nothing deployed, "Free disk and re-run"), so it clears this marker
# to let the same record retry once the resource is freed. The marker lives
# outside the repo so safe-deploy.sh's `git clean -fd` cannot remove it mid-deploy.
ATTEMPT_FILE="${TMPDIR:-/tmp}/maestro-deploy-agent.last-attempt"
LAST_ATTEMPT=""
if [ -f "$ATTEMPT_FILE" ]; then
  LAST_ATTEMPT="$(tr -d '[:space:]' < "$ATTEMPT_FILE")"
fi
if [ "$DEPLOYMENT_ID" = "$LAST_ATTEMPT" ]; then
  log "WARN: already drove deployment $DEPLOYMENT_ID (target ${TARGET_SHA:0:12}) and HEAD is not there — a prior deploy failed or rolled back; not retrying until a new production deployment appears"
  exit 0
fi
printf '%s\n' "$DEPLOYMENT_ID" > "$ATTEMPT_FILE"

# Best-effort GitHub deployment-status stamp. A status-post failure must never
# block or fail the deploy itself, so it degrades to a warning.
post_status() {
  local state="$1"
  local description="$2"
  gh api "repos/{owner}/{repo}/deployments/${DEPLOYMENT_ID}/statuses" \
    -f "state=${state}" \
    -f "description=${description}" \
    >/dev/null \
    || log "WARN: failed to post deployment status '${state}'"
}

# Drive the deploy and report its outcome from inside a single function so the
# whole post-deploy tail is parsed into bash's memory *before* safe-deploy.sh can
# run. safe-deploy.sh does `git reset --hard` + `git clean -fd`, so a deploy that
# lands a change to this very file replaces scripts/deploy-agent.sh on disk while
# this process is still running from it. Bash reads scripts incrementally by byte
# offset, so any post-deploy logic re-read from disk *after* that reset (the
# exit-code capture, the case mapping, the status/Telegram report, the final exit)
# could be stale or garbled — the identical hazard safe-deploy.sh guards against
# for itself via its DEPLOY_REEXEC re-exec. A bash function body is parsed in full
# at definition time (here, before any deploy runs) and thereafter executed from
# memory, so wrapping the run and its reporting in one function keeps the tail
# safe with no re-exec of the agent itself needed.
run_deploy_and_report() {
  local rc STATE HEADLINE

  post_status "in_progress" "Host deploy-agent running safe-deploy.sh"

  # Run the local deploy body. `set +e` so the 0/1/2/3 contract is captured rather
  # than tripping the ERR trap; safe-deploy.sh owns the rollback and the ≤30-min
  # graceful-stop window. From here down this function executes from bash's
  # in-memory parse, so safe-deploy.sh replacing this file on disk cannot garble it.
  set +e
  bash scripts/safe-deploy.sh
  rc=$?
  set -e

  # Map the captured exit code to a GitHub deployment-status state
  # (success/failure/error) and a human headline. The exit-2 and exit-3 headlines
  # reuse the deploy workflow's exact wording so the host agent and the still-live
  # runner path read identically to a human.
  case "$rc" in
    0)
      STATE="success"
      HEADLINE="✅ Deploy succeeded"
      ;;
    1)
      STATE="failure"
      HEADLINE="🚨 Deploy failed (auto-rolled back)"
      ;;
    2)
      STATE="error"
      HEADLINE="🚨🚨 Deploy FAILED and ROLLBACK FAILED — manual intervention required"
      ;;
    3)
      STATE="error"
      HEADLINE="⚠️ Deploy aborted before any change (loop left running) — nothing deployed or rolled back"
      # exit 3 is safe-deploy.sh's *transient* abort-before-any-change (e.g. low
      # disk): nothing was deployed and no rollback happened, and its guidance is
      # "Free disk and re-run". The attempt marker was written before the deploy
      # (above), so without clearing it here the id stays permanently attempted and
      # this gate-passed deploy would silently never land once the resource is
      # freed. Unlike exit 1/2 (a real failure that must not auto-retry), clear the
      # marker so the next tick re-drives the same record once the condition clears.
      rm -f "$ATTEMPT_FILE"
      ;;
    4)
      STATE="error"
      HEADLINE="⛔ Deploy refused: git config outside the allowlist (loop left running) — fix .git/config, then re-run manually"
      # exit 4 is persistent (it holds until a human edits .git/config), so the
      # attempt marker is kept: re-driving it every tick would only re-alert.
      ;;
    *)
      STATE="error"
      HEADLINE="🚨 Deploy exited with an unexpected code ${rc}"
      ;;
  esac

  post_status "$STATE" "$HEADLINE"
  send_telegram "${HEADLINE}
commit: ${TARGET_SHA}
deployment: ${DEPLOYMENT_ID}
host: $(hostname)"

  log "Deploy finished: rc=${rc} state=${STATE}"
  exit "$rc"
}

# The deploy state (DEPLOYMENT_ID, TARGET_SHA, ATTEMPT_FILE) is fully resolved
# above and the whole post-deploy tail is now pre-parsed in the function body, so
# this single call — which ends in `exit` — is the last thing bash reads from this
# file on disk, immune to safe-deploy.sh rewriting it under us.
run_deploy_and_report
