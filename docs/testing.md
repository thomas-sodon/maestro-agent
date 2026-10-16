# Testing Infrastructure

## Unit Test Patterns

Tests use the `mem_fs` fixture (defined in `tests/conftest.py`) which provides an in-memory filesystem. All `Path` and `open()` operations are intercepted and routed to in-memory storage. The Claude Agent SDK is always mocked via `unittest.mock.patch` — never called in tests.

Key test patterns:
- `mem_fs` fixture: patches `builtins.open`, `Path.exists/read_text/write_text/mkdir/replace/unlink`, `tempfile.mkstemp`, `os.close` to use in-memory storage
- `make_agent` fixture: creates a `MaestroAgent` with real class instances backed by in-memory filesystem
- `MockQueryResult` + `make_mock_query`: helpers for mocking `claude_agent_sdk.query` responses
- Source code reads (e.g., HTML templates) fall through to the real filesystem; only test data is in-memory
- **Assert mock interactions**: always verify that mocked dependencies were called with expected arguments (e.g., `mock.assert_called_once_with(...)`, `mock.assert_called()`)

### Sibling `maestro_sdk` on the import path

A few test modules (`tests/test_provider_cli_paced.py`, `tests/test_tool_adapter.py`) import the sibling `maestro_sdk` package at module top. It is installed separately (`pip install -e "maestro_sdk/"`) and that remains the supported setup — but when it is absent (the loop devbox), those imports would abort at *collection* and degrade the whole run to "could not run". `[tool.pytest.ini_options] pythonpath = ["maestro_sdk/src"]` — the pytest twin of `[tool.mypy] mypy_path` — resolves the sibling from the in-repo tree instead. pytest *prepends* that entry to `sys.path`, so this checkout's `maestro_sdk/src` takes precedence over an editable install pointing at another checkout.

## Component Test Infrastructure

Component tests exercise full multi-module flows with external services mocked at the Python level — no Docker required. Three mock layers replace the previous Docker-based infrastructure:

| Mock | What it replaces | Location |
|---|---|---|
| `SDKMock` | `AsyncMock(side_effect=[...])` for Claude SDK | `tests/component/sdk_mock.py` |
| `MockVCS` / `MockCodeHost` | Docker mock binaries + mock server for git/GitHub | `tests/component/mock_vcs.py` |
| `respx` | Docker mock server for Telegram Bot API | `tests/component/conftest.py` (`telegram_mock` fixture) |

**`SDKMock`** provides prompt-based routing for Claude SDK responses:
- `sdk.on(pattern, response)` — match prompts by substring or regex
- `sdk.then(*responses)` — ordered fallback queue (FIFO)
- `sdk.on_any(response)` — catch-all for unlimited matching
- `sdk.patch(*modules)` — context manager that auto-patches `query` in specified modules
- Verification: `assert_called()`, `assert_not_called()`, `assert_all_consumed()`, `assert_prompt_contained()`
- Response builders: `evolution_plan()`, `self_model_update()`, `review_result()`

**`MockVCS`** delegates local git operations (commit, branch, checkout) to real `SubprocessVCS` in an isolated `tmp_path` repo, while recording remote operations (push) in-memory. **`MockCodeHost`** implements the full `CodeHost` protocol in-memory — tests configure it directly (e.g., `code_host.issues = [...]`) instead of via HTTP admin endpoints.

**`respx`** intercepts `httpx` HTTP calls made by `python-telegram-bot`, mocking Telegram Bot API responses at the HTTP level.

The `_block_external_commands` guard in `tests/conftest.py` catches any leaked `gh` subprocess calls and `git` calls outside `tmp_path`, ensuring no external commands escape the mock layer. Its sibling `_block_real_sdk_transport` refuses (and records, failing at teardown) any `SubprocessCLITransport.connect` — a `patch("maestro.<mod>.query")` aimed at the wrong module would otherwise open a real Claude session; the `cli_paced` pty runner is outside its fence.

