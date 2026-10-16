#!/bin/bash
# Backup Maestro's persistent state (identity, journal, purpose, tasks).
# Run via cron on the host: 0 * * * * /path/to/scripts/backup-maestro.sh
#
# Cron user: Should be the same user that owns the data directory (typically
# the deploy user that owns the repo clone). Do NOT run as root unless
# necessary.
#
# Environment variables (optional — set in crontab or source from a file):
#   MAESTRO_MEMORY_DIR  — persistent state directory (default: $(pwd)/data,
#                         i.e. <repo-root>/data when run from the deploy dir)
#   MAESTRO_BACKUP_DIR  — local backup directory (default: ~/backups/maestro)
#   MAX_LOCAL_BACKUPS   — number of hourly backups to retain locally (default: 48)
#   HETZNER_STORAGE_BOX — rsync target for offsite backups (e.g. u12345@u12345.your-storagebox.de)
#   TELEGRAM_BOT_TOKEN  — for failure notifications (optional)
#   TELEGRAM_CHAT_ID    — for failure notifications (optional)
set -euo pipefail

MEMORY_DIR="${MAESTRO_MEMORY_DIR:-$(pwd)/data}"
BACKUP_DIR="${MAESTRO_BACKUP_DIR:-$HOME/backups/maestro}"
MAX_LOCAL_BACKUPS="${MAX_LOCAL_BACKUPS:-48}"
STORAGE_BOX="${HETZNER_STORAGE_BOX:-}"
# Defense-in-depth mkdir for the conventional cron log dir. This helps in
# exactly two cases: (a) manual invocations of this script with no `>>`
# redirect, and (b) recovery on cron firings *after* the directory has been
# created at least once (e.g. by an earlier manual run, the docs §9
# prerequisite, or the inline `mkdir -p` in the crontab entry below).
# It does NOT substitute for the inline `mkdir -p` in the crontab line in
# docs/vps-setup.md §9: cron's shell opens the `>>` redirect *before*
# exec'ing this script, so on a fresh VPS where the log dir is absent the
# redirect aborts with ENOENT and this script body — including this very
# mkdir — never runs. The inline `mkdir -p &&` in §9 is therefore
# load-bearing for the first-cron-firing path and must not be removed.
mkdir -p "${MAESTRO_LOG_DIR:-/opt/maestro/logs}" 2>/dev/null || true

# Notify on failure via Telegram (if configured), then fall back to stderr.
notify_failure() {
    local msg="Maestro backup FAILED on $(hostname) at $(date -Iseconds)"
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        curl -sf -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="$TELEGRAM_CHAT_ID" \
            -d text="$msg" >/dev/null 2>&1 || true
    fi
    echo "$msg" >&2
}
trap notify_failure ERR

mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_FILE="$BACKUP_DIR/maestro-$TIMESTAMP.tar.gz"

tar -czf "$BACKUP_FILE" \
    -C "$(dirname "$MEMORY_DIR")" \
    --exclude='*.pid' \
    --exclude='logs/' \
    "$(basename "$MEMORY_DIR")"

echo "$(date -Iseconds) Backup created: $BACKUP_FILE"

# Rotate local backups — keep the most recent N
cd "$BACKUP_DIR"
ls -1t maestro-*.tar.gz 2>/dev/null | tail -n +$((MAX_LOCAL_BACKUPS + 1)) | xargs -r rm -f

# Push to Hetzner Storage Box (if configured)
if [ -n "$STORAGE_BOX" ]; then
    rsync -az "$BACKUP_DIR/" "$STORAGE_BOX:maestro-backups/"
    echo "$(date -Iseconds) Synced to Hetzner Storage Box"
    # Rotate remote backups — keep 30 days (longer retention than local)
    ssh "$STORAGE_BOX" 'find maestro-backups/ -name "maestro-*.tar.gz" -mtime +30 -delete' 2>/dev/null || true
fi
