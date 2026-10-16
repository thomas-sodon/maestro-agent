# 133: Gate Runnability Oracle — One Owner of Whether the Local Gate Can Reach a Verdict, Consulted Before Any Implementation Attempt Is Spent

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:observing |
| **Affects** | new `maestro/gate_environment.py`, `maestro/gate_capacity.py`, `maestro/evolution.py`, `maestro/agent.py`, `maestro/gate_observation.py`, `maestro/attempt_disposition.py`, `maestro/red_root.py`, `maestro/memory.py`, `maestro/config.py`, `maestro/config_settings.py`, `maestro/flag_lifecycle.py`, `maestro/cli.py`, `maestro.toml`, `Dockerfile`, `scripts/docker-entrypoint.sh`, new `tests/test_gate_environment.py`, new `tests/test_gate_environment_owner.py`, `tests/test_structural_guards.py`, `tests/test_attempt_disposition.py`, `tests/test_red_root.py`, `tests/test_agent.py`, `tests/test_gate_observation.py`, `tests/test_gate_capacity.py`, `tests/test_evolution.py`, `tests/test_cli.py`, `docs/deployment.md`, `docs/self-evolution.md`, `docs/loop-lifecycle.md`, `CLAUDE.md` |
| **Depends on** | 124 (the `GateRunObservation` vocabulary and `attribute`), 126 (the `lease_for_gate_run` seam and `gate_runs.jsonl` telemetry), 131 (the `classify_attempt` disposition owner) |
| **Derived from** | live production data via /propose, 2026-09-15; root cause posted as RCA #3872 (`[rca] Dockerfile: no maestro_sdk install; PEP 420 shadow aborts gate collection`) |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

**What landed.** Step 1 — the oracle itself. `maestro/gate_environment.py` owns
the closed `GateRunnabilityKind` verdict vocabulary and the three pure probes
(`probe_provisioning`, `probe_collection`, `probe_fit`) composed by
`probe_runnability` in that precedence, with kind-scoped fingerprints, the
`_SIBLINGS` registry, and the `refresh_verdict` / `read_verdict` pair over
`Config.gate_environment_path`; it imports nothing from `gate_capacity` and
lands with no callers, pinned by `tests/test_gate_environment_owner.py` and
covered by `tests/test_gate_environment.py`. Step 3 — both provisioning-parity
halves: the deployment-side Dockerfile↔CI install parity guard
(`tests/test_dockerfile_structure.py::test_sibling_editable_installs_match_ci`)
and the runtime-side registry guard in `tests/test_gate_environment_owner.py`,
which walks `tests/` and `maestro/` by `ast` — module *and* function scope — and
fails when a first-party sibling import is absent from `_SIBLINGS`, with the
symmetric pin that every declared submodule resolves on disk. Step 2 — the loop
image is provisioned to CI's install line
(`34d97638`). The `Dockerfile` base stage copies `maestro_sdk/` and
`maestro_review/` and installs both editable ahead of `".[dev,email]"`, and
`scripts/docker-entrypoint.sh` editable-reinstalls both siblings from the
bind-mount on the writable path, so a real distribution beats the
`PYTHONPATH=/workspace` PEP 420 namespace shadow. The Dockerfile-half of step 3
is also present: `tests/test_dockerfile_structure.py`
(`test_sibling_editable_installs_match_ci`,
`test_sibling_install_parity_guard_catches_missing_install`) pins every CI
sibling install to a Dockerfile counterpart with a seeded-offender control.
Step 1's telemetry precondition arrived separately with proposal 134:
`GateRunRecord` already carries `tests_seconds`, `source` and
`deadline_seconds`.

Step 4 — the pre-spend selector hold. Behind default-off
`[loop] gate_environment_oracle_enabled`, `_implement_approved_plans` consults
the oracle once per iteration through `maestro/loop/plan_holds.py`
(`gate_environment_verdict`, `hold_plans_on_environment`,
`release_environment_hold` / `release_environment_holds` /
`release_environment_hold_state`), and only when it has an eligible plan; a
non-`SOUND` verdict opens no session and stamps
`EvolutionPlan.environment_hold_fingerprint` (`update_plan_environment_hold` /
`update_plans_environment_hold` in `maestro/memory.py`) with a
`gate_environment_hold` activation row, a journal note, a bounded repair
directive into an empty slot, and one `ask_thomas` per distinct
`notice_key` `(kind, identities)` fact.

Step 5 landed the oracle's second consumer, at the other end of the attempt: a
convergence red the cached verdict *owns* is attributed `ENVIRONMENT` (a kind
carrying `identities` owns the red only when it names every failing identity; an
identity-less `UNPROVISIONED` / `UNFIT` is the whole-run claim and owns all of
them), ranked above the enumerated-nothing `UNATTRIBUTABLE` arm so a bare
non-answer still reaches it; `classify_attempt` answers `HOLD_ENVIRONMENT`
unconditionally, and `implement_plan` stamps the fingerprint carried on
`ConvergenceResult.runnability` — charging no `failure_count`, no
`inherited_hold_count`, no demote and no issue, and writing its own
`gate_environment_hold` activation row.

Step 6 landed the third consumer, at the lease seam:
`lease_for_gate_run` gained a keyword-only `verdict_path` carrying the flag and
the location in one argument, *reads* (never refreshes — a probe would recurse
through this very seam) the cached verdict before the host is measured, and on a
`declines_gate_run` kind yields `GateRun(lease=None, runnability=verdict)`
having taken no slot and spawned no subprocess, recording one
`admission="declined"` row (`LeaseAdmission` gained that third value) excluded
from the contention and width buckets and surfaced as
`measure-gate-contention`'s `declined_rate`; `run_quality_gates` synthesizes the
failing `COLLECTION_ERROR` result from the verdict's `tests:`-scoped identities
and flags it `declined` so `_observe_baseline` refuses to bank it. The consult
sits below the lease's own `slot_dir is None` rollback.

Step 7 retired the mypy-only provisioning carve-out: `maestro/red_root.py` no
longer parses gate output for `maestro_sdk` markers. `diagnose_red_root` takes a
keyword-only `runnability` verdict — `_diagnose_red_root_on_ci_red` passes the
cached `read_verdict` under the oracle flag — and when
`gate_environment.blames_missing_sibling` reads it `UNPROVISIONED`, the red gate's
recorded blocker leads with `format_verdict`'s line ahead of the gate's own first
error. The no-rival-provisioning-marker pin in
`tests/test_gate_environment_owner.py` landed with it.