The autouse `_own_sdk_runtime` fixture in `tests/conftest.py` is the one owner of cross-test SDK state (proposal 138): it snapshots `maestro/sdk.py`'s and `maestro/providers/registry.py`'s runtime globals plus the `maestro` logger's handlers and level, and restores them after every test, so a sink `cli.main` points at the real `data/` tree cannot outlive the test that set it. Every name assigned under `global` in either module must be registered in that module's `_RUNTIME_GLOBALS`; `tests/test_sdk_runtime_owner.py` enforces it with `ast`. Tests set a runtime global through its `configure_*` seam or `monkeypatch.setattr`, never by direct attribute assignment and a manual restore; the same file's `test_no_direct_runtime_global_writes` scans every `tests/**/*.py` for such writes.

The live-state fence (`tests/_live_state_fence.py`, wired in `tests/conftest.py`) is its detecting complement: a `sys.addaudithook` hook, installed once per pytest process, watches write-shaped `open`, `os.mkdir`/`rmdir`/`remove`/`truncate`, `shutil.rmtree`, `os.rename`/`replace`/`link`/`symlink` and `sqlite3.connect` events whose path falls under a live-state root — each `maestro*.toml`'s `[paths] memory_dir` plus `Config`'s `~/.maestro` default, resolved once at session start in both absolute and real-path forms. Hits are attributed to the running test's nodeid (xdist workers relay theirs to the controller) and folded to one row per test and path with a hit count, and the hook refuses each such write with `PermissionError` before it reaches disk. The autouse `_live_state_verdict` fixture then fails the attempting test with the recorded stack — even when a best-effort handler (such as `_log_sdk_call`'s) swallowed the refusal — and `pytest_sessionfinish` fails the run for a write recorded outside any test (collection, late teardown), which the `live-state fence` terminal-summary section lists as `<outside any test>`. The fix for a hit is `mem_fs` or `tmp_path`, never an allowlist.

Component test fixtures (in `tests/component/conftest.py`):
- `make_agent()` — returns `(agent, code_host, vcs)` tuple with injected mocks
- `sdk()` — pre-configured `SDKMock` instance
- `code_host()` — `MockCodeHost` instance
- `vcs(workspace_dir)` — `MockVCS` wrapping real git in `tmp_path`
- `telegram_mock()` — `respx` routes for Telegram Bot API

## Browser Tests — Playwright

```bash
pip install -e ".[dev,browser]" && playwright install chromium
pytest tests/browser/ -v -m browser --no-cov --browser chromium
```

Browser tests exercise the dashboard frontend JavaScript in a real headless Chromium browser. They use a lightweight mock FastAPI server (no real agent) and validate UI rendering, interactivity (approve/reject plans, log filtering, SDK sessions), toast notifications, auto-refresh, error handling, and XSS protection. Tests are excluded from the default `pytest` run via the `browser` marker. They run in CI as a separate parallel job on every PR and push to main.

## Component Tests

```bash
pytest tests/ -v
```

Component integration tests exercise full multi-module flows (agent.run, contemplate, evolve, run_loop, evolution implementation, GitHub issues, Telegram notifications, consciousness growth, purpose lifecycle) with external services mocked at the Python level. No Docker required — all mocks are in-process. The Claude Agent SDK is mocked via `SDKMock` (`tests/component/sdk_mock.py`), GitHub/git operations via `MockVCS`/`MockCodeHost` (`tests/component/mock_vcs.py`), Telegram via `respx` HTTP mocking, and quality gate tools via `make_quality_gates_mock`. Tests use real filesystem (`tmp_path`) and real git repos in isolated temp directories. Component tests are included in the default `pytest` run alongside unit tests.

