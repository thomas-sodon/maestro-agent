# 138: Live-State Fence — No Test Process Can Write Production State, and One Owner Resets the SDK's Process-Global Sinks

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | partial |
| **What landed** | Steps 1–2 (landing together) — the `sys.addaudithook` live-state write barrier in `tests/_live_state_fence.py`, wired from `tests/conftest.py` with xdist relay and a terminal-summary section, made enforcing: the hook refuses each write with `PermissionError`, the autouse `_live_state_verdict` fixture fails the attempting test with its recorded stack, and `pytest_sessionfinish` fails the run for writes outside any test; tested in `tests/test_live_state_fence.py`. Steps 3–4 — `_RUNTIME_GLOBALS` with `snapshot_sdk_runtime` / `restore_sdk_runtime` in `maestro/sdk.py` and `snapshot_provider_runtime` / `restore_provider_runtime` in `maestro/providers/registry.py`; the AST registry guard (with a seeded-offender control) in `tests/test_sdk_runtime_owner.py`; and the autouse `_own_sdk_runtime` fixture (`_sdk_runtime_scope`) in `tests/conftest.py` restoring both modules' globals plus the `maestro` logger's handlers and level around every test, documented in `docs/testing.md` (PRs #4090, #4092). Step 5 (first slice) — the four `_reset_*_for_tests` hooks are deleted from `maestro/sdk.py` with their callers migrated to `monkeypatch` / the autouse owner, the transcript test's local `_cost_sample_backend` null is gone, and `tests/test_sdk_runtime_owner.py` (`test_no_ad_hoc_reset_hooks`, with a seeded control) forbids a reset hook returning (PR #4141). Step 5 remainder — the manual `_sdk_log_dir` / `_cost_sample_backend` save/restore sites in `tests/test_sdk.py` move to `configure_*` / `monkeypatch`, the redundant provider-name save/restore leaves the `test_provider_registry.py` / `test_capability_gating.py` fixtures, the manual logger-handler cleanups leave `test_cli.py` / `test_dashboard_api.py`, and `test_no_direct_runtime_global_writes` in `tests/test_sdk_runtime_owner.py` forbids their return. Step 6 — the two `test_cli.py` `main()` offenders patch `configure_cost_tracking` / `configure_sdk_logging` and assert the call, pinning their intent. Step 7 — the reversible operator script `scripts/quarantine_test_telemetry.py` (`--report` / `--apply`, gate-window and AST-derived test-only-caller rules, `--until` fence bound, `file_lock` + `atomic_write` ledger rewrite, live-`maestro.pid` refusal, log moves into `logs/sdk-quarantine/`), tested in `tests/test_quarantine_test_telemetry_script.py` and documented in `docs/deployment.md` (PR #4178, review follow-up PR #4179). |
| **What is missing** | Phase C — Step 8, the one-off production run of the quarantine script, with its before/after recorded. |
| **Affects** | `tests/conftest.py`, new `tests/_live_state_fence.py`, new `tests/test_live_state_fence.py`, `maestro/sdk.py`, `maestro/providers/registry.py`, `tests/test_sdk.py`, `tests/test_cli.py`, `tests/test_conversation_transcript.py`, `tests/test_sdk_runtime_owner.py`, new `scripts/quarantine_test_telemetry.py` |
| **Depends on** | 106 (the `CostSampleBackend` seam), 125 (CI off the production host) |
| **Derived from** | live production data via /propose, 2026-09-24 |

## Description

Maestro's own quality gate is writing test traffic into production state. When the loop runs pytest, it does so from inside the live workspace. Tests there leave process-global SDK sinks pointed at the live data directory. For the rest of that pytest process, every test that drives the SDK seam appends a real cost sample to `data/cost_samples.jsonl` and writes a session log under `data/logs/sdk/`. In the week measured below, **every** test-caller row in the production cost ledger falls inside one of the loop's own gate-run windows. 91% of all rows in the hot ledger are test traffic, and 70% of recent SDK session-log files are test runs.

This matters now because cost telemetry is one of the instruments Maestro uses to measure itself. The burn-rate throttle reads this ledger and so do the cost reports. The per-caller cost/effort analysis that prompted this proposal was also built on it. Phantom test spend moves the throttle: up to $— in a single 5-hour window, 11.5% of that window's $— budget. Test fixtures that reuse real caller names (`introspect`, `planner`, `execute_step`, …) also inflate per-call-site counts in ways a caller filter cannot undo. The "never write to the file system" testing rule exists only as prose. `mem_fs` protects a test only while that test is running, and the leak happens *after* it ends.

The failure class is: **state configured inside one test outlives it and later reaches a real, live-state path, because nothing owns the lifetime of process-global sinks and nothing stops a test process from writing live state.** The remedy closes this class with two structural pieces and one cleanup:

1. **A live-state fence.** A `sys.addaudithook` write barrier in the test session refuses and attributes any write under a resolved live-state root, whichever seam, global or cwd produced it.
2. **One lifecycle owner** for `maestro/sdk.py`'s process-global runtime. It is a registered, AST-guarded set of globals that an autouse fixture snapshots and restores around every test. It replaces four ad-hoc `_reset_*_for_tests` hooks and 33 hand-written save/restore sites.
3. **A reversible quarantine** that removes the already-polluted rows and logs.

When this lands, a test that leaks a sink cannot pollute anything, because the owner restores the sink. A test that tries to write live state fails and is named, because the fence catches it. The historical pollution sits in quarantine files rather than in the ledgers the throttle reads.

## Production Evidence

`$DATA_DIR` = `/opt/maestro/data`, resolved through the `maestro-loop` bind mount (`/opt/maestro` → `/workspace`). All reads were read-only.

**Test-caller rows in `cost_samples.jsonl`.** "Test-caller" means one of the 11 caller literals that appear as `caller=` values in `tests/` and never as a `caller=` value in `maestro/`: `test`, `tracker`, `contemplate`, `a`, `b`, `c1`, `c2`, `t`, `phase-a`, `non_probe`, `test_caller`. (`contemplate` does appear as a string in `maestro/`, as a CLI subcommand and phase name, but never as a caller label.) The figures below also count the fixture rows under the real fallback name `unknown` and the real probe name `warning_hold_probe`, which the gate-window test separates.

| Window | Finding |
|---|---|
| whole hot ledger, 2026-08-16T10:48Z → 2026-09-24T12:17Z | 195,918 rows; **177,970 (90.8%)** carry a test-caller signature. The first one is at 2026-08-16T11:17:18Z, 29 minutes after the oldest hot row, so the leak predates the visible ledger (`archive_threshold_days = 0`, no cold sibling). |
| 7 full days, 2026-09-17T00:00Z → 2026-09-24T00:00Z | 26,522 test-signature rows costing **$—**, against $— of real-named spend: **13.5% of the $— recorded**. |
| same 7 days, joined with `gate_runs.jsonl` | **26,522 of 26,522 (100%)** fall inside a recorded gate-run window. There were 161 gate runs in the window, each carrying phantom spend. Gate-run sources: 170 `in_process`, 5 unlabelled (window 2026-09-17 → 2026-09-24T12:17Z). |
| same 7 days, rolling 5 h | Maximum phantom spend in any `budget_rate_window_hours = 5.0` window: **$—**, starting 2026-09-17T21:31Z. That is 11.5% of the window budget (`max_loop_budget_usd_per_hour` × 5 h). |
| 2026-09-17T00:00Z → 2026-09-24T12:17Z, real-named callers inside gate windows | `execute_step` 485 rows ($—, averaging about a tenth of a real step's cost), `introspect` 477 ($0), `planner` 318 ($0), `reflect` 159 ($0), `distill_findings` 159 ($0). These are fixture rows under production caller names. They inflate per-caller call counts (for example, 477 of the 819 `introspect` rows) and no caller-name filter can remove them. |

Representative rows (the dev tree shows the same shapes):

```
{"timestamp": "2026-09-24T12:17:49…", "cost_usd": 0.0, "input_tokens": 100, "output_tokens": 50, …, "caller": "unknown", "duration_ms": 0, …}
{"timestamp": "2026-07-22T02:15:42…", "cost_usd": 0.0, "input_tokens": 100, "output_tokens": 50, …, "caller": "execute_step", "duration_ms": 100, …}
```

**`logs/sdk/`**, last 8 date directories (2026-09-17 → 2026-09-24): 23,721 `.log` files, of which **16,660 (70%)** are test-shaped (a `Prompt: test` header or `caller=None`). They total 6.9 MB. The `logs/sdk` tree is 384 MB overall. A representative test-shaped log:

```
[15:04:21.563] SDK session started at 2026-09-19T15:04:21…
[15:04:21.563] Prompt: test
[15:04:21.564] EMPTY STDERR on crash (attempt 1, caller=None, exc_type=RuntimeError)
[15:04:21.566] RETRY 3/3 after 4.0s: RuntimeError: Command failed with exit code 1
[15:04:21.567] Query complete: 1 turns, 100ms, cost=N/A
```

**Checked and clean.** `usage.jsonl` has 10,693 rows and none carries a test caller; its sink (`_usage_sink`) is installed by the agent, not by `cli.main`. The 1,111 transcript `.jsonl` sidecars in the same 8 days contain no test headers. `logs/maestro.log` shows no test traffic.

**Dev tree corroboration.** `/opt/maestro-dev/data/` exists only because tests created it; no loop runs in that checkout. It holds 2,515 cost rows and 1,346 SDK log files.

**Reproduction** (dev tree, 2026-09-24, `-p no:randomly --no-cov`):

| Run | Δ `data/cost_samples.jsonl` | Δ `data/logs/sdk` files |
|---|---|---|
| `tests/test_sdk.py` alone | +0 | +0 |
| `test_cli.py::test_main_dispatches_workspace_counterfactual` then `tests/test_sdk.py` | **+159** | **+190** |
| `test_cli.py::test_main_sets_up_file_logging_for_run` then `tests/test_sdk.py` | **+159** | +0 |

## Relevant Findings

- `maestro/cli.py` (`main`) configures the SDK for commands in its SDK-configuration tuple (`run`, `contemplate`, `evolve`, `loop`, `serve`, `workspace-counterfactual`). It calls `configure_sdk_logging(config.sdk_log_dir)`, `configure_sdk_transcripts(...)`, `configure_cost_tracking(_build_cost_sample_backend(config))` and seven other `configure_*` functions, each of which writes a module global in `maestro/sdk.py`. The values come from the real TOML chain, where `[paths] memory_dir = "data"` is **relative to the cwd**.
- `maestro/sdk.py` holds 21 process-global runtime values, each assigned under a `global` statement: `_cost_sample_backend`, `_sdk_log_dir`, `_structured_transcripts_enabled`, `_transcript_content_max`, `_usage_sink`, `_sdk_model`, `_sdk_fallback_model`, `_sdk_pricing`, `_pricing_warned`, `_sdk_thinking_budget_tokens`, `_sdk_task_budget_tokens`, `_sdk_stream_idle_timeout_seconds`, `_concurrency_semaphore`, seven warning-throttle/hold values, and `_anyio_format_verified`. Nothing owns their lifetime in tests. Four separate `_reset_*_for_tests` hooks (`_reset_warning_throttle_for_tests`, `_reset_concurrency_semaphore_for_tests`, `_reset_sdk_extras_for_tests`, `_reset_usage_sink_for_tests`) each reset a slice. Each is called from at most two test files and from no conftest.
- `maestro/providers/registry.py` (`configure_provider`, `configure_phase_providers`) has the same process-global shape for provider selection.
- `maestro/sdk.py` (`_log_sdk_call`) appends through `_cost_sample_backend` whenever it is set, and catches `(OSError, sqlite3.Error)` at `debug`. That is right for production best-effort telemetry, but it means a refused write in a test would pass silently. A write barrier therefore has to *attribute and fail*, not just raise.
- `maestro/cost_tracking.py` (`append_cost_sample`, via `JsonlCostSampleStore`) creates the parent directory before appending (406a3070). A leaked store in any cwd therefore creates `data/` and writes, with no failure to trip on.
- `maestro/sdk.py` (`SessionLogger`) writes a `.log` per query under `_sdk_log_dir` whenever that global is set.
- `tests/test_cli.py` (`test_main_dispatches_workspace_counterfactual`) drives `main()` with only the command handler patched, so it leaks both the log dir and the cost backend. `test_main_sets_up_file_logging_for_run` patches `configure_sdk_logging` and `configure_sdk_model` but not `configure_cost_tracking`, so it leaks the cost backend. Both run under `mem_fs`, so the configuring test itself writes nothing. The leak shows up in the *later* tests that call the SDK after `mem_fs` has been torn down.
- `tests/conftest.py` has autouse fences for single seams (`_fake_auth`, `_neutralize_gate_lease`, `_block_external_commands`) but none for process-global SDK state and none for writes to live-state paths.
- `tests/test_conversation_transcript.py` locally sets `_cost_sample_backend` to `None` with the comment *"a leftover `_cost_sample_backend` from another test file would make `query()` emit a second `.jsonl`"*. The leak was noticed and worked around at the consumer (5a3f38ac).
- 33 test sites hand-roll `original = <alias>._X` … `<alias>._X = original` save/restore of an `sdk.py` private global, through the aliases `_sdk` and `sdk_mod` under varying local names. Six sites hand-clean `logging.getLogger("maestro").handlers`.
- The loop's gate runs from the live workspace. `docker-compose.yml` bind-mounts the host checkout at `/workspace`, and the gate path atoms come from `Config.work_target()`. A relative `data/` written by the gate's pytest process therefore *is* `$DATA_DIR`. Proposal 125 moved CI off the production host, so CI no longer contributes. The remaining writer is the loop's own gate: `in_process` rows in `gate_runs.jsonl`, plus any pytest the implementation session runs through Bash.
- Consumers read the polluted ledger unfiltered. They include `maestro/agent.py` (`_compute_budget_throttle_multiplier`), `maestro/cost_tracking.py` (`build_budget_breakdown`), the `cost-report` CLI, and the count-bounded `_tail_cost_samples` phase report. In that tail, 91% of rows are fixtures.

## Prior Point Fixes (whack-a-mole ledger)

| # | Identifier | What it patched | Why the class survived | Retired by this design |
|---|---|---|---|---|
| 1 | ce6a4c0b (2026-06-18) | A `builtins.open` monkeypatch layered on `mem_fs` leaked into every later test and silently rerouted their writes | Fixed the one test. There is still no owner that restores cross-test process state. | Class closed by Steps 3–4 (owner) and 1–2 (fence) |
| 2 | 5a3f38ac (2026-08-06) | `test_conversation_transcript` nulls a "leftover" `_cost_sample_backend` locally | Patched the symptom at the consumer, so the leaking producer kept leaking | Step 5 deletes the workaround |
| 3 | 406a3070 (2026-07-21) | `append_cost_sample` creates its parent directory | Right for production, but it removed the last accidental barrier: a leaked store in a test cwd now materialises `data/` | Kept. The fence now provides the barrier deliberately (Step 2). |
| 4 | `_reset_warning_throttle_for_tests`, `_reset_concurrency_semaphore_for_tests`, `_reset_sdk_extras_for_tests`, `_reset_usage_sink_for_tests` | One ad-hoc reset hook per global slice, called opt-in | 12 of 21 globals have no reset, and nothing calls the hooks by default | Step 5 deletes all four |
| 5 | 33 manual save/restore sites in `tests/` | Per-test restoration of individual `_sdk` globals | Any test that forgets to restore leaks; the two offenders above did | Step 5 deletes them |
| 6 | 6 manual `maestro` logger-handler cleanups | Per-test removal of handlers `main()` attaches | Same shape: opt-in cleanup of process-global state | Step 5 deletes them |
| 7 | 650a46cc → 3e290943 | Component tests coupled to the live `MAESTRO_ENV` overlay. The first fix pinned levers per test; the second fixed it at the sole TOML parser. | Sibling class (tests reading live state rather than writing it). It is the precedent for fixing at the owner. | Not retired (already closed); cited as the pattern |
| 8 | Proposal 125 step 10 | pytest cwd-anchoring workarounds for runner state shared on the production host | Retired by moving CI off-host, but the loop's own gate still runs in the live workspace | Superseded at the test-process boundary by the fence |

## Implementation Plan

Steps 1–4 and 7 are independently mergeable. Step 5 retires the moles only after Step 4's owner has been proven. None of this changes loop runtime behaviour: every step is test infrastructure, a behaviour-neutral restructuring of `maestro/sdk.py`, or an operator script. There is therefore no `Config` flag. Rollback is `git revert` of the step.

### Phase A — The live-state fence

1. [x] **Report-mode write barrier.** New `tests/_live_state_fence.py`, wired from `tests/conftest.py` at session start.
   - At session start, resolve the live-state roots:
     - every `memory_dir` declared by a `maestro*.toml` at the pytest rootdir, parsed through the sole TOML parser and resolved against rootdir;
     - `Config`'s field default (`~/.maestro`);
     - that root's `logs/` directory.
   - Install one `sys.addaudithook` that watches the following events whose resolved path falls under a root, and records the event, path, current test nodeid and a short stack:
     - `open` with a write, create or append mode or flags;
     - `os.mkdir`, `os.rename`, `os.replace`, `os.remove`;
     - `sqlite3.connect`, which covers the `SqliteCostSampleStore` route under `[paths] sqlite_state_enabled`.
   - Report mode lists the recorded violations in `pytest_terminal_summary` and fails nothing.
   - `mem_fs`'s fake `open` never raises the audit event, so tests inside `mem_fs` are unaffected by construction.
   - *Depends on:* none.
   - *Acceptance:* with the two offender tests unfixed, a session running `test_main_dispatches_workspace_counterfactual` and then `tests/test_sdk.py` reports ≥1 violation. Each violation names the `data/cost_samples.jsonl` or `data/logs/sdk/` path and the test during which the write happened.
2. [x] **Enforce: refuse, attribute, fail.** `tests/_live_state_fence.py`, `tests/conftest.py`.
   - The hook raises `PermissionError` before the write happens, so nothing reaches disk.
   - An autouse per-test finalizer fails the test that recorded a violation and shows the recorded stack. This is needed because `_log_sdk_call` swallows the raised error at `debug`.
   - A `pytest_sessionfinish` check fails the session for violations recorded outside any test (collection, fixture teardown).
   - Audit hooks cannot be removed, so the hook checks a module-level armed flag and returns immediately when unarmed.
   - *Depends on:* 1, plus 4 for a green suite, because enforcing before the owner lands would red the suite on the known leak.
   - *Acceptance:* a seeded control in `tests/test_live_state_fence.py` is refused and attributed. The control opens a write handle under an injected fake root, so the refusal fires before any real write. Both the per-test failure and the session-level failure are proven by calling the fixture/hook functions directly.

### Phase B — One owner for the SDK's process-global runtime

3. [x] **Register the runtime globals and add snapshot/restore.** `maestro/sdk.py`, `maestro/providers/registry.py`, `tests/test_sdk_runtime_owner.py`.
   - Add a module-level `_RUNTIME_GLOBALS` tuple naming every global the module assigns under a `global` statement.
   - Add `snapshot_sdk_runtime()` and `restore_sdk_runtime(snapshot)` over it, plus the same pair for the provider registry.
   - Every `configure_*` keeps its signature and behaviour. Production calls `configure_*` exactly as today and never calls restore.
   - Add an AST guard to `tests/test_sdk_runtime_owner.py`: every name assigned under `global` in those two modules must appear in its module's registry. A new sink cannot then escape reset.
   - *Depends on:* none.
   - *Acceptance:* the guard fails on a seeded module that assigns an unregistered global (the `tests/test_fault_seeding.py` pattern). `snapshot → configure_cost_tracking(x) → restore` leaves `_cost_sample_backend` identical to the pre-snapshot value.
4. [x] **Autouse lifecycle fixture.** `tests/conftest.py`.
   - Snapshot the SDK runtime, the provider registry, and the `maestro` logger's handler list before each test.
   - Restore them after the test, closing any handler added during it.
   - This is the single owner of cross-test SDK state.
   - *Depends on:* 3.
   - *Acceptance:* in one session, `test_main_dispatches_workspace_counterfactual` followed by an SDK-driving test leaves `sdk._cost_sample_backend is None` and `sdk._sdk_log_dir is None` at the second test's start. The Step 1 fence records zero violations for the full suite.
5. [x] **Retire the moles.** `maestro/sdk.py`, `tests/test_sdk.py`, `tests/test_cli.py`, `tests/test_conversation_transcript.py`, and the other files that hand-restore state.
   - Delete the four `_reset_*_for_tests` hooks and their call sites.
   - Delete the 33 manual `_sdk` save/restore sites (replacing them with plain `monkeypatch.setattr` where a test needs a *value*, since restoration is now automatic).
   - Delete the transcript test's local `_cost_sample_backend` null and the six manual logger-handler cleanups.
   - *Depends on:* 4, and 2 enforced green.
   - *Acceptance:* `grep` finds no `_reset_` + `_for_tests` definition in `maestro/sdk.py`. An `ast` walk over `tests/` finds no assignment whose value is an attribute read of a `_RUNTIME_GLOBALS` name on any module object bound to `maestro.sdk` (whatever the alias — `_sdk`, `sdk_mod`, …), and no assignment back to one outside `monkeypatch.setattr`. The full suite is green with the fence enforcing.
6. [x] **Pin the two offenders' intent.** `tests/test_cli.py`.
   - Give `test_main_dispatches_workspace_counterfactual` and `test_main_sets_up_file_logging_for_run` an explicit assertion that the SDK configuration block ran: patch `configure_cost_tracking` and assert it was called.
   - These tests exist to prove that membership. Today they prove it only incidentally, by mutating real globals.
   - *Depends on:* 4.
   - *Acceptance:* both tests assert `configure_cost_tracking` was called once, and neither records a fence violation.

### Phase C — Cleaning up the polluted ledger

7. [x] **Operator quarantine script.** New `scripts/quarantine_test_telemetry.py`. It is read-only by default: `--report` prints counts and cost by caller and by classification rule over `--since`.
   - `--apply` moves classified rows from `cost_samples.jsonl` into `cost_samples.quarantine.jsonl`.
     - It holds the same `file_lock` that `JsonlCostSampleStore` takes and rewrites via `atomic_write()`.
     - It refuses to run while `maestro.pid` names a live process.
   - `--apply` also moves test-shaped `logs/sdk/**/*.log` files into `logs/sdk-quarantine/`, preserving their relative paths. It moves rather than deletes, so every action is reversible.
   - A row is test traffic if either:
     - (a) its timestamp falls inside a recorded `gate_runs.jsonl` window; or
     - (b) its caller is a literal that appears as a `caller=` value in `tests/` and nowhere in `maestro/`. This set is AST-derived when the script runs, so it automatically excludes real names such as `unknown` and `warning_hold_probe`. Rule (b) is the only rule for rows before the first recorded gate run (2026-09-09).
   - A log file is test-shaped if both:
     - its mtime falls inside a gate window, or (for files before 2026-09-09) its header matches the `Prompt: test` / `caller=None` shapes above; and
     - its header has no production caller label.
   - The burn-rate reader is deliberately left unchanged. Filtering at the reader would be the next mole.
   - *Depends on:* none. It can land and be dry-run before Phase A.
   - *Acceptance:*
     - `--report` over the fixture ledger in a `mem_fs` test classifies each seeded row correctly: a gate-window row, a test-only-literal row, a real `unknown` row outside any window, and a real `introspect` row outside any window.
     - `--apply` preserves the union of live and quarantine rows byte-for-byte.
     - `--apply` refuses while a live pid is present.
8. [ ] **Run it once in production and record the before/after.** This step is operational: no code, just a noted run.
   - Stop the loop (`scripts/safe-deploy.sh`'s stop path), run `--report`, have a human review the per-caller table, then run `--apply`.
   - Record the before/after row counts and the new 5-hour burn-rate reading in the PR that flips this proposal's status.
   - *Depends on:* 7, plus 4 deployed, so pollution does not resume immediately.
   - *Acceptance:* `--report --since` the deploy time shows zero rows under rule (a) or rule (b) after the next seven days of gate runs.

## Testing Strategy

- `tests/test_live_state_fence.py`:
  - Root resolution from a `mem_fs` TOML fixture, covering the relative `memory_dir`, an absolute override, and the `~/.maestro` default.
  - Audit-hook classification of each event: write vs read mode `open`, `os.open` flags, `mkdir`/`rename`/`replace`/`remove`, `sqlite3.connect`, a path outside every root, and the unarmed flag.
  - The seeded refusal control, the per-test finalizer failure, and the session-level failure. The hook and fixtures are driven directly with a fake root, so no real write is ever attempted.
- The Step 3 AST guard in `tests/test_sdk_runtime_owner.py` gets a seeded-offender control so it cannot pass vacuously.
- A lifecycle-fixture regression test proves the Step 4 acceptance, i.e. that the offender sequence leaves the globals `None`.
- The script's tests run under `mem_fs` with synthetic `cost_samples.jsonl`, `gate_runs.jsonl` and `logs/sdk` fixtures. They cover each classification rule, the pid refusal, reversibility (live ∪ quarantine = original), and the empty-ledger and no-gate-runs fallbacks.
- None of these tests use real filesystem writes, subprocesses, network or sleep. The fence becomes the deterministic enforcement test for the Testing Principles rule "never write to the file system" as far as live state is concerned.

## Risk Assessment

- **Audit-hook overhead.** `open` events are frequent in a 400-second suite. The hook does one armed-flag check plus a string-prefix test on the resolved path. Measure the suite's `tests_seconds` in `gate_runs.jsonl` before and after Step 1. The tripwire is a >2% p50 regression, which would call for caching resolved roots and skipping read-only modes first.
- **False refusals.** A test that legitimately needs to write a live-state path does not exist by rule. Report mode (Step 1) lists every hit before enforcement. Any genuine need is met by `mem_fs` or `tmp_path`, never by an allowlist.
- **Hidden restoration dependencies.** Some tests may rely on a global leaking from an earlier test. Step 4 would surface them as order-dependent failures. That is a feature, and each one is fixed by making the dependency explicit in the test.
- **Quarantine misclassification.** Rule (a) assumes the loop issues no real SDK calls while its gate runs. The 7-day data supports that (no `implement_plan`, `self_review`, `update_self_model` or other real-only caller appears in any window), but `--report` is mandatory before `--apply`. The move is reversible, and the live ledger's row set is preserved as live ∪ quarantine.
- **Rollback.** Revert the step. No state migration and no runtime flag are involved. A quarantined ledger can be restored by concatenating and sorting on timestamp.

## Out of scope

- **Running the loop's gate outside the live workspace**, for example in a disposable worktree whose `data/` is not `$DATA_DIR`. That is defence in depth, and a larger change to the gate lease and `Config.work_target()`. The fence closes the class at the test-process boundary whatever the cwd.
- **Fencing all real-disk writes, including `tmp_path`.** The "never write to the file system" rule is broader than live state. Ratcheting every `tmp_path` user onto `mem_fs` is a separate, larger migration. This proposal fences only the roots whose pollution corrupts production.
- **Re-deriving per-caller effort and cost baselines.** Any analysis built on the polluted ledger, such as per-call-site counts for `introspect`, `planner` or `execute_step`, should be re-run after Step 8. Doing that is not part of this proposal.
