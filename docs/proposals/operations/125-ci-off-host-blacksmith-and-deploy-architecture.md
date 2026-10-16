# 125: CI Off-Host — Move the Runners Off the Production Host, Re-Architect Deploy, and Retire the Scarcity Deformations

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:observing |
| **Affects** | `.github/workflows/quality-gates.yml`, `.github/workflows/code-review.yml`, `.github/workflows/reply-coverage.yml`, `.github/workflows/deploy.yml`, `.github/workflows/auto-update.yml` <!-- TODO: original path no longer exists — retired by commit 85e1e708, superseded by the merge queue -->, `docs/ci.md`, `docs/vps-setup.md`, `docs/deployment.md`, `scripts/safe-deploy.sh`, `maestro/health_check.py`, `maestro/diagnostics.py`, `maestro/flag_lifecycle.py`, `maestro.toml`, `tests/test_auto_update_workflow.py` <!-- TODO: original path no longer exists — removed with auto-update.yml -->, `tests/test_health_check.py`, `tests/test_diagnostics.py`, new `deploy/placement.toml`, new `deploy/systemd/`, new `maestro/host_capacity.py`, new `tests/test_workload_placement.py`, new `tests/test_host_capacity.py` |
| **What landed** | Phases A and B in full, the move itself, and the two documentation/supply-chain steps Phase D front-loaded. Step 1's `deploy/placement.toml` declares every long-lived workload with its tier, class and kind, and step 2's `tests/test_workload_placement.py` binds every workflow `runs-on:` to a declared runner label, enforces the batch/latency co-tenancy rule, and reds when any job but `deploy.yml`'s `deploy` is pointed back at a production-host runner (`[ci_placement].production_host_jobs`). Step 3's `deploy/systemd/` carries a resource drop-in per bounded systemd unit — `devshell.service` weighted strictly above both runners with a soft `MemoryHigh` band beneath its hard ceiling, each `obsidian` runner held below the container default — and the guard requires an explicit positive `CPUWeight` and a finite `MemoryMax` for every declared non-`incidental` systemd workload while staying green for the `incidental` ones. Steps 4 and 5 landed the host-capacity sensor default-off: `maestro/host_capacity.py` reads load per core, memory and swap residency, and free disk on the Docker root into a frozen `HostCapacityReading`; `summarize_health` folds any saturated resource into its conditions via `saturation_conditions`; `DiagnosticSnapshot.blind_spots` names host capacity as an unmeasured gap whenever no reading is present; and `[loop] host_capacity_sensor_enabled` is registered in `Config`, `maestro.toml` and `EXPERIMENTAL_FLAGS` with a dated `review_by`, threaded through `contemplate` and the loop's hoisted status snapshot. Step 7 onboarded Blacksmith and registered `blacksmith-4vcpu-ubuntu-2404` in the manifest under the `blacksmith-managed` tier, which holds no interactive or steady-autonomous neighbour; no `actions/cache` step or sticky disk exists anywhere in `.github/`, so every job is a genuinely fresh VM. Step 8's flip half landed — all four `quality-gates.yml` jobs plus `code-review.yml` and `reply-coverage.yml` target the managed label, `deploy.yml` alone declares `runs-on: self-hosted`, and the four orphaned `actions-runner-3` … `-6` manifest entries are gone. Step 8's de-registration half then landed too: `obsidian-2` was consolidated away — its manifest entry and its systemd resource drop-in deleted — leaving a single deploy-only self-hosted runner whose one surviving `colocation_exception` names `deploy.yml` alone (closing step 13's "one surviving exception" clause), and `tests/test_workload_placement.py` gained `test_at_most_one_host_colocated_runner` so the production tier can never silently regain a second bursty-batch co-tenant; the on-host runner de-registration and the full-day sub-`nproc` `ldavg-1` confirmation are the owed operational tail. Step 12 reconciled the docs: `docs/ci.md` §CI Runner Placement now points at the manifest as source of truth instead of restating a runner count, the billable-minutes rationale is deleted, and the auto-update safety argument rests on `GITHUB_TOKEN` non-recursion alone with the slot-scarcity cross-reference removed; `docs/vps-setup.md` matches. Step 13 landed in full — `auto-update.yml` is pinned to the immutable `docker://chinthakagodawita/autoupdate-action@sha256:53d7013a…` digest that `:v1` resolved to on 2026-09-07 and moved to the managed label, `test_runs_on_self_hosted` was rewritten as `test_runs_on_blacksmith` alongside a digest-*shape* assertion, that module's docstring is re-derived on `GITHUB_TOKEN` non-recursion, and both surviving `colocation_exception` reasons name `deploy.yml` alone. Step 9 restored test parallelism to the `quality` job on step 8's landed flip half: its pytest step now runs `-n auto --dist loadfile` with an authoritative serial fallback (mirroring `scripts/quality-gates.sh`'s `gate_maestro`) under a 10→15-minute timeout, the serial-on-purpose #3385 comment is gone, and `tests/test_quality_gate_scheduler.py` extends its `loadfile`/serial-fallback ownership to the CI step — retiring ledger entry 1, with the acceptance's three consecutive green runs still owed post-merge. Step 10 then deleted the shared-runner-state workarounds from `quality-gates.yml`: the `${{ github.workspace }}` / `-c` / `--rootdir` pytest cwd-anchoring is gone from the `quality`, `browser-tests` and `review-quality` steps, and the `quality` job's `pip install -e "maestro_sdk/"` comment is re-derived on the genuine sibling-package `packages.find` exclusion rather than the retired leaked-runner-state guard (the install itself stays, being a real dependency) — retiring ledger entries 2 and 3. Step 14's deploy agent then landed *dormant*: `scripts/deploy-agent.sh` polls the newest `production` deployment and drives `scripts/safe-deploy.sh` locally only when the record's sha is origin/main's tip — owning the ≤30-min stop window, single-flighted by `flock`, one attempt per deployment id (a rolled-back deploy is not re-driven every tick), carrying the 0/1/2/3 exit contract to a GitHub deployment-status stamp + Telegram headline — with its systemd service/timer under the non-`.d` `deploy/systemd/deploy-agent/`, a README section, and `tests/test_deploy_agent.py` pinning the shape; shipped without cutover, so the runner-driven `deploy.yml` stays the only active deployer until an operator enables the timer. Step 11 relaxed the scarcity-shaped job graph: `quality-gates.yml`'s `browser-tests` and `package-quality` jobs no longer declare `needs: [quality]`, so all three jobs now start in parallel on their own ephemeral Blacksmith VMs rather than fanning the heavy jobs in behind the fast gate; `tests/test_quality_gates_concurrency.py` inverts its two fan-in guards to `_fans_in_on_quality`-based independence assertions with a seeded-offender control keeping them non-vacuous, `docs/ci.md` records the deliberate billed-minutes-for-wall-clock trade (and that `deploy.yml`'s `workflow_run` still requires every job green, so a red `quality` still blocks deploy) — retiring ledger entry 7, with the before/after billed-minute delta still owed post-merge. Step 14 then completed its repo half: `deploy.yml` no longer deploys — it runs on the managed Blacksmith label and publishes the `production` deployment record under `deployments: write`, with a `failure()` Telegram step so a failed publish (which creates no record, so the agent never runs) is never silent — and `deploy/placement.toml` now describes a production tier carrying no Actions runner, with no `colocation_exception` anywhere and `production_host_jobs = []` kept as a live guard rather than deleted. Enabling `maestro-deploy-agent.timer` and de-registering the last self-hosted runner are the owed operator actions. The CI destination then moved again, off Blacksmith (commit 91a28e31): every workflow job then targeted a dedicated self-hosted CI host (option A) running three ephemeral, containerised self-hosted runners — one single-use JIT runner per fresh container, destroyed after the job — so the per-job isolation that retired ledger entries 2 and 3 holds there too. `deploy/placement.toml` replaced the `blacksmith-managed` tier with a self-hosted CI tier carrying only the `bursty-batch` CI-runner workload, and the production-host tier carries no Actions runner. Runner de-registration is since done: the repository's only registered runners were the three on that CI host, so of the operator actions named above only enabling the timer remains owed. `auto-update.yml` and `tests/test_auto_update_workflow.py` were retired outright (commit 85e1e708), the merge queue having superseded the sweep, and the reply-coverage audit was folded into `code-review.yml`'s `review` job (`reply-coverage.yml` is now `workflow_dispatch`-only). |
| **What is missing** | Step 14's repo half has since landed too — `deploy.yml` then ran on the dedicated CI pool and only publishes the `production` deployment record (with its own publish-failure Telegram alert), the host-local agent under `deploy/systemd/deploy-agent/` consumes it, and the manifest carries no `colocation_exception` and an empty `[ci_placement].production_host_jobs`; enabling the timer on the host is an operator action, not repo work; the production host no longer carries any Actions runner, the CI fleet having moved to a dedicated CI host. No behaviour-code remains. What is left is the burn-in: step 6's observation window was running at archival via the `maestro.production.toml` canary (`host_capacity_sensor_enabled = true`, PR #4396, declared in `PROD_ONLY_CANARIES`), re-anchored on deploy-window disk margin and swap residency now that the CI bursts it was written to calibrate against have left the host, with its thresholds, window and pass/abort criteria in the "Host-capacity sensor canary (proposal 125)" subsection of [self-evolution.md](../../self-evolution.md); then step 15's flag collapse, which stays gated on that window's evidence. |
| **Derived from** | live production data via /propose, 2026-09-03; revised for the Blacksmith decision |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

One 4-vCPU / 7.6 GiB VPS is the implicit substrate for four unrelated workload
classes — the operator's interactive development (`devshell.service` and its
Claude sessions), the steady autonomous Maestro loop (`maestro-loop`,
`maestro-dashboard`), bursty batch CI (two GitHub Actions runner services), and
incidental low-draw hobby services. Nothing
in the repo or on the host declares which workload belongs where or what any of
them may consume. The two workloads that already behave — the Maestro
containers — are the *only* ones carrying resource limits; the bursty CI
runners and the latency-sensitive interactive shell both run with
`MemoryMax=infinity`, no `CPUWeight`, and `Nice=0`, competing for the same four
cores at identical scheduling priority.