The component `config` fixture pins `env_name=None` (#3460), so component behavior is independent of an ambient `MAESTRO_ENV` — and, by the same token, nothing in the default run exercises a loop phase under the flag combination `maestro.production.toml` actually ships. Behavioral coverage of that shipped production posture lives in `tests/component/test_production_posture_regression.py`, which builds the production `Config` through `build_config_authoritative(CONFIG_DIR, "production")`, wires it in via `make_agent`'s `config_override` seam, and asserts `evolve()`'s production call sequence is exactly consumed *and* that the lever-gated readiness call did not fire. A non-vacuity anchor derives the levers under test from the base-vs-production field diff, so an overlay edit fails the guard instead of silently degrading it. A new production-only lever that changes a phase's priced call sequence belongs in this module.

## Structural-Guard Registry

Maestro's load-bearing invariants — the merge-truth chokepoint, CI-truth classification, salvage verdicts, WIP directives, session attachment, and more — are defended by **AST structural guards**: tests that parse `maestro/` source and fail the build when a decision is made outside its single sanctioned owner. These guards were once hand-copied standalone `test_*_chokepoint.py` files, each carrying its own duplicated line→function mapper. They now live as data in one registry behind a shared harness (proposal 109).

**Registry and runner.** The registry is `CHOKEPOINT_GUARDS` in `tests/structural_guards.py` — a plain list of `ChokepointGuard` specs plus their guard-specific detectors, carrying no assertions itself. The parameterized runner in `tests/test_structural_guards.py` drives every entry through the shared harness: two in-memory parity controls (`test_synthetic_bypass_is_flagged`, `test_synthetic_ok_is_clean`) under the default 5s budget, then the real-tree cases (`test_live_scan_is_clean`, `test_allowlist_entries_are_real`, `test_allowlist_entries_exempt_a_live_hit`, `test_subjects_still_exist`, and the relational runners) under the `@pytest.mark.timeout(15)` component budget. Parametrizing `ids=[g.name for g in CHOKEPOINT_GUARDS]` means a partial registry simply collects fewer cases without touching any guard file.

**Shared harness.** `tests/support/structural_guard.py` holds the skeleton every bespoke guard duplicated: the whole-AST scan (`scan_source` / `scan_for_violations`), the `(module, function)` allowlist filter, the synthetic controls (`assert_flags_synthetic` / `assert_synthetic_ok`), the allowlist-staleness check (`assert_allowlist_entries_real`) and its liveness counterpart (`assert_allowlist_entries_live` — every entry must still exempt a live detector hit, so a refactor that empties an exempted scope must delete its entry; `assert_partition_co_occurrence` applies the same rule to partition allowlists), the subject-existence retirement trigger (`assert_subjects_exist`), and the canonical `line_to_function` line→function mapper. The mapper's home defines it under the no-underscore name, so the harness is its one legitimate owner.

**Adding an entry.** Append a `ChokepointGuard` to `CHOKEPOINT_GUARDS` with: `subjects` (the symbol(s) it protects, so a deleted subject retires it), `provenance` (the proposal or incident that minted the invariant — `test_provenance_is_present` refuses a blank one), a `detector` (classifies one AST node, returning the primitive string it flags), and the two in-memory controls `synthetic_bypass` (a snippet the detector must flag) and `synthetic_ok` (a legitimate snippet it must not). Relational invariants a single-node detector cannot express use the optional `required_presence`, `scoped_references`, `co_occurrences`, `module_purity`, and `partition_co_occurrences` fields, each with its own runner.

**Population budget.** `_MAX_STRUCTURAL_GUARDS` in `tests/test_structural_guards.py` caps `len(CHOKEPOINT_GUARDS)` (asserted by `test_registry_population_within_budget`; its predicate `_population_within_budget` is a real numeric bound, proven non-vacuous by `test_population_budget_bites_past_the_cap`). It is a literal integer, not `len(...) + N`, so the population cannot silently grow: adding a guard is zero-sum — retire an entry (or migrate its subject away), or raise the cap in the same reviewed diff and justify it.

**Retirement trigger.** `test_subjects_still_exist` reads every symbol defined under `maestro/` once and fails when a guard's `subjects` no longer exist — a guard whose protected code is gone is dead weight, and the red build names it for removal.

**Meta-guard.** The budget governs registry entries, but a fresh bespoke `test_*_chokepoint.py` copying the old skeleton would route around it. `test_no_unregistered_skeleton_files` closes that bypass: its predicate `_files_defining_bespoke_skeleton` scans every `*.py` under the tests root and fails on any file that defines a module-level `_line_to_function` outside the sanctioned harness home (`_SANCTIONED_SKELETON_HOME`) and the documented allowlist `_ALLOWED_SKELETON_CARRIERS`, telling the author to migrate the guard into `CHOKEPOINT_GUARDS` or allowlist it with a reason. `test_skeleton_allowlist_names_live_carriers` keeps the allowlist self-pruning — a stale entry whose file was migrated into the registry or lost its skeleton turns the build red and names the entry to remove.

Three files carry the skeleton by design and are allowlisted:

- `tests/test_conversation_transcript.py` — the transcript-reader chokepoint, co-located with its behavioral suite rather than migrated into the registry.
- `tests/test_no_silenced_exceptions.py` — the whole-tree `except`-scan guard, a differently-shaped guard outside the registry's scope, keeping its own mapper copy.
- `tests/test_work_target_chokepoint.py` — the client-construction chokepoint, a later-minted guard awaiting migration into the registry.

**In-runner tail guards.** A third route past the budget is legitimate but was, until now, unledgered: some invariants assert a branch *shape* inside a named function — a cadence modulo in `phase_will_run`, a flag-gated `source_task_id` consumer — rather than classifying a single node over the tree, which the registry's detector contract cannot express without distorting it. Those guards live as plain `def test_*` functions in `tests/test_structural_guards.py` that parse a live `maestro/` module directly, so neither `_MAX_STRUCTURAL_GUARDS` (they are not registry entries) nor the meta-guard (they carry no `_line_to_function`) sees them. The route is now ledgered: `_TAIL_GUARD_NAMES` enumerates them and the literal `_MAX_TAIL_GUARDS` bounds them, under the same zero-sum rule the registry budget uses. The predicate `_bespoke_tail_guards` finds them — a module-level `def test_*` that references `_MAESTRO_ROOT` (or a constant derived from it) while naming no registry object, computed over decorators and body expanded one level through module-level helpers so a guard reaching its entry via a helper or a `parametrize` decorator stays exempt (a module-scoped fixture is expanded by the same rule and confers no exemption of its own — injecting `maestro_symbols` makes a test a live-source reader that still needs a registry name) — and the bidirectional `test_in_file_tail_guards_are_enumerated` makes both adding a fifth and leaving a stale entry behind a conscious diff (`test_tail_guard_detector_flags_and_exempts` proves the predicate bites in memory; `test_tail_guard_budget_bites_past_the_cap` proves the bound is real).

Four guards ride this route today:

- `test_priced_introspection_phase_is_cadence_gated` — proposal 113: every priced budget-gated phase carries an `iteration % <n>` cadence branch in `phase_will_run`, instead of falling through its default `return True`.
- `test_source_task_id_consumption_is_reuse_flag_gated` — proposal 116 step 9: only sanctioned `Evolution` functions consume `source_task_id` as an implementation base, and only behind `[loop] task_branch_reuse_enabled`.
- `test_generate_task_scheduling_is_master_switch_gated` — proposal 116 step 9: `generate_task` scheduling stays behind `[loop] self_directed_tasks_enabled`.
- `test_promotion_pressure_sites_name_master_switch` — proposal 116 step 9: every promotion-pressure site names `[audit] branch_promotion_pressure_enabled`.

**Retired concepts: a hard ban.** A retired concept may live *nowhere*, unlike a chokepoint decision that lives in one owner, so it gets its own registry: `RETIRED_CONCEPTS` in `tests/structural_guards.py` (proposal 148), separate from `CHOKEPOINT_GUARDS` and outside `_MAX_STRUCTURAL_GUARDS`. Each `RetiredConcept` declares its identifiers, keyword/parameter names, string literals, prose phrases, exempt modules, and exempt `(module, literal, reason)` sites (modules keyed by path under `maestro/`); `retired_concept_hits` finds references in one source, a keyword/parameter name also counting as an attribute read, as a string key, and as an annotated class field (`stage: str`). `tests/test_retired_concepts.py` fails on any hit under `maestro/` outside `exempt_modules` / `exempt_literal_sites` — there is no baseline. The keyword/parameter match is deliberately name-only (`stage` has no stage-bearing receiver to key on), so there is no attribute or keyword exemption: an unrelated `.stage`, `stage=`, or `"stage"` key added under `maestro/` must use a different name (e.g. `deploy_phase`), because a per-module name exemption would blind real hits too. To retire a further concept, append a `RetiredConcept`; the liveness test prunes stale exemptions. The `purpose_hypothesis` entry bans the deleted `SelfModel.current_purpose_hypothesis` field, and the `revenue_charter` entry bans the retired revenue-charter lineage (charter, net burn, scouting, income, intake, quotes, streams).