**What is missing.** The observation window, now running via the
`maestro.production.toml` canary (`[loop] gate_environment_oracle_enabled = true`;
criteria in the "Gate-runnability oracle canary (proposal 133)" subsection of
`docs/self-evolution.md`), then the remaining retirement it gates (step 8). Step 9's operator surface and prose have landed: `maestro preflight` routes its
startup sibling check through `probe_provisioning` and exits non-zero on an
`UNPROVISIONED` verdict, `maestro gate-check` refreshes the whole verdict before its
suite run — printing `format_verdict`'s line on stderr, adding one `runnability` key to
`--json` and persisting the row the loop reads next — and `docs/loop-lifecycle.md`,
`docs/modules.md` and CLAUDE.md each name the owner. The pre-spend consult pin on
`_implement_approved_plans` and the sole-`"ENVIRONMENT"`-producer pin on
`gate_observation.attribute` have landed in `tests/test_gate_environment_owner.py`,
and the two container smoke checks named in step 2's acceptance are now recorded in
`docs/deployment.md` alongside the sibling editable installs they exercise. The
no-rival-provisioning-marker pin landed with step 7.

The implementation lineage has been dead for two weeks. Since 2026-09-01 every
one of the twelve human-approved plans that reached `implement_plan` was demoted
to `failed`, and not one merged — and in at least ten of those twelve the red
that killed the plan was not the plan's diff. It was the *environment* the gate
runs in. The loop image never installs the `maestro_sdk` sibling that two test
modules import, so four in five gate runs abort at collection before a single
test executes; and when collection does succeed, the suite cannot fit its
ceiling at the width the cgroup lease grants (the fit root is closed by
companion proposal 134). No diff can clear either red. Yet "can this
environment run the gate to a verdict at all" has no owner: each site
re-discovers it *after* the SDK spend, from the gate's output text, in its own
vocabulary — a `could not run` string, an `INHERITED` hold, an `UNATTRIBUTABLE`
stuck terminal, a second-attempt `ASK_HUMAN`, a mypy-only provisioning
carve-out in `red_root` — and the plan generator, reading those symptoms, has
minted **82 plans** to "fix" an environment fact in code, five of which were
implemented and died in the very gate they targeted.

This proposal gives that fact one owner. A new zero-SDK oracle,
`maestro/gate_environment.py`, answers *is the gate runnable* before any
attempt is spent: are the sibling packages importable through the
namespace-package shadow, is the suite collectable, and does it fit its
deadline at the granted width. Every gate-running site already passes through
one AST-enforced seam, `lease_for_gate_run`; the oracle binds there, so a
non-runnable environment yields a synthesized verdict in about a second
instead of a 109-second ruff→mypy→collection-abort sweep, and
`_implement_approved_plans` consults it *before* selecting a plan, so an
approved plan is held — not demoted, not cold-restarted three times, not
turned into a GitHub issue — until the environment is sound. The loop image is
provisioned to what `docs/deployment.md` and CI already require, and a golden
test pins the Dockerfile to the CI install line so the gap cannot silently
reopen.

The end state: a red the diff did not cause never costs an approval, an
attempt, or a mint. Proposals 124 and 131 each declared "why the environment
red happens" out of scope and built a hold or a question *around* it. This
proposal is the piece both deferred, and it retires the point fixes they left
behind — the mypy-only provisioning classifier and the second-attempt
environment-recurrence rule. It stops the bleeding; proposal 134 (one lease
owning width, deadline and scope) restores the flow.

## Production Evidence

