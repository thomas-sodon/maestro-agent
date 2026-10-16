# 102: CI-Failure Diagnosis Oracle — One Reader for *Why* a PR's CI Is Red, So the Loop Fixes the Failing Check Instead of Re-Lapping Blind

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/red_root.py`, `maestro/vcs.py`, `maestro/agent.py`, `maestro/pr_readiness.py`, `maestro/outcome_grounding.py`, `maestro/loop/pr_remediation.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, CLAUDE.md, new `maestro/ci_diagnosis.py`, new `tests/test_ci_diagnosis.py`, new `tests/test_ci_truth_chokepoint.py`, `tests/test_config.py` |
| **What landed** | Steps 1–9 (Phase 1 + the Phase 2 consolidation, chokepoint, and default flip, plus the Phase 3 mole retirement). Step 1 (PR #3223) — new `maestro/ci_diagnosis.py`: the `CIFailure` frozen dataclass (`check_name` / `first_error_lines`), `diagnose_ci_failure(failing_checks, read_log)` returning a provenance-stamped `tuple[CIFailure, ...]`, and the pure `extract_first_errors(log_text)` line-scanner, plus `tests/test_ci_diagnosis.py`. Step 2 (PR #3223) — `read_run_log_failed` (`maestro/vcs.py`): the bounded, secret-redacted `gh run view --log-failed` seam that returns `""` fail-soft, covered by `tests/test_vcs_code_host.py`. Step 3 (PR #3223) — the `[loop] ci_failure_diagnosis_enabled` flag (`maestro/config.py` / `maestro/toml_config.py` / `maestro.toml`) wiring the oracle's `CIFailure` lines into `_observe_pr_check_runs` / `_record_red_root_diagnosis` (`maestro/agent.py`); flag-off is byte-identical to today. Step 4 (PR #3227) — the `## CI Failure` section threaded into `_compose_pr_remediation_prompt` (`maestro/loop/pr_remediation.py`) via `render_ci_failure_section`, gated on the same flag. Step 5 (PR #3237) — the rollup classifiers consolidated into `maestro/ci_diagnosis.py` (`FAILING_CONCLUSIONS` / `classify_ci_conclusions` / `CheckRun` relocated here; `vcs._FAILURE_STATES` and `red_root._FAILING_CONCLUSIONS` removed and re-imported), so "is CI red / which checks / why" has one owner. Step 6 (PR #3237) — `tests/test_ci_truth_chokepoint.py`, the AST-golden guard asserting `ci_diagnosis` is the sole rollup-classification / log-error-extraction owner with the `read_run_log_failed` fetch seam exempted, plus the CLAUDE.md CI-truth-chokepoint invariant line. Step 7 — the evidence-gated default flip: `[loop] ci_failure_diagnosis_enabled` now defaults `true` in `maestro.toml` and `Config` (`maestro/config.py`), so the oracle runs by default; a `[loop]` overlay `false` restores the byte-identical blind behavior. Step 8 (PR #3259) — the `[loop] ci_failure_diagnosis_enabled` flag retired so the oracle runs unconditionally on a CI-red own-PR lap, the "look to pytest, a CI-specific check" all-passed fallback deleted from `_record_red_root_diagnosis` (`maestro/agent.py`) in favour of a concrete-blocker priority chain, and the synthetic unnamed `CheckRun` fallback removed from `_observe_pr_check_runs` (a name-less aggregate FAILING is recovered by threading the authoritative rollup verdict into `diagnose_red_root`, not by fabricating a stand-in). Step 9 (PR #3271) — `bind_ci_failing_verdict_to_red_read` (`maestro/outcome_grounding.py`) narrowed to the oracle-surfaced-but-ignored case behind `[loop] red_read_oracle_scoped_enabled` (default `true`): it fires only when the CI-failure oracle extracted a concrete error line this lap (the `_ci_oracle_surfaced` cache) and the lap did not commit a fix on the owned branch, so it keeps an edited-and-pushed lap and no longer punishes a lap for *not* running a local gate when it pushed a fix — which local gates ran is irrelevant, and a lap that ran the wrong subset without pushing a fix is still downgraded; overlay `false` restores the blunt "no pytest/ruff/mypy ran" proxy. _(The `red_read_oracle_scoped_enabled` flag was subsequently retired in a follow-on, so the narrowed downgrade is now unconditional and the blunt-proxy overlay no longer exists.)_ |
| **What is missing** | Nothing — Phase 3 mole retirement (steps 8–9) has landed, so all nine steps are shipped; see **What landed**. |
| **Depends on** | 087 _(shipped)_, 089 _(shipped)_ |
| **Derived from** | live production data via /propose, 2026-07-24 |

## Description

> Status updated to shipped on 2026-07-31 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

When a self-authored PR's CI turns red, Maestro reads exactly two things off GitHub: the aggregate status-check *rollup verdict* (`success` / `failure` / `none` / pending) and the *names* of the failing checks. It never reads the failing check's actual output. Its only local reproduction — the red-root diagnostic — runs a hardcoded `ruff → mypy` subset that deliberately excludes `pytest`, coverage, browser tests, and the `maestro_review` / `maestro_sdk` gate suites, which is where nearly every real CI failure lives. The result is a loop that can name the failing check (`quality`) but has no concrete error to act on, so it re-laps or downgrades to `partial` without ever seeing *why* CI is red.

This proposal builds the missing capability as a single owner: a **CI-failure diagnosis oracle** (`maestro/ci_diagnosis.py`) that fetches the failing check-run's concrete log/annotations, extracts the first real error lines, redacts and bounds them, and threads them into the red-root artifact, the journal realization, and the remediation prompt. It then folds the four scattered re-derivations of "is CI red?" behind that one oracle and pins it with an AST/golden test, the same way proposal 087 put every merge-truth read behind `is_pr_settled` and `tests/test_merge_truth_chokepoint.py`.

The end state: on a CI-red own-PR lap, the first thing the loop knows is the concrete failing test (`tests/test_foo.py::test_bar — AssertionError: …`) or the concrete coverage/lint diagnostic — not the note "look to pytest, a CI-specific check." The failing-check name is no longer a dead end. The per-symptom pile of classifiers, markers, binds, and holds that accreted around the blind rollup — because the missing read was never built — has one place to live and several of its members can retire.

## Relevant Findings

- `maestro/red_root.py` (`build_red_root_gates`) — the local red-root reproduction is a fixed `(ruff, mypy)` pair; `pytest` is *deliberately* excluded ("so the read stays quick"). CI's `quality` job runs `ruff` + `mypy` + `pytest --cov` at 100 % coverage, and separate jobs run browser tests, `maestro_review`, and `maestro_sdk` — so the local gate is a strict subset of the CI matrix and cannot reproduce the dominant failure modes.
- `maestro/agent.py` (`_record_red_root_diagnosis`) — when local `ruff`/`mypy` pass, the recorded "concrete blocker" is the literal string "the demanding gates (ruff, mypy) pass on the current tree … look to pytest, a CI-specific check, or the PR branch tree." This is the anesthetic: a diagnosis that names no defect.
- `maestro/agent.py` (`_observe_pr_check_runs`) — the `CheckRun` sequence handed to `diagnose_red_root` is built solely from `get_pr_failing_check_names` with `conclusion="failure"` hardcoded, plus a synthetic unnamed `CheckRun` when the rollup says FAILING but names nothing. No log, annotation, or `detailsUrl` is ever read.
- `maestro/vcs.py` (`get_pr_check_conclusion`, `get_pr_failing_check_names`) — both parse `gh pr view --json statusCheckRollup` and read only each item's `conclusion` / `state` (and, for names, `name` / `context`). The rollup also carries `detailsUrl` per check-run, which is discarded — the pointer to the failing run's logs is thrown away.
- No code path anywhere in `maestro/`, `maestro_review/`, or `maestro_sdk/` invokes `gh run view`, `--log-failed`, check-run `annotations`, or a check-run `output` read. The capability to see a CI failure's content does not exist.
- `maestro/pr_readiness.py` (`ci_state_from_conclusion(conclusion: str)`) and `maestro/red_root.py` (`classify_ci_conclusions(check_runs)`) do *not* re-read the rollup — they consume already-derived inputs downstream of the two `vcs.py` parsers, so the topology is two rollup parsers plus two consumers, not four independent rollup reads. The genuine duplication is the failure-vocabulary set: `vcs._FAILURE_STATES` and `red_root._FAILING_CONCLUSIONS` are hand-mirrored frozensets (aligned by `9690ec55`, still two copies), and that shared membership test is what Phase 2 gives a single owner — the exact shape 087 collapsed for merge-truth.
- `maestro/outcome_grounding.py` (`bind_ci_failing_verdict_to_red_read`, `_FAILING_GATE_RE`) — the write-time bind downgrades a `CI_FAILING` completion verdict to `partial` unless the session ran `pytest` / `ruff check` / `mypy`. It punishes the blind lap but supplies no failure content; running any of the three locally satisfies the bind even when none reproduces the CI-only red (coverage, browser, `maestro_sdk`).
- `maestro/loop/pr_remediation.py` (`_compose_pr_remediation_prompt` delivery clause) instructs the agent to "read review/CI state with `gh pr view` / `gh pr checks`" — neither surfaces a failing log — while `merge_truth_first_preamble` (`maestro/prompts/remediation.py`) tells it to "re-chase [CI] to green." The agent is ordered to fix a red it is given no way to see.
- CLAUDE.md ("Bash output … hard-capped at 2000 chars post-query, never re-sent to the model") — even if the agent ran `gh run view --log-failed` itself, the harness truncates and drops the output, so a pre-extracted bounded excerpt is the only way the failure reaches the model.
- `tests/test_merge_truth_chokepoint.py` — the house AST-golden pattern this proposal reuses: one provenance-stamped oracle, enforced as the *only* door, is how 087 retired scattered per-site merge checks.

## Production Evidence

Live data from `$DATA_DIR = /opt/maestro/data` (host bind-mount of `maestro-loop`).

- **CI-blocker laps, `task_history.jsonl`, window 2026-07-15 → 2026-07-24 (9 days):** 25 of the last 85 `Remediate PR #N` laps (29 %) carried a CI merge blocker — 22 `CI_PENDING`, 3 `CI_FAILING` — across 16 distinct PR lineages (#2811, #3083, #3106, #3111, #3112, #3145, #3155, #3165, #3175, #3176, #3179, #3192, #3195, #3197, #3208, #3210). **Every one closed `partial`.** None recorded a concrete CI error.

- **The single live red-root artifact, `artifacts/red-root-pr-3195/red-root.md`:** the diagnosis on a FAILING PR names no defect —

  ```
  # Red-root diagnosis — PR #3195
  - CI state: FAILING
  - Concrete blocker: the demanding gates (ruff, mypy) pass on the current tree —
    ruff/mypy do not reproduce the red here; look to pytest, a CI-specific check,
    or the PR branch tree
  - Remote CI: FAILING
  - Failing checks: quality
  ```

  The failing check is named (`quality`) and the local subset passes, so the loop's own record says "look elsewhere" and stops. PR #3195's lineage burned 3 laps before it merged.

- **The self-authored whack-a-mole confession (`git show 55cb1706`, repo history):** Maestro's own commit message names the class exactly — "its blocker was `CI_FAILING`, a code-clearable red gate, yet across every lap my bash history holds only `git fetch` / `--is-ancestor` / thread enumeration and zero `pytest` / `ruff` / `mypy`. I diagnosed the red and never opened it, because the check cannot fail me and the fix can." The fix that shipped was a *punitive verdict downgrade*, not a way to see the failure.

## Prior Point Fixes (whack-a-mole ledger)

| Identifier | What it patched | Why the class survived |
|---|---|---|
| #2109 | `get_pr_check_conclusion` returns `"none"` distinct from `""` so a no-CI repo isn't deferred forever | Touches only the aggregate verdict; never reads a failing log |
| #2440 | `_is_advisory_check` skips `review` / `reply-coverage` so a red advisory can't block | Filters the rollup; the gating checks' *content* is still unread |
| `3a2b4c5e` | Red-root diagnosis: run `ruff → mypy` as action-zero, record the blocker | `ruff+mypy` is a strict subset of CI — misses pytest, coverage, browser, sdk/review |
| `c541518c` | Remote-CI red-root: split RUNNING from FAILING, surface the failing check *name* | Names, not logs — produces the "look to pytest" dead-end note |
| `10947c85` | Record FAILING when the rollup fails but no check name comes back | Cosmetic classification; still no failure content |
| `9690ec55` | Align `_FAILING_CONCLUSIONS` with `vcs._FAILURE_STATES` | Vocabulary alignment; no new read |
| `cb77602f` | `[loop] ci_pending_hold_dequeue_enabled` yields the slot on a CI-pending-only PR | Avoids one pending lap; doesn't read a failure or distinguish it from a real red |
| `55cb1706` | `bind_ci_failing_verdict_to_red_read` downgrades a `CI_FAILING` verdict lacking a gate run | Punitive; running the local subset satisfies it without reproducing the CI-only red |

Eight fixes, none of which built the one missing capability: *read the failing check's actual output*. Steps 7–9 below retire the entries this design subsumes.

## Implementation Plan

### Phase 1 — Build the oracle (read the failing check's content)

1. [x] **The CI-diagnosis oracle.** new `maestro/ci_diagnosis.py` — a pure module with a `CIFailure` frozen dataclass (`check_name`, `job_name`, `first_error_lines: tuple[str, ...]`, `details_url`) and `diagnose_ci_failure(rollup, run_log_reader)` that, given the parsed `statusCheckRollup` and an injected `run_log_reader` seam, returns a provenance-stamped `tuple[CIFailure, ...]` for the concretely-failing checks. `run_log_reader` is the sole side-effect seam (the red-root `GateRunner` pattern), so the oracle is pure under test. A pure `extract_first_errors(log_text)` finds the first `FAILED` / `error:` / assertion lines and bounds them.
   - *Depends on:* none.
   - *Acceptance:* `diagnose_ci_failure` returns the concrete first-error lines for a failing check given a stub reader, and `()` for an all-green rollup, with no real I/O.

2. [x] **The bounded, redacted log-reader seam.** `maestro/vcs.py` (`read_run_log_failed`) — a best-effort `CodeHost` method that extracts each failing check-run's `detailsUrl` from the rollup already fetched, resolves the run id, runs `gh run view <id> --log-failed`, and returns the output truncated to a hard char cap with tokens / `Bearer ` headers / Telegram bot tokens redacted before return. A fetch failure returns `""` (best-effort, fail-soft), never raises into the preflight. This method is the injected `run_log_reader` seam from step 1 — the *sole* owner of the raw `gh run view --log-failed` invocation and the `detailsUrl` resolution that drives it — deliberately kept in `vcs.py` rather than the oracle so `maestro/ci_diagnosis.py` stays pure under test; step 6's chokepoint exempts it by name, exactly as the merge-truth chokepoint tolerates its underlying `gh` runner.
   - *Depends on:* 1.
   - *Acceptance:* `read_run_log_failed` returns a redacted, length-capped string on a stubbed `gh` success and `""` on a stubbed `gh` failure, asserted without a real subprocess.

3. [x] **Wire the oracle into red-root, behind a default-off flag.** `maestro/agent.py` (`_observe_pr_check_runs`, `_record_red_root_diagnosis`), `maestro/config.py` / `maestro/toml_config.py` / `maestro.toml` (`[loop] ci_failure_diagnosis_enabled`, default `false`). When the flag is on and CI is FAILING, the red-root artifact and journal realization carry the oracle's concrete `CIFailure` lines instead of the "look to pytest" note. Flag-off is byte-identical to today.
   - *Depends on:* 1, 2.
   - *Acceptance:* with the flag on, `red-root.md` for a FAILING PR contains the stubbed failing-test line and not the "look elsewhere" fallback; with the flag off, output is byte-identical to the current diagnosis.

4. [x] **Thread the failure into the remediation prompt.** `maestro/loop/pr_remediation.py` (`_compose_pr_remediation_prompt`) — under the same flag, a `## CI Failure` section carries the bounded `CIFailure` excerpt so the model sees the concrete red at prompt-build time (the 2000-char bash cap makes in-session `gh run view` unusable). The "read CI state with `gh pr checks`" clause stays as the confirmation path.
   - *Depends on:* 3.
   - *Acceptance:* a FAILING-CI remediation prompt built with the flag on contains the injected failing-test line; flag-off omits the section entirely.

### Phase 2 — Consolidate the four readers behind one door

5. [x] **Route the rollup readers through the oracle.** Move the raw `statusCheckRollup` item-parse out of `maestro/vcs.py` (`get_pr_check_conclusion`, `get_pr_failing_check_names`) into `maestro/ci_diagnosis.py`, so the oracle becomes the sole parser of rollup *classification content* — the `conclusion` / `state` / `name` read the failure verdict derives from, the invariant step 6 pins; the `detailsUrl` read that drives the fetch seam stays in `maestro/vcs.py` (`read_run_log_failed`, step 2) and is the one carve-out. The two `vcs.py` classification methods keep their signatures and delegate. The two downstream consumers — `maestro/pr_readiness.py` (`ci_state_from_conclusion`) and `maestro/red_root.py` (`classify_ci_conclusions`) — drop their private failure-vocabulary copies (`_FAILURE_STATES` / `_FAILING_CONCLUSIONS`) and import the single membership classifier the oracle owns, so "is CI red / which checks / why" has one provenance-stamped owner. Behaviour is preserved; the duplication is removed. **Import-cycle care:** `CheckRun` is defined in `maestro/red_root.py`, so relocate its definition (and the shared classifier) into `maestro/ci_diagnosis.py` and have `red_root` import them back — otherwise routing `red_root`'s classifier through `ci_diagnosis` forms a `red_root → ci_diagnosis → red_root` cycle.
   - *Depends on:* 1.
   - *Acceptance:* the raw rollup item-parse lives only in `maestro/ci_diagnosis.py`; the four call sites import the shared classifier and no longer re-implement the failure-state membership test; existing `ci_state` / conclusion tests pass unchanged, with no new import cycle.

6. [x] **AST-golden chokepoint test.** new `tests/test_ci_truth_chokepoint.py` — the analogue of `tests/test_merge_truth_chokepoint.py`: a whole-`maestro/` AST scan asserting the oracle in `maestro/ci_diagnosis.py` is the sole owner of `statusCheckRollup` *classification* (the `conclusion` / `state` / `name` parse) and failing-check-log *error-extraction*, so no future site re-introduces a blind rollup read. The single exemption is the `maestro/vcs.py` (`read_run_log_failed`) fetch seam from step 2, which owns the raw `gh run view --log-failed` invocation and the `detailsUrl` resolution — the same carve-out the merge-truth chokepoint makes for its underlying `gh` runner.
   - *Depends on:* 5.
   - *Acceptance:* the test fails if a second module classifies `statusCheckRollup` items or extracts errors from a check log outside the oracle, passes on the consolidated tree, and does *not* flag the exempt `maestro/vcs.py` (`read_run_log_failed`) fetch seam that owns the raw `gh run view --log-failed` invocation.

7. [x] **Evidence-gated default flip.** `maestro/config.py` / `maestro/toml_config.py` / `maestro.toml` — flip `[loop] ci_failure_diagnosis_enabled` to `true` once Phase 1 has run in production and the falsifiability metric below improves.
   - *Depends on:* 3, 4, 6.
   - *Acceptance:* the flag defaults `true` and the CI-diagnosis wiring runs by default; a revert to `false` restores current behaviour.

### Phase 3 — Retire the subsumed moles (only after the replacement proves out)

8. [x] **Retire the "look elsewhere" fallback and the unnamed-FAILING synthesis.** `maestro/agent.py` (`_record_red_root_diagnosis`, `_observe_pr_check_runs`) — with the oracle supplying real check-runs and their logs, delete the "look to pytest, a CI-specific check" all-passed blocker text and the synthetic unnamed `CheckRun` fallback; the oracle names and explains every failing check.
   - *Depends on:* 7.
   - *Acceptance:* no code path emits the "look to pytest" string; a FAILING PR with a named check always records that check's concrete error.

9. [x] **Narrow the punitive red-read bind to a backstop.** `maestro/outcome_grounding.py` (`bind_ci_failing_verdict_to_red_read`, `_OPEN_THE_RED_GATE_DIRECTIVE`) — once the loop supplies the failure content, the bind's role shrinks from "the only pressure on a blind lap" to a narrow verdict-honesty backstop; scope it to the case where the oracle *did* surface a failure the lap ignored, so which local gates ran is irrelevant: it no longer fires on a lap that pushed a fix without running a local gate, and still downgrades a lap that ran the wrong subset without pushing a fix.
   - *Depends on:* 7.
   - *Acceptance:* the bind no longer downgrades a lap that edited-and-pushed a fix for the oracle-surfaced failure, and still downgrades a lap that ignored a surfaced red.

## Testing Strategy

- **Oracle purity (`tests/test_ci_diagnosis.py`).** Inject a stub `run_log_reader`; assert `diagnose_ci_failure` returns the concrete first-error lines for a failing rollup, `()` for all-green, and drops empty-name checks from the named surface. Assert `extract_first_errors` picks `FAILED …` / `error:` / assertion lines and bounds output. No real network, subprocess, or `sleep`.
- **Log-reader seam (`tests/test_vcs.py`).** Mock the `gh` runner; assert `read_run_log_failed` truncates to the cap, redacts token / `Bearer` / Telegram-bot patterns, and returns `""` on a `VCSError` without raising.
- **Red-root + prompt wiring (`tests/test_agent.py`, `tests/test_remediation_prompt.py`, `tests/test_loop_pr_remediation.py`).** With `mem_fs` and a fake `CodeHost`: flag-on writes the concrete failing line into `red-root.md`, the journal realization, and the `## CI Failure` prompt section; flag-off is byte-identical to today (guards the rollback path).
- **Config flag (`tests/test_config.py`).** Assert `[loop] ci_failure_diagnosis_enabled` parses and round-trips through the `maestro.{env}.toml` overlay, defaulting `false` (Phase 1) and `true` after the Phase-3 flip — the same three-file flag coverage 087/089 shipped.
- **Chokepoint enforcement (`tests/test_ci_truth_chokepoint.py`).** AST scan proves the oracle is the sole owner of rollup *classification* and log *error-extraction* (the `read_run_log_failed` fetch seam exempted); seed a second reader in a fixture module and assert the test fails, and assert the exempt seam does not trip it.
- **Bind narrowing (`tests/test_outcome_grounding.py`).** Assert the narrowed `bind_ci_failing_verdict_to_red_read` leaves an edited-and-pushed lap's verdict intact and still downgrades an ignored-red lap.

All externals mocked; all file I/O via `mem_fs`; per-test runtime under the 5 s unit gate.

## Risk Assessment

- **Extra `gh` calls per red lap.** The oracle adds one `gh run view --log-failed` per failing check on a CI-red lap only. Bounded by the number of failing checks, fail-soft to `""`, zero SDK cost, and gated behind `[loop] ci_failure_diagnosis_enabled` (default-off) so rollout is staged and reversible by a single flag.
- **Log parsing drift.** GitHub log format could change; `extract_first_errors` is best-effort and degrades to an empty excerpt (the artifact then reads "CI FAILING, log unavailable" — honest, never a false concrete claim). No behaviour depends on a successful parse.
- **Secret leakage into a public artifact / PR body.** The log excerpt is redacted (tokens, `Bearer` headers, `(?:bot)?\d+:[A-Za-z0-9_-]{30,}` Telegram tokens) and char-capped *before* it leaves `read_run_log_failed`, tested explicitly, so a raw CI log never reaches the journal, artifact, or prompt.
- **Consolidation regressing an existing verdict.** Phase 2 preserves each reader's current verdict (the shared classifier is the same membership test); the existing `ci_state` / conclusion tests are the regression guard, and Phase 2 lands before the default flip.
- **Rollback posture.** Every behaviour change is behind the default-off flag; Phases 1–2 are independently mergeable and leave the loop working if interrupted; Phase 3 (mole retirement) lands only after the flag is on and the metric has moved, so a revert restores the prior mechanism intact.
- **CLAUDE.md budget.** The CI-truth chokepoint parallels the merge-truth chokepoint, which carries one CLAUDE.md invariant line. This proposal intends exactly one such line (added in Phase 2 alongside `tests/test_ci_truth_chokepoint.py`); with only a few words of headroom under `test_claude_md_brevity`'s word cap, that line is budgeted by an offsetting tightening of the existing merge-truth-chokepoint bullet — no net word growth — which is why CLAUDE.md is in the Affects row.

## Falsifiability

If the design is right, of the `CI_FAILING` laps that fire red-root, the fraction whose artifact carries a concrete failing-test / error line rises from ~0 % (today: the sole live artifact says "look elsewhere") toward near-100 %, and the number of CI-blocker laps per PR lineage before it reaches green falls (measured against the `task_history.jsonl` remediation-lap count that produced the 25/85 figure). The design has failed if, with the flag on, red-root artifacts still say "look to pytest, a CI-specific check" or the CI-blocker laps-per-lineage count does not drop.

## Out of scope

- **`CI_PENDING` re-lapping (running vs. failing).** The 22 pending laps are a related but distinct facet — the loop lapping while CI is merely *running*. `[loop] ci_pending_hold_dequeue_enabled` already holds a pending-only PR; a cheaper running/failing split is left to that gate. The oracle sharpens the FAILING side only.
- **Local reproduction of browser / `maestro_sdk` / `maestro_review` failures.** The oracle reads the *remote* failure (authoritative regardless of which job failed); it does not add playwright or the sibling package suites to the loop container. Editing such a fix still leans on the remote log plus human review at merge.
- **External (non-own) PRs.** The oracle fires only on own `maestro/evolve/*` remediation laps, matching the red-root scope; foreign-PR CI is not this loop's obligation.