The consequence is not an outage. It is a steady tax paid in *application
shape*: every capacity collision to date has been absorbed by permanently
deforming CI configuration or product code rather than by making a placement
decision. Test parallelism was removed from CI because xdist workers were
OOM-killed on the shared runner. `pytest` invocations carry explicit
`--rootdir` and `-c` anchoring because a persistent runner's cwd leaked between
jobs. An install step re-declares a dependency because a prior job's editable
install leaked through shared runner state. The deploy script grew Docker prune
calls and a free-space preflight because the host filled to 100% and left the
loop down. The CI job graph fans in on `needs: [quality]` explicitly to ration
one runner slot. And when queueing got bad, more runners were registered on the
*same* box — the fleet reached **six**, multiplying contention rather than
adding capacity, then was cut back to two on 2026-09-01 leaving 16.5 GB of
orphaned working trees behind, while `docs/ci.md` still states there is a single
runner. That is nine point fixes converging on one unowned fact.

**The placement decision has been made in one half and re-opened in the other:
CI leaves this host; where it lands is between Blacksmith (a managed runner
service) and a dedicated box.** The destination was settled on Blacksmith and
this document was revised around that, until the corrected measurement below
showed its cost model had been built on a 2-of-6 runner sample — see
**Decision**. What is *not* in question is that CI leaves, and that changes
where the remaining work is either way. The original framing put the weight on
partitioning a shared box — a placement manifest, a co-tenancy invariant,
systemd bounds, then a move. Once CI leaves the host entirely, most of that
partitioning is satisfied by the move itself. What remains, and what this
revision is mostly about, is threefold:

1. **The deploy job cannot leave.** `deploy.yml` runs `scripts/safe-deploy.sh`
   against the deployment directory *on the production host*. It is not
   relocatable by changing a label, and the reason is structural (see **The
   deploy constraint**). This proposal now carries an explicit deploy
   architecture decision with five costed options, because "move CI to
   Blacksmith" is under-specified until that sub-decision is made.
2. **The deformations can finally be deleted.** This was Phase D and was
   previously conditional; under Blacksmith's per-job ephemeral VMs it becomes
   the payload. Two ledger entries that a dedicated self-hosted box would only
   have *conditionally* retired are retired outright.
3. **Two premises the current tree relies on are void.** The "eliminate
   billable minutes" rationale in `docs/ci.md` has been false since
   2026-03-01, and the `auto-update.yml` safety argument rests on a runner
   scarcity that ceases to exist. Both are documented as fact in the tree
   today and neither is true after this move. See **Voided premises**.

Note this is unrelated to proposal 106, which consolidates *code* infrastructure
(config loading, sidecar state, parallel oracles). The overlap is only in the
word "infrastructure"; 106 is not a dependency.

## Decision

> **Resolved in practice (2026-09-27): option A.** CI moved off Blacksmith to a dedicated self-hosted CI host running ephemeral containerised self-hosted runners (commit 91a28e31). Per-job fresh containers keep ledger entries 2 and 3 retired, so option A's persistent-runner caveat below does not apply to this pool. The Blacksmith-specific cost and billing-granularity analysis below is historical.

**Blacksmith is option B2 of the original option table — a managed CI-runner
service.** That choice was made, and this document was revised around it. It is
now **re-opened between B2 and A**, on evidence this document itself produced:

The cost model behind "on price the two are a wash" was derived from a
**2-of-6 runner sample** (see **Production Evidence**). Corrected to the whole
fleet, the 29-day mean is 13,808 CI-minutes/month, not 5,328 — so Blacksmith is
≈ **$—/month**, not ≈ $—, against ≈ €—/month flat for option A's dedicated
box. That is roughly **5×**, not a wash, and it trips option A's own stated
re-entry condition ("reconsider only if Blacksmith's billed minutes materially
exceed the model") *before* a first invoice rather than after one.

The non-price axes are unchanged and still favour B2: zero machines to operate,
and per-job ephemeral VMs that retire ledger entries 2 and 3 outright rather
than conditionally. So this is a genuine trade, not a reversal — roughly $— a
month, or ~$— a year, against one less machine to own and a whole class of
shared-runner defects deleted rather than merely bounded. **The operator owns
this call.** Everything downstream of the Decision — Phases C and D — is written
for B2 and is described below in terms of "the Blacksmith label"; under A the
same steps hold with that label naming a dedicated host's runner pool instead,
except that step 10 becomes conditional again (persistent runners on a private
box do not give per-job isolation) and step 13's digest pin stays worth doing
on its own merits.

The table below records the grounds for every option.

| Option | Verdict | Grounds |
|---|---|---|
| **B2. Managed CI-runner service (Blacksmith)** | **Open — leads on every non-price axis** | Zero machines to operate, per-job ephemeral VMs (which retire the shared-runner-state class outright rather than conditionally), a warm cache layer so it does not pay GitHub-hosted's cold-start tax, and migration that is a `runs-on` label swap plus a GitHub App install. Compute is $0.004/min for Ubuntu x64 against 3,000 free minutes/month → ≈ **$—/month** at the corrected six-runner 29-day mean (13,808 min/month) and ≈ $—/month at the sustained 2026-09-02 rate (25,218 min/month). The ≈ $— figure this document carried before was the 2-of-6 sample. |
| A. CI runners → dedicated VPS | **Open — leads decisively on price** | A Hetzner CX33-class box is ≈ €—/month, **flat as CI volume grows** — which matters more now that measured volume is 2.6× what the first draft assumed, and step 11 will raise job count further. On the corrected mean it is ≈ 5× cheaper than B2, and its re-entry condition ("reconsider if Blacksmith's billed minutes materially exceed the model") is already met. It still loses the non-price axes: a second machine to patch, monitor and pay for, and *persistent* runners that leave ledger entries 2 and 3 only conditionally retired — the very class the six-runner sprawl above shows this host is bad at owning. |
| B. GitHub-hosted runners | Not chosen | At $0.008/min for the 2-core Linux image against the same 3,000 included minutes: ≈ **$—/month** at the corrected mean (13,808 min) and ≈ $—/month at the sustained rate (25,218 min) — exactly 2× B2 in both modes, since the per-minute rate is double and the included allowance identical. Plus a cold `playwright install` on every browser run. Strictly dominated by B2 on both price and cache warmth, so it stays out regardless of how B2-vs-A resolves. |
| C. Move the Maestro loop | Rejected | Highest migration cost for the least relief: DNS record, Cloudflare origin certificate, nginx vhost, `.env` secrets, a 31 GB home directory, a bind-mounted data directory, and a deploy targeting that path. It is already capped at 2.0 CPU / 2 GiB and shows **no measured degradation**. |
| D. Move `devshell` | Rejected | Moves the victim, not the cause. It is the latency-sensitive tier whose experience the concern is about. |
| E. Resize to a larger single box | Rejected as primary remedy | Raises the ceiling without partitioning; the shared-runner-state moles survive a resize untouched. |
| F. AWS / hyperscaler | Rejected | Not cheaper in either mode. Always-on runs ≈ 4–7× Hetzner; spot + ephemeral autoscaling is defeated by this job mix, where **48.1% of jobs finish in under 60 seconds** while consuming only 4.4% of compute — a 90 s boot-and-register would add **5.40 h of wall-clock across 216 jobs** (the duration sample — see **Two denominators, unreconciled**), a 6.3× overhead on the short half. Blacksmith wins this axis by keeping runners warm rather than booting per job. |

### Corrected economics

The original Risk Assessment claimed the GitHub self-hosted platform fee "was
withdrawn within 48 hours and has never taken effect." **That is wrong.** The
$0.002/min GitHub Actions platform charge took effect **2026-03-01** and applies
to *all* Actions usage in private repositories, self-hosted runners included;
self-hosted usage now also draws down the plan's included quota. `maestro` is a
private repository, so it is in scope. Three consequences:

- **The "eliminate billable minutes" rationale in `docs/ci.md` §Self-Hosted
  Runner has been void for six months.** The stated reason the self-hosted runner
  exists no longer holds, independent of this proposal.
- **The platform fee is not a differential cost of this move.** At the corrected
  29-day mean (≈ 13,808 min/month) it is ≈ $—/month gross, partially
  absorbed by the plan's included quota — and it is paid identically whether the
  job runs on the VPS, on a dedicated box, or at Blacksmith. It does not favour
  any option, but note it is ≈ 2.6× what the two-runner sample implied, so the
  *total* CI bill under every option is higher than the first draft stated.
- **The real differential is Blacksmith compute alone**: ≈ $—/month at the
  corrected mean after the 3,000 free minutes, against ≈ €—/month for option
  A's dedicated box. On the two-runner sample these looked like a wash and the
  decision was made on the non-price axes; on the corrected mean the gap is
  ≈ 5×, which is what re-opens the Decision above. Option A's price is also
  *flat* in CI volume while B2's is linear, so the gap widens as the burst
  trend and step 11's fan-out add minutes.

Two figures in that model need verification against a first invoice rather than
being trusted:

- **Billing granularity.** The ≈ $— estimate is derived from *runner-hours*.
  **104 of 216 jobs finish in under 60 seconds** — on the duration sample,
  whose denominator differs from the queue-wait sample for the same window
  (see **Two denominators, unreconciled**). If Blacksmith rounds billing per
  job rather than per elapsed minute, that half of the mix is mispriced and the
  true bill is higher. This is the single largest uncertainty in the cost
  model.
- **The speed claim.** Blacksmith advertises ~2× faster hardware, which would
  cut both its own compute bill *and* the GitHub platform fee, since both are
  per-minute. Treat as unverified: the falsifiability section below measures it
  against the recorded `quality` p50 of 339 s and `browser-tests` p50 of 209 s.

All prices were read on 2026-09-03 and must be re-verified at decision time.
Blacksmith's Startup Program (<100 employees, <$50M raised, <5 years) may apply;
the OSS program does not, since the repository is private.

## The deploy constraint

This is the fact that makes "move CI to Blacksmith" under-specified, and it is
not visible from the workflow files alone.

The production image sets `PYTHONPATH=/workspace`, and `docker-compose.yml`
bind-mounts the repository at `/workspace`. The Dockerfile's own comment states
the intent: *"Imports resolve from the bind-mounted workspace at runtime, so code
updates are picked up on container restart without rebuilding."* **The image
carries dependencies; the code the loop actually executes is the host's git
working tree.** That same working tree is also the agent's own workspace — the
loop edits it, runs gates in it, and branches from it.

`scripts/safe-deploy.sh` therefore does work that is intrinsically local: it
tags rollback images, notifies the running loop, gracefully stops it with
`docker compose stop -t 1800` (**up to 30 minutes**), resets the working tree to
the fetched SHA, conditionally rebuilds the image, runs two smoke-test
containers against a snapshot volume, restarts, and prunes. Every one of those
steps touches the production host's Docker daemon and filesystem.

Three consequences constrain the option space:

1. **Relocating `deploy.yml` cannot move deploy's CPU cost off the box.** Deploy
   accounted for **1.36 h of the 9.36 measured runner-hours** (14.5%), and that
   work happens on the production host under every option below except part of
   D5. The runner *agent process* is negligible; the deploy *work* is the cost.
   So this sub-decision is about architecture and the co-tenancy invariant — not
   about capacity.
2. **Any architecture that drives the deploy from a remote runner must survive a
   30-minute controlling connection**, because the graceful stop window is
   inside the script.
3. **The exit-code contract must survive.** `safe-deploy.sh` distinguishes 0 /
   1 (rolled back) / 2 (rollback also failed) / 3 (aborted before any change),
   and `deploy.yml`'s alert step branches on all three. `environment: production`
   also stamps a GitHub deployment status. Whatever triggers the deploy must
   still carry those four outcomes to a human.

## Deploy architecture options

| ID | Architecture | Verdict | Grounds |
|---|---|---|---|
| **D1** | **Keep one self-hosted runner on the production host, serving `deploy.yml` — and `auto-update.yml` until step 13.** Every other workflow moves to Blacksmith, and the placement manifest declares that runner as the single permitted co-tenant under an exception whose reason names the workflows it actually serves. | **Recommended for cutover** | Smallest diff, zero new failure modes, and it concedes nothing on capacity — the deploy work stays on the box under D2 and D3 as well. The exit-code contract, the deployment status, and the Telegram alert path are all untouched. Cost: the co-tenancy invariant needs an explicit, reviewed exception rather than being trivially true (see Phase A). |
| D2 | **SSH from the Blacksmith runner.** `deploy.yml` runs hosted and connects to the production host with a deploy key to invoke `safe-deploy.sh`. | **Rejected** | Three independent problems. (a) The ≤30-minute graceful stop runs *inside* the SSH session — a dropped connection severs the deploy mid-flight, after the loop has been stopped and possibly after the tree has been reset, in a state the script's own rollback cannot reach. Fixing that means detaching the script (`systemd-run`/`nohup`) and polling for completion, at which point it is a worse D3. (b) It opens an inbound SSH surface, which to firewall properly requires Blacksmith's static-IP add-on. (c) It makes a host private key a repository secret that is decrypted on third-party infrastructure. |
| D3 | **Pull-based deploy agent on the host.** A systemd timer/service on the production host watches for a deploy signal — a GitHub deployment record, a moved ref, or a release tag — and runs `safe-deploy.sh` locally. CI's job becomes publishing the intent and reporting the result. | **Recommended as the target** | Removes the runner from the box entirely with no inbound network surface, and the 30-minute stop window is owned by a local process that nothing can sever. `deploy.yml` already declares `environment: production`, so a GitHub Deployment record exists as a ready-made signal carrier. Costs: a signal protocol, polling latency on every deploy, and a new path for the four exit codes to reach a human — the alert step's Telegram fallback would move into the agent. Worth doing after D1 is proven, not during the cutover. |
| D4 | **The loop deploys itself.** Mount the Docker socket into the agent container so Maestro can run `docker compose` against its own services. | **Rejected** | The Docker socket is root-equivalent on the host. Granting it to an autonomous agent whose entire safety model is human-approved self-modification crosses the human-in-the-loop boundary this repository is built around. Not a capacity question; a trust-chain one. |
| D5 | **Registry-mediated build.** Build the production image on Blacksmith, push to GHCR, and have the host pull instead of building. Pairs with D1 or D3. | **Optional follow-on** | The *only* option that moves measured load off the production host: the image build and its disk churn (`docker system df` reports 4.701 GB of volumes and 2.498 GB of build cache) leave the box, directly attacking ledger entries 4 and 5. But it moves only the *dependency* half — the code half stays bind-mounted per **The deploy constraint**, so the host still needs a working-tree update and D5 cannot replace D1/D3. It also adds a registry, image retention policy, and a pull step to the rollback path. Defer until the capacity sensor says the disk margin warrants it. |

**Recommendation: D1 for the cutover, D3 as the target, D5 as an independent
follow-on gated on sensor evidence.** D1 and D3 are sequential, not exclusive —
D1 keeps the cutover to a label swap, and D3 can be built and proven behind it
without a second migration.

**One further placement call within D1:** keep `auto-update.yml` on the
surviving runner *for the cutover only*, and move it in step 13. It runs a
*third-party* Docker container action pinned to the **mutable** `:v1` tag while
holding `GITHUB_TOKEN` with `contents: write`. It fired 5 times in the 200-run
window, so leaving it home through the cutover costs nothing measurable and
keeps that image off third-party infrastructure while the move is being proven.

Leaving it home is not the safe end state, though, and the tree already says so.
`docs/ci.md` §Auto-Update records that if the `:v1` tag were repushed, the next
push to `main` would silently pull and execute the new image **on the VPS** with
push access to every open PR's head branch. That is the same exposure pointed at
the production host rather than at Blacksmith, so this call trades one half of
the risk for the other instead of removing it — the defect is the mutable tag,
not the runner's location. Two consequences this proposal carries rather than
hides:

- The residual risk is **unchanged** by the cutover. Only the digest pin retires
  it, which is why step 13 promotes that pin from "deferred" to a prerequisite
  and moves the workflow behind it (see Risk Assessment).
- Until step 13 the surviving runner serves `deploy.yml` **and**
  `auto-update.yml`, so the `colocation_exception` reason recorded in steps 1
  and 3 must say exactly that. Calling it "deploy-only" before step 13 lands
  would put a false reason in the manifest — and that reason is the only thing
  standing between the exception and a rubber stamp.

## Voided premises

Two arguments the tree currently states as fact do not survive this move. Both
must be actively re-derived, not merely reworded.

1. **"To eliminate billable minutes."** `docs/ci.md` §Self-Hosted Runner gives
   this as the founding reason for self-hosting. The GitHub platform fee has
   applied to private-repository self-hosted usage since 2026-03-01. The
   premise was already false before this proposal.
2. **"Cannot storm the one self-hosted slot."** `docs/ci.md` §Auto-Update argues
   the sweep is safe because a `GITHUB_TOKEN` push does not re-trigger other
   workflows, *and* because those workflows serialize on a single slot. The
   second clause is a scarcity artifact. On elastic hosted capacity there is no
   slot to storm, so the safety argument must rest on the `GITHUB_TOKEN`
   non-recursion property alone — which is still true and still sufficient. The
   cross-reference to §Self-Hosted Runner must be removed rather than restated,
   or the doc will keep deriving a safety property from a bound that no longer
   exists. This is ledger entry 8, upgraded from "restate" to "re-audit".

## Production Evidence

Live data sampled read-only from `$DATA_DIR` (resolved via the `maestro-loop`
`/workspace` bind mount) and from the host itself on 2026-09-03. Every figure
names its window. These measurements are the grounds for the move and are
unchanged by the choice of destination.

**Host shape** (read 2026-09-03 02:40 UTC): 4 vCPU AMD EPYC-Rome, 7.6 GiB RAM,
4.0 GiB swap, 150 GB disk at 49% used, uptime 134 days.

**CI is the bursty tier, and the fleet was six runners, not two.** The host
carries six runner trees, `actions-runner` through `actions-runner-6`. Only
`actions-runner` (`obsidian`) and `actions-runner-2` (`obsidian-2`) are
registered as systemd services today; the other four ran jobs through
**2026-09-01** — the last of them at 17:13 UTC — and were de-registered that
day, two days before this reading, leaving their working directories behind.
Worker-log counts over 29 full days (2026-08-05 → 2026-09-02) put **5,471 jobs
on six runners**, of which the two survivors account for 2,063.

Runner-hours per day across **all six** `_diag` `Worker_*.log` sets over that
window: mean **189 jobs/day, 7.67 runner-hours/day** (≈ 460 CI-minutes/day).
The two-runner sub-sample — 71 jobs/day and 2.96 runner-hours/day, the figure
the first draft of this document was built on — describes the surviving
*slots*, not the CI load. The distribution is skewed: 2026-08-25 was 17.60 h
across six runners, 2026-09-01 was 14.33 h, and **2026-09-02 was 326 jobs /
14.01 runner-hours on two runners = 58.4% of a single slot's entire 24 hours**.

Read together those three days say something sharper than "the burst is
growing." All three sit in a 14–18 runner-hour band once every runner is
counted, against a six-runner 29-day mean of 7.67 h — so 2026-09-02 is **1.8×**
the mean, not the 4.7× the two-runner sub-sample implied. What changed on
2026-09-01 is not demand but *slots*, which fell from six to two. That is the
direct cause of the queue wait measured below, and it is why the cost model
must be re-derived on the six-runner mean rather than on the surviving pair.
(Only these three days were re-measured per-day; the 7.67 h figure is the full
29-day mean.)

**Most CI latency is waiting for a core, not computing.** Job-level data for
234 jobs over the 10.90 h window 2026-09-02 15:46 UTC → 2026-09-03 02:40 UTC:

```
TOTAL job-seconds (runner occupancy): 9.36 runner-hours
2 runner slots available: 21.80 slot-hours -> utilization 42.9%
1 runner slot would be: 85.9% utilization

=== job queue wait ===
 n=234 p50=19s p90=508s p99=838s max=974s
 jobs waiting >60s: 102 (43.6%)
 jobs waiting >300s: 55 (23.5%)
 total queue-wait time: 10.60h
```

Total queue wait (**10.60 h**) exceeded total execution (**9.36 h**) in the same
window, on the two slots that remained after the 2026-09-01 cut: 43.6% of jobs
still waited over a minute for one. This is the number the move is expected to
collapse.

**Two denominators, unreconciled.** The queue-wait sample above is n=234. The
duration-derived figures elsewhere in this document — option F's 5.40 h boot
overhead, and the "104 of 216 jobs under 60 seconds" billing-granularity
argument — run on n=216 for the *same* 10.90 h window. The 216 arithmetic is
internally consistent (48.1% of 216 is 104; 90 s × 216 is 5.40 h), but nothing
recorded here explains the 18-job gap between the two samples. Since 216 is the
denominator under what **Corrected economics** calls the single largest
uncertainty in the cost model, reconcile the samples — or re-derive both on one
— before acting on the cost conclusion. On n=234 the boot overhead would be
5.85 h, not 5.40 h.

**The bursts saturate the box.** `sar` for 2026-09-03 00:00–02:40 UTC on 4 CPUs:
`%idle` fell to **0.64%**, `ldavg-1` peaked at **11.07** (2.8× oversubscription),
and `runq-sz` reached **16**. Across that window at least one CI job was running
**50.9%** of wall-clock time, with two concurrent **29.7%** of the time — two of
four cores to CI, plus the loop's allotment, before the operator's own session.
Day averages from `sar` show the same skew: `%user` was 5.71–22.05% on
2026-08-26 → 2026-08-31 but **51.18%** on 2026-09-03.

**Memory pressure is chronic but secondary**: 3.4 GiB of 4.0 GiB swap in use at
read time despite `vm.swappiness=10`, with `pswpout` 68,997,513 and
`pgmajfault` 181,377,279 accumulated over the 134-day uptime. No OOM kills
appear in the last 30 days of journal — the historical OOM casualty is
documented in CI config instead (ledger entry 1).

**The isolation is exactly inverted.** The two capped workloads are the two that
do not need capping; the bursty and the latency-sensitive ones are both
unbounded:

```
maestro-loop       NanoCpus=2000000000 (2.0)  Memory=2147483648 (2G)
maestro-dashboard  NanoCpus=1000000000 (1.0)  Memory=1073741824 (1G)

actions.runner.thomas-sodon-maestro.obsidian.service     MemoryMax=infinity  CPUWeight=[not set]  Nice=0
actions.runner.thomas-sodon-maestro.obsidian-2.service   MemoryMax=infinity  CPUWeight=[not set]  Nice=0
devshell.service                                         MemoryMax=infinity  CPUWeight=[not set]  Nice=0
```

After the move, `devshell.service` and (under D1) one surviving self-hosted
runner remain unbounded. That residue is what Phase A now addresses.

**Honest negative result — the Maestro loop is not degraded.** Iteration-start
gaps computed over all 4,546 rows of `usage.jsonl` show a p50 of **122–136 s on
every single day** from 2026-08-20 through 2026-09-03, with no upward trend.
The loop is the best-behaved tenant and is *not* the workload being harmed.
The harm lands on CI latency and on the operator's interactive session — which
is precisely why the remedy moves CI and not the loop.

**Disk is a latent repeat of #3632, and the largest reclaimable item is not
Docker.** `docker system df` reports 10.6 GB reclaimable of 10.8 GB total
(3.389 GB images, 4.701 GB volumes, 2.498 GB build cache), and the deployment
home is 31 GB. But the six runner trees total **23.5 GB**, of which the four
de-registered ones (`actions-runner-3` … `-6`, 4.0–4.2 GB each) are **16.5 GB
backing no registered service, no running process, and no repo artifact** —
more than the entire Docker reclaimable figure, sitting unnoticed since
2026-09-01. The prune calls added to `scripts/safe-deploy.sh` after #3632 are
load-bearing, and nothing measures the margin between deploys. The Docker churn
is *deploy-generated*, so it survives the CI move and only option D5 attacks it;
the 16.5 GB of orphaned runner trees is pure cleanup, reclaimable today
(step 8).

**Incidental co-tenants are not the lever.** The low-draw hobby services
(a few hundred MB RSS combined, at 0.0% CPU) are measurably
negligible. Removing them would reclaim memory but no meaningful CPU; they are
noted for tidiness, not proposed for eviction.

**Workflow mix** over 200 runs (2026-09-02 00:02 → 2026-09-03 02:39 UTC): Quality
Gates 71, AI Code Review 53, Reply Coverage 53, Deploy 18, Auto-Update 5. By
job-seconds in the 10.90 h window: `quality` 3.55 h (p50 339 s),
`browser-tests` 1.95 h (p50 209 s), `review` 1.93 h (p50 236 s), `deploy`
1.36 h.

## Prior Point Fixes (whack-a-mole ledger)

| # | Identifier | What it patched | Why the class survived |
|---|---|---|---|
| 1 | PR #3385 + the `quality` job's pytest step comment in `.github/workflows/quality-gates.yml` | xdist parallelism landed, then was disabled for CI: parallel xdist workers were OOM-killed on the shared runner, and a dead worker's lost coverage tripped `--cov-fail-under` spuriously. CI runs serial; xdist is local-only in `scripts/quality-gates.sh`. | Surrendered CI speed to co-tenancy instead of separating the tenants. |
| 2 | `.github/workflows/quality-gates.yml` (`quality`, `browser-tests`, `review-quality` pytest steps) | Explicit `-c` / `--rootdir` anchoring so a stale self-hosted-runner cwd can't silently collect a nested suite (cites PR #2433). | Persistent shared runner state. Survives any resize; only ephemeral job isolation retires it. |
| 3 | `.github/workflows/quality-gates.yml` (`quality` install step) | `pip install -e "maestro_sdk/"` re-declared so the dependency is not an accident of a prior job's editable install leaking through shared runner state. | Same root as 2. |
| 4 | Issue #3632, commit `0c0ce3b5` | Host disk hit 100%, deploys failed on ENOSPC and left the loop down. Fixed by adding `docker image prune` / `docker builder prune` and a `MAESTRO_DEPLOY_MIN_FREE_KB` preflight (exit 3) to `scripts/safe-deploy.sh`. | Capacity accounting pushed into the deploy script; nothing measures headroom between deploys. |
| 5 | Commit `bdc53cfa` | Follow-up: move the disk preflight above the pre-deploy notification so the alert text is honest. | A fix to the fix — the signature of patching at the wrong layer. |
| 6 | Issue #1249 + the `concurrency` key in `.github/workflows/quality-gates.yml` | Per-SHA concurrency groups on `push:[main]` so a *pending* run isn't cancelled when a third commit queues behind an in-progress one. | Correct in its own right, but only *reachable* because runs queue at all. |
| 7 | `.github/workflows/quality-gates.yml` (`needs: [quality]` on `browser-tests`, `review-quality`, `sdk-quality`), documented in `docs/ci.md` §Self-Hosted Runner | The three heavy jobs fan in behind the fast gate explicitly to ration one runner slot. | The CI job graph is shaped by hardware scarcity rather than by dependency truth. |
| 8 | `.github/workflows/auto-update.yml` + `docs/ci.md` | The sweep's safety argument rests on `GITHUB_TOKEN` non-recursion so it cannot storm the one self-hosted slot those jobs already serialize on. | A design-safety argument made load-bearing on scarcity — see **Voided premises**. |
| 9 | `actions.runner.thomas-sodon-maestro.obsidian{,-2}.service` plus four de-registered runner trees (`/opt/actions-runner` … `-6`); all absent from the repo | Runners were added to relieve queueing — the fleet reached **six** on the same box and was cut back to two on 2026-09-01. | Every addition went to the *same four cores*, multiplying contention rather than adding capacity; every removal left a 4 GB tree behind. `docs/ci.md` still asserts a **single** runner, and the host has been at one, six, and two without a single commit recording any of it. No checked-in artifact owns runner inventory — which is why this proposal's own first-draft cost model was built on a 2-of-6 sample. |

**Retired outright by this design:**

- **1** — xdist is restored once CI runs on dedicated Blacksmith hardware. Runner
  size becomes a purchase decision rather than a scarcity fight (step 7).
- **2 and 3** — **upgraded from conditional to unconditional.** The original
  design retired these only *if* a dedicated host made ephemeral job isolation
  affordable; persistent runners on a private box would have kept the class
  alive. Blacksmith gives a fresh VM per job by construction, so the stale-cwd
  anchoring and the re-declared editable install become deletable (step 9).
  **Caveat:** enabling Blacksmith's *sticky disk* add-on for cache warmth
  re-introduces cross-job filesystem state and would partially re-open this
  class. Prefer path-scoped caching (`actions/cache` against Blacksmith's cache
  backend) over a whole-filesystem sticky disk, and record that choice in the
  manifest.
- **7** — the fan-in is relaxed (step 8). Note the tradeoff *inverts* rather than
  vanishing: on hosted capacity, fanning out four jobs in parallel trades
  wall-clock for billed minutes, and the short-job half of the mix makes that
  cheap in wall-clock but not free.
- **9** — both `obsidian` runners are de-registered. Under D1 exactly one runner
  replaces them — serving `deploy.yml` and, until step 13, `auto-update.yml` —
  and it is *declared* in the manifest rather than existing only on the host
  (steps 1, 3 and 12).

**Re-audited, not restated: 8** — see **Voided premises**.

**De-risked but deliberately kept: 4, 5** (the deploy guard stays; the capacity
sensor means the next disk crunch surfaces as a reading first, and only option
D5 moves the churn that causes it) and **6** (correct independent of capacity).

## Relevant Findings

- `.github/workflows/quality-gates.yml` (`quality` job, pytest step) — the comment
  records the OOM incident and the resulting decision to run serially; the same
  file's `browser-tests`, `review-quality`, and `sdk-quality` jobs all carry
  `needs: [quality]` and `runs-on: self-hosted`.
- `.github/workflows/deploy.yml` — `runs-on: self-hosted`, `environment: production`,
  a `working-directory` resolved from `vars.DEPLOY_DIR` or the deploy home, and an
  alert step that branches on `safe-deploy.sh` exit codes 1 / 2 / 3. This is the
  job that cannot move; the `environment` declaration is also the readymade
  signal carrier for option D3.
- `.github/workflows/code-review.yml` — the only workflow passing
  `CLAUDE_CODE_OAUTH_TOKEN`. Under a hosted runner this token is decrypted on
  third-party infrastructure alongside a full repository checkout.
- `.github/workflows/auto-update.yml` — a third-party Docker container action
  pinned to the mutable `:v1` tag, holding `GITHUB_TOKEN` with `contents: write`.
  `docs/ci.md` justifies its `self-hosted` placement on the runner already having
  Docker; that justification transfers to Blacksmith, but the mutable-tag
  exposure does not transfer safely.
- All five workflows hardcode `runs-on: self-hosted` across eight job
  declarations. There is no indirection, so relocating CI today means editing
  five files with no single owner of the target.
- `docs/ci.md` (§Self-Hosted Runner) — asserts a *single* runner shared with the
  production loop, derives the `needs:` fan-in rationale from it, and states the
  zero-billable-minutes rationale. All three assertions are now false or void.
- `Dockerfile` (production stage, `PYTHONPATH=/workspace`) and `docker-compose.yml`
  (the `.:/workspace` bind mount) — jointly the reason the deploy is not
  relocatable. The image carries dependencies; the executed code is the host's
  working tree.
- `scripts/safe-deploy.sh` (`MIN_FREE_KB` / `MAESTRO_DEPLOY_MIN_FREE_KB`, the
  `docker image prune` and `docker builder prune` calls, exit code 3, the
  `docker compose stop -t 1800` graceful window, `smoke_snapshot_create`) — the
  local-by-construction deploy body, and the 30-minute window that rules out D2.
- `docker-compose.yml` (`loop` and `dashboard` `deploy.resources.limits`) —
  the only declared resource bounds anywhere in the repo, covering the two
  workloads that are not causing contention.
- `maestro/health_check.py` (`summarize_health`, `HealthSummary`,
  `_NO_OP_RATE_THRESHOLD`, `_THROTTLE_RESIDENCY_THRESHOLD`) — the established
  threshold-gated sensor shape this proposal reuses for host capacity.
- `maestro/diagnostics.py` (`DiagnosticSnapshot.blind_spots`) — the existing
  surface for naming measurement gaps; host capacity is currently one of them.
- `maestro/flag_lifecycle.py` (`ExperimentalFlag`, `EXPERIMENTAL_FLAGS`,
  `CutoverFlag`, `CUTOVER_FLAGS`, `review_by`) — the four-stage flag lifecycle
  the sensor must follow.
- `tests/test_auto_update_workflow.py` (`test_runs_on_self_hosted`,
  `test_action_ref_is_pinned`, `test_permissions_are_least_privilege`) —
  precedent that workflow YAML shape is already pinned by a deterministic test.
  Note its actual scope: `test_runs_on_self_hosted` asserts the `runs-on` of
  `auto-update.yml`'s own job and nothing else, and that job does *not* move at
  step 8 under the D1 recommendation — so the file is untouched by step 8 and is
  rewritten at step 13, when `auto-update.yml` moves. **No test anywhere pins
  `runs-on` for the six jobs that do move** (`quality-gates.yml`,
  `code-review.yml`, `reply-coverage.yml`); step 2's manifest test is the first
  coverage they get, which is part of why it is sequenced before the move.
- `maestro.toml` (`[loop] max_loop_budget_usd_per_hour = 0.0`,
  `[loop] peak_throttle_enabled = false`) — the loop's self-throttles are
  disabled. They gate LLM *spend* and iteration cadence, not CPU, so they are
  not a capacity control; see Out of scope.

## Implementation Plan

### Phase A — Declare what remains on the host, and bound it

Narrower than the original Phase A: once CI leaves, the manifest's job is not to
partition a crowded box but to make the *residue* explicit — the one permitted
CI co-tenant and the unbounded interactive shell.

1. [x] **Add the placement manifest.** new `deploy/placement.toml` — declare every
   long-lived workload with its `class` (`interactive` / `steady-autonomous` /
   `bursty-batch` / `incidental`), its `host_tier`, and for CI workloads the
   runner `label` workflows must target. Record today's reality first —
   both registered `obsidian` runners **and the four de-registered
   `actions-runner-3` … `-6` trees still on disk**, so that the inventory drift
   the ledger's entry 9 names becomes a checked-in fact before anything moves.
   The orphans are recorded as `retired` with their reclaimable size, which is
   what makes step 8's cleanup auditable rather than folklore. Include a
   `colocation_exception` field with a required `reason`, which the surviving
   runner will claim in step 3. **Both
   `obsidian` entries must carry that exception from the outset**, with a reason
   naming the pre-move state ("CI runners share the production host until
   step 8"). Step 2's invariant is written against the *target* placement, so a
   manifest that records today's reality without those two exceptions would land
   step 2 red and keep it red until step 8 — the exception field exists precisely
   so the pre-move state is declared rather than the rule weakened. Both
   exceptions are deleted with the runners themselves in step 8.
   - *Depends on:* none.
   - *Acceptance:* the manifest parses, lists every service named in Production
     Evidence including `obsidian-2` and the four `retired` trees, and step 2's
     invariant holds against it unchanged — today's co-tenancy is legal only
     through a declared exception.
2. [x] **Pin workflows to the manifest.** new `tests/test_workload_placement.py` —
   assert every `runs-on:` value across `.github/workflows/*.yml` is a label
   declared in the manifest, and that no `host_tier` hosts a `bursty-batch`
   workload alongside an `interactive` *or* a `steady-autonomous` one *unless*
   the `bursty-batch` entry carries a `colocation_exception` with a non-empty
   reason. Both classes are named deliberately: step 8's acceptance asserts the
   pair, so an invariant covering only `interactive` would leave half of what
   step 8 claims unchecked by anything. This is what makes "someone points
   `quality-gates.yml` back at `self-hosted`" a red build rather than a silent
   regression.
   - *Depends on:* 1.
   - *Acceptance:* the test fails when a workflow's `runs-on` is changed to an
     undeclared label, and fails when a `bursty-batch` entry is given the
     `interactive` tier — or the `steady-autonomous` one — without an exception
     reason.
3. [x] **Check in systemd resource drop-ins for every bounded systemd
   co-tenant.** new `deploy/systemd/` — a drop-in per unit setting explicit
   `CPUWeight` and `MemoryMax`, with the interactive tier weighted above the
   batch tier. Covers `devshell.service` and, under D1, the surviving runner,
   whose exception reason names both workflows it serves until step 13. Extend
   `tests/test_workload_placement.py` to assert every manifest-declared
   `systemd` workload **whose class is not `incidental`** has a drop-in with
   a finite `MemoryMax` and an explicit `CPUWeight`. The `systemd` narrowing is
   sharper than "non-container": it also spares the managed `blacksmith-ci-pool`
   (`kind = "managed"`), which has no host unit on this box to bound. The class
   exemption is load-bearing rather than cosmetic: step 1 requires the manifest
   to list
   the incidental hobby services, which **Out of scope**
   deliberately leaves un-evicted and unbounded, so an unscoped assertion would
   demand drop-ins this proposal does not write and red on its own manifest.
   - *Depends on:* 1, 2.
   - *Acceptance:* the test fails if a declared non-`incidental` service has no
     drop-in, or has `MemoryMax=infinity`, and stays green for an `incidental`
     service with no drop-in.

### Phase B — Surface saturation before it becomes an incident

Independently motivated: the sensor's strongest root is #3632, whose cause
(deploy-generated image and build-cache churn) stays on the host after the move.

4. [x] **Add the host-capacity sensor behind a default-off flag.** new
   `maestro/host_capacity.py`, `maestro/health_check.py`, `maestro/flag_lifecycle.py` —
   read load-per-core, memory and swap residency, and free disk on the Docker
   root; fold a reading into `HealthSummary` using the existing threshold-gated
   shape. Register the switch in `EXPERIMENTAL_FLAGS` default-off with a dated
   `review_by`.
   - *Depends on:* none.
   - *Acceptance:* with the flag off, `summarize_health` output is byte-identical
     to today; with it on and a synthetic saturated reading, the summary names
     the saturated resource.
5. [x] **Name host capacity in the blind-spot surface.** `maestro/diagnostics.py` —
   have `DiagnosticSnapshot.blind_spots` report host capacity as unmeasured when
   the sensor is off.
   - *Depends on:* 4.
   - *Acceptance:* `blind_spots()` includes a host-capacity entry exactly when
     the flag is off.
6. [ ] **Flip the sensor on and observe — re-anchored on deploy windows.**
   `maestro.toml` — one reversible flip with the kill switch retained. The
   original ordering (observe *before* the move, while CI bursts still existed)
   was overtaken: CI left the host before the flip, so no CI burst remains to
   calibrate against and deploy windows are the only reproducible saturation
   event.
   - *Depends on:* 4, 5.
   - *Acceptance:* before the flip, the flag's burn-in note records the
     deploy-window disk-margin and swap-residency thresholds and the
     observation-window length; the sensor then fires across a measured deploy
     window that crosses either threshold and stays quiet across a full quiet
     day. The original "fires during a measured CI burst" criterion is retired
     as unmeetable.

### Phase C — The move

7. [x] **Onboard Blacksmith and register the CI label.** `docs/ci.md`,
   `docs/vps-setup.md`, `deploy/placement.toml` — install the Blacksmith GitHub
   App, select the machine type (size the `quality` job for restored xdist, per
   step 9), decide the caching strategy explicitly (path-scoped cache preferred
   over a sticky disk, per ledger entries 2 and 3), and record the resulting
   `runs-on` label and tier in the manifest.
   - *Depends on:* 1, 2.
   - *Acceptance:* the Blacksmith label appears in the manifest under a tier
     holding no `interactive` workload, and `tests/test_workload_placement.py`
     is green.
8. [x] **Flip the workflows and de-register the old runners.**
   `.github/workflows/quality-gates.yml`, `code-review.yml`, `reply-coverage.yml`
   — point all six of their job declarations at the Blacksmith label. **Leave
   `deploy.yml` on `self-hosted` (option D1)**, and leave `auto-update.yml` there
   too — its move is step 13, gated on the digest pin (see Risk Assessment).
   Then remove `obsidian` and `obsidian-2` from the host, leaving one runner
   that still serves both `deploy.yml` and `auto-update.yml` until step 13, and
   update the manifest to match: delete both `obsidian` entries together with
   the pre-move `colocation_exception`s step 1 stamped on them, and reword the
   surviving runner's exception reason to name the two workflows it now serves.
   **Also delete the four de-registered `actions-runner-3` … `-6` trees and
   their `retired` manifest entries**, reclaiming ≈ 16.5 GB — they back no
   service and no process, and leaving them is how they went unnoticed for the
   two days that produced this document's first cost model.
   `tests/test_auto_update_workflow.py` is **not** touched here — it pins only
   `auto-update.yml`'s own job, which does not move at this step.
   Independently revertible: flipping the label back restores today's behaviour.
   - *Depends on:* 7.
   - *Acceptance:* no workflow except `deploy.yml` and `auto-update.yml` targets
     a label whose tier hosts an `interactive` or `steady-autonomous` workload;
     the manifest carries no `obsidian` entry, no `retired` entry and no pre-move
     exception; the host holds exactly one runner tree; peak `ldavg-1` on
     the production host stays below `nproc` across a full day outside deploy
     windows.

### Phase D — Retire the deformations (only after the move is proven)

This is now the payload, not the tail.

9. [x] **Restore test parallelism in CI.** `.github/workflows/quality-gates.yml` —
   re-enable xdist for the `quality` job and delete the serial-on-purpose
   comment. Retires ledger entry 1.
   - *Depends on:* 8.
   - *Acceptance:* the `quality` job runs with xdist and `--cov-fail-under` holds
     across three consecutive green runs.
10. [x] **Delete the shared-runner-state workarounds.**
    `.github/workflows/quality-gates.yml` — remove the `-c` / `--rootdir`
    anchoring and the re-declared `pip install -e "maestro_sdk/"` from every job
    that now runs on an ephemeral Blacksmith VM. Retires ledger entries 2 and 3
    outright. **Do not apply to any job left on the surviving self-hosted
    runner** — at this point that is `deploy.yml` and, until step 13,
    `auto-update.yml`.
    - *Depends on:* 8, 9.
    - *Acceptance:* three consecutive green runs with the workarounds removed,
      and the caching strategy from step 7 confirmed not to persist a working
      directory across jobs.
11. [x] **Relax the scarcity-shaped job graph.** `.github/workflows/quality-gates.yml` —
    drop `needs: [quality]` where there is no real dependency, keeping it only
    where a fast-fail short-circuit is genuinely wanted. Retires ledger entry 7.
    Measure billed minutes before and after: this trades minutes for wall-clock.
    - *Depends on:* 8, 9.
    - *Acceptance:* `browser-tests` and `package-quality` start without waiting
      on `quality`; wall-clock to full CI feedback drops; the billed-minute delta
      is recorded.
12. [x] **Reconcile the documentation and re-audit the voided premises.**
    `docs/ci.md`, `docs/deployment.md`, `docs/vps-setup.md` — replace the
    "single self-hosted runner" prose with the manifest as source of truth,
    delete the billable-minutes rationale, and re-derive the
    `auto-update.yml` safety argument on `GITHUB_TOKEN` non-recursion alone,
    removing the cross-reference to §Self-Hosted Runner. Retires ledger entry 9,
    re-audits entry 8.
    - *Depends on:* 8.
    - *Acceptance:* no doc asserts a runner count or a billable-minutes claim,
      and no safety argument in `docs/ci.md` cites slot scarcity.
13. [x] **Digest-pin `auto-update.yml` and move it off the host.**
    `.github/workflows/auto-update.yml`, `tests/test_auto_update_workflow.py`,
    `deploy/placement.toml`, `docs/ci.md` — replace the mutable
    `docker://chinthakagodawita/autoupdate-action:v1` ref with an exact
    `@sha256:…` digest, then point the job at the Blacksmith label. Rewrite
    `test_runs_on_self_hosted` to assert the manifest-declared target rather than
    `self-hosted`, and re-derive that file's module docstring — which today
    grounds the workflow's safety in the single shared self-hosted runner — on
    `GITHUB_TOKEN` non-recursion alone, matching step 12's edit to `docs/ci.md`.
    This is the step that makes the surviving runner genuinely deploy-only, and
    it retires the residual mutable-tag risk `docs/ci.md` §Auto-Update already
    records rather than relocating it. Step 14 cannot be satisfied without it.
    - *Depends on:* 8, 12.
    - *Acceptance:* the action ref is a `@sha256:` digest, no workflow other than
      `deploy.yml` targets `self-hosted`, and the manifest's one surviving
      `colocation_exception` reason names `deploy.yml` alone.
14. [x] **Build the pull-based deploy agent (option D3).** new `deploy/systemd/`
    unit + timer, `scripts/safe-deploy.sh`, `.github/workflows/deploy.yml` — move
    deploy triggering to a host-local agent watching the `production` deployment
    record, carrying all four exit codes and the Telegram alert path with it.
    Retires the last self-hosted runner and the D1 colocation exception.
    - *Depends on:* 8, 12, 13.
    - *Acceptance:* a deploy triggered by a `main` merge completes end-to-end with
      no runner registered on the host; exit codes 1, 2 and 3 each still produce
      their distinct alert; the manifest carries no `colocation_exception`.
15. [ ] **Collapse the sensor flag.** `maestro/flag_lifecycle.py`, `maestro.toml`,
    `maestro/diagnostics.py`, `tests/test_diagnostics.py`,
    `tests/test_health_check.py`, `tests/test_host_capacity.py` — move the
    host-capacity switch to `CUTOVER_FLAGS`, then delete **every** dead
    off-branch it leaves behind: the flag-off arm step 4 put in
    `summarize_health` *and* the flag-off arm step 5 put in
    `DiagnosticSnapshot.blind_spots`, together with the tests that covered them.
    Listing only `flag_lifecycle.py` and `maestro.toml` would strand the
    `blind_spots` arm as unreachable code and red `--cov-fail-under=100` — the
    exact failure this step exists to prevent.
    - *Depends on:* 6, 8.
    - *Acceptance:* the flag is absent from `EXPERIMENTAL_FLAGS`, the sensor runs
      unconditionally, `blind_spots()` no longer reports host capacity as
      unmeasured under any input, and coverage holds at 100% with no
      `# pragma: no cover` added.

## Testing Strategy

All tests follow CLAUDE.md principles: `mem_fs` for file I/O, no real network,
no real `sleep`, no real subprocess, mocked externals. Host readings are
injected as fixtures — nothing reads `/proc`, `sar`, or Docker for real.

- `tests/test_workload_placement.py` (new) — the deterministic, zero-LLM-cost
  enforcement test at the centre of the remedy, in the house pattern of
  `tests/test_structural_guards.py` and `tests/test_auto_update_workflow.py`.
  Branches: every workflow `runs-on` resolves to a declared label (happy path);
  an undeclared label reds (guard); a `bursty-batch` workload sharing a
  `host_tier` with an `interactive` *or* a `steady-autonomous` one reds *unless*
  a `colocation_exception` with a non-empty reason is present (the D1 exception
  and step 1's two pre-move ones, plus its own negative case — an exception with
  an empty reason still reds); a declared non-`incidental` service missing a
  drop-in reds, while an `incidental` one missing a drop-in stays green; a
  drop-in with `MemoryMax=infinity` reds. Workflow YAML and drop-ins are parsed
  from fixtures, not from the live host.
- `tests/test_auto_update_workflow.py` (extend) — `test_runs_on_self_hosted`
  covers only `auto-update.yml`'s own job, which stays `self-hosted` through
  step 8 and moves at step 13; it needs no change before then. It is rewritten
  in the same commit as step 13 to assert the workflow's *manifest-declared*
  target, and that file's module docstring — which today derives the workflow's
  safety from "the single shared self-hosted runner" — is re-derived on
  `GITHUB_TOKEN` non-recursion alone, per **Voided premises**.
- `tests/test_host_capacity.py` (new) — the sensor over injected readings:
  a saturated reading crosses the threshold and names the resource; a healthy
  reading stays quiet; a partially-unreadable reading degrades without silencing
  (no broad `except`, per the fail-loud invariant); the flag-off path returns
  exactly today's output.
- `tests/test_health_check.py` (extend) — `summarize_health` is byte-identical
  with the flag off, and gains exactly one section with it on.
- `tests/test_diagnostics.py` (extend) — `blind_spots()` includes the
  host-capacity entry when the flag is off and omits it when on.
- `tests/test_no_line_citations_in_docs.py` — must stay green with this file
  present.

Step 14's deploy agent is tested at the unit level over a mocked signal source
and a mocked `safe-deploy.sh` invocation, asserting each of the four exit codes
maps to its alert shape. The end-to-end path is proven by the step's own
acceptance criterion on a live deploy, not by a test.

## Risk Assessment

**Third-party execution of secrets is the new exposure, and the original
proposal's "no trust-chain surface is touched" claim does not survive option B2.**
Two concrete items: `code-review.yml` passes `CLAUDE_CODE_OAUTH_TOKEN` to the
runner, and `auto-update.yml` runs a third-party Docker image on a *mutable*
`:v1` tag while holding `GITHUB_TOKEN` with `contents: write`. Under Blacksmith
both would be decrypted and executed on infrastructure the operator does not
own, alongside a full private-repository checkout. Mitigations, in order of
cost: (a) leave `auto-update.yml` on the surviving runner through the cutover —
5 runs in the measured window, so it costs nothing — while recording that this
only aims the mutable-tag risk at the VPS, which `docs/ci.md` §Auto-Update
already documents, rather than removing it; (b) promote the deferred digest pin
(`docker://chinthakagodawita/autoupdate-action@sha256:…`) from "deferred to a
digest-pin pass" to a prerequisite of moving that workflow — step 13 does
exactly this, and is what retires the risk (a) merely redirects; (c) accept the
review token's exposure as the ordinary cost of hosted CI, noting it is the same
posture every GitHub-hosted user has. This is a judgement the operator should
make explicitly rather than inherit from a label swap.

**Migration risk is materially lower than option A's.** There is no host to
provision and no dual-pool cutover to sequence: the old runners stay registered
while workflows are flipped one file at a time, and every flip is a one-line
revert. The exposure that remains is cache cold-start on the first runs after
step 8, and an unproven caching strategy — which is why step 7 requires the
caching choice to be made deliberately rather than discovered.

**The cost model was wrong once already, and its remaining assumption is
unverified.** The first draft's ≈ $—/month came from a 2-of-6 runner sample; the
corrected figure is ≈ $—/month, and that correction alone re-opened the
Decision. What remains unverified is granularity: the estimate is derived from
runner-hours, but 104 of 216 jobs finish under 60 seconds — and that denominator
is itself unreconciled (see **Two denominators, unreconciled**). If billing
rounds per job, the short-job half is mispriced *upward again*. The first
invoice is the test only if B2 is chosen; option A (a flat-rate dedicated box,
unaffected by job count) is already the cheaper choice on the corrected mean,
and the manifest makes moving between them a label change either way. Step 11
makes this worse before it makes it better — fanning out the job graph increases
job count.

**Restoring xdist (step 9) could re-introduce the #3385 OOM.** Mitigation: it is
gated behind step 8, the Blacksmith machine type is sized for it in step 7, and
its acceptance requires three consecutive green runs with coverage holding.
Revert is a one-line workflow change.

**Deleting the shared-state workarounds (step 10) is only safe if jobs are
genuinely isolated.** If a sticky disk was enabled in step 7 for cache warmth,
the stale-cwd class is still live and step 10 would re-open #2433. This is why
step 10 depends on step 7's caching choice being recorded, and why its
acceptance names it.

**The enforcement test could become a nuisance gate.** A test that reds whenever
a workflow gains a runner label risks being weakened under time pressure.
Mitigation: the manifest edit is a two-line change and the failure message names
it, so the cheap path is to declare the workload rather than to disable the test.
The `colocation_exception` field exists precisely so the honest answer to a real
exception is to declare it, not to delete the invariant.

**The sensor could add noise to contemplation.** Mitigation: default-off behind
`EXPERIMENTAL_FLAGS` with a dated `review_by`, a bounded observation window with
written tripwires, and threshold gating in the established `summarize_health`
shape. The sensor is zero-SDK-cost.

**Rollback posture.** Phase A is additive — a manifest, a test, and drop-in
files; reverting is deleting them. Phase B is flag-gated default-off with an
unconditional-parity acceptance. Phase C is revertible by flipping `runs-on`
back while the old runners are still registered. Phase D deletes mechanism, and
every step in it depends on step 8 having been proven. Step 13 is revertible by
flipping one `runs-on` back — the digest pin it lands is worth keeping either
way. Step 14 is the only structural change to the deploy path and is gated
behind a fully reconciled Phase D. An interrupted rollout at any point leaves a
working system.

## Falsifiability

The design is right if, after step 8, job queue-wait p90 falls from **508 s** to
under ~30 s, the share of jobs waiting over 60 s falls from **43.6%** to under
5%, and peak `ldavg-1` on the production host stays below `nproc` (4) across a
full day *outside deploy windows* — the carve-out is honest, because the deploy
build stays on the box under the recommended option.

It is wrong in four distinguishable ways:

- **Queue-wait stays high.** The bottleneck was job duration, not host
  contention, and the remedy should have been shorter jobs.
- **Job durations do not improve.** Blacksmith's ~2× hardware claim is measured
  against the recorded `quality` p50 of **339 s** and `browser-tests` p50 of
  **209 s**. If they do not drop, the billed-minute model must be re-derived at
  1× and the cost conclusion revisited.
- **The first invoice materially exceeds the model.** The billing-granularity
  assumption was wrong on top of the sampling error already corrected here, and
  option A — already ahead on the corrected mean — becomes preferable by more.
- **The capacity sensor never fires** across a window containing a measured
  burst — the thresholds describe nothing real. Note step 6 must run *before*
  step 8 for this test to be available at all.

## Out of scope

- **Retiring the deploy disk guard** (`scripts/safe-deploy.sh`, ledger 4 and 5).
  Correct independent of capacity; the sensor only means the next crunch is seen
  earlier, and only option D5 moves the churn that causes it.
- **Changing the bind-mount code-delivery model.** Option D5's registry-mediated
  build touches this and is deliberately scoped out of the main plan; it is a
  separate proposal if the sensor shows the disk margin warrants it.
- **The per-SHA concurrency design** (ledger 6). Correct on its own terms.
- **A general digest-pin pass across all actions.** Only the `auto-update.yml`
  `:v1` pin is in scope here, and only as a prerequisite of moving that one
  workflow.
- **Evicting the incidental co-tenants** (low-draw hobby services).
  Measured at ~0.0% CPU and ~320 MB combined — tidiness, not a capacity lever.
- **Migrating the Maestro loop or `devshell`.** Rejected with grounds in the
  Decision table.
- **Enabling the loop's burn-rate or peak-hour throttles.** These gate LLM spend
  and iteration cadence, not CPU or memory; presenting them as a capacity control
  would misattribute the mechanism.
- **Resizing the production host.** Reasonable later if the interactive tier
  needs more cores, but not a substitute for partitioning.