Live data read from `$DATA_DIR = /opt/maestro/data` (the loop
container's `/workspace/data` bind-mount, resolved via `docker inspect`), all
reads strictly read-only. At the time of reading (2026-09-15 ~12:00 UTC)
`maestro-loop` is `Exited (1)` since ~08:43 UTC with `lifecycle.json` showing
`current_phase: implement_approved_plans` and `consecutive_stream_stalls: 3` —
the same auto-fix stream-stall crash shape as issue #3760. Not acted on here.

**The approved→merged funnel is at zero** (`evolution_plans.jsonl`, 630 plans,
whole file):

- 31 plans ever entered implementation (`failure_count > 0` or `merged`): 17
  merged, 13 failed, 1 obsolete. Every merged plan was created 2026-08-16 →
  2026-08-29; the last merge (`96d886a6516e`, 2026-09-01) put `maestro_sdk/src`
  on **mypy's** path — the mypy half of the same gap.
- Since 2026-09-01: **0 merged, 12 failed.** 11 of the 13 failed plans burned all
  three `max_implementation_attempts` cold restarts; the two demoted today
  (`70ab579c26d6`, `7cc6182899d1`) sit at attempt 1/3 and will be cold-restarted
  twice more into the same wall.
- `last_failure_reason` classes over the 13: 4 `Tests: could not run` (the
  pre-124 collapse of `TIMEOUT`/`UNAVAILABLE`), 3 `tests:TIMEOUT`, 1 pytest
  `ERROR tests/test_provider_cli_paced.py` collection abort, 3 "self-review did
  not converge", 1 SDK `CLIJSONDecodeError` buffer overflow, 1 genuine test
  failure (2026-08-16). Of the three self-review non-convergences,
  `7cc6182899d1` was `INHERITED`-held twice against the same collection red on
  `origin/main` (`tests/test_provider_cli_paced.py`,
  `tests/test_tool_adapter.py` — journal, 2026-09-14) and `46bd1ff8401a` was
  itself a pytest-path fix. **At least 8, and on inspection 10, of 13 failures
  are environment reds, not diff reds.**

**The gate almost never reaches a verdict** (`gate_runs.jsonl`, 350 rows,
2026-09-09 → 2026-09-15, the proposal-126 telemetry; rows include both the
in-process gate and hand-run `maestro gate-lease` invocations and carry no
source field — the wrapper records only `CLEAN` / `UNAVAILABLE` / `TIMEOUT` /
`UNKNOWN`, so every `COLLECTION_ERROR` and `FAILED` row is in-process — and
`duration_seconds` is whole-slot wall time, not a per-command time):

- Outcomes: `COLLECTION_ERROR` 279 (80%), `TIMEOUT` 30 (9%), `FAILED` 21,
  `CLEAN` 18 (5%), `UNKNOWN` 2.
- `COLLECTION_ERROR` runs last 104–120 s each (p50 109 s — ruff, mypy, then a
  pytest probe that aborts at collection and is re-run once without `-x` by
  `_observe_gate`): **279 × 109 s ≈ 8.5 h of the 14.0 h total gate wall-clock
  over six days bought nothing.** They ran 56–73 times per day on 09-12 → 09-14
  with almost no implementation attempts those days: the tend lap, the
  baseline capture and the session-landing readers re-discovering the same
  abort every iteration.
- The 30 `TIMEOUT` rows (all 300–305 s, `workers: 1`; 27 of them
  log-correlated to an in-process convergence read, a pytest killed at the
  300 s `selfcheck`) are the fit class; their full profile and closure are
  proposal 134's. This proposal *detects* that
  class so no attempt is spent on it, and does not claim to close it.

**What the convergence loop saw** (`gate_activations.jsonl`,
`gate_convergence_terminal`, 8 rows since the 124 canary went live
2026-09-08): all 8 `stuck`, 0 `green`; 6 `UNATTRIBUTABLE` (five at `rounds=0` —
auto-fix changed nothing because there was nothing to fix), 2 `INHERITED`.

**What it cost** (`usage.jsonl`, 2026-09-01 → 2026-09-15): 26 iterations carried
an `implement_plan` call; the implementation lineage (`implement_plan` +
`self_review` + `auto_fix` + `replan_for_scope`) spent **$— for zero merges**,
p50 $— / max $— per attempt. The journal's own status sweep (2026-09-15 02:27)
attributes ~$— of overnight burn to the `7cc6182899d1` episode including a
rate-limit + stream-stall crash and a maintenance trip.

**The mint storm the symptom spawned** (`evolution_plans.jsonl`, titles matching
`cli_?paced|import[- ]?path|importorskip|pytest|maestro_sdk|module-search|gate-check`
created 2026-08-27 → 2026-09-11, less one false match — a pytest-marker
registration): **82 plans.** 69 obsoleted at the admission door (`backlog
saturated (5 >= 5)`) after the priced reflect+propose calls had run, and 1 as a
phantom-RED; 6 human-rejected; 5 implemented and failed in the gate they
targeted (3 attempts each — ledger row 7); 1 merged (the mypy path).
`rejection_history.jsonl` records the human refuting the premise in all six
rejections — "already fixed on main by `a5a52168`",
"provably cannot clear the abort" — which is true in CI and the dev checkout,
where the sibling is installed, and false in the loop container, where it
never was.

**Why the collection red is environmental** (verified against the tree and the
image):

- `Dockerfile` installs only `".[dev,email]"`; the packaging boundary excludes
  `maestro_sdk*` from `packages.find`, and `docs/deployment.md` says the sibling
  install is required ("mypy maestro/ + test suite need it") —
  `.github/workflows/quality-gates.yml` runs `pip install -e "maestro_sdk/"`
  first; the image never does. With `PYTHONPATH=/workspace`, the
  `/workspace/maestro_sdk/` *directory* (a `src/` layout with no top-level
  `__init__.py`) resolves as a PEP 420 namespace package: `import maestro_sdk`
  succeeds, `maestro_sdk.mcp_config` raises `ModuleNotFoundError` — exactly
  the four errors the journal names, and exactly why `pytest.importorskip`
  "provably cannot clear it". The dev checkout collects both files in 0.22 s.
- Ten modules the suite collects import `maestro_review` (the eight under
  `tests/test_maestro_review/`, `tests/component/test_review.py`, and
  `tests/test_tend_bug_comments.py` inside test functions), which resolves today
  only because it is a flat package under `PYTHONPATH`; nothing pins that
  either.

**Tracked issues.** The cause is diagnosed once, in RCA **#3872** (Dockerfile
omits the sibling install; `PYTHONPATH=/workspace` exposes `maestro_sdk/` as a
PEP 420 namespace; reproduced on the host with `-S` and against the built image
with `find_spec("maestro_sdk") is None`). Every other open ticket on this class
is a symptom — a plan auto-filed by `_demote_plan_to_failed` after dying in
this gate — and none names the cause:

- *Direct* (plans that tried to fix this in code and died in this gate):
  #3268, #3822, #3820, #3770, #3656, #3655.
- *Collateral* (unrelated plans killed by the same collection abort): #3666,
  #3683, #3684, #3759, #3630, #3461.
- *Adjacent*: #3506 (the `packages.find` exclude invariant has no test — step 3
  closes it from the runtime side).
- *Closed, same family*: #2589 (cli_paced import fix, 2026-08-27), #3760
  (`gate_capacity` OOM RCA — the origin of the one-worker fit class, proposal
  134).

Steps 2–3 resolve #3872; the twelve open symptom tickets should be closed as
duplicates of it when step 2 lands, not left for the loop to re-open with a
further fix-plan.

## Prior Point Fixes (whack-a-mole ledger)

| # | Identifier | What it patched | Why the class survived |
|---|---|---|---|
| 1 | `a5a52168` (2026-07-19) "make quality gates green again" | Restored the `maestro_sdk` provider import contract | Green where the sibling is installed (CI, dev); the loop image never installs it |
| 2 | `bc9e57f6` / plan `96d886a6516e` (2026-09-01) | `maestro_sdk/src` on **mypy's** search path | Fixed the mypy half only; pytest collection untouched |
| 3 | `5131e58e`, `e0b29bf4` — `red_root.classify_mypy_provisioning_signature` | Names a mypy `maestro_sdk` red as a PROVISIONING-GAP in the PR-remediation preflight | Mypy-only, remediation-only; the implementation gate stays blind (`test_diagnose_red_root_provisioning_carveout_is_mypy_only` pins the narrowness) |
| 4 | Proposal 110 `converge_to_green` | Progress-gated fix loop instead of a round cap | Cannot progress on a red no diff can move: `rounds=0`, `stuck` |
| 5 | Proposal 124 (`8fcf58de`, `5b2ddcba`) | `INHERITED` attribution + hold instead of demote | A collection abort or non-answer is `UNATTRIBUTABLE` by construction (`attribute` refuses a synthetic or truncated set), so it is never held; 124 out-of-scoped "provisioning the loop container's `maestro_sdk` install" |
| 6 | Proposal 131 (`f07f1c54`, `bb059d96`, `d0e8e363`) | `ASK_HUMAN` on a *recurring* environment red | Requires `prior_failing == current_failing`; the first attempt's prior set is empty, so the first attempt is always spent; 131 out-of-scoped "why the environment red happens" |
| 7 | Plans `16c8bc2f4db3`, `09617d408796`, `46bd1ff8401a`, `1040c6180f7f`, `0c26cf4995cc` | Maestro's own five implementations of the importorskip / pytest-path / `--full` fix | Each died in the gate it targeted, 3 attempts each — the gate cannot certify a fix to itself |
| 8 | `b2bf00d1`, `fc41daef`, `2cfcc4dd` | `quality-gates.sh` resolves the `maestro` CLI from the checkout / `~/.local/bin` | Hand-run environment only; adjacent |

