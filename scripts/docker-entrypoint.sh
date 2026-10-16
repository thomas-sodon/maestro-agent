#!/bin/sh
set -e

# Configure git identity (if provided via env vars)
if [ -n "$GIT_AUTHOR_NAME" ]; then
    git config --global user.name "$GIT_AUTHOR_NAME"
fi
if [ -n "$GIT_AUTHOR_EMAIL" ]; then
    git config --global user.email "$GIT_AUTHOR_EMAIL"
fi

# Mark bind-mounted workspace as safe (container runs as non-root user,
# workspace is owned by the host user — git 2.35.2+ rejects this).
git config --global --add safe.directory /workspace

# Configure gh CLI as git credential helper (uses GITHUB_TOKEN)
if [ -n "$GITHUB_TOKEN" ]; then
    gh auth setup-git 2>&1 || echo "WARNING: gh auth setup-git failed — git push may not work" >&2
fi

# Clean up stale git lock files from previous crashes
if [ -f /workspace/.git/index.lock ]; then
    echo "WARNING: Removing stale .git/index.lock from previous crash" >&2
    rm -f /workspace/.git/index.lock
fi

# Keep $MEMORY_DIR/*.lock files. An flock is released by the kernel when its
# holder exits, so a leftover lock file is harmless; unlinking one is not. The
# loop and dashboard containers both run this entrypoint, so a dashboard restart
# that unlinked a lock the running loop holds would let the next opener flock a
# fresh inode and two writers enter the same read-modify-write (proposal 150).
MEMORY_DIR="${MAESTRO_MEMORY_DIR:-/workspace/data}"

# ---------------------------------------------------------------------------
# Fix bind-mount permissions (runs as root before dropping privileges)
# ---------------------------------------------------------------------------
# The memory directory lives inside the bind-mounted workspace and may be
# owned by the host user rather than UID 1002.  Ensure the maestro user
# (UID 1002) can write to it.
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$MEMORY_DIR/logs/sdk"
    chown -R 1002:1002 "$MEMORY_DIR/logs" 2>/dev/null || true
    # Ensure the maestro user can write state files to the memory dir root
    chown 1002:1002 "$MEMORY_DIR" 2>/dev/null || true
    # Re-own (never unlink) root-created lock files: `docker compose exec loop
    # maestro ...` runs as root and may create <state>.lock as root:root, which
    # the uid-1002 loop then cannot open. chown keeps the inode, so a held flock
    # stays valid.
    find "$MEMORY_DIR" -maxdepth 1 -name '*.lock' -type f -exec chown 1002:1002 {} + 2>/dev/null || true

    # Copy git config to maestro user's home (gosu switches HOME)
    if [ -f /root/.gitconfig ]; then
        cp /root/.gitconfig /home/maestro/.gitconfig
        chown 1002:1002 /home/maestro/.gitconfig
    fi

    # Seed a writable Claude CLI config so "save config with lock" does not
    # ENOENT on lstat('/home/maestro/.claude.json'). Never clobber an existing
    # file (may hold a live session); just ensure it exists and is owned by the
    # maestro user. Best-effort (2>/dev/null || true): under `set -e` a write
    # failure on an edge-case filesystem (read-only /home, full disk) must not
    # abort startup — the mitigation is for a cosmetic runtime nuisance, so a
    # failed seed leaves us no worse than before (file absent, CLI ENOENTs at
    # runtime), matching the sibling chown and the non-root fallback below and
    # the "never block startup" invariant in docs/deployment.md.
    CLAUDE_CONFIG="/home/maestro/.claude.json"
    if [ ! -f "$CLAUDE_CONFIG" ]; then
        # `2>/dev/null` precedes the `>` redirect: POSIX shells apply
        # redirections left-to-right, so if the stdout open fails the shell
        # writes its `cannot create ...` diagnostic to fd 2 — redirect fd 2 to
        # /dev/null *first* or that error leaks before suppression takes effect.
        echo '{}' 2>/dev/null > "$CLAUDE_CONFIG" || true
    fi
    chown 1002:1002 "$CLAUDE_CONFIG" 2>/dev/null || true

    # Sync Python dependencies from the bind-mounted workspace so that packages
    # added after the image was built (e.g. aioimaplib when email support was
    # introduced) are installed before the agent starts.  Running as root here
    # ensures pip can write to the system site-packages directory.
    #
    # We use an EDITABLE install (pip install -e) so the maestro package is
    # imported directly from /workspace/maestro/ via a .pth file, with zero
    # possibility of a stale baked-in copy from the Dockerfile shadowing it.
    # The previous non-editable install copied maestro/ into site-packages on
    # every run, which created a window where (a) the copy could fail silently,
    # or (b) Python's import resolution could pick up the stale site-packages
    # copy instead of the workspace copy — causing a deploy to roll back
    # because the new healthcheck heartbeat code was never reached.
    #
    # Failures here are FATAL: if dependency sync fails, the container exits
    # non-zero rather than silently running stale code. The deploy script will
    # see the unhealthy container and roll back, which is the correct outcome.
    #
    # Smoke-test containers set MAESTRO_SKIP_PIP_SYNC=1 to skip this step —
    # safe-deploy.sh runs them immediately after `docker compose build` against
    # the current pyproject.toml, so the image already has the right deps by
    # construction and the pip resolve is several seconds of wasted startup.
    if [ -n "${MAESTRO_SKIP_PIP_SYNC:-}" ]; then
        echo "Skipping workspace dependency sync (MAESTRO_SKIP_PIP_SYNC set)" >&2
    elif [ -f /workspace/pyproject.toml ]; then
        # The dashboard container mounts /workspace read-only ("dashboard only
        # reads code" in docker-compose.yml), so we cannot reinstall editable
        # there: the `rm -rf /workspace/maestro.egg-info` step fails with
        # EROFS, and even if it were skipped, setuptools' `egg_info` command
        # refreshes the egg-info directory mtime during editable install and
        # also fails with "Cannot update time stamp of directory" on the
        # read-only mount.
        #
        # `[ -w /workspace ]` correctly returns false on a read-only bind mount
        # even when running as root, because access(2) returns EROFS for write
        # checks on read-only filesystems regardless of caller privilege.
        #
        # On the read-only path we leave the baked-in image install in place
        # (so the `maestro` console script is still available) and rely on
        # PYTHONPATH=/workspace from the Dockerfile to put the bind-mounted
        # workspace ahead of site-packages on sys.path, so `import maestro`
        # still resolves to /workspace/maestro and reflects live code edits.
        # This is why `pip uninstall -y maestro` MUST stay inside the writable
        # branch: running it on the read-only path would leave the container
        # with no maestro install at all.
        if [ -w /workspace ]; then
            # Drop any stale baked-in copy first so pip's editable install always
            # wins, even if the previous install was non-editable.
            pip uninstall -y maestro >/dev/null 2>&1 || true
            # Nuke any stale *.egg-info directories from previous installs.
            # setuptools' egg_info command runs `os.utime(egg_info_dir, None)` to
            # refresh the directory mtime; on some bind-mounted filesystems this
            # fails ("Cannot update time stamp of directory 'maestro.egg-info'")
            # even when running as root, because the directory was created by a
            # previous container run with different ownership/mount semantics.
            # Removing it forces setuptools to create a fresh one we can write to.
            rm -rf /workspace/maestro.egg-info /workspace/maestro_review.egg-info /workspace/maestro_sdk.egg-info
            # The sibling packages maestro_sdk/ and maestro_review/ are excluded
            # from maestro's own distribution and installed separately (see the
            # base-stage Dockerfile + CI). Reinstall them editable from the
            # bind-mount first so a code update picks up sibling changes and a real
            # distribution wins over the PYTHONPATH=/workspace PEP 420 namespace
            # shadow, rather than the container running the baked-in copy.
            if ! pip install --quiet --no-cache-dir -e "/workspace/maestro_sdk" -e "/workspace/maestro_review"; then
                echo "FATAL: editable install of maestro_sdk/maestro_review failed — refusing to start with stale code" >&2
                exit 1
            fi
            if ! pip install --quiet --no-cache-dir -e "/workspace[dev,email]" -c /workspace/constraints.txt; then
                echo "FATAL: editable install of /workspace[dev,email] failed — refusing to start with stale code" >&2
                exit 1
            fi
        else
            echo "[entrypoint] /workspace is read-only — keeping baked-in maestro install (PYTHONPATH=/workspace ensures imports still resolve to the bind-mount)" >&2
        fi
        # Sanity check: confirm maestro is loaded from /workspace, not from a
        # leftover site-packages copy. The path is verified programmatically
        # (not merely printed) so a wrong-location import fails loudly and
        # early instead of the container silently running stale code. An
        # explicit `raise SystemExit` (not `assert`) is used so the check
        # survives Python running under -O / PYTHONOPTIMIZE, which strips
        # `assert` statements. The path is still logged on success so it stays
        # visible in `docker compose logs loop` for any future deploy investigation.
        python -c "import maestro, maestro.cli
