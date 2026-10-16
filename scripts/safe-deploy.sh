#!/bin/bash
# Safe deployment with smoke tests and automatic rollback.
#
# Called by the Deploy GitHub Actions workflow. Expects to run in the
# deployment directory (where the repo is cloned and docker-compose.yml lives).
#
# Exit codes:
#   0 — deploy succeeded
#   1 — deploy failed, rollback performed
#   2 — deploy failed, rollback also failed (manual intervention needed)
#   3 — aborted before any change (e.g. low disk): nothing deployed, loop left
#       running, no rollback needed — distinct from 1 so the deploy alert does
#       not mislabel it "auto-rolled back"; transient, auto-retried
#   4 — git config refused before any change (allowlist / origin /
#       info/attributes / commondir; scripts/deploy_git_config_check.py,
#       proposal 150), checked at start and again before the fetch: loop left
#       running, persistent — not auto-retried; the re-check after the loop
#       stops (before reset) and the re-exec check exit 2 instead
# -E (errtrace) propagates the ERR trap (installed below) into shell functions,
# command substitutions, and subshells, so an unguarded failure inside $(...)
# also triggers rollback rather than exiting silently under set -e.
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo "$(date -Iseconds) [deploy] $*"; }
fail() { log "FAIL: $*" >&2; }

# Hardened git (proposal 150, Step 1). The loop container mounts this checkout
# read-write, .git/ included, so a planted config key could run on the host
# during fetch/reset/clean/diff. The `git` function below shadows the binary so
# every call — rollback, $(...) substitutions, the re-exec path — carries
# neutralising -c flags. safe.bareRepository=explicit stops discovery falling
# back to a planted bare repo in the working-tree root when .git/ is broken
# (its config would bypass the allowlist check). The empty credential.helper= clears every inherited
# helper (the host's `store` included); the fixed inline helper after it, scoped
# to https://github.com so a redirected remote is never handed the token, answers
# `get` with the token in MAESTRO_DEPLOY_GIT_TOKEN so the https fetch still
# authenticates.
DEPLOY_EXPECTED_ORIGIN="https://github.com/thomas-sodon/maestro-agent.git"
export MAESTRO_DEPLOY_GIT_TOKEN="${GITHUB_TOKEN:-}"
export GIT_TERMINAL_PROMPT=0
DEPLOY_GIT_CREDENTIAL_HELPER='!f() { test "$1" = get || exit 0; test -n "$MAESTRO_DEPLOY_GIT_TOKEN" || exit 0; echo username=x-access-token; echo "password=$MAESTRO_DEPLOY_GIT_TOKEN"; }; f'
DEPLOY_GIT_HARDENING=(-c safe.bareRepository=explicit -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.sshCommand= -c credential.helper= -c "credential.https://github.com.helper=$DEPLOY_GIT_CREDENTIAL_HELPER")
git() { command git "${DEPLOY_GIT_HARDENING[@]}" "$@"; }

ROLLBACK_HEAD=""
LOOP_WAS_RUNNING=false
SMOKE_VOLUME=""
# Container path of the loop heartbeat file. MUST match the loop-service volume
# mount + healthcheck path in docker-compose.yml (the `.:/workspace` mount plus
# MAESTRO_WORKSPACE=/workspace, referenced by the loop healthcheck's os.stat()).
# Single-sourced here so the timeout-path diagnostic below cannot drift from the
# compose source of truth.
LOOP_HEARTBEAT_PATH="/workspace/data/loop.heartbeat"

# ---------------------------------------------------------------------------
# Re-exec support: when the deploy script itself is updated by git pull, we
# re-execute the new version so that any new logic (directory creation,
# rebuild triggers, etc.) takes effect.  The DEPLOY_REEXEC env var prevents
# infinite loops and carries state from the first invocation.
# ---------------------------------------------------------------------------
if [ -n "${DEPLOY_REEXEC:-}" ]; then
    ROLLBACK_HEAD="$DEPLOY_ROLLBACK_HEAD"
    LOOP_WAS_RUNNING="${DEPLOY_LOOP_WAS_RUNNING:-false}"
    unset DEPLOY_REEXEC DEPLOY_ROLLBACK_HEAD DEPLOY_LOOP_WAS_RUNNING
    log "Resumed after re-exec (rollback target: ${ROLLBACK_HEAD:0:12})"
    # Jump past pre-deploy and pull — already done by the first invocation.
    SKIP_PULL=true