The fit-class ledger — xdist parallelization, the CI serial/parallel
oscillation, issue #3760, proposal 126's cgroup sizing and serial collapse — is
carried by proposal 134. The design below retires 3, the environment arm of 6,
and the premise behind 1, 2 and 7 (the image is provisioned, so the red stops
existing).

## Relevant Findings

- `maestro/evolution.py` (`run_quality_gates`) — one `lease_for_gate_run` slot
  covers ruff → mypy → pytest; the pytest argv is `pytest <tests> -x --tb=short
  -q --cov=<source> --cov-fail-under=<cov_fail_under> *worker_args(lease)`.
- `maestro/evolution.py` (`_run_gate_command`) — `TimeoutExpired` →
  `parse_non_answer(tool, "TIMEOUT", …)`, `OSError` → `UNAVAILABLE`; a
  collection abort is a *finished* run whose output `parse_gate_output` reads
  as a `COLLECTION_ERROR` row.
- `maestro/evolution.py` (`_observe_gate`) — a probe `-x` cut short at a
  collection abort is re-run once without `-x`, so a collection error costs two
  pytest launches per gate run.
- `maestro/evolution.py` (`_observe_baseline`) — the docstring already names the
  gap: "What the cache key still cannot capture is the *environment* — an
  uninstalled dependency reds every commit alike … a restart re-reads it."
- `maestro/evolution.py` (`converge_to_green`, `_progress_signals`,
  `_convergence_terminal`) — progress is a strictly shrinking failure-identity
  set; a synthetic `tests:COLLECTION_ERROR` / `tests:TIMEOUT` identity cannot
  shrink, so the loop ends `stuck` at `rounds=0` and the terminal classifies it.
- `maestro/gate_observation.py` (`attribute`) — returns `UNATTRIBUTABLE` for any
  run whose signature carries a synthetic identity or a truncated gate; the
  environment red is therefore neither `ATTRIBUTED` nor `INHERITED`, and no
  hold fires.
- `maestro/attempt_disposition.py` (`_ENVIRONMENT_KINDS`, `_is_environment_red`,
  `classify_attempt`) — the only place today that names the environment-kind
  set `{COLLECTION_ERROR, TIMEOUT, UNAVAILABLE}`; its `ASK_HUMAN` arm needs a
  non-empty `prior_failing` equal to the current set, i.e. a second attempt.
- `maestro/agent.py` (`implement_plan`) — `prior_failing =
  frozenset(plan.resume_failing_identities)`; the first attempt's set is empty,
  so the first terminal is `ABANDON` → `_demote_plan_to_failed` (attempt 1/3,
  "will be retried"). `70ab579c26d6` now carries
  `resume_failing_identities: ["tests:TIMEOUT"]` for exactly this reason.
- `maestro/agent.py` (`_implement_approved_plans`) and
  `maestro/loop/plan_holds.py` (`plan_on_inherited_hold`) — the selector
  already declines a plan on a keyed hold (`inherited_hold_main_sha` vs the
  current `origin/main`); there is no
  environment-keyed equivalent, so a non-runnable environment is discovered per
  plan, per attempt, after the spend.
- `maestro/agent.py` (`_demote_plan_to_failed`) — increments `failure_count`,
  notifies, and on the terminal attempt files a GitHub `bug` issue whose body is
  the last 1000 chars of gate output — for an environment red, an issue about
  nothing.
- `maestro/gate_capacity.py` (`lease_for_gate_run`, `LeaseAdmission`,
  `GateRunOutcome`) — the single seam every in-process gate reader and the
  `maestro gate-lease` wrapper pass through (AST/regex-enforced by
  `tests/test_gate_lease_owner.py`); records one `gate_runs.jsonl` row per run
  with `workers`, `in_flight`, `admission`, `outcome`, `duration_seconds`.
  `duration_seconds` spans the whole slot (host measurement, slot wait, ruff,
  mypy, the pytest probe and any `-x`-less re-run), and the row records neither
  the pytest command's own time nor its source; the wrapper's child
  (`_run_gate_child`) runs under no timeout at all.
- `maestro/red_root.py` (`MAESTRO_SDK_PROVISIONING_DIRECTIVE`,
  `_MAESTRO_SDK_PROVISIONING_MARKERS`, `classify_mypy_provisioning_signature`)
  — a second, rival spelling of "this red is provisioning, not code", scoped to
  mypy and to the PR-remediation preflight.
- `maestro/config.py` (`selfcheck_timeout`, `max_implementation_attempts = 3`,
  `gate_lease_dir`, `work_target`) and `maestro.toml` `[timeouts] selfcheck =
  300` — the per-command ceiling `probe_fit` reads until 134 replaces it with
  the lease-derived deadline.
- `maestro.production.toml` — `gate_observation_enabled = true`,
  `attempt_disposition_enabled = true`; `maestro.toml` — both default-off,
  `gate_lease_enabled = true`. The 124/131 canaries are live and both saw the
  class without closing it.
- `Dockerfile` — `COPY maestro/`, `pip install ".[dev,email]"`,
  `ENV PYTHONPATH=/workspace`; no sibling install.
  `.github/workflows/quality-gates.yml` — `pip install -e "maestro_sdk/" &&
  pip install -e ".[dev,email]"` and `-e "maestro_review/[dev]" -e
  "maestro_sdk/[dev]"`. `docs/deployment.md` — `pip install -e "maestro_sdk/"`
  listed as required. `scripts/docker-entrypoint.sh` — editable-reinstalls
  `maestro` on the writable path; not the siblings.
