# 126: Gate Execution Lease — One Owner for How Much of the Host a Gate Run May Take, and When It May Start

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `scripts/quality-gates.sh`, `.claude/commands/impl.md`, `.claude/commands/polish.md`, `.claude/commands/process-comments.md`, `maestro/evolution.py`, `maestro/gate_check.py`, `maestro/loop/pr_remediation.py`, `maestro/cli.py`, `maestro/config.py`, `maestro/config_settings.py`, `maestro/flag_lifecycle.py`, `maestro.toml`, `.github/workflows/quality-gates.yml`, new `maestro/gate_capacity.py`, new `tests/test_gate_capacity.py`, new `tests/test_gate_lease_owner.py`, new `tests/support/gate_sites.py`, `tests/test_gate_command_owner.py`, `tests/test_quality_gate_scheduler.py` |
| **What landed** | Phases A–D in full, steps 1-15 — the owner, its seams, the flag, every one of the eight gate sites routed through it, the serial-fallback amplifier retired, CI parallelism restored, one gate-run telemetry row recorded per admitted run, and the falsifiability read surface. Step 1: `maestro/gate_capacity.py` owns the capacity fact — the `LeaseAdmission` `Literal`, frozen `HostCapacity` (`cores`, `available_mib`) and frozen `GateLease` (`workers`, `slots`), and the pure zero-I/O `plan_lease(capacity, in_flight)` taking the smaller of a memory-bound budget (available memory less `SYSTEM_RESERVE_MIB` and the in-flight peers' `SESSION_BASELINE_MIB`, over `PER_WORKER_MIB`) and a core-bound budget split across the runs sharing the host, clamped to `[1, cores]`, with degenerate capacity falling to the one-worker/one-slot floor. Step 2: the confined `acquire_slot(lock_dir, slots)` context manager — a counting semaphore probing `gate-slot-{index}.lock` files with `LOCK_EX | LOCK_NB` via `_hold_first_free_slot`, released in a nested `try/finally` on normal exit and on exception alike, borrowing the shape of `maestro/fileutil.py`'s `file_lock` — plus the two live-host readers `measure_host_capacity` (`_available_cores` via `os.sched_getaffinity`, `_read_available_mib` via `/proc/meminfo`'s `MemAvailable`) and `count_in_flight` (a *shared*-lock probe so concurrent counters do not read each other as holders). Step 3: the `maestro gate-lease -- <command>` exec wrapper (`cmd_gate_lease` / `_run_gate_child`, `maestro/cli.py`), substituting a `{workers}` placeholder across the child argv, holding one slot for the child's whole lifetime, counting `in_flight` only *after* admission, failing open at the one-worker floor on a saturated host, and surfacing the child's status (`128 + N` on signal death, `127` on a missing binary); `maestro/tool_gating.py` unwraps a `gate-lease`-wrapped segment — bounded against a `gate-lease`-wrapping-`gate-lease` chain — so the wrapper cannot become an action-gate bypass. Step 4: `maestro gate-classify` (`cmd_gate_classify`) reads a captured run's output on stdin and prints one `GateObservationKind` token via `gate_observation.parse_gate_output`, or via `parse_non_answer` when `--kind` names the non-answer, always exiting 0 so the classification is the output rather than the status. Step 5: `[loop] gate_lease_enabled`, default-off in `maestro/config.py`, `maestro/config_settings.py` and `maestro.toml`, registered in `EXPERIMENTAL_FLAGS` (`maestro/flag_lifecycle.py`) with `review_by = 2027-09-30` and `gate=None`; off, the wrapper takes no slot and substitutes today's `auto` width. Step 6: the read-only `maestro gate-lease --enabled` probe, exiting 0/1 on that flag and acquiring no slot. Covered by `tests/test_gate_capacity.py`, `tests/test_cli.py`, `tests/test_tool_gating.py` and `tests/component/test_config_integration.py`. Then the two gate-site families *outside* the existing AST path guard were routed through that already-built wrapper. Step 7: `scripts/quality-gates.sh`'s `gate_maestro` now branches the parallel core suite on the `maestro gate-lease --enabled` probe, routing it through `maestro gate-lease -- … -n '{workers}' --dist loadfile` when the flag is on while keeping the flag-off `-n auto --dist loadfile` line — and the serial fallback — byte-identical (the amplifier retirement is deferred to step 12). Step 8: the three hand-run command docs (`impl.md`, `polish.md`, `process-comments.md`) route their core suite through the same wrapper unconditionally (prose cannot branch on the flag; the flag-off wrapper is transparent), keeping `--dist loadfile` and naming no literal worker count. Step 10: `tests/test_quality_gate_scheduler.py` is retargeted off the `-n auto` literal onto what must survive — `--dist loadfile` present plus the routed `{workers}` placeholder in the script and every doc, the single flag-off rollback line still pinned — leaving the two serial-fallback pins for step 12. Step 11: new `tests/test_gate_lease_owner.py` is the AST/regex worker-count owner guard over the eight gate sites, which are now named once in the new `tests/support/gate_sites.py` that `tests/test_gate_command_owner.py` also imports (the shared site list is how the path guard and the worker-count guard scan one set, not two); its one carve-out is the exact flag-off rollback line. The slot-lock directory is host-scoped via the new `[loop] gate_lease_slot_dir` (default `~/.maestro/gate_slots`) so the lease bounds *cross-checkout* concurrency rather than a per-checkout `memory_dir / "gate_slots"`. Also covered by `tests/test_gate_lease_owner.py`, `tests/test_gate_command_owner.py`, `tests/test_quality_gate_scheduler.py` and `tests/test_config.py`. Step 9 then routed the three in-process Python gate readers through the same owner via a shared `lease_for_gate_run(slot_dir)` seam in `maestro/gate_capacity.py` and the pure `worker_args(lease)` argv atom: `EvolutionEngine.run_quality_gates` (`maestro/evolution.py`), `build_gate_commands` / `run_gate_check` (`maestro/gate_check.py`, threaded from `cmd_gate_check` and `preflight` in `maestro/cli.py`) and the remediation gate build in `maestro/loop/pr_remediation.py` each hold one slot across their whole ruff -> mypy -> pytest sequence and splice the granted width into the pytest argv, keyed off the derived `Config.gate_lease_dir` (`None` while the flag is off, so `worker_args` returns `()` and every command list stays byte-identical). Covered by `tests/test_gate_capacity.py`, `tests/test_evolution.py`, `tests/test_gate_check.py`, `tests/test_pr_remediation.py` and `tests/test_cli.py`. Step 12 then retired the serial-fallback amplifier in `gate_maestro` (`scripts/quality-gates.sh`): a failed parallel run is captured and piped through `classify_gate_run` to `maestro gate-classify`, which retries only the resource-artifact class (`COVERAGE` / `TIMEOUT` / `UNAVAILABLE`, the latter two named via `--kind` from the wrapper's `127` / `128 + N` statuses) exactly once at the leased width, while an answered red (`FAILED` / `COLLECTION_ERROR`) is returned immediately with no second run; the two serial-fallback pins in `tests/test_quality_gate_scheduler.py` and the matching prose in the three command docs (`impl.md`, `polish.md`, `process-comments.md`) are replaced by classified-retry assertions, retiring ledger entry 4. Covered by `tests/test_quality_gate_scheduler.py` and `tests/test_cli.py`. Step 13 then un-deformed CI (`.github/workflows/quality-gates.yml`, PR #3754): the `quality` job runs `pytest … -n auto --dist loadfile` across cores again now that it owns a dedicated single-tenant Blacksmith VM, and the stale serial-on-purpose justification was rewritten, retiring ledger entry 3. Step 14 then made the improvement measurable (`maestro/gate_capacity.py`): the frozen `GateRunRecord`, the `file_lock`-serialised `record_gate_run` / corruption-tolerant `_parse_gate_run_dict` append/parse pair (mirroring the cost-samples ledger), and `observation_outcome` collapsing a run's per-gate `GateObservationKind`s worst-first (via `_OUTCOME_PRECEDENCE`) into one recorded outcome; `lease_for_gate_run` now yields a mutable `GateRun` handle whose `outcome` each of the four gate sites (`run_quality_gates`, `run_gate_check`, `preflight`, `cmd_gate_lease`) stamps before context exit, appending exactly one row per admitted run to the new `Config.gate_runs_path` sidecar (under the per-checkout `memory_dir`, deliberately not the host-shared slot dir) — byte-identical rollback when the flag is off, where a `None` slot dir writes no row. Covered by `tests/test_gate_capacity.py`, `tests/test_evolution.py`, `tests/test_gate_check.py`, `tests/test_agent_lifecycle.py` and `tests/test_cli.py`. Step 15 then added the read surface (`maestro/cli.py`, `cmd_measure_gate_contention`): `maestro measure-gate-contention` joins `gate_runs.jsonl` into p50/p95 wall time bucketed by observed concurrent `in_flight`, plus the resource-artifact and fallback-firing rates, via the pure `measure_gate_contention` over `read_recent_gate_runs` (`maestro/gate_capacity.py`), with `--last` / `--json` and a clean exit on an empty ledger — matching the `measure-gate-triggers` / `measure-premise-accuracy` family. Covered by `tests/test_gate_capacity.py` and `tests/test_cli.py`. |
| **What is missing** | Nothing — step 15's `maestro measure-gate-contention` read surface has now landed, completing all fifteen steps, so the falsifiability clause is now readable over `gate_runs.jsonl`. |
| **Depends on** | 124 _(proposed; phase A landed — `gate_observation.py`, commit `6b2da363`)_, 125 _(proposed; the Blacksmith migration this builds on landed — `9427f0fc`, `d61dc135`)_ |
| **Derived from** | live host measurement via /propose, 2026-09-03 / 2026-09-04 |

## Description

> Status updated to shipped on 2026-09-09 by /plan-proposal — see [README.md](../README.md#shipped) for the landing record.

Proposal 107 gave "**which paths** do my quality gates cover?" a single owner —
the two path atoms on `maestro/work_target.py` (`WorkTarget.gate_source_path`,
`WorkTarget.gate_tests_path`) — and pinned it with an AST guard,
`tests/test_gate_command_owner.py`, so no reader may re-hardcode a gate command.
The sibling question one level up has no owner at all: **how much of this host
may a gate run consume, and may it start right now?** That fact is re-derived
independently at eight entry points. Four are the Python gate readers the AST
guard already scans, and each answers it by assuming it owns the entire machine.
The other four sit outside that guard's scope and each writes `-n auto`
literally: `scripts/quality-gates.sh`, and the three command docs
(`.claude/commands/impl.md`, `polish.md`, `process-comments.md`) that prescribe
the suite as a raw command the agent runs by hand. That unguarded half is
precisely where the deformations have accumulated — and the hand-run half is the
plausible source of the five simultaneous runs measured below.

The consequence is a superlinear collapse under multi-agent load, and a positive
feedback loop that makes it worse. Measured on the production host on
2026-09-03: six concurrent interactive Claude sessions ran five simultaneous
full-suite runs — sixteen live `xdist` workers on four physical cores, 167 MiB
free RAM and sustained 45 MiB/s swap-out — and the longest in-flight run had been
going **17m46s against the ~100s uncontended baseline** the script's own comment
claims. Contention then manufactures red twice over: `pytest`'s `timeout = 5`
with `timeout_method = "thread"` is *wall-clock*, so an oversubscribed core turns
a fast test into a failure, and an OOM-killed worker's lost coverage trips
`--cov-fail-under=100`. The remedy shipped for that (the serial fallback, PR
#3385) re-runs the whole suite serially on any parallel failure — **doubling the
load at exactly the moment the box is saturated**. The mitigation is an
amplifier.

This is not the same concern as proposal 125. 125 partitions the host *between*
workload classes and explicitly records `devshell` as "the victim, not the
cause," bounding it with systemd `CPUWeight` / `MemoryMax` drop-ins. A cgroup
bound caps the blast radius; it does not teach the gate runner that other runs
exist — under 125 alone the same five runs would still start simultaneously and
simply OOM inside their own cgroup instead of thrashing host swap. 125 owns
placement *between* tiers; this proposal owns admission *within* the interactive
tier. They compose and neither subsumes the other.

The end state is one owner — a **gate execution lease**. Every gate entry point
— Python, shell, and hand-run command doc alike — asks one pure policy function
for a `(may I start now, with how many workers)` answer derived from measured
host capacity, and receives a bounded worker count instead of picking one
itself. Naming a worker count at a gate site becomes an AST/regex failure
exactly as naming a gate path already is.
The serial fallback is replaced by the landed failure vocabulary from proposal
124: a resource artifact — a non-answer, or the coverage-only red an OOM-killed
worker leaves behind — re-acquires a lease and retries at the granted width; a
red that already enumerates real failures is returned as red without a second
full run.

## Production Evidence

Live loop state (`$DATA_DIR` = `/opt/maestro/data`, read-only) is
**thin for this class and deliberately not leaned on**: the hot
`task_history.jsonl` holds 33 rows (25 `partial` / 8 `success`) with one
`timeout` and one `cov-fail-under` keyword hit — the harm here lands on the
*operator's* interactive sessions, which the loop does not record. The evidence
below is therefore direct host measurement plus the repo's own fix history.

**Host saturation, measured 2026-09-03 23:03–23:15 UTC.** Four physical cores
(`lscpu`: 4 CPUs, 1 thread/core), 7751 MiB RAM, 4096 MiB swap:

```
load average: 8.78, 6.54, 3.79          # 1-min avg on 4 cores
Mem:  total 7751  used 6824  free 167   # MiB
Swap: total 4095  used 1899
vmstat: r=9-10  so=45260                # runnable queue; KiB/s swapped OUT, sustained
5 concurrent `pytest tests/ -n auto --dist loadfile` runs, 16 live xdist workers
longest in-flight run: 17m46s  (script comment's uncontended baseline: ~100s)
```

**Per-agent working set, same window.** `VmSwap` added back to RSS because three
sessions were partly paged out; PSS/RSS measured at 0.99 on readable processes,
so inter-tree page sharing is negligible and RSS is a fair proxy:

| Unit | Measured |
|---|---|
| Session running the full suite (`-n auto` → 4 workers) | **1620 / 1717 / 1715 MiB** across three sessions |
| Idle session (no suite) | ~300 MiB |
| One `xdist` worker, steady state | 250–380 MiB |
| Worker floor — bare interpreter → `import maestro.agent` | 10 MiB → **87 MiB** |
| Non-agent system baseline (incl. `maestro-dashboard` at 140 MiB) | 1132 MiB |

**`-n auto` is a static topology read, never an availability read.** Confirmed
against the installed `pytest-xdist` 3.8.0: `auto` resolves through
`psutil.cpu_count(logical=False)`, falling back to `len(os.sched_getaffinity(0))`
— it consults neither load average, nor free memory, nor other running gate
runs. On this host that is 4, so five concurrent runs request twenty workers for
four cores. The sixteen counted live in the sample sit exactly one run's worth
below that twenty, which is what a run outside its parallel phase looks like: a
run in its serial fallback (ledger entry 4 below) spawns no `xdist` workers at
all, and a run still collecting has not spawned its four yet. Twenty is what the
policy requests; sixteen is what was resident at the instant of the sample.

**The containerised workloads are bounded; the interactive one is not.**
`docker inspect maestro-loop` reports `Memory` = 2147483648 (2 GiB) and
`NanoCpus` = 2000000000 (2.0 CPU). The loop's own gate runs are capped. The five
concurrent interactive runs are capped by nothing.

**CI is not the bottleneck and is no longer the constraint the code assumes.**
Per-job durations on Blacksmith, 2026-09-04 11:39–11:47 UTC: `quality` 3m00s and
3m02s, `browser-tests` 3m25s, whole workflow ~6.5 min. The `quality` job still
runs serially under a comment justifying it by "the shared self-hosted runner"
— a runner that job no longer uses.

## Prior Point Fixes (whack-a-mole ledger)

Every entry treats gate-run cost as a property of *one run in isolation*. None
models concurrency across runs.

| # | Identifier | Date | What it patched | Why the class survived it |
|---|---|---|---|---|
| 1 | `f2d52731` | 2026-08-08 | Parallelised the local suite with `-n auto` to fit a gate run under the agent's Bash timeout | Tuned a single run; `auto` claims every core, so N runs oversubscribe N× |
| 2 | `350a820d` | 2026-08-08 | Pinned `--dist loadfile` so the structural-guard AST fixture is not rebuilt per worker | Bounds per-worker *fixture* cost — not worker count, not peak memory |
| 3 | `4575a73d` | 2026-08-08 | Made the CI `quality` job serial after xdist workers were OOM-killed on the shared runner | Removed parallelism instead of bounding it; now stale after proposal 125 |
| 4 | `23d9b0ff` | 2026-08-08 | Serial re-run of the whole suite on any parallel failure (PR #3385) | Converts contention into 2× load — the amplifier, not the fix |
| 5 | `8fa26a3a` | 2026-05-24 | Repaired the Stop hook's changed-file set — a purely-staged edit was invisible to it — so its `changed` scope gates every modified package | Hardened *package* selection, never test selection: any `maestro/` or `tests/` edit still runs all ~11.7k |
| 6 | `9427f0fc`, `d61dc135` | 2026-09-03 | Moved the workflows to Blacksmith (proposal 125) | Moved the *other* tenant off the box; the interactive tier's admission model is untouched |

Four of the six landed on a single day. The design below **retires 3 and 4**
outright and supersedes the `-n auto` half of 1; 2 is kept (it is correct on its
own terms), and 5 and 6 stay as-is.

## Relevant Findings

- `maestro/work_target.py` (`WorkTarget.gate_source_path`, `gate_tests_path`) and
  `tests/test_gate_command_owner.py` — the exact house precedent this proposal
  extends. One owner for a contested gate fact, substituted as a *variable* at
  four readers, with an AST guard that reds the build when a reader re-hardcodes
  it. The guard scans `gate_check.py`, `red_root.py`, `evolution.py` and
  `loop/pr_remediation.py` — deliberately Python-only.
- `scripts/quality-gates.sh` (`gate_maestro`) — writes `pytest ... -n auto --dist
  loadfile`, then on *any* non-zero exit re-runs `pytest tests/ -v` serially. It
  is one of four gate entry points outside `test_gate_command_owner.py`'s scope,
  and the only *scripted* one that names a worker count.
- `.claude/commands/impl.md`, `.claude/commands/polish.md`,
  `.claude/commands/process-comments.md` — the other three. Each prescribes
  `pytest tests/ -v -n auto --dist loadfile` as a raw command the agent executes
  by hand, so each is a full-width gate run that no script mediates. This is the
  interactive path the measurement above caught, which makes routing it
  load-bearing rather than tidy: bound only the script and the five concurrent
  runs recur unchanged, the p95 read stays flat, and the falsifiability clause
  below would acquit a model that was never actually tested.
- `maestro/evolution.py` (`run_quality_gates`) and `maestro/loop/pr_remediation.py`
  — both build `["pytest", tests, "-x", "--tb=short", "-q", ...]`: serial, no
  worker count, no admission check. Correct inside the loop's 2-CPU cgroup, and
  two more independent answers to the same question.
- `maestro/gate_check.py` (`build_gate_commands`) — a third Python answer:
  `("pytest", tests, "-q")`, serial, uncapped, invoked on demand via
  `maestro gate-check`. `red_root.py` is the fourth reader the guard scans, but
  it runs ruff and mypy only — no `pytest`, so no worker count to own today, and
  the enforcement step below is what keeps it that way.
- `tests/test_quality_gate_scheduler.py` — four static-text pins over exactly the
  strings this proposal rewrites: `-n auto --dist loadfile` present in the
  script, `-n auto --dist loadfile && return 0`, `$PY -m pytest tests/ -v ||
  return 3`, and every `-n auto` in the three command docs carrying `--dist
  loadfile` plus a serial-fallback note. It encodes today's shape as an
  invariant, so it is a deliberate step in the plan below, not collateral.
- `pyproject.toml` (`[tool.pytest.ini_options]`: `timeout = 5`,
  `timeout_method = "thread"`, `addopts` carrying `--cov-fail-under=100`) — the
  two mechanisms by which contention becomes a red verdict rather than a slow
  one. `thread` means wall-clock, so the timeout measures the *host*, not the
  test; and a worker killed for memory takes its coverage share with it.
- `maestro/gate_observation.py` (`GateObservationKind`, `parse_gate_output`,
  `parse_non_answer`, `GateObservation.signature`) — landed for proposal 124,
  the sole owner of the local gate-failure vocabulary, and the classifier the
  serial fallback should have been built on. Two of its properties bind Phase C's
  design rather than decorate it. First, the non-answer pair `TIMEOUT` /
  `UNAVAILABLE` is *supplied by the runner*, not read out of a transcript:
  `parse_non_answer` takes the kind as an argument precisely because a run that
  never answered is the one with no returncode to read, and it then salvages
  "the partial output a killed runner still wrote." The output-to-kind door is
  `parse_gate_output`. Second, the artifact the serial fallback exists for — an
  OOM-killed `xdist` worker whose lost coverage trips `--cov-fail-under=100`
  while every test passed — arrives through that door as a **`COVERAGE` red**,
  not as a non-answer (`_PYTEST_COVERAGE_RE` parses the shortfall line and
  `_red_kind` ranks it below `FAILED`). So the retry class Phase C needs is
  "non-answer, *or* coverage-only red"; a rule keyed on the non-answer pair
  alone would hand back as red the exact spurious failure the fallback was built
  to absorb.
- `maestro/fileutil.py` (`file_lock`) — the house `fcntl.flock` precedent. It is
  `LOCK_EX`-blocking, i.e. a mutex; the lease needs a *counting* semaphore, so
  this proposal reuses its shape (an adjacent lock file, released on context
  exit) rather than the function itself.
- `maestro/flag_lifecycle.py` (`ExperimentalFlag`, `EXPERIMENTAL_FLAGS`,
  `CUTOVER_FLAGS`) — the four-stage flag lifecycle every behaviour change here
  gates behind, with a dated `review_by`.
- `docs/proposals/operations/125-ci-off-host-blacksmith-and-deploy-architecture.md`
  — Decision row D records "Move `devshell`: Rejected — moves the victim, not the
  cause," and Out of scope defers resizing the host. 125 bounds this tier from
  the outside; it never touches what the tier's own tooling requests.

## Implementation Plan

### Phase A — Give the fact an owner

1. [x] **Add the pure capacity owner.** new `maestro/gate_capacity.py` — a frozen
   `GateLease` (`workers`, `slot`) and a pure `plan_lease(capacity, in_flight)`
   returning the granted worker count and slot budget from measured constants
   (per-worker footprint, per-session baseline, system reserve). Pure, zero I/O,
   no subprocess — the `gate_observation.py` / `plan_grounding.py` skeleton:
   `Literal` closed vocabulary, frozen dataclasses, module-level `def`s.
   - *Depends on:* none.
   - *Acceptance:* `plan_lease` returns a monotonically non-increasing worker
     count as `in_flight` rises, saturating at a floor of 1, and never returns
     fewer than 1 worker or more than the host's core count.
2. [x] **Add the confined acquire/release seam.** `maestro/gate_capacity.py` —
   one I/O function acquiring a slot as a counting semaphore (`LOCK_EX |
   LOCK_NB` probed across N slot files, released on context exit), modelled on
   `maestro/fileutil.py` (`file_lock`). Blocking with a bounded wait; every
   other symbol in the module stays pure.
   - *Depends on:* 1.
   - *Acceptance:* with N slots held, the (N+1)th acquisition blocks until one is
     released, and a killed holder's slot is reclaimed by the OS.
3. [x] **Expose the lease to the shell as an exec wrapper.** `maestro/cli.py` —
   `maestro gate-lease -- <command>`, which acquires a slot, runs `<command>`
   with the granted worker count substituted, and releases the slot when the
   child exits. A wrapper rather than a bare printer because a subcommand that
   prints a number and exits closes its `flock` fd before `pytest` even starts,
   leaving the slot free for the entire run it was meant to reserve; and because
   the alternative — re-acquiring in bash — would restate the semaphore in a
   second language, which is the duplication this proposal exists to remove.
   This is what closes the shell-outside-the-guard gap.
   - *Depends on:* 2.
   - *Acceptance:* with the flag on, `maestro gate-lease -- <cmd>` holds one
     slot for the full lifetime of `<cmd>`, exits with `<cmd>`'s status, and
     releases the slot on normal exit, on non-zero exit, and on signal death.
4. [x] **Expose the failure classifier to the shell.** `maestro/cli.py` — a
   `maestro gate-classify` subcommand reading a captured gate run's output on
   stdin plus the run's exit status, and printing
   `maestro/gate_observation.py`'s `parse_gate_output` kind as a single
   closed-vocabulary token. `parse_gate_output` is the output-to-kind door;
   `parse_non_answer` is not, since it takes the non-answer kind as an argument,
   so a caller that already knows its run was killed or never started passes
   that kind through rather than asking the transcript for it. Step 12 needs
   this judgment inside `scripts/quality-gates.sh`, and no seam exposes it
   today; this is the same one-Python-owner / one-thin-shell-seam shape as step
   3.
   - *Depends on:* none (`gate_observation.py` landed with 124 phase A).
   - *Acceptance:* a seeded coverage-shortfall transcript prints `COVERAGE`, a
     seeded assertion failure prints `FAILED`, and a run the caller reports as
     killed prints a non-answer token — each exiting 0, since the classification
     is the output, not the exit status.
5. [x] **Register the flag.** `maestro/config.py`, `maestro/config_settings.py`,
   `maestro/flag_lifecycle.py`, `maestro.toml` — `[loop] gate_lease_enabled`,
   default-off everywhere, in `EXPERIMENTAL_FLAGS` with a dated `review_by`.
   Off ⇒ today's behaviour byte-for-byte. That covers `gate_maestro`'s literal
   `-n auto --dist loadfile` line, which the flag-off branch keeps verbatim, and
   it covers step 3's wrapper, which with the flag off takes no slot and
   substitutes today's `auto` width — so the three command docs, which step 8
   routes through the wrapper unconditionally and which carry no flag branch of
   their own, are unchanged under rollback too.
   - *Depends on:* 1.
   - *Acceptance:* with the flag off, every gate command string is identical to
     the pre-change build, and a run invoked through the wrapper takes no slot
     and runs at today's width.
6. [x] **Expose the flag to the shell.** `maestro/cli.py` — a read-only
   `maestro gate-lease --enabled` probe, exiting 0 when `[loop]
   gate_lease_enabled` is on and non-zero when it is off, so `gate_maestro` can
   branch on the flag without restating where the flag lives. The script needs
   that branch and nothing else supplies it: steps 3 and 4 expose the lease and
   the classifier, not the flag. The wrapper cannot absorb the branch either,
   because steps 10 and 11 require the literal `-n auto --dist loadfile` line to
   survive in the script as the rollback pin, and a wrapper that substitutes the
   width leaves no such literal in the script text to pin — step 5's flag-off
   wrapper transparency covers *effect*, which is enough for the command docs
   and not enough for a pinned line. Same one-Python-owner /
   one-thin-shell-seam shape as steps 3 and 4.
   - *Depends on:* 5.
   - *Acceptance:* the probe exits 0 with the flag on and non-zero with it off,
     reads the same `Config` field step 5 registers, and acquires no slot — it
     is a read, not an acquisition.

### Phase B — Route every entry point through it

7. [x] **Route the shell runner.** `scripts/quality-gates.sh` (`gate_maestro`) —
   run the core suite through step 3's wrapper, so the slot is held by the lease
   for the run's lifetime and the worker count comes from `plan_lease` instead of
   `-n auto`. Under the flag, read via step 6's probe; unset ⇒ today's line
   unchanged.
   - *Depends on:* 3, 5, 6.
   - *Acceptance:* two concurrent invocations do not run more workers between
     them than the lease grants, and the flag-off branch stays byte-identical.
8. [x] **Route the hand-run command docs.** `.claude/commands/impl.md`,
   `.claude/commands/polish.md`, `.claude/commands/process-comments.md` — replace
   the raw `pytest tests/ -v -n auto --dist loadfile` each prescribes with the
   step 3 wrapper, keeping `--dist loadfile` (ledger entry 2, explicitly kept).
   Without this the measured offender stays unbounded and the whole lease is
   unfalsifiable, so it is a first-class routing step, not documentation tidying.
   No flag branch here: prose cannot branch, and step 5's flag-off wrapper is
   transparent, so the docs prescribe the wrapper unconditionally.
   - *Depends on:* 3, 5.
   - *Acceptance:* none of the three docs prescribes a literal worker count, and
     the command each prescribes holds a slot for the run's lifetime once the
     flag is on.
9. [x] **Route the Python readers.** `maestro/evolution.py`,
   `maestro/gate_check.py`, `maestro/loop/pr_remediation.py` — take the worker
   count from the lease rather than leaving it implicit, so the loop's runs are
   visible to the same slot accounting as the interactive ones.
   `maestro/red_root.py` is the fourth reader the existing AST guard scans but
   runs no `pytest` at all, so it needs no slot and inherits only step 11's ban.
   - *Depends on:* 5.
   - *Acceptance:* a loop-side gate run occupies a slot for its duration and is
     counted in `in_flight`.
10. [x] **Retarget the scheduler pins.** `tests/test_quality_gate_scheduler.py` —
    its `-n auto --dist loadfile` assertions pin today's literal in both the
    script and the three command docs, which steps 7 and 8 rewrite. Retarget them
    onto what must actually survive: `--dist loadfile` present on every gate
    command line and in every command doc, and the worker count sourced from the
    lease rather than named. The two serial-fallback pins are left alone here —
    step 12 owns them, alongside the fallback itself. No assertion is deleted
    without a replacement.
    - *Depends on:* 7, 8.
    - *Acceptance:* dropping `--dist loadfile` from any gate command line or
      command doc still reds the build; the lease-granted path is no longer
      required to name `-n auto`, while the single flag-off fallback line still
      is, so step 5's rollback promise keeps a pin of its own.
11. [x] **Pin the new owner.** new `tests/test_gate_lease_owner.py`, and extend
    `tests/test_gate_command_owner.py` — no gate reader may name a worker count.
    AST-walk the four Python gate readers for an `-n` / `"auto"` / integer-worker
    constant in a gate command; regex-scan `scripts/quality-gates.sh` and the
    three command docs for the same, since they are not Python. Exactly one
    carve-out, matched as a whole exact literal line so it cannot spread: the
    single flag-off fallback line in `gate_maestro` that step 5's rollback
    criterion requires to stay byte-identical. Runs regardless of the flag.
    - *Depends on:* 7, 8, 9, 10.
    - *Acceptance:* re-introducing a worker count at any of the eight gate
      sites, outside the one carve-out, reds the build — including a seeded
      offender placed in the carve-out's own file but on a different line, so
      the carve-out cannot be widened into a hole.

### Phase C — Retire the amplifier

12. [x] **Replace the serial fallback with the 124 classifier.**
    `scripts/quality-gates.sh` (`gate_maestro`) and
    `tests/test_quality_gate_scheduler.py` — on a failed parallel run, classify
    via step 4's `maestro gate-classify` and retry only the **resource-artifact**
    class: a `TIMEOUT` / `UNAVAILABLE` non-answer, *or* a `COVERAGE` red carrying
    no enumerated test failure — the OOM-killed-worker signature the serial
    fallback exists for, which reaches the classifier as a red rather than as a
    non-answer. That retry re-acquires a lease and runs at the granted width; a
    red naming actual failures (`FAILED` / `COLLECTION_ERROR`) is returned
    immediately with no second run. The narrowing against today is the point: the
    fallback stops re-running the suite for reds that already carry their whole
    answer, and stops re-running it at full width when it does retry. Retire the
    test's two serial-fallback pins in the same step, replacing them with
    retry-shape assertions. Retires ledger entry 4.
    - *Depends on:* 4, 7, 11.
    - *Acceptance:* a seeded genuine test failure produces exactly one suite run;
      a seeded coverage-only red and a seeded killed-worker non-answer each
      produce exactly one retry at the granted width, not a full-width serial
      re-run.
13. [x] **Un-deform CI.** `.github/workflows/quality-gates.yml` — restore
    parallelism on the `quality` job now that it runs on a dedicated Blacksmith
    runner, and delete the stale "shared self-hosted runner" justification.
    Retires ledger entry 3.
    - *Depends on:* none (independent of the lease — CI is single-tenant).
    - *Acceptance:* the `quality` job stays green and its measured duration falls
      below the serial 3m00s baseline recorded above.

### Phase D — Make the improvement falsifiable

14. [x] **Record gate runs.** `maestro/gate_capacity.py` — append one row per gate
    run (start, wall duration, granted workers, observed `in_flight`, whether the
    bounded-wait fallback admitted the run, outcome kind from
    `gate_observation`) to a `gate_runs.jsonl` sidecar. Append-only, per the
    file-persistence invariant.
    - *Depends on:* 7, 9.
    - *Acceptance:* a completed gate run appends exactly one well-formed row, and
      a run admitted by the fallback rather than by a slot is marked as such.
15. [x] **Add the read surface.** `maestro/cli.py` — `maestro
    measure-gate-contention`, joining `gate_runs.jsonl` into p50/p95 wall time
    bucketed by concurrent `in_flight`, plus the resource-artifact
    (non-answer + coverage-only-red) and fallback-firing rates. Read-only,
    offline, `--json`, matching the `measure-gate-triggers` /
    `measure-premise-accuracy` family.
    - *Depends on:* 14.
    - *Acceptance:* the command reports a p95 per concurrency bucket over a
      stated window and exits 0 on an empty ledger.

## Testing Strategy

All new tests use `mem_fs`, mock every external, and patch `asyncio.sleep` /
`time.sleep` — no real subprocess, no real lock contention, no real network,
per the CLAUDE.md testing principles.

- `tests/test_gate_capacity.py` — `plan_lease` across the capacity matrix:
  single-tenant (full width), saturated (floor of 1 worker), memory-bound before
  core-bound and the reverse, and the guard branches where measured capacity is
  absurd (zero cores, zero memory). Slot acquisition is exercised against
  `mem_fs` with the `fcntl` calls patched, covering the acquired, contended, and
  released-on-exception paths.
- `tests/test_gate_lease_owner.py` — the deterministic enforcement test named in
  the design: AST-walk over the four Python gate readers plus a regex scan of
  `scripts/quality-gates.sh` and the three command docs, asserting no
  worker-count literal at any of the eight gate sites. Includes a seeded-offender
  control placed in the carve-out's own file but outside the carve-out line, so
  neither the scan nor the carve-out can pass vacuously — mirroring the shape of
  `tests/test_gate_command_owner.py` and `tests/test_overlay_safety.py`.
- `tests/test_quality_gate_scheduler.py` — retargeted in step 10 and again in
  step 12, so its own coverage moves with it: `--dist loadfile` still pinned on
  every gate command line and command doc, the `-n auto` literal no longer
  required, and the serial-fallback pins replaced by retry-shape pins. Each
  replacement lands in the same step that invalidates what it replaces.
- Extend `tests/test_gate_check.py` and the `run_quality_gates` tests — flag off
  ⇒ byte-identical command lists (the rollback proof); flag on ⇒ the granted
  worker count is substituted as a variable, never a constant.
- Fallback classification (step 12) — seeded `GateObservation` fixtures across
  the whole boundary: a genuine `FAILED` red, a `COVERAGE`-only red, and a
  `TIMEOUT` / `UNAVAILABLE` non-answer, asserting the first produces one run and
  the other two produce exactly one bounded-width retry, so the amplifier cannot
  be reintroduced silently. The coverage-only case is the load-bearing one — it
  is the artifact PR #3385's fallback was built for, and the one a
  non-answer-only rule would silently return as red.
- `tests/test_cli.py` — `measure-gate-contention` over a `mem_fs` ledger:
  populated, empty, and one corrupted row (skipped, not fatal); `gate-lease` with
  a mocked child asserting the slot is held across the child's lifetime and
  released on non-zero exit and on signal death, plus the flag-off pass-through
  asserting no slot is taken and today's width is substituted; `gate-lease
  --enabled` over both flag states, asserting the exit status flips and no slot
  is acquired; `gate-classify` over seeded stdin for each vocabulary token.

## Risk Assessment

- **The lease deadlocks or starves a gate run.** The highest-severity failure
  here would be gates that never start. Mitigations: bounded wait, then admission
  at the *minimum* grant — one worker, no slot — rather than at today's `-n auto`
  width; `LOCK_NB` probing so a dead holder's slot is reclaimed by the OS rather
  than held forever; and the flag itself, whose off-path is the current code.
  Fail-open is chosen deliberately over fail-closed — a slow gate is a nuisance,
  an unrunnable gate blocks every agent on the box. Failing open to *today's*
  width would be worse than either: the timeout only fires under sustained
  saturation, which is exactly when every waiter would then start at full width
  simultaneously, reproducing the collapse the lease exists to prevent — and the
  resulting flat p95 would be misread by the falsifiability clause below as
  evidence against the model rather than against the fallback. One worker still
  guarantees forward progress without oversubscribing. Every fallback admission
  is recorded in `gate_runs.jsonl` (step 14) and surfaced as a rate by step 15,
  so a fallback that fires routinely is visible rather than silently load-bearing.
- **A too-narrow lease makes a single-tenant run slower than today.** Real: an
  agent working alone should still get the full four workers. `plan_lease` keys
  on *observed* `in_flight`, so the single-tenant grant equals today's `-n auto`;
  step 15's per-concurrency-bucket p95 is what proves this rather than assuming
  it.
- **Restoring xdist in CI (step 13) reintroduces the OOM that made it serial.**
  The original cause was a *shared* runner; Blacksmith jobs are single-tenant.
  Step 13 is independently revertable and its acceptance criterion is a measured
  duration against the recorded 3m00s baseline, so a regression is visible in
  one run.
- **Coverage integrity.** Nothing here changes `--cov-fail-under=100`, the test
  set, or the selection of tests — only how many workers execute them and when.
  The 100% gate stays exactly as authoritative as it is today.
- **Rollback.** `[loop] gate_lease_enabled` off restores current behaviour
  byte-for-byte (step 5's acceptance criterion). Step 13 is independent of the
  flag and independently revertable. Steps 14 and 15 are additive rather than
  independent: 14 records only what the lease path produces, so the flag off
  simply stops new rows, and 15 is read-only and exits 0 on an empty ledger.
  Phase C lands only after Phase B is proven, per the "retire the moles last"
  ordering.
- **Falsifiability.** If the design is right, `measure-gate-contention` shows p95
  wall time at 4–6 concurrent sessions converging toward the single-tenant p50,
  and the resource-artifact rate — non-answers plus coverage-only reds, the two
  ways contention manufactures a false red — falling toward zero. If p95
  stays flat under concurrency after the lease is on, the model is wrong —
  serialisation is not the binding constraint and the next hypothesis is
  per-worker memory, not scheduling. That verdict is only earned once every one
  of the eight entry points is routed (steps 7–9) and the fallback-admission rate
  is near zero, since either gap leaves full-width runs starting unbounded and
  would produce the same flat p95 for reasons that have nothing to do with the
  model. Read the fallback rate first, then the p95.

## Performance

Sizing context for the operator, from the measured units above. The question is
what a target agent headcount costs in RAM, and the answer is dominated by the
admission policy, not the headcount.

Two constants carry the arithmetic, both from the measured-units table. A
session costs **0.3 GiB** idle. A worker costs **0.35 GiB**, derived as
(1.7 − 0.3) ÷ 4 — the suite-running session's footprint less its own idle
baseline, over its four workers — which lands inside the directly measured
250–380 MiB band, so the two measurements corroborate each other. Every row
below counts each session's 0.3 GiB exactly once and adds workers on top of it,
rather than adding a whole 1.7 GiB suite-running session to a baseline it
already contains.

| Policy | Arithmetic for 10 concurrent agents | Requirement |
|---|---|---|
| Today — every session free to run the full suite at once | 10 × (0.3 + 4 × 0.35) GiB + 1.1 GiB system | **~18 GiB** |
| With the lease — one full-width suite run at a time | 10 × 0.3 GiB + 4 × 0.35 GiB + 1.1 GiB | **~5.5 GiB** |
| With the lease — two concurrent runs at half width | 10 × 0.3 GiB + 2 × (2 × 0.35) GiB + 1.1 GiB | **~5.5 GiB** |

The last two rows agreeing is the point, not a coincidence: the lease caps the
*total* worker count, so how that grant is split across concurrent runs does not
change the peak. Only the unbounded row scales with headcount, and it scales by
forty workers.

**Recommendation: 16 GiB / 8 vCPU, paired with the lease** — not the ~18 GiB the
unbounded row implies, because that row describes forty `xdist` workers, a
configuration worth provisioning for only with roughly forty cores. The lease is
what makes the headcount affordable; RAM alone relocates the wall from
swap-thrash to CPU-thrash. 16 rather than 12 GiB for two reasons: the sessions
measured here run a 1 M-context model whose node heap was sampled over a single
~20-minute window and should not be sized to the low end of that sample, and swap
must stop being load-bearing — 1899 MiB of swap at 45 MiB/s sustained
swap-out is the direct cause of the 17m46s run. On cores, 8 vCPU covers both
lease rows: one four-worker run plus ten mostly API-blocked sessions.

Note that this section is *context for a hardware decision the operator owns*,
not a step in the plan. Proposal 125 scoped host resizing out; nothing in Phases
A–D depends on new hardware, and the lease is worth landing on the current box —
it is the change that converts added capacity into throughput rather than into a
higher ceiling for the same collapse.

## Out of scope

- **Test-level selection for the Stop hook.** The `changed` scope narrows
  *packages*, so any edit under `maestro/` or `tests/` still runs all ~11.7k
  tests; selecting an impacted subset is the largest single local win available.
  It is deliberately excluded because a subset is incoherent with
  `--cov-fail-under=100` and would require running the local gate `--no-cov`,
  making CI the sole coverage authority. That is a real trust-model change
  deserving its own proposal, not a rider on a scheduling fix.
- **systemd `CPUWeight` / `MemoryMax` bounding of `devshell.service`.** Owned by
  proposal 125 Phase A. Complementary: cgroup bounds cap the blast radius, the
  lease prevents the collision.
- **Resizing the production host.** An operator decision, scoped out by 125 as
  well. The Performance section informs it and does not depend on it.
- **Raising `timeout` or `--cov-fail-under`.** Both are the *detectors* that made
  this class visible. Under a correct lease the wall-clock timeout becomes a true
  signal again; loosening it would hide the next regression.
- **The `--dist loadfile` pin and the `changed`-scope detector** (ledger 2 and 5).
  Correct on their own terms and untouched.
