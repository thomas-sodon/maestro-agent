# 134: Gate Fit — One Lease Owning Width, Deadline, and Scope, So the Suite the Cgroup Can Run Is the Suite the Ceiling Allows

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:observing |
| **Affects** | `maestro/gate_capacity.py`, `maestro/gate_environment.py`, `maestro/evolution.py`, `maestro/config.py`, `maestro/config_settings.py`, `maestro/flag_lifecycle.py`, `maestro/cli.py`, `maestro.toml`, `tests/test_gate_capacity.py`, `tests/test_gate_lease_owner.py`, `tests/test_gate_environment.py`, `tests/test_evolution.py`, `tests/test_cli.py`, `tests/test_structural_guards.py`, `docs/loop-lifecycle.md`, `CLAUDE.md` |
| **Depends on** | 133 (the runnability oracle and its `UNFIT` verdict), 126 (the `lease_for_gate_run` seam, `plan_lease`, `gate_runs.jsonl`), 125 Phase A (cgroup bounding, partial) |
| **Derived from** | live production data via /propose, 2026-09-15 |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic it supersedes. See [README.md](../README.md#status-key).

**What landed.** Step 1 — the cgroup-aware memory reserve. `HostCapacity`
carries `bounded_by`, `measure_host_capacity` records which of the host and
cgroup readings produced `available_mib`, `plan_lease` omits
`SYSTEM_RESERVE_MIB` on a cgroup-bounded figure under the default-off
`[loop] gate_lease_cgroup_reserve_enabled` flag (registered in
`flag_lifecycle.EXPERIMENTAL_FLAGS`), and every `gate_runs.jsonl` row stamps
`available_mib` / `bounded_by` unconditionally.

**What landed since.** The *censored* half of step 2 — the lease owns the
pytest run's budget as well as its width. `GateLease.deadline_seconds` is
filled by `lease_for_gate_run` from the pure `derive_deadline` over the
`gate_runs.jsonl` rows at the granted width (escalating 1.5x off the most
recent in-process run killed at its own deadline, retaining a completed run's
budget as the floor, bounded by `[timeouts] selfcheck` / the new `[timeouts]
selfcheck_max`), `deadline_or_default` is its single reader, and
`_run_gate_command` spends it on the `tests` gate alone (and its enumerate
re-run). `GateRunRecord.source` / `deadline_seconds` are stamped regardless of
the default-off `[loop] gate_budget_lease_owned_enabled` flag, with every
caller but the loop's own suite naming its own `source` so an unbudgeted row
cannot reset a converging escalation. Flag-off the enforced timeout is
byte-identical. The *completed* half landed after it: `_completed_tests_p50`
medians the newest `_DEADLINE_P50_WINDOW` positive `tests_seconds` readings of
in-process rows at the granted width whose outcome ran to completion
(`_COMPLETED_TESTS_OUTCOMES`), `derive_deadline` spends it as
`min(selfcheck_max, max(selfcheck, ceil(p50 x 1.25)))` on a finished latest
row, with the serial p50 split across the width standing in when only
one-worker runs are measured and the finished row's own budget retained as the
floor otherwise.

Step 3 landed with it: `GateRunRecord.scope` names which suite a row's
`tests_seconds` measured, the budget derivation reads full-scope rows only, and
`measure-gate-contention` renders a per-width fit table.

Step 4 landed whole, in `maestro/gate_scope.py` rather than the unborn
`maestro/gate_environment.py`. Its *oracle* half came first:
`select_impacted_tests` (pure, over an injected `{test path: source}` map), its
`impacted_tests` / `read_test_sources` disk wrapper, `count_test_modules`, and
the read-only `maestro gate-scope` CLI that renders a selection and its fraction
of the suite. Its *consumption* half followed: `restrict_signature` (the pure
projection of a signature onto a narrowed suite), `run_quality_gates`' `scope=`
seam, and `converge_to_green`'s impacted inner rounds behind default-off
`[loop] gate_scope_impacted_enabled` — scope-matched baselines, a full-scope
with-coverage certification read as the only path to a `green` *or* a `stuck`
terminal, a bounded widening after a red certification, and one `gate_scope`
activation row per convergence.

**What is missing.** No behaviour-code. Both `probe_fit` clauses have since
landed: `probe_fit` takes `deadline_seconds` / `cap_seconds` from its caller and
reads `UNFIT` only once the escalation has reached `[timeouts] selfcheck_max`
(sparing a climbing reading as `SOUND` with `detail="escalating"`), both
evidence-assembly sites (`loop/plan_holds.py`'s `gate_environment_verdict` and
`gate_check.py`'s `refresh_runnability_verdict`) hand it a lease-derived budget
under `[loop] gate_budget_lease_owned_enabled` and `selfcheck` / `cap_seconds=0`
off it, and `tests/test_gate_lease_owner.py` pins both sites by `ast` against a
literal. What remains is the burn-in: the first-stage deadline flag
`gate_budget_lease_owned_enabled` runs via the `maestro.production.toml` canary
(criteria in the "Gate-fit budget canary (proposal 134)" subsection of
`docs/self-evolution.md`), its observation window opening at that PR's
production merge. The 300 s-ceiling figures below predate #3895's raise of
`[timeouts] selfcheck` to 900 s, under which the flag is inert until a run is
killed at, or its p50 × 1.25 exceeds, that floor, so the window is judged
against a post-#3895 deploy-day baseline rather than those figures; the next stages,
`gate_lease_cgroup_reserve_enabled` (the #3760 canary) then
`gate_scope_impacted_enabled`, stay default-off everywhere, and none has been
graduated to a base default.

When the local gate does reach the suite, the suite cannot finish. At the one
worker the lease grants in 328 of 350 runs, the in-process gate has not
completed a full suite once in six days of telemetry: its `TIMEOUT` rows are
pytest killed at the 300 s per-command ceiling, while the same serial suite
hand-run through `maestro gate-lease`, which no ceiling kills, takes
~370–425 s. The green suite is killed every time at that width, the
convergence loop reads the unchanging `tests:TIMEOUT` identity as a stall, and
the plan is demoted — `70ab579c26d6` this morning, after its fixer had already
run the targeted tests in 0.77 s and written, correctly, that the timeout was
the full suite and not its diff. Proposal 133 makes that red a *hold* instead
of a demotion. This proposal makes it stop happening.

The root is that "how wide may the gate run" and "how long may it run" have
two owners that never consult each other. Proposal 126's lease sizes the width
to the cgroup — correctly — but subtracts a *host*-calibrated memory reserve
(the OS and the dashboard, which live *outside* a 2 GiB cgroup) from the
cgroup's headroom, so the grant floors at one worker; and the deadline is a
`[timeouts]` constant calibrated for a four-core host that the lease never
reads. 126 explicitly refused to raise the ceiling, calling it the detector
that made oversubscription visible. It was right, and the detector is now
detecting a different, permanent fact: the suite at the granted width does not
fit. Meanwhile every convergence round re-runs the entire 13.9k-test suite with
coverage — five to seven minutes per round, the whole of it exposed to the
ceiling — although the round's only job is to converge the diff; 126 deferred
test-level selection as a trust-model change deserving its own proposal.

This proposal makes the lease the single owner of the gate's budget and scope.
The reserve becomes cgroup-aware; the pytest deadline becomes a *derived* fact
of the lease — the measured fit of in-process runs at the granted width,
escalated when a run is killed at its deadline, floored at the existing
`selfcheck` and hard-capped by a new `selfcheck_max` — rather than a bump of
the constant; and the fix loop's inner rounds probe the impacted tests with
`--no-cov` while the pre-loop read and a full-suite certification read keep
coverage, so `green` is still certified by the full gate and CI stays the
coverage authority it already is. The end state: a suite that fits at the
width the cgroup allows is never killed by a ceiling set for another machine,
a round costs what the diff costs, and 133's `UNFIT` hold releases because the
fact it holds on has stopped being true.

## Production Evidence

Live data read from `$DATA_DIR = /opt/maestro/data` (the loop
container's `/workspace/data` bind-mount, resolved via `docker inspect`), all
reads strictly read-only. The shared funnel, cost and mint-storm figures are in
proposal 133; this section carries the fit class.

**The suite does not fit at the granted width** (`gate_runs.jsonl`, 350 rows,
2026-09-09 → 2026-09-15). Rows include both the in-process gate and hand-run
`maestro gate-lease` invocations and carry no source field, so each class below
is attributed by the wrapper's outcome vocabulary (it records only `CLEAN` /
`UNAVAILABLE` / `TIMEOUT` / `UNKNOWN`) and by correlation with
`logs/maestro.log`; `duration_seconds` is whole-slot wall time, not the pytest
command's:

- `workers: 1` in **328 of 350** runs (`workers: 2` in 22, all at
  `in_flight 0`); `admission: slot` in all 350.
- `TIMEOUT` 30 rows (9%), every one 300–305 s at `workers: 1`. The 27 whose log
  window names a caller are the in-process gate — each closes on `Gate
  convergence round 1 … ['tests:TIMEOUT']` or the stall-and-demote line — so
  they are pytest runs killed at the 300 s `selfcheck`: right-censored readings
  that say the suite needs *more* than 300 s, not how much more. Only 3 of the
  30 had another run in flight — contention is not the cause; the width is.
- **No in-process full suite has completed at one worker.** The long
  one-worker `CLEAN` rows — 369, 370, 378, 386, 392, 404, 404, 424 s, and one of
  216 s — all start inside a fixer round (after its `Gate convergence round 1`
  line, before the round's own re-read), seven of the eight long ones within
  20 s of the fixer's `scripts/quality-gates.sh` call: hand-run suites through
  `gate_maestro`, which runs ruff and mypy outside the lease and then `maestro
  gate-lease -- pytest tests/ -v` under no timeout, so those rows time pytest
  alone (with `-v`, both `--cov` targets, no `-x`). They bound the serial suite
  at ~370–425 s against a 300 s ceiling. The seven 0–3 s `CLEAN` rows are
  hand-run targeted probes.
- **At `workers: 2` the in-process gate does finish:** the 2026-09-14 22:39
  `CLEAN` row (266 s, whole slot) starts one second after `Implementing plan:`
  in the log; another two-worker `CLEAN` reads 262 s. The 20 two-worker
  `FAILED` rows (295–314 s) and the one one-worker `FAILED` (368 s) cluster at
  the ceiling, consistent with a `-x` probe cut short and re-run without `-x`
  under the same `selfcheck` — a killed re-run still records `FAILED` (the
  probe, stamped `truncated`) — so they are not completed-suite timings.
- The two `gate_convergence_terminal` rows at `rounds=1` (2026-09-14 14:45
  `INHERITED`, 2026-09-15 03:42 `UNATTRIBUTABLE`) each paid one auto-fix round
  plus two full-suite runs to learn nothing the first read had not said.

**What the fixer saw** (`logs/maestro.log`, 2026-09-14 21:06 and 22:09 UTC, the
`70ab579c26d6` attempts): "Gate convergence round 1 (ceiling 20) — failing set:
1 (sample: ['tests:TIMEOUT'])"; the fixer runs `maestro gate-lease -- pytest
tests/test_loop_dequeue_gates.py tests/test_ci_pending_hold_gate.py` (pass,
0.77 s) and writes "the TIMEOUT is the full-suite run oversubscribing the
container … per #3760"; the round ends `stuck` and the plan is demoted at
03:42 with `resume_failing_identities: ["tests:TIMEOUT"]`.

**Which plans it killed** (`evolution_plans.jsonl`): 3 of the 13 failed plans
carry `tests:TIMEOUT` (`09617d408796`, `1040c6180f7f`, `70ab579c26d6`); the 4
pre-124 `Tests: could not run` rows (`16c8bc2f4db3`, `a8b8b9992309`,
`b8e2da8e4c2a`, `223ea116deb1`) collapsed `TIMEOUT` and `UNAVAILABLE` into one
string and cannot be split after the fact.

**Why one worker** (verified against the tree and the deployment):

- `docker-compose.yml` bounds the `loop` service to `cpus: '2.0'`,
  `memory: 2G`; `restart: "no"`.
- `gate_capacity.plan_lease` computes `memory_budget = (available_mib −
  SYSTEM_RESERVE_MIB − in_flight × SESSION_BASELINE_MIB) // PER_WORKER_MIB`
  with `SYSTEM_RESERVE_MIB = 1100` — documented as "the operating system and
  the non-agent system baseline (the dashboard, background services)" measured
  on the *host*. `_read_available_mib` returns the tighter of host and cgroup
  headroom (`memory.max − memory.current`); the host shows ~5.7 GiB
  `MemAvailable` (read 2026-09-15 with the loop down), so inside the 2 GiB
  cgroup the cgroup binds, and two workers at `in_flight 0` need
  `(headroom − 1100) // 350 ≥ 2` — headroom ≥ 1800 MiB, i.e. `memory.current`
  ≤ ~250 MiB for the whole cgroup, the loop and any resident SDK session
  included. The 22 two-worker rows are the moments that bar was cleared; the
  other 328 did not clear it or had a peer in flight. How far below it they
  sat is **unmeasured**: `GateRunRecord` records no `available_mib` (step 1
  adds it). `worker_args` returns `()` at one worker, so the run is serial.
- `_run_gate_command` runs every gate under the one `[timeouts] selfcheck =
  300`; the lease width is spliced into the same argv beside it; neither reads
  the other.
- Each `converge_to_green` round is a full `run_quality_gates` — ruff, mypy,
  pytest with `--cov` over `tests/` — so a round's exposure to the ceiling is
  the whole suite regardless of what the diff touched.

## Prior Point Fixes (whack-a-mole ledger)

| # | Identifier | What it patched | Why the class survived |
|---|---|---|---|
| 1 | `f2d52731` (2026-08-08) | Parallelize pytest with xdist "to fit gate runs under timeout" | Width sized to the host; OOM-stalls the 2 GiB loop cgroup (issue #3760) |
| 2 | `4575a73d` → `8c074dba`, `350a820d` | CI job serial, then parallel once it owned a VM; `--dist loadfile` pin | CI-side oscillation; the local gate's fit was never modelled |
| 3 | Issue #3760 (closed) — RCA | Workers sized to host → OOM-stall crash | Fixed by ledger 4, which created the one-worker timeout |
| 4 | `49613626` (2026-09-08, proposal 126) | Size workers to cgroup limits, default the lease on | Cgroup headroom minus a host reserve floors at one worker |
| 5 | `407d4dd3` (2026-09-11, PR #3826) | Collapse single-worker lease runs to serial pytest | Made the one-worker grant cheaper, not faster; the serial suite takes ~370–425 s hand-run and never completes in-process under the 300 s ceiling |
| 6 | `6f31bfda` (proposal 126 step 12) | Retire the serial gate fallback in favour of classified retry | Retry re-buys the same timeout |
| 7 | Proposal 126 `## Out of scope` | "Raising `timeout` … both are the *detectors*"; test-level selection deferred | Correct against transient oversubscription; blind to a permanent width/ceiling mismatch |
| 8 | Proposal 110 `converge_to_green` | Progress-gated fix loop | A synthetic `tests:TIMEOUT` identity never shrinks: `stuck` after one round |
| 9 | Proposal 124 `attribute` | `INHERITED` hold | A non-answer is `UNATTRIBUTABLE` by construction; never held (133 adds the hold) |

The design below retires the host reserve of 4, the serial collapse of 5 (the
width becomes whatever the cgroup honestly affords), the ceiling's blindness in
7, and the per-round full-suite cost that 8 exposes to it.

## Relevant Findings

- `maestro/gate_capacity.py` (`plan_lease`, `SYSTEM_RESERVE_MIB`,
  `PER_WORKER_MIB`, `SESSION_BASELINE_MIB`) — the memory-bound budget divides
  headroom-minus-reserve by a 350 MiB per-worker figure; the reserve is a host
  measurement applied to whichever of host/cgroup headroom was tighter.
- `maestro/gate_capacity.py` (`HostCapacity`, `measure_host_capacity`,
  `_read_available_mib`, `_read_cgroup_v2_available_mib`,
  `_read_cgroup_v1_available_mib`, `_available_cores`) — the tighter-of-two
  choice is made and then forgotten: `HostCapacity` records `cores` and
  `available_mib` but not which bound produced them.
- `maestro/gate_capacity.py` (`GateLease`, `worker_args`) — the grant carries
  `workers` and `slots`; there is no deadline field; `worker_args` returns `()`
  at one worker.
- `maestro/gate_capacity.py` (`lease_for_gate_run`, `GateRunOutcome`) — the
  single seam; the telemetry row records `workers`, `in_flight`, `admission`,
  `outcome`, `duration_seconds` — but `duration_seconds` is whole-slot wall
  time (host measurement, slot wait, ruff, mypy, the pytest probe and any
  `-x`-less re-run) and the row names neither its source nor the pytest
  command's own time, so it cannot measure fit per width by itself; proposal
  133 step 1 adds `tests_seconds` and `source`.
- `maestro/evolution.py` (`_run_gate_command`) — `subprocess.run(...,
  timeout=self._config.selfcheck_timeout)` for ruff, mypy and pytest alike;
  `TimeoutExpired` → `parse_non_answer(tool, "TIMEOUT", …)`.
- `maestro/evolution.py` (`run_quality_gates`) — one argv for every caller:
  `pytest <tests> -x --tb=short -q --cov=<source> --cov-fail-under=<n>
  *worker_args(lease)`; no scope parameter.
- `maestro/evolution.py` (`converge_to_green`, `_progress_signals`,
  `_convergence_terminal`) — every round calls `run_quality_gates` in full and
  compares its signature to the previous full read; a `green` decision returns
  that round's own review with no further read — the terminal's `reread=True`
  re-read fires only on the auto_fix-made-no-changes path, and only under
  `gate_observation_enabled`.
- `maestro/evolution.py` (`_observe_gate`) — an `-x`-truncated probe is re-run
  without `-x` under the same `selfcheck_timeout`; the docstring already notes
  the re-run is "the likeliest place" for a timeout.
- `maestro/config.py` (`selfcheck_timeout = 300`, `cov_fail_under = 97`) and
  `maestro.toml` `[timeouts] selfcheck` — the ceiling and the local coverage
  floor; `pyproject.toml` `--cov-fail-under=100` is CI's floor, so CI is already
  the coverage authority.
- `maestro/gate_environment.py` (`probe_fit`, `UNFIT`) — proposal 133's
  detector reads in-process `gate_runs.jsonl` rows at the current width — the
  `tests:TIMEOUT` (censored) share and the completed rows' `tests_seconds` —
  against the pytest deadline; today that deadline is `selfcheck_timeout`.
- `maestro/cli.py` (`measure-gate-contention`) — p50/p95 gate wall time
  bucketed by `in_flight`; not by `workers`, and with no notion of a deadline.
- `tests/test_gate_lease_owner.py` (`test_no_gate_site_names_a_worker_count`)
  — the AST/regex guard that no gate site hardcodes a width; the pattern to
  extend to deadlines.
- `tests/test_evolution.py` (`test_run_quality_gates_flag_off_is_byte_identical`)
  — pins the argv; the flag-off identity every step below must keep.
- `docker-compose.yml` (`loop` service) — `cpus: '2.0'`, `memory: 2G`.

## Implementation Plan

The failure class, in one sentence: **a gate run that would finish at the width
the cgroup affords is killed by a deadline set for another machine, or floored
to a width the cgroup does not require, because width, deadline and scope have
three owners that never consult each other — and the plan is charged for it.**

Manifestations closed, by step: one-worker floor inside a cgroup (step 1);
green suite killed at 300 s (step 2); no per-width fit reading (step 3);
full-suite exposure and cost per fix round (step 4); a future site hardcoding
a deadline or a scope (step 5). 133's `UNFIT` hold releases on its own once
steps 1–2 are live, because `probe_fit` reads the lease-derived deadline.

### Phase A — One budget

1. [x] **Make the memory reserve cgroup-aware.** `maestro/gate_capacity.py`
   (`HostCapacity.bounded_by: Literal["host", "cgroup"]` recorded by
   `measure_host_capacity` from which of host/cgroup won in
   `_read_available_mib`; `plan_lease` applies `SYSTEM_RESERVE_MIB` only to a
   host-bounded figure and no own-process reserve to a cgroup-bounded one — the
   OS and dashboard live outside the cgroup limit, and `memory.current` already
   counts every process inside it, this loop and its resident SDK session
   included, so subtracting a baseline for them again would double-count; the
   `in_flight × SESSION_BASELINE_MIB` term stays for both bounds, the existing
   conservative allowance for peers that planned at admission but have not yet
   spawned their workers). Unconditionally and additively, the
   `gate_runs.jsonl` row stamps `available_mib` and `bounded_by`, so the
   headroom every grant is planned against is a recorded figure and the flag is
   flipped only once those rows show what it would grant. Flag `[loop]
   gate_lease_cgroup_reserve_enabled` (default-off, `EXPERIMENTAL_FLAGS`,
   `review_by = 2026-12-31`, gate `gate_lease_reserve`). Flag-off, `plan_lease`
   is byte-identical.
   - *Depends on:* none.
   - *Acceptance:* `plan_lease(HostCapacity(cores=2, available_mib=1400,
     bounded_by="cgroup"), in_flight=0)` grants 2 workers flag-on and 1
     flag-off (1400 MiB is an illustrative table input, not a measured
     reading — the recorded `available_mib` supplies that); the same capacity
     at `in_flight=1` grants 1 either way (the core split);
     `HostCapacity(cores=4, available_mib=5600, bounded_by="host")` grants the
     same either way; a recorded row carries `available_mib` and `bounded_by`.
2. [x] **Derive the pytest deadline from the granted width.** *(Both halves of
   the derivation landed; the `probe_fit` comparison, unblocked by shipped 133,
   has since landed.)*
   `maestro/gate_capacity.py` (`GateLease.deadline_seconds`; `plan_lease` takes
   `probe_fit`'s reading of the in-process rows at the granted width and
   derives the deadline two ways. *Completed:* `min(selfcheck_max,
   max(selfcheck, ceil(p50 × 1.25)))` over the completed rows' `tests_seconds`,
   with `serial p50 ÷ workers` standing in when only one-worker runs are
   measured. *Censored* — the last run at this width ended on `tests:TIMEOUT`:
   `min(selfcheck_max, ceil(that run's deadline_seconds × 1.5))`, so a suite
   that truly needs more than the deadline lifts it run by run until a run
   completes or the cap is reached, instead of yielding only `TIMEOUT` rows
   from which no completed p50 can ever be read — an in-process pytest never
   completes above its own deadline. `selfcheck` when unmeasured; a pre-change
   `TIMEOUT` row without `deadline_seconds` reads as censored at `selfcheck`;
   hand-run wrapper rows — no ceiling, a different argv — are never read. The
   `gate_runs.jsonl` row stamps `deadline_seconds` unconditionally; flag-off it
   is `selfcheck`), `maestro/evolution.py` (`_run_gate_command` takes the
   deadline from the lease for the `tests` tool; ruff and mypy keep
   `selfcheck_timeout`), `maestro/gate_environment.py` (`probe_fit` compares
   against `deadline_seconds`, and under the flag reads `UNFIT` only while runs
   are still censored at `selfcheck_max` or the completed p50 exceeds 0.9 ×
   `selfcheck_max` — so 133's hold parks plans only on a suite that does not
   fit even at the cap, never while escalation is still buying fit; the tend,
   baseline and landing readers keep running the gate, so escalation is never
   starved of rows), `maestro/config.py` / `maestro/config_settings.py` /
   `maestro.toml` (`[timeouts] selfcheck_max`, the hard cap, default 900). This
   is not a timeout bump: `selfcheck` is
   unchanged and remains the ruff/mypy ceiling and the pytest *floor*; the
   pytest deadline is a derived fact of the same owner as the width, bounded
   above by a cap that still kills a genuine hang. Flag `[loop]
   gate_budget_lease_owned_enabled` (default-off, `EXPERIMENTAL_FLAGS`,
   `review_by = 2026-12-31`, gate `gate_budget`); flag-off,
   `deadline_seconds == selfcheck` and the subprocess timeout is
   byte-identical.
   - *Depends on:* 1.
   - *Acceptance:* the figures below are an illustrative pair chosen to
     exercise every branch, not the shipped defaults (`maestro.toml` owns
     those and has since raised them) — with `selfcheck = 300`,
     `selfcheck_max = 900`: no
     measurement → 300; the last one-worker run a `TIMEOUT` at 300 → 450, then
     a `TIMEOUT` at 450 → 675, a `TIMEOUT` at 675 → 900, and a `TIMEOUT` at
     900 → 900 (the cap, never above); a completed one-worker p50 of 380 s →
     475; two workers with only a one-worker p50 of 380 s measured → 300 (the
     floor, since `ceil(190 × 1.25) = 238`); a 424 s wrapper row changes
     nothing; flag-off it is 300 in every case and `_run_gate_command` passes
     exactly `selfcheck_timeout`.
3. [x] **Record fit per width.** `maestro/gate_capacity.py`
   (`lease_for_gate_run` stamps `scope` on the `gate_runs.jsonl` row, beside
   133's `tests_seconds` / `source` and step 2's `deadline_seconds`),
   `maestro/cli.py` (`measure-gate-contention` gains a per-`workers` bucket
   over in-process rows with a `fit` column — completed-run p50
   `tests_seconds` over the deadline — and a `censored` column — the `TIMEOUT`
   count at that width — so the falsifiability figure below is one command).
   Telemetry only; unconditional; readers tolerate rows without the new fields.
   - *Depends on:* 2.
   - *Acceptance:* a row written under the flag carries `scope`;
     `measure-gate-contention --json` over a fixture of in-process rows at
     widths 1 and 2 reports two buckets with the right p50s, `fit` ratios and
     `censored` counts, and excludes `wrapper` rows; a pre-change row without
     the fields is bucketed by `workers` with `fit = null`.

### Phase B — Scope

4. [x] **Impacted-tests probe for the fix loop's inner rounds.** *(Landed whole
   in `maestro/gate_scope.py` — the oracle half plus the read-only `maestro
   gate-scope` CLI, then the consumption half: `restrict_signature`, the
   `run_quality_gates` `scope=` seam, and the fix loop's impacted rounds with
   their full-scope certification read.)*
   `maestro/gate_environment.py` (`impacted_tests(files_affected, tests_path)`
   — zero-LLM: the `tests/test_<module>*.py` naming convention plus an `ast`
   walk of `tests/` for modules importing a changed module, through the
   `WorkTarget` path atoms; an empty result means "full"),
   `maestro/evolution.py` (`run_quality_gates` takes a `scope`;
   `converge_to_green` runs its inner rounds against the impacted set with
   `--no-cov`, and the pre-loop read stays full with coverage; `self_review`'s
   terminal is unchanged). Flag `[loop] gate_scope_impacted_enabled`
   (default-off, `EXPERIMENTAL_FLAGS`, `review_by = 2026-12-31`, gate
   `gate_scope`). Because `plan_next_round` reads any strictly shrinking set as
   progress and `all_clean` as `green`, three rules keep a scope change from
   reading as either. *Scope-matched baselines:* an impacted probe is compared
   only against an impacted signature — round 1 against the pre-loop full
   observation restricted to the impacted scope (test identities whose node id
   lies in an impacted module, plus the ruff and mypy identities, whose gates
   still run whole; the coverage identity dropped from both sides under
   `--no-cov`), later rounds against the previous impacted probe; when that
   restriction is empty — the full red lies wholly outside the impacted set, a
   lone `tests:TIMEOUT` included — the plan's rounds run full-scope exactly as
   today. *Certification read:* an impacted round `plan_next_round` reads as
   `green` is not a terminal; `converge_to_green` issues a new full-scope,
   with-coverage `run_quality_gates` — independent of
   `gate_observation_enabled` and of `_convergence_terminal`'s `reread`, which
   today fires only on the no-changes path and only flag-on — and only an
   `all_clean()` certification returns `green`, through
   `_convergence_terminal` with that read as its review. *Red certification:*
   it consumes a round against `ceiling` and is compared to the previous full
   read (the pre-loop read or the last certification); a strict subset
   continues, becoming the new full baseline and widening the impacted scope
   to the modules its failures name; an unchanged or grown full signature ends
   `stuck`, so the loop cannot oscillate between a green subset and an
   unchanged full red. The trust model is stated, not smuggled: CI
   (`--cov-fail-under=100`) is already the coverage authority the local 97%
   floor defers to; the inner round's job is to converge the diff, the
   certification's job is to certify it.
   - *Depends on:* 3.
   - *Acceptance:* a plan with `files_affected == ["maestro/red_root.py"]`
     probes exactly `tests/test_red_root.py` plus every test module whose `ast`
     imports `maestro.red_root`; round 1 compares the impacted probe against
     the pre-loop signature restricted to that scope, never the full signature;
     a round whose impacted probe goes green issues exactly one full-scope
     `--cov` certification read and returns `green` only when it is
     `all_clean()`; a red certification whose full signature equals the
     pre-loop signature ends `stuck`; a pre-loop red of only `tests:TIMEOUT`
     runs full-scope rounds; flag-off, every round's argv is byte-identical to
     today (`test_run_quality_gates_flag_off_is_byte_identical` extended).

### Phase C — Pin the owner

5. [x] **Extend the lease-owner guard to deadline and scope.** *(The deadline
   half, the scope half and the two doc one-liners landed; the `probe_fit`
   AST clause, unblocked by shipped 133, has since landed.)*
   `tests/test_gate_lease_owner.py`, `tests/test_structural_guards.py` — the
   no-hardcoded-width AST/regex guard also refuses a literal `timeout=` on any
   `tests`-tool gate command outside `_run_gate_command`, and any pytest argv
   naming a `tests/` path outside `run_quality_gates`'s scope seam;
   `probe_fit` reads its deadline only from a `GateLease` (AST); the
   certification read and the terminal re-read in `converge_to_green` are
   provably full-scope (a seeded `scope="impacted"` on either fails). `docs/loop-lifecycle.md` gains the
   one-liner: the lease owns width, deadline and scope; CLAUDE.md's gate-lease
   invariant line is extended by the two nouns.
   - *Depends on:* 2, 4.
   - *Acceptance:* seeding `timeout=300` into a copy of a gate site fails the
     guard; seeding `scope="impacted"` into a copy of the certification read
     or of the terminal re-read fails it; the existing worker-count seed still
     fails.

## Testing Strategy

All tests honour CLAUDE.md: `mem_fs` for every file, no real subprocess,
network, sleep or clock; `plan_lease` and the deadline derivation are pure and
table-tested; the argv and timeout reaching `subprocess.run` are captured by a
recording stub.

- **Reserve (`tests/test_gate_capacity.py`).** `bounded_by` recorded from each
  of the v2 / v1 / host reader paths; `available_mib` and `bounded_by` stamped
  on the row; `plan_lease` tables for cgroup-bounded and host-bounded
  capacity, `in_flight` 0–3, flag on/off identity; the degenerate-capacity
  floor unchanged.
- **Deadline (`tests/test_gate_capacity.py`, `tests/test_evolution.py`).**
  Floor / completed-p50 derived / censored escalation (300 → 450 → 675 → 900)
  / cap / unmeasured / wrapper row ignored / pre-change `TIMEOUT` row read as
  censored at `selfcheck` / flag-off table; `_run_gate_command` receives the
  lease deadline for `tests` and `selfcheck_timeout` for `ruff` and `mypy`; a
  `TimeoutExpired` at the derived deadline still yields the `TIMEOUT`
  non-answer with salvaged partial output; under the flag `probe_fit` reads
  `UNFIT` only when runs are censored at `selfcheck_max`.
- **Telemetry (`tests/test_gate_capacity.py`, `tests/test_cli.py`).** Row
  fields; `measure-gate-contention` buckets, `fit` and `censored` over
  in-process rows only; tolerance of rows without the fields; `--json` shape.
- **Scope (`tests/test_gate_environment.py`, `tests/test_evolution.py`).**
  `impacted_tests` by name convention, by `ast` import, changed test file
  itself, no match → empty → full; per-round argv under the flag;
  scope-matched baselines (an impacted probe is never compared to a full
  signature; an empty restriction, a lone `tests:TIMEOUT` included, runs
  full-scope rounds); the certification read always full with coverage, and
  `green` unreachable without an `all_clean()` certification; a red
  certification continues on a strict subset of the previous full read and
  ends `stuck` otherwise; flag-off identity.
- **Guards (`tests/test_gate_lease_owner.py`,
  `tests/test_structural_guards.py`).** Seeded literal deadline, seeded
  impacted certification or terminal read, seeded `probe_fit` reading a
  constant — each an in-memory fault per `tests/test_fault_seeding.py`.

## Risk Assessment

- **Deadline derivation masking a hang.** `selfcheck_max` is a hard cap; the
  derived value is bounded above by it and below by `selfcheck`, and is a
  function of *measured* in-process runs at the same width — a completed p50,
  or a killed run escalating 1.5× — so an unmeasured environment gets the
  floor, not a guess. A hang escalates the deadline to the cap within three
  killed runs and is then killed at 900 s — at most 600 s later than today
  under the default cap; those escalation runs are the price of reading a
  censored suite at all.
- **Two workers OOM-stalling the 2 GiB cgroup again (#3760).** The reserve
  change grants two workers at `in_flight 0` only when the cgroup headroom —
  measured after everything already resident in it, this loop and its session
  included — is ≥ `2 × PER_WORKER_MIB` (700 MiB free); `PER_WORKER_MIB = 350`
  already errs conservative, and step 1 records the headroom so the canary
  reads the margin instead of inferring it. If the canary shows a stream-stall
  recurrence, the flag is reverted and the deadline step alone (at one worker,
  escalated to 450–675 s or derived from a completed p50) still covers the
  ~370–425 s serial suite.
- **Impacted-scope rounds converging on a subset.** Impacted probes are compared
  only against impacted baselines, and the certification read — the full suite
  with coverage — is the only way to `green`. CI at 100% remains authoritative,
  as it is today at the 97% local floor. An import-graph miss means a round
  converges on too few tests and the certification catches it — one consumed
  round, never a false green.
- **Rollback.** Three independent default-off flags; each step is byte-identical
  off; the telemetry step is additive.
- **Falsifiability.** If the design is right, within 14 days of Phase A live:
  `maestro measure-gate-contention` shows `TIMEOUT` below 2% of gate runs (from
  9%), the completed-run p50 at the granted width under the derived deadline
  (`fit < 1.0`) with no width bucket whose latest runs are all `censored`; the
  recorded cgroup headroom says whether `workers ≥ 2` is reachable at this
  cgroup size — if in-process rows rarely clear 700 MiB, step 1 buys nothing
  here and step 2 carries the fit alone, a measured finding rather than an
  assumed one; at least one approved plan merges (from 0 of 12 since 2026-09-01) and no
  `gate_convergence_terminal` row is an `UNATTRIBUTABLE` stall on a lone
  `tests:TIMEOUT`. Within 14 days of Phase B live, the median convergence round
  is under 90 s. The design has failed if two workers reproduce the #3760
  stream-stall crash, or if `TIMEOUT` persists at one worker with a derived
  deadline above the measured p50.

## Out of scope

- **Runnability — provisioning and collection** — proposal 133 owns the oracle,
  the image install, the pre-spend hold and the `ENVIRONMENT` attribution; this
  proposal only feeds `probe_fit` a lease-derived deadline.
- **The `--cov-fail-under` split (97 local / 100 CI)** — unchanged; this
  proposal relies on CI being the coverage authority and does not move the
  local floor.
- **Resizing the loop cgroup or the production host** — an operator decision
  (proposal 125); every step here makes the lease honest about whatever bound
  it is given.
- **The `--dist loadfile` pin and `PER_WORKER_MIB`** — correct on their own
  terms; untouched.
- **Raising `[timeouts] selfcheck` itself** — deliberately not done; the
  constant stays as the floor and the ruff/mypy ceiling.
- **Governing the generator** — proposals 116 / 120.