- `tests/test_provider_cli_paced.py`, `tests/test_tool_adapter.py` — import
  `maestro_sdk` at module scope (a collection abort under the shadow);
  `tests/test_config.py` imports it only inside test functions (`Cadence`,
  `MarkerPack`, `marker_packs`) — a run-time `ModuleNotFoundError` that
  `--collect-only` cannot see, so provisioning, not collection, is the probe
  that catches it. Ten collected modules import `maestro_review`.
- `maestro/cli.py` (`cmd_preflight`) — checks secrets and MCP launcher PATHs,
  fail-loud; does not check that the gate suite can be collected.
- `tests/test_red_root.py`
  (`test_diagnose_red_root_provisioning_carveout_is_mypy_only`) — a test that
  pins the carve-out's narrowness: structural evidence of a whacked mole.
- `tests/test_gate_lease_owner.py`, `tests/test_gate_command_owner.py`,
  `tests/test_structural_guards.py`, `tests/test_overlay_safety.py` — the house
  AST/regex/golden-guard patterns this proposal extends.

## Implementation Plan

The failure class, in one sentence: **an approved plan's implementation attempt
— SDK spend, a cold restart, a demotion, a GitHub issue, a mint — is charged
against a quality-gate red that no diff can clear, because the gate's
runnability has no owner and is re-derived after the spend, per site, in five
vocabularies.**

