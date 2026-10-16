# Hetzner VPS Setup

Complete setup guide for deploying Maestro on a Hetzner Ubuntu VPS with SSL via Cloudflare origin certificate.

> **Archived — no active deployment.** Maestro is no longer developed or deployed. This page records how the production host was set up while the experiment ran; read present-tense descriptions of the host as historical.

> **Known limitation.** This layout is not hardened. The loop container bind-mounts the checkout read-write as the host `maestro` user, and that user is in the `docker` group, which is root-equivalent on the host. The backup cron, the nginx config symlink and the deploy agent all run or load files from that same agent-writable checkout, so the agent can change what they execute. A hardened setup runs them from an operator-owned clone under a separate deploy user (see [deployment.md §Deploy clone](deployment.md#deploy-clone-host-trust-root--first-act)).

**Target domain:** `maestro.example.com`
**Traffic flow:** Client → Cloudflare (public SSL) → nginx on VPS (origin cert) → localhost:8420 (dashboard container)

## Prerequisites

- Hetzner VPS running Ubuntu 22.04+
- Root or sudo access
- `maestro.example.com` DNS A record pointing to the VPS IP in Cloudflare (proxied / orange cloud)
- Cloudflare SSL/TLS mode set to **Full (strict)**
- Cloudflare origin certificate generated for `maestro.example.com` (15-year validity recommended)

### Generate Cloudflare Origin Certificate

1. Cloudflare dashboard → SSL/TLS → Origin Server → Create Certificate
2. Let Cloudflare generate a private key and CSR
3. Hostnames: `maestro.example.com`
4. Certificate validity: 15 years
5. Save both the certificate (PEM) and private key — the key is shown only once

## 1. System User

```bash
# Create the maestro user (if not already present)
adduser maestro
```

## 2. Docker

```bash
# Install Docker CE (official method)
apt-get update
apt-get install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Allow maestro user to run Docker without sudo
usermod -aG docker maestro
```

Verify: `docker compose version`

## 3. Clone Repo and Configure

```bash
# Switch to maestro user
su - maestro

# Clone the repo. Every path below assumes this checkout, /home/maestro/maestro
# (the maestro user's ~/maestro, which is also scripts/deploy-agent.sh's default).
git clone https://github.com/thomas-sodon/maestro-agent.git ~/maestro
cd ~/maestro

# Create the environment file
cp .env.example .env
chmod 600 .env
```

Edit `.env` with the required secrets:

```bash
# Required secrets
ANTHROPIC_API_KEY=sk-ant-api03-...       # or CLAUDE_CODE_OAUTH_TOKEN
MAESTRO_ENV=production
GITHUB_TOKEN=github_pat_...

# Dashboard auth — required for external access
# Generate with: openssl rand -base64 32
MAESTRO_DASHBOARD_TOKEN=<paste-generated-token-here>

# Optional secrets
TELEGRAM_BOT_TOKEN=...

# Git identity (for Docker)
GIT_AUTHOR_NAME=Maestro
GIT_AUTHOR_EMAIL=maestro@example.com
GIT_COMMITTER_NAME=Maestro
GIT_COMMITTER_EMAIL=maestro@example.com
HETZNER_STORAGE_BOX=u12345@u12345.your-storagebox.de
```

Non-secret configuration (paths, repo, dashboard port/host, verbose)
is in `maestro.production.toml`, loaded when `MAESTRO_ENV=production` is set.

Generate a dashboard token:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 4. SSL Certificates

Place the Cloudflare origin certificate and private key on the server:

```bash
# As root/sudo
mkdir -p /etc/ssl/maestro

# Copy the certificate PEM content into origin.pem
nano /etc/ssl/maestro/origin.pem

# Copy the private key PEM content into origin-key.pem
nano /etc/ssl/maestro/origin-key.pem

# Lock down permissions
chmod 644 /etc/ssl/maestro/origin.pem
chmod 600 /etc/ssl/maestro/origin-key.pem
```

## 5. Nginx

```bash
# Install nginx
apt-get install -y nginx

# Remove the default site
rm -f /etc/nginx/sites-enabled/default

# Symlink the maestro config from the repo (stays up to date with git pull)
ln -sf /home/maestro/maestro/nginx/maestro.conf /etc/nginx/sites-enabled/maestro.conf

# Test and start
nginx -t
systemctl enable nginx
systemctl reload nginx
```

**Note:** The nginx config is symlinked from the repo. When `git pull` updates it, run `sudo systemctl reload nginx` to pick up changes.

### Cloudflare IP ranges

`nginx/maestro.conf` carries a literal `set_real_ip_from` whitelist of Cloudflare's published edge ranges so the realip module can rewrite `$remote_addr` to the `CF-Connecting-IP` value only when the peer is genuinely a CF edge. Refresh when Cloudflare updates its ranges. Each line in the Cloudflare lists is a bare CIDR; the `sed` wraps each as `set_real_ip_from <cidr>;`:

```bash
curl -sf https://www.cloudflare.com/ips-v4 | sed 's/.*/set_real_ip_from &;/'
curl -sf https://www.cloudflare.com/ips-v6 | sed 's/.*/set_real_ip_from &;/'
```

Paste the two formatted lists over the `set_real_ip_from` block in `nginx/maestro.conf`, update the `Last refreshed: <ISO date>` line in the same comment, then on the VPS run `sudo nginx -t && sudo systemctl reload nginx`. Cloudflare announces range changes in advance on their blog and via the `cloudflare-ip-ranges` mailing list, so this is rare-and-scheduled, not continuous.

To confirm the realip whitelist works (one-time, after any change to the `set_real_ip_from` block):

1. From an external client through Cloudflare, hit a low-rate path and check `docker compose logs dashboard | tail` — the logged client IP should be the real client, not a CF edge IP.
2. From a direct-to-VPS connection bypassing Cloudflare, with a forged header:
   `curl --resolve maestro.example.com:443:<VPS_IP> -H 'CF-Connecting-IP: 1.2.3.4' https://maestro.example.com/health` — the dashboard log must show your real peer IP, **not** `1.2.3.4`.

## 6. Firewall

```bash
# Allow only SSH, HTTP (redirect), and HTTPS
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# Verify — port 8420 should NOT be listed
ufw status
```

## 7. Start Maestro

```bash
# As maestro user
cd ~/maestro

# Build and start containers
docker compose build
docker compose up -d

# Check health
docker compose ps
```

The dashboard binds to `127.0.0.1:8420` (localhost only). External access goes through nginx on port 443.

## 8. Host Deploy Agent

Production deploys ran from a host-local systemd agent, not a GitHub Actions runner — the production host ran no runner at all. An operator publishes a `production` GitHub deployment record (by hand or from a deploy workflow of their own — none ships in this repository); the agent (`scripts/deploy-agent.sh`) polls it and, when its target sha is `origin/main`'s tip, runs `scripts/safe-deploy.sh` locally for a blue-green rollout with automatic rollback — safe-deploy drives this host's own Docker daemon and resets the bind-mounted working tree the loop runs from, so it structurally cannot run anywhere but here. CI now runs on GitHub-hosted `ubuntu-latest` runners; see [ci.md §CI Runner Placement](ci.md), [deployment.md §Continuous Deployment](deployment.md#continuous-deployment), and `deploy/placement.toml`.

Install and enable the agent's units (full runbook: [deploy/systemd/README.md](../deploy/systemd/README.md#the-deploy-agent-executable-units)):

```bash
# From the repo root, as root or a sudo-capable admin account (not the maestro
# user, which has no sudo rights). Adjust User=/WorkingDirectory=/ExecStart=
# in the .service for your deploy user and repo-clone path before copying.
sudo cp deploy/systemd/deploy-agent/maestro-deploy-agent.service /etc/systemd/system/
sudo cp deploy/systemd/deploy-agent/maestro-deploy-agent.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now maestro-deploy-agent.timer
systemctl status maestro-deploy-agent.timer   # confirm it is scheduled
```

The agent needs Docker (it drives the host's own daemon), a `GITHUB_TOKEN` to poll and stamp deployment records, and its `WorkingDirectory=` (or a `DEPLOY_DIR` env var, default `$HOME/maestro`) pointed at the repo clone. **Enabling the timer is the live cutover step** — until it is enabled, no deployment records are consumed and deploys do not run.

## 9. Backup Cron

The cron-spawned shell opens the stdout/stderr redirect target **before**
exec'ing the script, so its parent directory must exist; otherwise
`open(2)` returns `ENOENT`, the shell exits non-zero, and
`backup-maestro.sh` never runs. Create the log directory as a
prerequisite:

```bash
# As maestro user
mkdir -p /home/maestro/maestro/logs
crontab -e
```

Add this line for hourly backups (the `cd` makes the script's default
`data/` resolve inside the checkout; the inline `mkdir -p` keeps the cron
entry self-bootstrapping if the directory is ever pruned, e.g. by a
tmpfiles cleanup or repo wipe):

```
0 * * * * cd /home/maestro/maestro && mkdir -p /home/maestro/maestro/logs && scripts/backup-maestro.sh >> /home/maestro/maestro/logs/backup.log 2>&1
```

The redirect target `/home/maestro/maestro/logs/` is owned by the `maestro` user, so the cron-spawned shell can append to it; do not point this at `/var/log/`, which is root-owned and not writable by the `maestro` user that runs the cron job.

If using Hetzner Storage Box for offsite backups, set `HETZNER_STORAGE_BOX` in `.env` and ensure SSH key access is configured.

## 10. Verification

Run these checks to confirm everything is working:

```bash
# Dashboard container is healthy
docker compose ps

# Port 8420 bound to localhost only
ss -tlnp | grep 8420
# Expected: 127.0.0.1:8420

# Nginx listening on 443
ss -tlnp | grep 443

# Health check through nginx (from the VPS)
curl -k https://localhost/health  # -k: origin cert is trusted only by Cloudflare's edge, not the OS CA store

# Firewall is correct
sudo ufw status
# Expected: 22, 80, 443 only

# Full chain test (from any external machine)
curl https://maestro.example.com/health
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `nginx -t` fails | Check cert paths exist and are readable: `ls -la /etc/ssl/maestro/` |
| 502 Bad Gateway | Dashboard container not running or not bound to 8420: `docker compose ps` |
| Connection refused on 443 | nginx not running: `systemctl status nginx` |
| Cloudflare 526 error | SSL mode not set to "Full (strict)", or origin cert expired/wrong hostname |
| Can reach 8420 externally | `docker-compose.yml` missing `127.0.0.1:` prefix; re-run `docker compose up -d` |
| Health check timeout in deploy | Check `docker compose logs dashboard` for startup errors |

### Log Locations

- nginx: `journalctl -u nginx` or `/var/log/nginx/error.log`
- Dashboard: `docker compose logs dashboard`
- Loop: `docker compose logs loop`
- Backup: `/home/maestro/maestro/logs/backup.log`
- Deploy: GitHub Actions workflow run logs

## Updating Nginx Config

The nginx config at `nginx/maestro.conf` is symlinked into `/etc/nginx/sites-enabled/`. When the deploy workflow pulls new code, the symlink automatically points to the updated file. To apply changes:

```bash
sudo nginx -t && sudo systemctl reload nginx
```
