# 141: Runtime Contract — One Declared Owner of What the Production Container Must Provide, Verified Inside the Built Image Before Merge

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | partial |
| **Affects** | new `maestro/runtime_contract.py`, new `deploy/runtime_contract.toml`, `maestro/cli.py` (`cmd_preflight`, `_build_agent`, new `runtime-contract` subcommand), `maestro/config.py` (`Config.check_mcp_launchers`, `MCP_LAUNCHER_INSTALL_HINTS`, `Config._check_provider_credential`, the new `[loop]` flag), `maestro/config_settings.py` (the new `[loop]` flag), `maestro/flag_lifecycle.py` (`EXPERIMENTAL_FLAGS` / `CUTOVER_FLAGS`), `maestro.toml`, `maestro/gate_environment.py` (`probe_provisioning`, `_SIBLINGS`), `maestro/gate_capacity.py` (`measure_host_capacity`), `maestro/host_capacity.py` (`read_host_capacity`, `_read_docker_root_free_gib`), `maestro/providers/cli_paced_gate.py` (`GATE_HOOK_COMMAND`), `Dockerfile`, `docker-compose.yml`, `.github/workflows/quality-gates.yml`, `deploy/placement.toml`, `pyproject.toml` (pytest markers), `docs/deployment.md`, `docs/ci.md`, new `tests/test_runtime_contract.py`, new `tests/test_runtime_contract_owner.py`, new `tests/container/`, `tests/test_gate_environment_owner.py`, `tests/test_dockerfile_structure.py`, `tests/test_docker_entrypoint_install.py` |
| **Depends on** | 133 (`probe_provisioning` / `_SIBLINGS` become contract projections), 134 (the fit lease whose in-image budget this measures), 125 (the managed CI pool and `deploy/placement.toml` the new job must declare itself in), 121 (`constraints.txt` reproducible resolution the in-image build relies on) |
| **Derived from** | live production data via /propose, 2026-09-26 |
| **What landed** | Steps 1–2 (PR #4402): `deploy/runtime_contract.toml` declares entries of all seven kinds; `maestro/runtime_contract.py` owns `load_contract` (`ContractError` on an unknown kind/scope, missing kind field, or malformed `required_when`), the pure `evaluate` over injected `Observations` under a `RequirementContext`, and the one impure `observe` reusing `shutil.which`, `gate_environment._resolves`, the `gate_capacity` cgroup readers and the `host_capacity` readers (writability is probed non-mutatingly with `os.access`, not a create-and-lock). `maestro runtime-contract` (`cmd_runtime_contract`, `--json`) prints the per-entry verdict and exits 1 on a required violation. Step 3 (PRs #4402, #4423): `tests/test_runtime_contract_owner.py` fails on any statically spelled binary not declared as a `binary`/`toolbelt` entry, and on any `/sys/fs/cgroup` / `/proc` / `/var/lib/docker` path literal or home-directory use (`Path.home()`, an expanded `~`) not declared via a `readable_fact`'s `paths` or a `writable_path`'s `used_by` — and on a stale declaration; `writable_path` templates expand `{home}`. Step 4: `maestro runtime-contract --network` resolves each required, container-scoped `launcher_package` on its launcher's public registry (npm for `npx`, PyPI for `uvx`), reading `ok`/`violated` instead of `unverified`; an unmapped launcher or unparseable spec is a `ContractError` at load. Step 11, `docker_root_free` half (PR #4396, proposal 125's host-capacity canary): `host_capacity._read_docker_root_free_gib` falls back to the `/workspace` bind mount when `/var/lib/docker` is absent, so the in-container reading is no longer `None` without mounting the Docker root. Step 11, toolbelt half: the `Dockerfile` `base` stage installs `procps` and `jq`, and `tests/test_dockerfile_structure.py` binds every apt-provisioned `toolbelt` entry to the `apt-get install` list. Step 8: `[loop] runtime_contract_enforced` (default off, `EXPERIMENTAL_FLAGS`) — `cmd_preflight` and `_build_agent` fold the contract offline through `runtime_contract.enforce_runtime_contract`; on, a required violation (or a missing/malformed contract) exits non-zero naming the entry, off, it is a WARNING. |
| **What is missing** | The in-image CI job, `container` test lane and fit measurement (steps 5–7); the projections (steps 9–10); proxy retirement and flag collapse (steps 12–13). Steps 5–7 and 12 are blocked on CI runner infrastructure: the step 5 job (PR #4444, commit ed409ed46) ran once — job `container-contract` in a Quality Gates run on 2026-10-03 — and failed at its first probe with `docker: command not found` (the self-hosted CI runners exposed no Docker daemon), and was reverted in aad546a25 (PR #4447) because its bind mounts (`.:/workspace`, `./data`) resolve on the daemon's host, not the job container. Unblocking needs an operator-provided `docker` CLI with buildx and compose reaching a daemon inside CI jobs, bind-mount sources that resolve to the job checkout (identical host path, DinD, or a copied named volume), and runner headroom for the `production` build under the compose limits. |

## Description

Maestro is tested in one environment and run in another. CI runs the suite
natively on a Blacksmith Ubuntu runner: `setup-python`, a hand-written
`pip install` line, no resource limits, `MAESTRO_ENV` unset, the checkout owned
by the runner user. Production runs the `Dockerfile` `production` image under
`docker-compose.yml`: `PYTHONPATH=/workspace` over a bind-mount, an entrypoint
that re-installs editable packages and drops to UID 1002 via `gosu`, a
2-CPU / 2 GiB cgroup, a slim Debian toolbelt, `MAESTRO_ENV=production`, and a
second container (the dashboard) that sees the tree read-only. **CI never
builds that image and never executes a line of code inside it.** The first
time a merged change meets the real runtime is `scripts/safe-deploy.sh` on the
production host, after merge. That smoke only runs `preflight` and a single
`--smoke` iteration, with `MAESTRO_SKIP_PIP_SYNC=1`, and never runs the gate.
So most runtime facts are first exercised by the live loop itself.

The failure class, in one sentence: **code depends on facts about the
production container that are declared nowhere executable and checked nowhere
before merge.** Those facts cover the binaries on PATH, which distributions are
installed rather than shadowed, cgroup limits, mount writability, the runtime
user and HOME, and the prod overlay. Each time one of these facts is
discovered, the house response has been a point fix plus, at best, a *static
text parse of the Dockerfile or entrypoint* that pins that one fact. Examples
are `_LAUNCHER_APT_PACKAGES`, `test_sibling_editable_installs_match_ci`, and
the `.claude.json` seed-string assertions. These are proxies: they check that
a line exists, not that the image behaves. The next fact nobody thought to
parse ships green.

This proposal replaces the proxies with one structural owner, in three parts:

1. **A declared runtime contract.** `deploy/runtime_contract.toml`, loaded by
   `maestro/runtime_contract.py`, owns every environment fact code may depend
   on: binaries, distributions, writable paths, readable host/cgroup facts, the
   runtime identity, and the agent's shell toolbelt.
2. **One probe that evaluates the contract.** It is a pure evaluator over
   injected observations plus a thin reader, run in three places: a new CI job
   *inside the freshly built production image under the compose limits* before
   merge, `preflight` at deploy, and `_build_agent` at startup.
3. **An AST guard that makes the contract the only door.** Any external binary
   name, cgroup/`/proc` read, or `Path.home()` dependency in `maestro/` must be
   declared, so a new dependency cannot land undeclared.

Once this lands, an environment-shaped bug turns red in the PR that introduces
it, not in the loop two days later.

## Production Evidence

Data dir: `/opt/maestro/data` (host bind-mount of `maestro-loop`,
resolved via `docker inspect`), plus read-only `docker exec maestro-loop`
probes on 2026-09-26. All reads were read-only.

- **Gate runs that could not reach a verdict for environment reasons:
  321 of 607 (52.9%).** `gate_runs.jsonl`, full ledger, 2026-09-09 → 2026-09-26.
  - 279 are `COLLECTION_ERROR`, all between 2026-09-09 and 09-14. This is the
    PEP 420 `maestro_sdk` namespace shadow that exists only under the image's
    `PYTHONPATH=/workspace` (RCA #3872).
  - 42 are `TIMEOUT`, between 2026-09-09 and 09-17. The full suite at the
    cgroup-bounded `workers=1` took 418–427s against a 300s cap (#3895). Before
    that, workers were sized to the host rather than the cgroup (#3760).
  - After both fixes, 2026-09-18 → 09-26: 211 CLEAN, 8 FAILED, 0 environment
    kinds. The class was closed by point fixes, one at a time.
  - Sensor reused: the `GateRunRecord.outcome` vocabulary that proposal 133's
    `GateRunnabilityKind` classifies.
- **80 evolution plans minted with an environment fact as their title premise,
  2026-08-27 → 09-11.** From `evolution_plans.jsonl` (660 plans), matched on
  `maestro_sdk`, PEP 420, cgroup, `npx`, OOM, `.claude.json`, Dockerfile, or
  in-container:
  - 70 `obsolete`, 5 `rejected`, 4 `failed`, 1 `merged`.
  - The loop tried to fix the image from inside the code, because the code was
    the only thing its gate could see.
- **17 crash milestones, 2026-09-08 → 09-16**, in `journal.jsonl`
  (`triggered_by: crash`). They match 17 `run_loop crashing` lines in
  `logs/maestro.log.1`, and trace to the fit/timeout arm of the same class
  (stream stalls during `auto_fix` / `implement_plan` while the gate ran against
  the cgroup; #3760, #3895). The separate 2026-09-03 → 09-07 cluster of 59
  crashes is the 1 MiB SDK-frame class, which is **not** container-caused and is
  excluded here.
- **Spend during the environment-red window: ≈ $—**, per `usage.jsonl` daily
  sums for 2026-09-09 → 09-17 (09-10 absent). #3895 records "no autonomous work
  has landed" for part of that window. Not all of this spend is attributable,
  but essentially none of it could merge.
- **23 agent tool calls failed on a missing toolbelt binary**, 2026-08-24 →
  09-26, in `logs/maestro.log{,.1,.2,.3}`:
  - 11 × `ps: command not found`
  - 7 × `pgrep`
  - 3 × `jq`
  - 2 × `free`

  The image is `python:3.14-slim` and ships no `procps` or `jq`. Nothing
  declares which shell tools the agent may assume.
- **Live latent defect (new, found by this investigation).**
  `docker exec maestro-loop ls -d /var/lib/docker` reports
  `No such file or directory`. `host_capacity._read_docker_root_free_gib`
  therefore always returns `None` in production, so the disk-saturation arm of
  `read_host_capacity` can never fire where it is deployed. Nothing tests it
  anywhere but a mocked path.
- Live image probe (`command -v`):
  - Present: `git gh npx node ruff mypy python3.14 maestro`.
  - Missing: `uvx claude ps pgrep free jq`.
  - Runtime: loop process `Uid 1002`, cgroup `cpu.max = 200000 100000`,
    `memory.max = 2147483648`, `nproc = 4`. The host and the cgroup disagree on
    cores inside the container: that is the fact #3760, #3782, #4103 and #4080
    each re-discovered.

## Prior Point Fixes (whack-a-mole ledger)

| # | Identifier | What it patched | Why the class survived |
|---|---|---|---|
| 1 | `cc7cb0e7` | UID/GID 1002 to match the prod host | Hard-coded; nothing verifies it against the host |
| 2 | `0a9048d5`, `56efed07`, `3e5a69be` | `/data/logs` PermissionError. Three successive fixes ending in `gosu` step-down | Writability is fixed per path, not declared per path |
| 3 | `6787a322`, `f373b085` | chown `triage_queue`; widen `OSError` for read-only fs | Same class, next path |
| 4 | `a68653cf`, `3aea1716` | Missing `aioimaplib` in the image; rebuild on pyproject-hash mismatch | Install drift detected only at runtime |
| 5 | `8eed7898`, `f4001bac`, `b607b17e` | Editable install to shadow site-packages; stale egg-info; read-only `/workspace` | Entrypoint install path never smoke-tested (`MAESTRO_SKIP_PIP_SYNC=1`) |
| 6 | `5062d188` (#3054), `f06ab497` | Image lacked `npx`, so every deploy failed at preflight; fixed the uvx apt mapping | Guarded by `_LAUNCHER_APT_PACKAGES`, a static Dockerfile parse |
| 7 | `a638774c` (#2573) | CLI couldn't save `~/.claude.json` (ENOENT) | Guarded by string assertions on the entrypoint, not by a write |
| 8 | `34d97638` (#3872) | PEP 420 shadow: 279 collection errors | Guarded by `test_sibling_editable_installs_match_ci` (static) plus 133's runtime probe, both after the fact |
| 9 | `49613626` (#3760), `f9aa190b` (#3782), `2e44dd36`, `93db6507` (#4103) | Gate width sized to host, not cgroup; zero headroom; reserve bound; fit oracle reserve | cgroup readers are tested only against patched paths; four fixes to one fact |
| 10 | `7dbce0da` (#4080) | Host loadavg divided by container quota | The quota change for #9 silently moved another sensor's denominator |
| 11 | `baa64dbb` (#3895) | Gate duration inside the cgroup exceeds the selfcheck cap | In-image suite duration is never measured pre-merge |
| 12 | `b98373af` / `0350399a` / `d16b7ed1` (#3588) | Unbounded `mcp` resolved 2.x at image build and broke the in-proc shim | Image build first happens at deploy |
| 13 | `cc02e7cc` (#3637) | Prod overlay never exercised behaviourally | CI runs `MAESTRO_ENV` unset |
| — | #4160 (open) | `cli_paced` gate hook runs `maestro` off PATH; exit 127 fails open | `check_secrets` only `which`es `claude` |
| — | #4132 (open) | GitHub MCP launcher names an npm package that 404s | `check_mcp_launchers` only `which`es `npx` |
| — | (new) | `/var/lib/docker` absent in-container; disk arm dead | No in-image readability check |

Thirteen shipped fixes plus two open and one latent defect, all the same
class. The design **retires** ledger rows 6, 7 and 8's static guards
(`_LAUNCHER_APT_PACKAGES` and its two tests, the `.claude.json` string-assertion
tests, `test_sibling_editable_installs_match_ci` and its seeded control),
replacing each with a behavioural in-image check. It folds `check_mcp_launchers`
/ `MCP_LAUNCHER_INSTALL_HINTS` and the ad-hoc `shutil.which("claude")` in
`_check_provider_credential` into contract projections (steps 9–10). The
runtime fixes (gosu, cgroup readers, entrypoint installs) stay: they are
correct. What changes is that each becomes a declared, probed contract entry
instead of an unguarded fact.

## Relevant Findings

- `.github/workflows/quality-gates.yml` (`quality`, `browser-tests`,
  `package-quality` jobs):
  - All run natively on `blacksmith-4vcpu-ubuntu-2404` via
    `.github/actions/py-setup`.
  - No job runs `docker build` or executes in the image.
  - The `quality` install line installs `maestro_sdk` but not `maestro_review`,
    so CI does not even match the image's package set.
- `scripts/safe-deploy.sh`:
  - The only place the image is exercised: post-merge, on the prod host.
  - Rebuilds only when `pyproject.toml` / `Dockerfile` / entrypoint /
    `constraints.txt` change or the `pyproject.sha256` label mismatches.
  - Runs `docker compose run loop preflight` and `--smoke --max-iters 1`, both
    with `MAESTRO_SKIP_PIP_SYNC=1`, so the entrypoint's writable-path install is
    never smoke-tested.
  - Failures roll back, which protects prod but blocks every later deploy
    (#3054: "an unbroken wall of failure").
- `maestro/cli.py` (`cmd_preflight`):
  - Checks config, secrets/auth, MCP launchers, `ensure_dirs`, schema, loads,
    core imports, and `gate_environment.probe_provisioning`.
  - It is the nearest thing to a contract, but it requires credentials and
    network, so it cannot run in CI, and it covers only the facts someone added
    after an incident.
- `maestro/config.py`:
  - `Config.check_mcp_launchers` resolves only the launcher binary (`npx`) with
    `shutil.which`, never the package it launches (#4132).
  - `Config._check_provider_credential` `which`es `claude` but not the `maestro`
    binary that `GATE_HOOK_COMMAND` needs (#4160).
- `maestro/gate_environment.py`:
  - `probe_provisioning` and `_SIBLINGS` are proposal 133's precedent for this
    design: a declared registry plus an AST guard
    (`tests/test_gate_environment_owner.py`) that every first-party sibling
    import is declared.
  - It covers one fact family (distributions vs. PEP 420 namespaces) and runs
    only at runtime.
- `maestro/gate_capacity.py`:
  - Covers `measure_host_capacity`, `_available_cores`, and the
    `_read_cgroup_v2_*` / `_read_cgroup_v1_*` readers.
  - These are the correct post-#3760 readers, tested only through patched paths
    (`tests/conftest.py` `_neutralize_gate_lease` patches
    `measure_host_capacity` to a fixed `HostCapacity` for every test).
- `maestro/host_capacity.py` (`_read_docker_root_free_gib`, `read_host_capacity`):
  - Stats `/var/lib/docker`, which compose does not mount into the loop
    container.
  - Returns `None` in production on every call.
- `maestro/providers/cli_paced_gate.py` (`GATE_HOOK_COMMAND`): the bare string
  `maestro _gate-bash-hook`, resolved through the inherited PATH, with a
  fail-open exit-127 path (#4160).
- `tests/test_dockerfile_structure.py` (`_LAUNCHER_APT_PACKAGES`,
  `test_production_mcp_launchers_provisioned`,
  `test_non_apt_launcher_requires_dockerfile_check_not_apt`,
  `test_sibling_editable_installs_match_ci`,
  `test_sibling_install_parity_guard_catches_missing_install`):
  - Each is one whacked mole expressed as a regex over Dockerfile text.
  - They cannot see what `apt` actually installed, what `PYTHONPATH` shadows,
    or what the entrypoint does at runtime.
- `tests/test_docker_entrypoint_install.py` (`TestEntrypointStructure`):
  - Asserts the entrypoint *contains* the `.claude.json` seed and the `[ -w ]`
    gate.
  - `TestWritabilityCheckSemantics` runs `[ -w ]` on a host `tmp_path`, not in
    the image.
- `docker-compose.yml`:
  - `loop`: `cpus: '2.0'`, `memory: 2G`, `init: true`, `.:/workspace` rw,
    `MAESTRO_ENV=production`.
  - `dashboard`: `.:/workspace:ro` plus writable `./data`.
  - These are the exact constraints a pre-merge in-image run must reproduce.
- `deploy/placement.toml` and `tests/test_workload_placement.py`: every workflow
  `runs-on` must be declared, so the new CI job must register itself there
  (proposal 125).

## Implementation Plan

### Phase A — One owner, declared

1. [x] **Contract schema and loader.**
   - Files: new `deploy/runtime_contract.toml`, new `maestro/runtime_contract.py`.
   - The TOML declares entries of a closed set of kinds:
     - `binary` (name, the feature/flag that requires it, the provisioning hint)
     - `distribution` (import name that must resolve to a real distribution,
       absorbing 133's `_SIBLINGS`)
     - `writable_path` (path template, e.g. `{home}/.claude.json`,
       `{memory_dir}/logs`)
     - `readable_fact` (a named host/cgroup reader, e.g. `cgroup_cpu_quota`,
       `docker_root_free`)
     - `identity` (uid/gid, HOME)
     - `toolbelt` (shell tools the agent's prompts may assume)
     - `launcher_package` (the npm/uvx package an enabled MCP launcher fetches)
   - Each entry carries `required_when` (a `Config` flag expression, or
     `always`), so a disabled feature's binary is not required.
   - Each entry also carries `scope` (`container`, the default, or `host`). A
     `host`-scoped entry is a fact only the host side can read (step 11's
     option for `docker_root_free`); the in-container evaluation reports it
     `host-only` rather than evaluating it.
   - `load_contract()` returns frozen dataclasses. The module is pure: no
     filesystem probing.
   - *Depends on:* none.
   - *Acceptance:* `load_contract()` parses the shipped TOML into a
     `RuntimeContract` with at least one entry of every kind, and rejects an
     unknown kind or a malformed `required_when` with a named error.
2. [x] **Pure evaluator plus thin reader.**
   - File: `maestro/runtime_contract.py`.
   - `evaluate(contract, config, observations) -> ContractVerdict` is pure over
     an injected `Observations`, with one `Finding(entry, status, detail)` per
     required entry.
   - `observe(contract, config)` is the only impure function. It reuses
     `shutil.which`, `gate_environment.probe_provisioning`'s spec resolution,
     `gate_capacity`'s cgroup readers, and `host_capacity`'s readers, so no fact
     gains a second reader.
   - A `readable_fact` that returns `None` is a finding, not a pass. This
     catches the dead `/var/lib/docker` arm.
   - For a `writable_path`, `observe` creates the path if absent and takes a
     `file_lock` on it (a directory is probed with a created-then-removed
     sentinel file), as the runtime user. The write check lives here, outside
     pytest, because proposal 138's live-state fence refuses any test-process
     write under a production `memory_dir` or `~/.maestro`, and it admits no
     allowlist.
   - *Depends on:* 1.
   - *Acceptance:* for every entry kind there is a unit test with a satisfying
     and a violating `Observations`, and `evaluate` returns `ok=False` for any
     violating entry whose `required_when` holds.
3. [x] **Declare-before-use AST guard.**
   - File: new `tests/test_runtime_contract_owner.py`.
   - Walk `maestro/` by `ast` and fail when any of these names or reads
     something the contract does not declare:
     - a list/tuple literal argv whose first element is a string literal,
       passed to a `subprocess.*` call **or to an argv wrapper**. The guard
       first collects the wrappers structurally: any function or method in
       `maestro/` that forwards one of its parameters as the argv of a
       `subprocess.*` call (the `self._run([...])` shape in `maestro/vcs.py`,
       which is how most `git`/`gh` argv literals reach a subprocess). Keying
       on the collected wrapper set, not on every call, keeps ordinary list
       literals such as `", ".join([...])` out of view.
     - the positional string-literal first argument of
       `asyncio.create_subprocess_exec`, which takes argv as separate
       positional arguments rather than a list
     - a `shutil.which(<literal>)` argument
     - a module-level `*_COMMAND` string constant's first token (the
       `GATE_HOOK_COMMAND` shape)
     - a `Path`/`open` literal under `/sys/fs/cgroup`, `/proc`, or
       `/var/lib/docker`
     - a `Path.home()` / `expanduser("~")` use
   - The test includes a seeded-offender control per detected shape (a direct
     `subprocess.*` argv and a wrapper-routed argv among them), following the
     `test_sibling_install_parity_guard_catches_missing_install` pattern, so the
     guard cannot pass vacuously.
   - *Depends on:* 1.
   - *Acceptance:* the guard passes on the tree once the contract is complete.
     Adding `subprocess.run(["jq", ...])`, or `self._run(["jq", ...])` through
     an existing wrapper, to any `maestro/` module fails it, naming `jq` and the
     file.

### Phase B — Verified inside the image, before merge

4. [x] **`maestro runtime-contract` subcommand.**
   - File: `maestro/cli.py`.
   - Runs `observe` → `evaluate` and prints a per-entry table (`--json` for
     machines). It exits non-zero on any required violation.
   - It dispatches before `check_secrets` and reaches no network, credential,
     SDK, or host seam, so it can run in CI with no secrets. `launcher_package`
     entries are reported `unverified` unless `--network` is passed.
   - *Depends on:* 2.
   - *Acceptance:* with injected observations, the command exits 0 on a
     satisfied contract and 1 on a violated one. `--json` output round-trips
     through `ContractVerdict`.
5. [ ] **In-image contract job in CI.**
   - Files: `.github/workflows/quality-gates.yml`, `deploy/placement.toml`,
     `docs/ci.md`.
   - A new `container-contract` job builds the `production` target with buildx
     and a registry/GHA layer cache.
   - Before `docker run` it chowns the checkout to `1002:1002`. In production
     the bind-mounted tree is owned by the runtime user; on the runner it is
     owned by the runner UID, and the entrypoint chowns only `$MEMORY_DIR`
     (non-recursively) and its `logs` before `gosu`, so without this step the
     dropped user could not write `/workspace` and every `.pytest_cache` /
     editable-install write would fail for a reason production never sees.
   - It then runs the image **as compose runs it**: `--cpus 2 --memory 2g`,
     `--init`, the checkout bind-mounted at `/workspace`, `MAESTRO_ENV=production`,
     the unmodified entrypoint (pip-sync path included, so the editable-install
     path is finally exercised), `gosu` drop to 1002.
   - In that container it runs, in order:
     1. `maestro runtime-contract --json --network`. This is where
        distribution resolution (the PEP 420 `maestro_sdk` shadow, #3872) and
        writability are checked, because it runs outside pytest: pyproject's
        `[tool.pytest.ini_options] pythonpath = ["maestro_sdk/src"]` prepends
        the source tree, so inside any pytest process `maestro_sdk` always
        resolves as a regular package and a missing editable install is
        invisible there.
     2. `python -m pytest --collect-only -q --no-cov tests/` (catches the
        remaining import-drift collection errors in seconds)
     3. `python -m pytest -m container --no-cov tests/container/`. `--no-cov`
        is required, as for the browser lane: `addopts` carries
        `--cov-fail-under=100` over `maestro`/`maestro_review`, which a lane
        covering a sliver of `maestro/` can never meet.
   - A second variant re-runs step 1 with the dashboard's mount shape
     (`/workspace:ro` plus writable `data`) to cover the read-only path.
   - *Depends on:* 4, 11. Step 11's provisioning lands first (or in the same
     PR), so the job is green on `main` from its first run: the live probe shows
     `ps`/`pgrep`/`free`/`jq` missing and `docker_root_free` reading `None`,
     both violations under step 2 until step 11 provisions or re-scopes them.
   - *Acceptance:*
     - The job is declared in `deploy/placement.toml` and passes
       `tests/test_workload_placement.py`.
     - It is green on `main` from the first run.
     - On a branch that removes `-e "maestro_sdk/"` from the `Dockerfile`, the
       job fails at the contract step, naming the `maestro_sdk` distribution
       entry.
     - On a branch that removes `nodejs npm`, it fails at the contract step.
6. [ ] **Container-marked behavioural tests.**
   - Files: new `tests/container/`, `pyproject.toml` (the `container` marker
     registration, and the `addopts` edit from `-m 'not browser'` to
     `-m 'not browser and not container'`).
   - A `container` marker is excluded from the default run exactly like
     `browser`. These tests assert *behaviour inside the image*:
     - `gate_capacity.measure_host_capacity()` reports `cores == 2` and
       `available_mib <= 2048` under the imposed limits (#3760 / #3782 / #4103).
     - `host_capacity.read_host_capacity()` has no `None` field other than
       one whose `readable_fact` entry the contract declares `scope = "host"`
       (the `/var/lib/docker` arm, per step 11's choice).
     - The `maestro` binary on PATH executes `_gate-bash-hook` with exit 0
       (#4160).
     - `Config.from_env()` loads under `MAESTRO_ENV=production` and
       `validate_provider_capabilities` passes (#3637's overlay load).
   - Two facts are deliberately *not* in this lane, because pytest cannot
     observe them truthfully; step 5's contract step owns them:
     - Writability of `{home}/.claude.json` and every declared
       `writable_path` (#2573, ledger row 2): the live-state fence refuses the
       write.
     - `maestro_sdk` resolving to a real distribution, never a namespace
       (#3872): pytest's `pythonpath` masks it.
   - *Depends on:* 5.
   - *Acceptance:* each test above fails when its fact is seeded wrong in a
     throwaway image build. The PR that lands this step records one seeded-red
     run per test in its description.
7. [ ] **In-image fit measurement on env-path changes and nightly.**
   - File: `.github/workflows/quality-gates.yml`.
   - When a PR touches `Dockerfile`, `docker-compose.yml`,
     `scripts/docker-entrypoint.sh`, `constraints.txt`, `pyproject.toml`,
     `maestro/gate_*.py`, or `maestro.production.toml`, and nightly on `main`,
     the job runs the full maestro suite inside the limited container.
   - It fails if the measured `tests_seconds` exceeds the production overlay's
     resolved gate budget (the proposal 134 lease deadline, capped at
     `[timeouts] selfcheck_max`).
   - This measures the fit class pre-merge instead of discovering it as
     `TIMEOUT` rows in `gate_runs.jsonl`.
   - *Depends on:* 5.
   - *Acceptance:* the job emits the measured `tests_seconds` and the resolved
     budget as a step summary, and fails when measured exceeds budget.

### Phase C — Same probe at deploy and startup

8. [x] **Contract in preflight and startup, behind a flag.**
   - Files: `maestro/cli.py` (`cmd_preflight`, `_build_agent`),
     `maestro/config.py` and `maestro/config_settings.py` (the flag, declared
     in both as every `[loop]` flag is), `maestro.toml`,
     `maestro/flag_lifecycle.py`.
   - `[loop] runtime_contract_enforced` is default-off and registered in
     `EXPERIMENTAL_FLAGS` with a `review_by` horizon.
   - When on, `preflight` and `_build_agent` fail loud on any required
     violation. When off, violations are logged at WARNING with the entry name.
   - `safe-deploy.sh` gets this for free because its smoke already runs
     `preflight`.
   - *Depends on:* 2.
   - *Acceptance:* with the flag on and an injected missing `npx` for an enabled
     launcher, `cmd_preflight` exits non-zero naming the contract entry. With it
     off, the same input logs a WARNING and preflight continues.
   - *As landed:* for an *enabled* launcher, preflight check 2b and
     `_build_agent`'s `check_mcp_launchers` still exit 1 on a missing `npx`
     before the contract is folded, whatever the flag says, so that input stays
     owned by the launcher check until step 9 folds it into the contract; the
     acceptance is pinned with an `always`-required `binary npx` entry instead.
     The `_build_agent` call covers every caller that builds an agent (`loop`,
     `serve`, `run`, `resume-task`, `contemplate`, `evolve`,
     `workspace-counterfactual`), not only loop startup.
9. [ ] **Fold launcher and provider checks into contract projections.**
   - File: `maestro/config.py`.
   - `Config.check_mcp_launchers` and the `claude` `which` in
     `Config._check_provider_credential` become thin calls that evaluate the
     `binary` / `launcher_package` entries for the enabled features.
   - `MCP_LAUNCHER_INSTALL_HINTS` moves into the contract's per-entry
     `provision` field. `GATE_HOOK_COMMAND`'s `maestro` becomes a declared
     `binary` with `required_when = "cli_paced routed"` (closes #4160).
   - The GitHub launcher's package becomes a `launcher_package` entry that the
     CI `--network` step resolves (turns #4132 red pre-merge).
   - *Depends on:* 8.
   - *Acceptance:*
     - `MCP_LAUNCHER_INSTALL_HINTS` no longer exists.
     - `check_mcp_launchers`'s existing tests pass unchanged against the
       projection.
     - A cli_paced route with `maestro` absent from PATH fails
       `check_secrets` / preflight.
10. [ ] **Absorb `_SIBLINGS` into the contract.**
    - Files: `maestro/gate_environment.py`, `tests/test_gate_environment_owner.py`.
    - `probe_provisioning` reads its sibling set from the contract's
      `distribution` entries. The AST sibling-import guard now checks membership
      in the contract, one registry instead of two.
    - *Depends on:* 3.
    - *Acceptance:* `_SIBLINGS` no longer exists as a literal.
      `tests/test_gate_environment_owner.py`'s seeded-offender control still
      fails on an undeclared sibling import.

### Phase D — Provision the declared contract, then retire the proxies

11. [x] **Provision the declared toolbelt and fix the dead arm.**
    - Files: `Dockerfile`, `maestro/host_capacity.py`, `docker-compose.yml`,
      `deploy/runtime_contract.toml`, `docs/deployment.md`.
    - Lands before step 5 (or in its PR) despite its phase placement, so the
      in-image job is never red on `main` for a fact already known missing.
    - Install the `toolbelt` entries the contract declares (`procps` for
      `ps`/`pgrep`/`free`, `jq`). The prompts already assume them.
    - For `docker_root_free`, either mount the Docker root read-only into the
      loop container or re-declare the fact `scope = "host"` and move its
      reading to the host-side deploy agent. Choose by the operator's
      preference at plan time.
    - The contract step in CI proves which choice was made once step 5 lands.
    - *Depends on:* 1.
    - *Acceptance:* `maestro runtime-contract` run in the built image reports
      every `toolbelt` entry satisfied and `docker_root_free` either satisfied
      or `host-only`; step 5's first run reports the same.
      Zero `command not found` for a declared tool is attainable by construction.
12. [ ] **Retire the static Dockerfile/entrypoint proxies.**
    - Files: `tests/test_dockerfile_structure.py`,
      `tests/test_docker_entrypoint_install.py`.
    - Delete `_LAUNCHER_APT_PACKAGES`, `test_production_mcp_launchers_provisioned`,
      `test_non_apt_launcher_requires_dockerfile_check_not_apt`,
      `test_sibling_editable_installs_match_ci`,
      `test_sibling_install_parity_guard_catches_missing_install`, and the
      `.claude.json` seed string assertions in `TestEntrypointStructure`.
    - Each is superseded by a behavioural in-image check from steps 5–6. Keep
      the formatting/UTF-8 guards, which are not environment facts.
    - Land only after step 5's job has been required-green on `main` for the
      burn-in window.
    - *Depends on:* 5, 6, and a burn-in window of ≥ 14 days with the job
      required.
    - *Acceptance:* none of the listed symbols exist, and
      `container-contract` is a required status check on `main`.
13. [ ] **Collapse the enforcement flag.**
    - Files: `maestro/flag_lifecycle.py`, `maestro/config.py`,
      `maestro/config_settings.py`, `maestro.toml`.
    - Once `[loop] runtime_contract_enforced` has been on everywhere through its
      window with zero false-positive preflight failures, move it to
      `CUTOVER_FLAGS` and delete the WARNING-only branch.
    - *Depends on:* 8, 9.
    - *Acceptance:* the flag no longer exists, and preflight's contract
      violation path is unconditional.

## Testing Strategy

- **Unit (`tests/test_runtime_contract.py`, `mem_fs`, no real I/O).**
  - `load_contract`: happy path, unknown kind, malformed `required_when`.
  - `evaluate`: every kind × {satisfied, violated, not-required}.
  - A `None` reading is a violation.
  - The `required_when` flag expression is evaluated against a `Config` built
    from the production overlay.
  - `observe` is tested with `shutil.which`, spec resolution, the cgroup
    readers, and the `writable_path` create/lock probe patched, and asserts it
    calls the *existing* readers, which pins that no second reader appears.
- **CLI.** `maestro runtime-contract`:
  - The exit code on satisfied and violated contracts.
  - The `--json` shape.
  - `--network` absent → `launcher_package` reported `unverified`, not `ok`.
  - Dispatch never calls `check_secrets` (a mock asserts it is uncalled).
- **Structural guard (`tests/test_runtime_contract_owner.py`).** The
  declare-before-use AST walk with a seeded-offender control for each detected
  shape (direct argv literal, wrapper-routed argv literal, positional
  `create_subprocess_exec` argv, `which` literal, `*_COMMAND` constant,
  cgroup/proc path, `Path.home()`), following `tests/test_gate_environment_owner.py`. This is the
  deterministic, zero-LLM-cost enforcement that stops the next undeclared
  dependency.
- **Preflight / startup (flag on and off).** Injected violations produce
  fail-loud versus WARNING, covering the guard branch and the fallback branch
  per CLAUDE.md.
- **Container lane (`tests/container/`, `-m container`).** The lane is excluded
  from the default and coverage run, like `browser`. It runs only inside the
  built image in the `container-contract` CI job. Like the browser lane, it is
  the declared exception to "no real system services": it exists precisely to
  read the real cgroup, PATH, and filesystem. Each test's fault-seeded red run
  is recorded in its landing PR.
- **Existing tests that must stay green unchanged:**
  - `tests/test_gate_environment*.py`
  - `tests/test_config.py` launcher tests (via the projection)
  - `tests/test_workload_placement.py` (the new job is declared)
  - `tests/test_no_line_citations_in_docs.py`

## Risk Assessment

- **CI cost and latency.** Building the image adds minutes per PR. Mitigations:
  - A buildx layer cache: the dependency layers change only with
    `pyproject.toml` / `constraints.txt`.
  - Only the cheap steps (contract, collect-only, container-marked tests) run
    on every PR.
  - The full-suite fit run (step 7) is path-filtered plus nightly.

  If the per-PR increment exceeds ~3 minutes p50 after caching, the job moves
  to path-filtered plus nightly as a whole. That is a documented fallback, not
  silent removal.
- **False positives at startup.** A contract entry declared `always` that is
  actually feature-scoped would refuse to start the loop. Mitigations:
  - Step 8 ships WARNING-only by default.
  - The CI job proves every required entry against the real image before the
    flag can flip.
  - `required_when` scoping is unit-tested against the production overlay.
- **Guard over-reach.** The AST guard could flag legitimate dynamic argv. It
  only inspects *literal* first elements passed to `subprocess.*` or a
  collected argv wrapper, and `*_COMMAND` constants, so dynamic argv and
  unrelated list literals are out of its view by construction. The allowlist, if one is ever
  needed, follows the `_ENTRY_HANDLER_BASELINE` shrink-only pattern.
- **Blacksmith Docker availability.** The managed pool supports Docker. If a
  runner lacks buildx, the job fails loud rather than skipping. There is no
  `continue-on-error`.
- **Rollback.** Every phase is independently revertible:
  - Phase A adds a pure module and a test.
  - Phase B adds a CI job, which can be made non-required.
  - Phase C is flag-gated.
  - Phase D's retirements come last and only after the replacement has been
    required-green.

  No state-file or schema change is involved.

## Falsifiability

- **Metric.** The share of `gate_runs.jsonl` rows with an environment outcome
  (`COLLECTION_ERROR`, `TIMEOUT`, and 133's `UNPROVISIONED` / `UNCOLLECTABLE` /
  `UNFIT`) over a 30-day window after step 5 is required. It should drop to
  ~0% (baseline: 52.9% over 2026-09-09 → 09-26).
- **Secondary.**
  - Count of `[rca]` / `[bug-hunt]` issues whose root cause is an image,
    mount, cgroup, PATH, or overlay fact and which were first observed in
    production. Baseline: ≥ 13 over 2026-04 → 09 per the ledger. Target: zero
    new ones whose fact was declarable.
  - Deploy-smoke rollbacks attributable to an image fact. Baseline: #3054 and
    #3588 each produced multi-run failure walls.
  - `command not found` tool results for declared toolbelt binaries.
    Baseline: 23 over 2026-08-24 → 09-26.
- **The design has failed if** an environment-caused production incident lands
  whose fact *was* declared in the contract and the in-image job was green. That
  would mean the CI container does not reproduce production, and the
  reproduction (limits, mounts, user, overlay) is what needs fixing, not
  another contract entry.

## Out of scope

- **Host-level capacity and disk exhaustion** (#3632 ENOSPC) — owned by
  `safe-deploy.sh`'s disk guard and proposal 125's host re-architecture. The
  contract covers what the *container* must provide, not host housekeeping.
- **Env-file credential staleness across `docker start`** (#2855): an operator
  runbook fact about compose recreate semantics. The contract checks that
  `GITHUB_TOKEN` is *present*, not that it is the operator's latest value.
- **Cross-container process topology** (#4075, the dashboard reading loop-only
  in-memory fields). This is a single-owner-of-state defect between two
  processes, not an image fact. Catching it needs a two-service compose test,
  which is a candidate follow-on for the container lane but not designed here.
- **The fit lease's width/deadline policy itself.** Proposal 134 owns it. Step 7
  only *measures* in-image fit pre-merge against the budget 134 resolves.
- **Proposal 026's sandbox container.** If it lands, its image joins this
  contract as a second target. That is not designed here.
- **The 1 MiB SDK-frame crash cluster** (2026-09-03 → 09-07). It is not an
  environment fact. The tool-result frame cap invariant owns it.