Manifestations closed, by step: collection abort from the missing sibling
(steps 1, 2, 3, 6, 9); first-attempt spend and 3× cold restart of any
environment red, including the fit class (steps 4, 5); 279 × 109 s
tend/baseline/landing collection sweeps (step 6); the mypy-only carve-out
(step 7); the second-attempt `ASK_HUMAN` environment rule (step 8); the
Dockerfile↔CI provisioning drift reopening silently (steps 3, 9); plans minted
to fix an environment fact in code (steps 2, 3 remove the premise — the
generator-side gate is proposal 120's and is listed out of scope). Closing the
fit class itself is proposal 134.

### Phase A — One owner, provisioned image

1. [x] **Add the runnability oracle.** new `maestro/gate_environment.py` — a
   zero-SDK leaf owning the closed verdict vocabulary
   `GateRunnabilityVerdict(kind, fingerprint, identities, detail, measured_at)`
   with `kind ∈ {SOUND, UNPROVISIONED, UNCOLLECTABLE, UNFIT}` and three pure
   probes over injected readers: `probe_provisioning` (`importlib.util.find_spec`
   for every registered sibling **and one declared submodule per sibling** —
   `maestro_sdk` → `maestro_sdk.mcp_config`, `maestro_review` → its first
   submodule — so the PEP 420 namespace shadow is caught, including for a
   function-scope importer such as `tests/test_config.py` that collection never
   sees); `probe_collection` (`pytest <tests> --collect-only -q -p
   no:cacheprovider` through an **injected collector**, parsed through
   `gate_observation.parse_gate_output` — reusing the owner, no rival parser);
   `probe_fit` (over the injected `gate_runs.jsonl` rows, reading only
   in-process rows at the same `workers`: `UNFIT` when at least three of the
   last five ended on the `tests:TIMEOUT` identity — the right-censored
   reading, since an in-process pytest can never complete above its own
   deadline — or when the last three completed rows' p50 `tests_seconds`
   exceeds 0.9 × the pytest deadline, which is `selfcheck_timeout` until 134
   hands it the lease-derived value). `probe_runnability` composes them in that
   precedence and returns exactly one verdict. The `fingerprint` is kind-scoped,
   so it moves only with the fact the verdict is about: `UNPROVISIONED` hashes
   the identities and the sibling spec origins; `UNCOLLECTABLE` adds the
   `origin/main` head (a merge can repair a collection red in code); `UNFIT`
   hashes the injected capacity-planned width at `in_flight = 0` — a property
   of the cgroup, not of momentary contention — and the deadline.
   `refresh_verdict` probes and writes the row to `gate_environment.json`
   (`atomic_write`, `file_lock`) at most once per iteration; `read_verdict` is
   its zero-subprocess reader (missing or corrupted file → `None`, never a
   crash). `gate_environment` imports nothing from `gate_capacity` — its row
   reader, planned width and collector are injected, and the composition root
   runs the collector under `lease_for_gate_run(..., consult_runnability=False)`
   (step 6) — so the probe can never consult the verdict it is computing and
   the seam→oracle import edge stays one-way. **Telemetry precondition**
   (unconditional, additive): `GateRunRecord` gains `tests_seconds` (the pytest
   probe command's own wall time, stamped by `run_quality_gates`; `None` when
   pytest did not run) and `source` (`in_process` / `wrapper`, stamped by the
   seam's caller) — whole-slot `duration_seconds` cannot stand in for either,
   and rows without the fields are ignored by `probe_fit`. No verdict caller
   yet.
   - *Depends on:* none.
   - *Acceptance:* `probe_provisioning` returns `UNPROVISIONED` naming
     `maestro_sdk.mcp_config` when a fake `find_spec` resolves the package to a
     namespace origin and the submodule to `None`; `probe_collection` returns
     `UNCOLLECTABLE` with the `tests:COLLECTION_ERROR` identity on a seeded
     `ERROR` row; `probe_fit` returns `UNFIT` when three of the last five
     in-process one-worker rows are `TIMEOUT` against a 300 s deadline, `UNFIT`
     when the last three completed rows read `tests_seconds` 280/285/290 s, and
     `SOUND` when they read 180/190/200 s, ignoring `source = "wrapper"` rows
     and rows without `tests_seconds`; `UNPROVISIONED` wins over `UNFIT`; an
     `UNPROVISIONED` fingerprint is unchanged when only the `origin/main` head
     moves, and an `UNFIT` fingerprint is unchanged across reads taken at
     different `in_flight`.
2. [x] **Provision the loop image to CI's install line.** `Dockerfile`,
   `scripts/docker-entrypoint.sh`, `docs/deployment.md` — `COPY maestro_sdk/`
   and `COPY maestro_review/` into the base stage and run the same editable
   sibling installs `.github/workflows/quality-gates.yml` runs (`pip install -e
   "maestro_sdk/" -e "maestro_review/[dev]"`) before `".[dev,email]"`; the
   entrypoint's writable-path editable reinstall covers the siblings alongside
   `maestro`. The install must land each sibling on `sys.path` as a path entry
   — setuptools' static `__editable__.*.pth` for these layouts, or a regular
   install: a regular package later on `sys.path` beats the
   `/workspace/maestro_sdk` namespace portion earlier on it (on the host, with
   the editable `.pth` present `find_spec("maestro_sdk").origin` is
   `src/maestro_sdk/__init__.py`; under `python -S` it is `None`), whereas a
   finder-hook editable install sits on `sys.meta_path` behind `PathFinder`,
   which returns the namespace spec first. This is the root closed at the root;
   the oracle in step 1 keeps it closed.
   - *Depends on:* none.
   - *Acceptance:* both checks run with the repo bind-mounted at `/workspace`
     under `PYTHONPATH=/workspace` — the only arrangement in which the shadow
     exists; a bare `docker run` of the image has an empty `/workspace` and
     passes whether or not the shadow is beaten. `docker compose run --rm
     --entrypoint python loop -c "import maestro_sdk.mcp_config,
     maestro_review"` (the baked install, as the read-only and
     `MAESTRO_SKIP_PIP_SYNC` paths run it) exits 0; on the running loop
     container, `docker compose exec loop python -c "import maestro_sdk;
     print(maestro_sdk.__file__)"` prints a path under
     `/workspace/maestro_sdk/src/` (the entrypoint's reinstall) and `docker
     compose exec loop python -m pytest tests/test_provider_cli_paced.py
     tests/test_tool_adapter.py --collect-only -q -p no:cacheprovider` reports
     no `ERROR` row. Both are recorded as deployment smoke checks in
     `docs/deployment.md` (they need `--entrypoint` / `exec` because the
     entrypoint ends in `exec maestro "$@"`); the unit guard is step 3.
3. [x] **Pin Dockerfile↔CI provisioning parity.** new
   `tests/test_gate_environment_owner.py` — a golden text guard (the
   `tests/test_overlay_safety.py` shape: known set pinned, seeded offender
   control) asserting every `pip install -e "<sibling>/…"` target in
   `.github/workflows/quality-gates.yml` has a counterpart install in
   `Dockerfile`, and that every **first-party** top-level package `tests/`
   imports — by `ast` walk, at module *or* function scope (`tests/test_config.py`
   imports `maestro_sdk` only inside test functions) — is in
   `gate_environment`'s sibling registry. First-party means a repo-root
   directory other than `maestro/` (today `maestro_sdk`, `maestro_review`);
   stdlib and third-party imports (`pytest`, …) are outside the guard's domain.
   - *Depends on:* 2.
   - *Acceptance:* removing the `maestro_sdk/` install line from a copy of the
     Dockerfile fails the guard naming `maestro_sdk`; a function-scope
     `from maestro_extra import x` in a seeded test source, beside a seeded
     repo-root `maestro_extra/` directory, fails it naming `maestro_extra`; a
     seeded `import pytest` does not.
4. [x] **Hold approved plans on a non-runnable environment — before the
   spend.** `maestro/agent.py` (`_implement_approved_plans`), `maestro/memory.py`
   (`EvolutionPlan.environment_hold_fingerprint`, `update_plan_environment_hold`),
   `maestro/config.py` / `maestro/config_settings.py` / `maestro.toml` /
   `maestro/flag_lifecycle.py` (`[loop] gate_environment_oracle_enabled`,
   default-off, `EXPERIMENTAL_FLAGS` entry with `review_by = 2026-12-31`, gate
   `gate_environment_hold`). Flag-on, the selector calls `refresh_verdict()`
   (at most once per iteration) before building `eligible`; on a non-`SOUND`
   verdict it implements nothing this iteration, stamps the verdict's
   fingerprint on each plan it would have selected (plans stay `approved`;
   `failure_count` untouched), appends one `gate_activations.jsonl` row, writes
   one journal `evolution_note`, fills an *empty* `next_task_directive` with a
   bounded `Environment repair: <detail>` directive, and delivers **one**
   `ask_thomas` message per `(kind, identities)` — a durable
   `gate_environment.json` `notified` marker, the `deliver_claimed_escalation`
   re-read pattern — so a fingerprint that moves while the fact stands (an
   `UNCOLLECTABLE` head advancing on every merge) re-stamps the held plans
   without re-asking. The hold self-extinguishes on a `SOUND` verdict, which
   releases every held plan on the next call; a changed fingerprint under a
   still-non-`SOUND` verdict never releases a plan into an attempt. Flag-off,
   byte-identical.
   - *Depends on:* 1.
   - *Acceptance:* with an `UNPROVISIONED` verdict and two `approved` plans,
     `_implement_approved_plans` opens no implementation session, both plans
     remain `approved` with `failure_count == 0` and the verdict fingerprint
     stamped, and exactly one `ask_thomas` call is made across two consecutive
     calls even when `origin/main` advances between them; an `UNCOLLECTABLE`
     verdict whose head moves between two calls re-stamps the fingerprint,
     opens no session and sends no second `ask_thomas`; a `SOUND` verdict on
     the next call implements the first plan.
5. [x] **Give the convergence terminal an `ENVIRONMENT` attribution and a
   first-attempt hold.** `maestro/gate_observation.py` (`Attribution` gains
   `ENVIRONMENT`; `attribute` takes an optional `runnability` verdict and
   returns `ENVIRONMENT` when that verdict owns the red — naming every identity
   in the run's signature, or carrying no identities at all, which is the
   whole-run claim `UNPROVISIONED` and `UNFIT` make — sourced, never re-derived,
   and ranked above the enumerated-nothing arm so a bare non-answer reaches it), `maestro/attempt_disposition.py` (`AttemptVerdict` gains
   `HOLD_ENVIRONMENT`, returned on `ENVIRONMENT` regardless of
   `prior_failing`, ahead of `RESUME`), `maestro/agent.py` (`implement_plan`
   honours `HOLD_ENVIRONMENT` exactly like `HOLD_INHERITED` but stamps
   `environment_hold_fingerprint` instead of `inherited_hold_main_sha` and
   consumes no hold budget). Flag-gated by step 4's flag. This closes the case
   the pre-spend read missed (an environment that broke mid-attempt).
   - *Depends on:* 1, 4.
   - *Acceptance:* a `stuck` convergence whose observation is a lone
     `tests:TIMEOUT` with an `UNFIT` verdict classifies `HOLD_ENVIRONMENT` on
     `failure_count == 0` with empty `prior_failing`; the plan stays `approved`,
     no `_demote_plan_to_failed` call, no GitHub issue; the same observation
     with a `SOUND` verdict keeps today's `ABANDON`.

### Phase B — Bind the seam, retire the moles, pin the owner

6. [x] **Bind the oracle into the lease seam so a non-runnable gate never
   spawns the suite.** `maestro/gate_capacity.py` (`lease_for_gate_run` gains a
   keyword `consult_runnability: bool = True` and, when set, reads the cached
   `read_verdict()` — never `refresh_verdict`, so the seam issues no probe and
   cannot recurse into the collector; on `UNPROVISIONED` / `UNCOLLECTABLE` it
   yields a run pre-stamped `admission = "declined"` — new `LeaseAdmission`
   value — carrying the verdict; `UNFIT` is *not* declined here — the suite may
   still be worth running for its enumeration, and 134 owns the fit; a `None`
   verdict — nothing probed yet — admits as today), `maestro/evolution.py`
   (`run_quality_gates` returns a `QualityGateResult` whose observation is
   synthesized from the verdict's identities, `all_clean() is False`, without
   launching ruff/mypy/pytest), `maestro/red_root.py` (`diagnose_red_root`
   records the verdict's detail as the blocker), `maestro/cli.py` (the
   `maestro gate-lease` wrapper passes `consult_runnability=False`: it is the
   tool the fixer and the operator use to *check* a repair, it runs in a
   process that cannot refresh the loop's per-iteration cache, and declining it
   would lock out the very run that proves the environment sound; `maestro
   preflight` and `maestro gate-check` call `refresh_verdict`, so an operator's
   post-repair check rewrites the cache the loop reads next). Every in-process
   reader — the tend lap, `_observe_baseline`, `_run_session_landing`,
   `_attribute_owned_gate_red`, the landing pushers, `run_gate_check` —
   inherits this through the one seam. Behind step 4's flag.
   - *Depends on:* 1, 4.
   - *Acceptance:* with a cached `UNCOLLECTABLE` verdict, `run_quality_gates`
     issues zero subprocesses, returns `passed is False` with signature
     `{"tests:COLLECTION_ERROR"}`, and appends one `gate_runs.jsonl` row with
     `admission == "declined"` and `duration_seconds < 5`; under the same
     verdict `maestro gate-lease -- pytest tests/test_red_root.py` is admitted
     (`admission == "slot"`) and the oracle's own collector runs; with an
     `UNFIT` verdict the argv and admission are byte-identical to today.
7. [x] **Retire the mypy-only provisioning carve-out.** `maestro/red_root.py`
   (delete `MAESTRO_SDK_PROVISIONING_DIRECTIVE`,
   `_MAESTRO_SDK_PROVISIONING_MARKERS`, `classify_mypy_provisioning_signature`;
   `diagnose_red_root` reads the verdict from step 6), `tests/test_red_root.py`
   (the carve-out tests, including
   `test_diagnose_red_root_provisioning_carveout_is_mypy_only`, are replaced by
   verdict-fed cases). Only after step 6 has recorded ≥ 1 `declined` admission
   in production.
   - *Depends on:* 6.
   - *Acceptance:* `grep -n provisioning maestro/red_root.py` is empty and a
     RED mypy run under an `UNPROVISIONED` verdict records the verdict's detail
     as the blocker with no mypy-output parsing.
8. [ ] **Retire the second-attempt environment-recurrence rule.**
   `maestro/attempt_disposition.py` (delete `_ENVIRONMENT_KINDS`,
   `_is_environment_red`, and the environment arm of `ASK_HUMAN`; `ASK_HUMAN`
   stays in the vocabulary for 131's remaining producers), `maestro/agent.py`
   (`implement_plan`'s `ASK_HUMAN` branch unchanged),
   `tests/test_attempt_disposition.py`. Coordinated with proposal 131's burn-in
   window (its canary criteria note the arm's replacement); lands only after
   step 5 has held ≥ 1 plan in production.
   - *Depends on:* 5.
   - *Acceptance:* `classify_attempt` on a recurring lone `tests:TIMEOUT` with
     a `SOUND` verdict returns `ABANDON`, with an `UNFIT` verdict
     `HOLD_ENVIRONMENT`, and `ASK_HUMAN` is unreachable from an
     environment-kind observation.
9. [ ] **Pin the single owner and the pre-spend consult.**
   `tests/test_gate_environment_owner.py`, `tests/test_structural_guards.py` —
   AST guards: `GateRunnabilityVerdict` / `probe_runnability` /
   `refresh_verdict` / `read_verdict` are defined only in
   `maestro/gate_environment.py`, which imports nothing from
   `maestro/gate_capacity.py`; no module outside it spells the
   environment-kind set or a `maestro_sdk` provisioning marker string;
   `lease_for_gate_run` is the sole gate-run caller of `read_verdict` and calls
   no `refresh_verdict`; `_implement_approved_plans` calls `refresh_verdict`
   before any `implement_plan` call in its body; `attribute` is the only
   producer of `"ENVIRONMENT"`. Plus `maestro preflight` and `maestro
   gate-check` print the refreshed verdict, `preflight` exiting non-zero on
   `UNPROVISIONED` (the MCP
   launcher PATH-check pattern), and `docs/self-evolution.md` /
   `docs/loop-lifecycle.md` gain the one-liner; CLAUDE.md gains one invariant
   line naming the owner.
   - *Depends on:* 4, 5, 6, 7, 8.
   - *Acceptance:* seeding `_ENVIRONMENT_KINDS = {...}` into a copy of
     `maestro/attempt_disposition.py` fails the guard; removing the
     `refresh_verdict` call from a copy of `_implement_approved_plans` fails it;
     seeding a `refresh_verdict` call into a copy of `lease_for_gate_run` or a
     `from maestro import gate_capacity` into a copy of
     `maestro/gate_environment.py` fails it;
     `maestro preflight` under a faked namespace-shadowed `maestro_sdk` exits
     non-zero naming `maestro_sdk.mcp_config`.

## Testing Strategy

All tests honour CLAUDE.md: `mem_fs` for every file, no real subprocess,
network, sleep or clock; injected readers for `find_spec`, the pytest
collector, `gate_runs.jsonl`, and `rev_parse`.

- **Oracle (`tests/test_gate_environment.py`).** Pure-probe tables for
  `probe_provisioning` (installed / namespace-shadowed / absent / submodule
  missing), `probe_collection` (clean, one `ERROR` row, timeout of the
  collector itself → `UNCOLLECTABLE` with the `TIMEOUT` identity),
  `probe_fit` (fits, censored `TIMEOUT` majority → unfit, completed p50 above
  0.9 × deadline → unfit, no rows → `SOUND` with `detail = "unmeasured"`;
  rows at a different width, `wrapper` rows and rows without `tests_seconds`
  ignored), precedence (`UNPROVISIONED` wins over `UNCOLLECTABLE` wins over
  `UNFIT`), kind-scoped fingerprint stability (same inputs → same hash; a head
  move changes only an `UNCOLLECTABLE` hash; an `in_flight` change changes
  none), `refresh_verdict` / `read_verdict` (a second refresh in an iteration
  issues no probe; `read_verdict` issues no subprocess; corrupted
  `gate_environment.json` → `None`, never a crash).
- **Selector hold (`tests/test_agent.py`).** Flag-off byte-identity; flag-on
  non-`SOUND` → no session, plans stay `approved`, one `ask_thomas` per
  fingerprint across repeated calls, directive only into an empty slot,
  release on `SOUND` and on fingerprint change; `ask_thomas` transport error is
  best-effort (`_notify_best_effort` semantics) and never blocks the hold.
- **Terminal hold (`tests/test_attempt_disposition.py`,
  `tests/test_gate_observation.py`, `tests/test_agent.py`).** `attribute`
  returns `ENVIRONMENT` only when the verdict names every non-clean gate's
  identity (a genuine `FAILED` alongside a `TIMEOUT` stays `UNATTRIBUTABLE`);
  `HOLD_ENVIRONMENT` on first attempt; `implement_plan` stamps the fingerprint,
  cleans the branch, consumes no `inherited_hold_count`, files no issue.
- **Lease seam (`tests/test_gate_capacity.py`, `tests/test_evolution.py`,
  `tests/test_cli.py`).** `declined` admission records a row and yields
  without spawning; `consult_runnability=False` and a `None` verdict admit as
  today; the `gate-lease` wrapper is never declined; every row carries
  `tests_seconds` and `source`;
  `run_quality_gates` synthesizes the observation; `UNFIT` does not decline;
  flag-off argv identity (`test_run_quality_gates_flag_off_is_byte_identical`
  extended).
- **Provisioning guard (`tests/test_gate_environment_owner.py`).**
  Dockerfile↔workflow parity with a seeded-offender control; sibling registry
  covers every first-party (repo-root) top-level import under `tests/`, at
  module or function scope; third-party imports are ignored.
- **Structural guards (`tests/test_structural_guards.py`,
  `tests/test_gate_environment_owner.py`).** Sole-owner, sole-consult,
  no-rival-spelling, sole-`"ENVIRONMENT"`-producer — each with an in-memory
  seeded-fault case per `tests/test_fault_seeding.py`.

## Risk Assessment

- **False non-`SOUND` stalls all implementation.** Bounded three ways: the
  verdict is visible in `maestro preflight` / `gate-check` and delivered once to
  Thomas per fingerprint; a hold older than 7 days while `preflight` reads
  `SOUND` is a written tripwire; and every flag is default-off and
  independently revertible. Nothing in Phase A removes an existing path — the
  convergence terminal still runs — so a wrong `SOUND` degrades to today's
  behaviour, never below it.
- **`UNFIT` holds park plans until 134 lands.** By design: a held plan costs
  nothing and loses nothing; today the same plan costs $— and a demotion per
  attempt. The written tripwire is spend, not throughput — throughput is 134's.
- **`ENVIRONMENT` excusing a real regression.** `attribute` grants it only when
  the oracle's verdict names *every* non-clean gate's identity and no gate is
  an ordinary `FAILED`/`COVERAGE` — the same conservative shape as `INHERITED`;
  a mixed red stays `UNATTRIBUTABLE` and is demoted as today.
- **Image change.** Adds two editable sibling installs to the base stage — a
  larger image and a longer build; both siblings are already required by CI and
  `docs/deployment.md`. Rollback is a Dockerfile revert; the parity guard then
  fails loud in CI, by design.
- **Retirement ordering.** Steps 7–8 delete behaviour proposals 124/131 are
  still observing; they are gated on the replacement having fired in
  production and are sequenced last.
- **Falsifiability.** If the design is right, within 14 days of Phase A live:
  `maestro measure-gate-contention` shows `COLLECTION_ERROR` below 5% of gate
  runs (from 80%); `evolution_plans.jsonl` records zero demotions with a
  `TIMEOUT` / `could not run` / collection `last_failure_reason`;
  `gate_runs.jsonl` carries ≥ 1 `declined` admission and
  `gate_activations.jsonl` ≥ 1 `gate_environment_hold` row; no
  `gate_convergence_terminal` row is an `UNATTRIBUTABLE` environment-kind
  stall. The design has failed if the image is provisioned and the oracle
  reads `SOUND` yet a plan is still demoted on a collection red, or if an
  environment hold parks every approved plan past 7 days while `preflight`
  reads `SOUND`.

## Out of scope

- **Closing the fit class** — the cgroup-blind memory reserve that pins one
  worker, the per-command ceiling that never sees the lease width, and the
  full-suite-per-round cost — is proposal 134. This proposal detects `UNFIT`
  and holds; it does not change width, deadline or scope.
- **Governing the generator** that minted 82 fix-plans for an environment fact
  — proposals 116 / 120 own the pre-mint authority; this proposal removes the
  premise (the red) rather than the minting.
- **The `--cov-fail-under` split (97 local / 100 CI) and the sibling suites'
  absence from the local gate** — the "local too lenient" direction 124 named;
  the oracle registers the siblings for *collection*, not for running their
  suites.
- **Transient SDK aborts** (`cli_crash` / `stream_stall` / `stale_session`,
  including the crash that has the loop down today) — the SDK-resilience layer
  per `docs/sdk-internals.md`; unchanged here.
- **Collapsing the 124 / 131 canary flags** — proposal 111's cutover machinery
  once their windows close; the retirement steps here coordinate with, and do
  not perform, that collapse.
