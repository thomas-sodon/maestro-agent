# systemd resource drop-ins for the bounded host units

> **Archived — no active deployment.** Maestro is no longer developed or deployed. This page records how the production host was set up while the experiment ran; read present-tense descriptions of the host as historical.

The `<unit>.d/` files here are **declarative artifacts**. Nothing in this repo
runs them — they exist to be installed onto the production host, and to be
pinned by a test so a bound cannot silently go missing.

This directory also holds one set of *executable* units — a service and timer
under [`deploy-agent/`](#the-deploy-agent-executable-units) — which, once the
operator enables the timer, systemd actually runs on the host. They are
documented in their own section at the end; everything between here and there
concerns the resource drop-ins only.

Each `<unit>.d/10-placement.conf` mirrors, one-for-one, the path
`/etc/systemd/system/<unit>.d/10-placement.conf` on the host. systemd merges a
unit's `.d/*.conf` drop-ins over its base unit, so installing one adds the
`[Service]` settings here without editing the vendored unit file.

## What they bound, and why

The production VPS ran the operator's interactive shell
alongside the containerised Maestro services and the host-local deploy agent (a
`Type=oneshot` unit — see below). It ran no self-hosted GitHub Actions
runner: production deploys were `production` GitHub deployment records that the
host deploy agent polled and applied. The containers
already carry limits (`docker-compose.yml`); the interactive shell unit did not.
Left unbounded (`MemoryMax=infinity`, no `CPUWeight`), the shell competes for the
same cores at identical priority as the steady-autonomous containers — the
unboundedness behind the historical OOM pressure — so it carries an explicit
bound.

The one bounded host unit sets an explicit `CPUWeight` (a relative CPU share) and
a finite `MemoryMax`:

| Unit | Tier | `CPUWeight` | `MemoryHigh` | `MemoryMax` |
|---|---|---|---|---|
| `devshell.service` | interactive | 400 | 5G | 6G |

`devshell.service` carries a soft `MemoryHigh` below its hard `MemoryMax`.
cgroup-v2 `MemoryMax` is a hard wall that OOM-kills with no throttle stage; the
soft `MemoryHigh` reclaims-and-throttles first, so the operator's own session
degrades gracefully instead of being killed outright. (The Maestro containers are
`kind = "container"`, so they carry no systemd drop-in of their own — their share
is the cgroup default weight plus the hard quota `docker-compose.yml` already
sets.)

The nominal bounds overcommit the host: `devshell.service`'s `MemoryMax=6G`
plus the loop's 6G plus the dashboard's 1G is 13G against the host's physical memory.
Today that sum is moot: `devshell.service` is not installed on the current
host, so only the containers' 7G is live. Once it is installed, the overcommit
is acceptable only because the three never all peak together, and
`MemoryHigh=5G` throttles the shell before any hard wall is reached.

The specific numbers are operator-tunable starting values. What
`tests/test_workload_placement.py` locks is the shape, not the values: an
explicit `CPUWeight` and a finite `MemoryMax` for every bounded systemd unit —
and, whenever a batch-runner drop-in is present, the interactive tier weighted
strictly above it.

## Source of truth

`deploy/placement.toml` is the source of truth for **which** units must be
bounded. Every workload it declares with `kind = "systemd"` and a `class` other
than `incidental` must have a drop-in here; the incidental services
(low-draw hobby services) are deliberately left un-evicted and
unbounded, so they carry no drop-in. Container and managed workloads have no
host unit to bound.

## Installing (operator, out-of-band)

```sh
# From the repo root, for each bounded unit:
sudo mkdir -p /etc/systemd/system/<unit>.d
sudo cp deploy/systemd/<unit>.d/10-placement.conf /etc/systemd/system/<unit>.d/
sudo systemctl daemon-reload
systemctl cat <unit>          # confirm the merged CPUWeight / MemoryMax
```

Applying these to the live host is a separate operator action, not part of any
PR. Nothing on the host is touched by checking these files in.

## The deploy-agent executable units

Unlike the resource drop-ins above, `deploy-agent/` holds two *executable*
systemd units — not a `[Service]` merge over a vendored base:

- `maestro-deploy-agent.service` — a `Type=oneshot` unit whose `ExecStart` runs
  `scripts/deploy-agent.sh` as the deploy user. The agent polls the newest
  `production` GitHub deployment record; when its target sha differs from the
  host's checked-out `HEAD` it runs `scripts/safe-deploy.sh` locally — owning the
  ≤30-minute graceful-stop window, so no dropped remote connection can sever a
  deploy mid-flight — then stamps the GitHub deployment status and sends a
  Telegram headline carrying safe-deploy.sh's 0/1/2/3 exit-code contract.
- `maestro-deploy-agent.timer` — schedules the service (`OnBootSec` +
  `OnUnitActiveSec`; no `Persistent=`, which is a no-op on a monotonic timer). A
  short interval is safe: the agent no-ops when the host is already at the target
  sha, skips a record whose sha is not origin/main's tip or a deployment it has
  already attempted, and a `flock` single-flight guard makes an activation that
  overlaps a still-running deploy a no-op too.

They deliberately live in a subdirectory that does **not** end in `.d`, so the
placement guard's `*.d`-only enumeration in `tests/test_workload_placement.py`
never treats them as a resource drop-in demanding a manifest entry.

### Installing and enabling (operator, out-of-band)

```sh
# From the repo root. Adjust User=/WorkingDirectory=/ExecStart= in the .service
# for your deploy user and repo-clone path before copying:
sudo cp deploy/systemd/deploy-agent/maestro-deploy-agent.service /etc/systemd/system/
sudo cp deploy/systemd/deploy-agent/maestro-deploy-agent.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now maestro-deploy-agent.timer
systemctl status maestro-deploy-agent.timer   # confirm it is scheduled
```

**Enabling the timer is the live cutover step, and is deliberately left to the
operator.** Checking these units in touches nothing on the host: the units sit
in the repo, inert, until an operator runs the `enable --now` above.
No CI workflow deploys; a `production` deployment record (created by hand or by
an operator's own deploy workflow) is what this agent consumes — so **until the
timer is enabled there is no deployer at all**: the record is created, no
failure alert fires, and nothing acts on it. Enable and
verify the timer (`systemctl status maestro-deploy-agent.timer`, agent log
showing a clean no-op) *before* relying on automated deploys.