if not maestro.__file__.startswith('/workspace/'):
    raise SystemExit('loaded from wrong path: ' + maestro.__file__)
print('[entrypoint] maestro loaded from:', maestro.__file__)" \
            || { echo "FATAL: maestro import/path check failed after install" >&2; exit 1; }
    fi

    exec gosu maestro "$0" "$@"
fi
# ---------------------------------------------------------------------------
# Below this point we are running as the maestro user
# ---------------------------------------------------------------------------

# Fallback: if somehow running without root (e.g. USER set in compose override),
# try to create directories but don't fail if we can't.
mkdir -p "$MEMORY_DIR/logs/sdk" 2>/dev/null || \
    echo "WARNING: Cannot create $MEMORY_DIR/logs/sdk — logging may fail until host-side permissions are fixed" >&2

# Best-effort seed of the Claude CLI config in the non-root fallback too, so the
# "save config with lock" ENOENT mitigation is not root-only. Non-fatal.
# Hard-code /home/maestro/.claude.json (as the root block and the gitconfig copy
# above do) rather than "$HOME": gosu does not rewrite HOME on the privilege drop,
# and no USER/ENV HOME is set in the image, so $HOME is /root or empty even in the
# fallback's target scenario (container started directly as the maestro user via a
# compose USER override). The CLI resolves its config to /home/maestro/.claude.json
# regardless, so seeding "$HOME/.claude.json" would create the wrong, unwritable
# file and fail to mitigate the very ENOENT it targets.
# `2>/dev/null` precedes the `>` redirect (see the root-block seed above): a
# failed stdout open must find fd 2 already pointing at /dev/null so its
# `cannot create ...` diagnostic is suppressed rather than leaked to the terminal.
[ ! -f /home/maestro/.claude.json ] && echo '{}' 2>/dev/null > /home/maestro/.claude.json || true

exec maestro "$@"