fi

rollback() {
    # Disarm the ERR trap so this function can never recursively re-enter
    # itself if a future unguarded line inside rollback fails.
    trap - ERR
    fail "Initiating rollback..."
    smoke_snapshot_cleanup
    local rc=0

    # 1. Restore code to previous commit
    # A container mounting the workspace rw (smoke test, `up -d`, a loop that
    # never stopped) may have run since the last check, so re-check before
    # rollback's own git: a refusal skips the reset (rc=1, exit 2), restores
    # the images, and leaves services stopped — restarting them on the
    # un-reverted tree would resume the container that may have planted the key.
    local config_refused=false
    if [ -n "$ROLLBACK_HEAD" ] && ! python3 -I "$(dirname "$0")/deploy_git_config_check.py" --repo . --expected-origin "$DEPLOY_EXPECTED_ORIGIN"; then
        fail "git config refused — rollback git reset skipped, services left stopped, manual intervention required"
        rc=1
        config_refused=true
    elif [ -n "$ROLLBACK_HEAD" ]; then
        log "Reverting code to $ROLLBACK_HEAD"
        git reset --hard "$ROLLBACK_HEAD" 2>/dev/null || {
            fail "git reset failed — manual intervention required"
            rc=1
        }
    fi

    # 2. Restore previous Docker image (if we tagged one)
    if docker image inspect maestro-loop:rollback >/dev/null 2>&1; then
        log "Restoring rollback image"
        docker tag maestro-loop:rollback maestro-loop:latest || rc=1
        docker tag maestro-dashboard:rollback maestro-dashboard:latest 2>/dev/null || true
    fi

    # 3. Restart services (skipped under a refused git config, see step 1)
    if [ "$config_refused" = true ]; then
        # Rollback may be entered after `up -d` or while the loop still runs, so
        # stop explicitly: "left stopped" must hold from every entry point.
        docker compose stop || { fail "docker compose stop failed during refused rollback"; rc=1; }
        fail "services stopped and not restarted — fix the git config, then re-run the deploy"
    else
        log "Restarting services from rollback state"
        docker compose up -d --remove-orphans || {
            fail "docker compose up failed during rollback"
            rc=1
        }
    fi

    if [ "$rc" -ne 0 ]; then
        fail "Rollback completed with errors — check manually"
        exit 2
    fi
    log "Rollback complete — previous version restored"
    # rollback succeeded — drop the now-stale rollback tags
    cleanup
    exit 1
}

cleanup() {
    # Remove rollback image tags (they're just extra tags, not separate images)
    docker rmi maestro-loop:rollback 2>/dev/null || true
    docker rmi maestro-dashboard:rollback 2>/dev/null || true
    # Reclaim what each deploy leaves behind, or the host root fs fills
    # monotonically until a deploy dies on ENOSPC (#3632). `docker rmi
    # ...:rollback` above only drops a *tag*; the image-before-last stays
    # dangling and stale build cache accumulates without bound. Prune both.
    # `docker image prune -f` removes only dangling images — never
    # :latest/:rollback, never an image a running container references — so
    # it is safe on the live host. Use `--filter until=<age>` (not
    # `--keep-storage`, unsupported by the deploy host's Docker 29.2.1 builder
    # prune). Guarded with `|| log` so a prune failure on an otherwise
    # successful deploy is visible without failing the deploy (fail-loud but
    # non-fatal).
    docker image prune -f || log "WARN: docker image prune failed"
    docker builder prune -f --filter until=168h \
        || log "WARN: docker builder prune failed"
    smoke_snapshot_cleanup
}

# ---------------------------------------------------------------------------
# Smoke-test snapshot helpers
#
# To avoid mutating the live memory dir during smoke tests, we copy it into a
# throwaway Docker named volume and mount that volume at /workspace/data when
# running the smoke test containers — this bolt-on overrides the live data
# subdirectory inside the workspace bind-mount. The snapshot is destroyed
# after smoke tests (or rollback, or unexpected exit via the EXIT trap below).
# ---------------------------------------------------------------------------
smoke_snapshot_create() {
    SMOKE_VOLUME="maestro-smoke-snapshot-$$-$(date +%s)"
    log "Creating smoke-test snapshot volume $SMOKE_VOLUME"
    docker volume create "$SMOKE_VOLUME" >/dev/null || {
        fail "Failed to create snapshot volume"
        SMOKE_VOLUME=""
        return 1
    }
    # Populate from real memory dir using the already-present maestro-loop
    # image (no extra image pull). Source mounted read-only so the live
    # memory dir cannot be touched. Run as root with --entrypoint sh to
    # bypass the normal entrypoint (no pip install, no gosu drop), then
    # chown the destination to UID 1002 so the maestro user can write to it.
    docker run --rm \
        --user 0:0 \
        --entrypoint sh \
        -v "$MEMORY_DIR":/src:ro \
        -v "$SMOKE_VOLUME":/dst \
        maestro-loop:latest \
        -c 'cp -a /src/. /dst/ && chown -R 1002:1002 /dst' \
        || {
            fail "Failed to populate snapshot volume"
            docker volume rm "$SMOKE_VOLUME" >/dev/null 2>&1 || true
            SMOKE_VOLUME=""
            return 1
        }
    log "Snapshot populated"
}

smoke_snapshot_cleanup() {
    if [ -n "$SMOKE_VOLUME" ]; then
        log "Removing smoke-test snapshot volume $SMOKE_VOLUME"
        docker volume rm "$SMOKE_VOLUME" >/dev/null 2>&1 \
            || log "Warning: failed to remove snapshot volume $SMOKE_VOLUME"
        SMOKE_VOLUME=""
    fi
}

# Ensure the snapshot volume is removed even on unexpected exit (signal,
# set -e failure during build, etc.). The function is idempotent.
trap 'smoke_snapshot_cleanup' EXIT

# Git config allowlist (proposal 150, Step 1): runs before the first git command
# and before the rollback ERR trap is armed, so a refusal never reaches
# rollback's `git reset`. First run: nothing fetched, notified, stopped or reset
# yet → exit 4 (a persistent refusal, distinct from the transient exit 3 the
# deploy agent re-drives). Re-exec (SKIP_PULL=true): the old script already reset the tree
# and stopped the loop, so no git and no rollback may run → exit 2.
if ! python3 -I "$(dirname "$0")/deploy_git_config_check.py" --repo . --expected-origin "$DEPLOY_EXPECTED_ORIGIN"; then
    if [ "${SKIP_PULL:-}" = true ]; then
        fail "git config refused after re-exec — manual intervention required"
        exit 2
    fi
    fail "git config refused — nothing deployed"
    exit 4
fi

# Make rollback the default for any unhandled failure. The explicit
# `|| { fail ...; rollback; }` guards below remain as intent documentation and
# still cover command-substitution failures if errtrace propagation is ever
# incomplete; this trap closes the gap for every command they don't guard.
trap 'rollback' ERR

# ---------------------------------------------------------------------------
# Pre-deploy & pull (skipped on re-exec — already done by first invocation)
# ---------------------------------------------------------------------------
if [ "${SKIP_PULL:-}" != true ]; then
    log "Starting safe deployment"

    # Record current state for rollback
    ROLLBACK_HEAD=$(git rev-parse HEAD)
    log "Current HEAD: $ROLLBACK_HEAD"

    # Tag current images for rollback (may not exist on first deploy)
    if docker image inspect maestro-loop:latest >/dev/null 2>&1; then
        docker tag maestro-loop:latest maestro-loop:rollback
        log "Tagged current loop image as rollback"
    fi
    if docker image inspect maestro-dashboard:latest >/dev/null 2>&1; then
        docker tag maestro-dashboard:latest maestro-dashboard:rollback
        log "Tagged current dashboard image as rollback"
    fi

    # Check if loop is currently running
    if docker compose ps --format json 2>/dev/null | python3 -c "
import sys, json
for line in sys.stdin:
    svc = json.loads(line)
    if svc.get('Service') == 'loop' and svc.get('State') == 'running':
        sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
        LOOP_WAS_RUNNING=true
    fi

    # Fetch (read-only — does not move HEAD or touch the working tree)
    # Re-check right before the fetch (the loop is still running and can write
    # .git/): nothing fetched, notified, stopped or reset yet → exit 4.
    if ! python3 -I "$(dirname "$0")/deploy_git_config_check.py" --repo . --expected-origin "$DEPLOY_EXPECTED_ORIGIN"; then
        trap - ERR
        fail "git config refused before fetch — nothing deployed"
        exit 4
    fi
    log "Fetching latest code from origin/main"
    git fetch origin main || { fail "git fetch failed"; rollback; }

    # Pin the fetched SHA now, before the stop window. FETCH_HEAD is a single
    # process-global ref shared with the still-running loop, which issues its own
    # `git fetch origin …` calls (merge-truth, fast-forward) during the ≤30-min
    # graceful-stop below — each rewrites FETCH_HEAD. Capturing the SHA here, while
    # FETCH_HEAD still holds the deploy's target, makes the reset deterministic:
    # it lands exactly the commit this deploy validated, never whatever the loop
    # last fetched (issue #2814). Under `set -Eeuo pipefail` + ERR trap, a
    # rev-parse failure trips rollback.
    DEPLOY_TARGET="$(git rev-parse FETCH_HEAD)"

    # Fail-loud pre-deploy disk-space guard (#3632). A deploy builds a fresh
    # loop image and allocates new container layers; on an already-near-full
    # Docker root the `docker compose build` or the later `docker compose up -d`
    # dies on ENOSPC. Because the graceful loop stop below runs first, a
    # full-disk deploy would strand the loop Exited(0) with no room to restart
    # it. Abort *here*, before the loop is stopped, so a low-disk deploy leaves
    # the loop running rather than down — a louder, earlier failure, not loop
    # auto-recovery (the loop's `restart: "no"` posture is deliberately
    # unchanged). Placed above the pre-deploy notify below too (the guard needs
    # no live loop): `maestro notify-pre-deploy` persists a Message + a journal
    # "milestone" in the running loop, so a guard that aborted *after* it would
    # leave the loop a phantom "deploy imminent" note for a deploy that never
    # happened — running it first keeps the "nothing has been deployed" contract
    # below literally true and the journal honest. Read free space on the Docker
    # root, since that is where the build and container layers land (df on the
    # deploy dir may be a different filesystem). Override the threshold via
    # MAESTRO_DEPLOY_MIN_FREE_KB.
    MIN_FREE_KB="${MAESTRO_DEPLOY_MIN_FREE_KB:-5242880}"  # default 5 GiB
    # A non-numeric override would make the `-lt` test below error out and the
    # elif evaluate false — silently disarming the guard, the opposite of its
    # fail-loud purpose. Validate and fall back to the default so a typo'd
    # override keeps the guard armed (and the typo visible in the deploy log).
    case "$MIN_FREE_KB" in
        ''|*[!0-9]*)
            log "Warning: non-numeric MAESTRO_DEPLOY_MIN_FREE_KB='${MAESTRO_DEPLOY_MIN_FREE_KB:-}' — using default threshold 5242880 KB"
            MIN_FREE_KB=5242880
            ;;
    esac
    DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
    [ -n "$DOCKER_ROOT" ] || DOCKER_ROOT=/var/lib/docker
    AVAIL_KB="$(df -k --output=avail "$DOCKER_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9')" || AVAIL_KB=""
    if [ -z "$AVAIL_KB" ]; then
        log "Warning: could not read free space on Docker root $DOCKER_ROOT — skipping disk-space preflight"
    elif [ "$AVAIL_KB" -lt "$MIN_FREE_KB" ]; then
        fail "No space left-class abort: only ${AVAIL_KB}KB free on Docker root $DOCKER_ROOT (need ${MIN_FREE_KB}KB) — refusing to stop the loop for a deploy that would fail on ENOSPC. Free disk and re-run; the loop is left running."
        # Disarm the ERR trap and exit without rollback: the loop has NOT been
        # stopped, NOT been notified, and nothing has been deployed, so there is
        # nothing to roll back — and rollback's own `docker compose up -d` would
        # itself fail on a full disk. Exiting here (loop untouched) is the whole
        # point of the guard preceding the notify and stop. Exit 3 (not 1) so
        # the deploy alert reports "aborted before any change, loop left
        # running" instead of the false "auto-rolled back" that exit 1 implies.
        trap - ERR
        exit 3
    fi

    # Notify the running loop that a deploy is about to start, so it can
    # contextualise the merges it would otherwise discover only post-restart.
    # Must precede the stop+reset below so the notify reaches a live container.
    # Best-effort: failures are logged but never fatal.
    if [ "$LOOP_WAS_RUNNING" = true ]; then
        log "Notifying running loop of impending deploy"
        docker compose exec -T loop maestro notify-pre-deploy \
            --from-ref "$ROLLBACK_HEAD" \
            --ref "$DEPLOY_TARGET" \
            --run-id "${GITHUB_RUN_ID:-manual}" \
            || log "Warning: pre-deploy notify failed (continuing)"
    fi

    # Gracefully stop the loop. Must precede the reset below: an Executor
    # mid-Edit on a still-running loop would otherwise write to the
    # freshly-reset tree, leaving partial source for the next start to
    # import (issue #1795).
    if [ "$LOOP_WAS_RUNNING" = true ]; then
        log "Stopping loop gracefully (waiting up to 30 min for current phase to finish)..."
        docker compose stop -t 1800 loop || log "Warning: stop loop returned non-zero"
        log "Loop stopped"
    fi

    # Fail-loud guard: if the stop above silently failed (or a future edit
    # removed it) we refuse to reset rather than re-exposing #1795.
    if docker compose ps --format json 2>/dev/null | python3 -c "
import sys, json
for line in sys.stdin:
    svc = json.loads(line)
    if svc.get('Service') == 'loop' and svc.get('State') == 'running':
        sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
        fail "Loop is still running — refusing to touch source tree"
        rollback
    fi

    # Reset working tree to the fetched commit. Safe now: the loop is stopped.
    # Reset to the SHA pinned before the stop window, not the bare FETCH_HEAD —
    # a concurrent loop fetch may have rewritten FETCH_HEAD (issue #2814).
    # Re-check once the loop is stopped, right before the reset: the running loop
    # could write .git/config or .git/info/attributes during the stop window, and
    # the -c flags above do not neutralise a planted filter/include/url key. The
    # loop is stopped and the tree untouched; no git (rollback included) may run
    # under a refused config, so the loop is left stopped → exit 2.
    if ! python3 -I "$(dirname "$0")/deploy_git_config_check.py" --repo . --expected-origin "$DEPLOY_EXPECTED_ORIGIN"; then
        trap - ERR
        fail "git config refused after the loop stopped — tree untouched, loop left stopped, manual intervention required"
        exit 2
    fi
    log "Resetting working tree to pinned deploy target $DEPLOY_TARGET"
    git reset --hard "$DEPLOY_TARGET" || { fail "git reset failed"; rollback; }
    git clean -fd || log "Warning: git clean returned non-zero — some untracked files may remain"

    # Re-exec if the deploy script itself was updated.  Bash reads scripts
    # incrementally by byte offset, so after git-reset replaces the file the
    # running process may execute stale or garbled code.  Re-executing ensures
    # the new version's logic (directory creation, rebuild triggers, etc.) runs.
    if git diff "$ROLLBACK_HEAD" HEAD --name-only | grep -qF 'scripts/safe-deploy.sh'; then
        log "Deploy script updated — re-executing new version"
        export DEPLOY_REEXEC=1
        export DEPLOY_ROLLBACK_HEAD="$ROLLBACK_HEAD"
        export DEPLOY_LOOP_WAS_RUNNING="$LOOP_WAS_RUNNING"
        exec "$0" "$@"
    fi
fi

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
# Check if dependencies changed (pyproject.toml, Dockerfile, entrypoint, or the
# pinned constraints file — a constraints-only bump changes no other watched
# file and does not move the pyproject hash, so without it here the new pin
# would never reach the rebuilt image).
DEPS_CHANGED=false
if git diff "$ROLLBACK_HEAD" HEAD --name-only | grep -qE '^(pyproject\.toml|Dockerfile|scripts/docker-entrypoint\.sh|constraints\.txt)$'; then
    DEPS_CHANGED=true
    log "Dependencies or Dockerfile changed — full rebuild"
fi

# Fallback: rebuild if the image's pyproject.toml hash doesn't match the workspace.
# This catches cases where a previous deploy pulled code changes but the image
# was never rebuilt (e.g. git diff was empty because both commits had the change).
if [ "$DEPS_CHANGED" != true ]; then
    CURRENT_SHA=$(sha256sum pyproject.toml | cut -d' ' -f1)
    IMAGE_SHA=$(docker inspect --format '{{index .Config.Labels "pyproject.sha256"}}' maestro-loop:latest 2>/dev/null || echo "")
    if [ "$CURRENT_SHA" != "$IMAGE_SHA" ]; then
        DEPS_CHANGED=true
        log "pyproject.toml hash mismatch (image: ${IMAGE_SHA:0:12}, current: ${CURRENT_SHA:0:12}) — full rebuild"
    fi
fi

if [ "$DEPS_CHANGED" = true ]; then
    log "Building Docker image"
    CURRENT_SHA="${CURRENT_SHA:-$(sha256sum pyproject.toml | cut -d' ' -f1)}"
    docker compose build --build-arg PYPROJECT_SHA256="$CURRENT_SHA" || { fail "Docker build failed"; rollback; }
else
    log "No dependency changes — skipping image rebuild"
fi

# ---------------------------------------------------------------------------
# Ensure memory directory exists on host (the entrypoint handles subdirectory
# creation and permission fixup as root before dropping to the maestro user)
# ---------------------------------------------------------------------------
MEMORY_DIR="${MAESTRO_MEMORY_DIR:-$(pwd)/data}"
mkdir -p "$MEMORY_DIR"
log "Ensured memory directory exists at $MEMORY_DIR"

# ---------------------------------------------------------------------------
# Snapshot the memory dir so smoke tests do not mutate live production state.
# The snapshot is a throwaway Docker named volume seeded from $MEMORY_DIR and
# mounted at /workspace/data for both smoke test runs, overriding the live
# data subdirectory inside the workspace bind-mount from docker-compose.yml.
# ---------------------------------------------------------------------------
log "Creating snapshot of memory dir for smoke tests..."
if ! smoke_snapshot_create; then
    fail "Snapshot creation failed"
    rollback
fi

# ---------------------------------------------------------------------------
# Smoke test 1: Preflight (import + config + state validation)
#
# Both smoke test containers set MAESTRO_SKIP_PIP_SYNC=1 — the image was just
# rebuilt against the current pyproject.toml (or its hash already matches),
# so the entrypoint's workspace pip install would be a no-op resolve taking
# several seconds per container start for nothing.
# ---------------------------------------------------------------------------
log "Running preflight smoke test (120s timeout)..."
if ! timeout --kill-after=30s 120s docker compose run --rm --no-deps \
        -e MAESTRO_SKIP_PIP_SYNC=1 \
        -v "$SMOKE_VOLUME":/workspace/data \
        loop preflight; then
    fail "Preflight smoke test failed or timed out"
    rollback
fi
log "Preflight passed"

# ---------------------------------------------------------------------------
# Smoke test 2: Single loop iteration (deploy-validation mode)
#
# --smoke runs contemplate with the same SDK settings as production
# (effort, model, session reuse, update_self_model) so the iteration
# validates the real configuration end-to-end and catches creds/network/
# model drift. Phases that do external work or downstream evolution are
# skipped: respond_to_messages, implement_approved_plans, check_merged_plans,
# process_rejected_plans, generate_task, evolve, and dream. Phase
# orchestration, heartbeat, and shutdown paths still execute.
# ---------------------------------------------------------------------------
log "Running single-iteration smoke test (fast mode, 600s timeout)..."
if ! timeout --kill-after=30s 600s docker compose run --rm --no-deps \
        -e MAESTRO_SKIP_PIP_SYNC=1 \
        -v "$SMOKE_VOLUME":/workspace/data \
        loop loop --smoke --max-iters 1; then
    fail "Single-iteration smoke test failed or timed out"
    rollback
fi
log "Single-iteration smoke test passed"

# Smoke tests done — drop the snapshot now (EXIT trap is a safety net).
smoke_snapshot_cleanup

# ---------------------------------------------------------------------------
# Start services
# ---------------------------------------------------------------------------
log "Starting services with new build"
docker compose up -d --remove-orphans || { fail "docker compose up failed"; rollback; }

# Force-recreate the dashboard so static / template / Python edits in
# maestro/dashboard/** always take effect on deploy. Without this,
# `up -d` is a no-op for an unchanged image and the running container
# keeps its in-memory bytecode — so any edit that doesn't bump the
# image hash would otherwise stay invisible until manual restart.
# The dashboard has no in-memory state to preserve (plan/message state
# lives on the ./data bind-mount), so unconditional recreation is safe.
log "Force-recreating dashboard container"
docker compose up -d --force-recreate --no-deps dashboard \
    || { fail "dashboard force-recreate failed"; rollback; }

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
log "Waiting for containers to become healthy..."
HEALTHY=false
for i in $(seq 1 30); do
    if docker compose ps --format json | python3 -c "
import sys, json
unhealthy = []
for line in sys.stdin:
    svc = json.loads(line)
    status = svc.get('Health', '')
    if status and status != 'healthy':
        unhealthy.append(f\"{svc['Service']}: {status}\")
if unhealthy:
    print('\n'.join(unhealthy))
    sys.exit(1)
print('All services healthy')
" 2>&1; then
        HEALTHY=true
        break
    fi
    log "Attempt $i/30: waiting..."
    sleep 10
done

if [ "$HEALTHY" != true ]; then
    fail "Containers did not become healthy within 5 minutes"
    docker compose ps
    docker compose logs --tail=20
    # Dump the loop heartbeat file so it's immediately obvious whether
    # the loop container never wrote it, wrote it and went stale, or
    # wrote it recently (in which case the bug is elsewhere).
    echo "[deploy] Loop heartbeat file status:"
    # Read the heartbeat straight from the host ./data bind-mount: the loop
    # service mounts the repo root at /workspace, so the file on the deploy host
    # is the same one the loop writes, minus the /workspace mount prefix. This
    # works even when the loop container has exited — the most common reason the
    # health check fails — unlike exec-ing into a container that is no longer
    # running (#491). The host-relative path is derived from LOOP_HEARTBEAT_PATH
    # (the single compose-mirrored source of truth, #492) by stripping the mount
    # prefix; both the constant and that strip are regression-tested against
    # docker-compose.yml (tests/test_safe_deploy_heartbeat_path.py), so a compose
    # mount/filename change fails the gate rather than silently drifting this.
    # A genuine read failure routes through `log`, staying non-fatal (the
    # rollback below still runs) but visible in the deploy log.
    HOST_HEARTBEAT_PATH="${LOOP_HEARTBEAT_PATH#/workspace/}" python3 -c "
import os, time
p = os.environ['HOST_HEARTBEAT_PATH']
exists = os.path.exists(p)
print('exists:', exists)
print('mtime:', os.stat(p).st_mtime if exists else None)
print('age_seconds:', round(time.time() - os.stat(p).st_mtime, 1) if exists else None)
" || log "heartbeat diagnostic read failed"
    rollback
fi

# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------
cleanup
log "Deployment successful ($(git rev-parse --short HEAD))"
