# 124: Attributed Gate Observation — One Structured, Diff-Attributed, Freshly Re-Read Gate Fact Every Terminal Decision Binds To, Retiring the Tree-Dirtiness and Gate-Label Progress Proxies

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:observing |
| **Affects** | new `maestro/gate_observation.py`, `maestro/evolution.py`, `maestro/gate_convergence.py`, `maestro/agent.py`, `maestro/memory.py`, `maestro/red_root.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro/flag_lifecycle.py`, `maestro.toml`, `tests/structural_guards.py`, tests |
| **What landed** | Phase A, steps 1–4 plus step 5's producer half — the structured fact, its producer, and the attributor, all inert. Step 1: `maestro/gate_observation.py` owns the local-gate failure vocabulary — the `GateNonAnswer` / `GateObservationKind` / `GateTool` `Literal` aliases, frozen `GateFailure` (with `identity()`), frozen `GateObservation` (with `signature()` at failure-identity granularity, non-empty for any non-`CLEAN` gate via the synthetic `<tool>:<kind>` identity, and a `truncated` flag stamping an enumeration the producer already knows is short — the `_MAX_FAILURES` cap, or a `-x` probe whose enumerate re-run did not answer), frozen `GateRunObservation` (with `signature()` / `all_clean()`), and the pure zero-I/O `combined_output` / `parse_gate_output` / `parse_non_answer` / `truncated_by_first_failure`, covered by `tests/test_gate_observation.py`. Step 2: `EvolutionEngine._observe_gate` (`maestro/evolution.py`) re-runs an `-x`-truncated pytest probe once with `-x` filtered out of the probe's own argv, so a coverage-only red and every green gate still run exactly once. Step 3: `EvolutionEngine._run_gate_command` splits a finished run from a named non-answer, returning an `isinstance`-discriminated `CompletedProcess \| GateObservation`, with `TimeoutExpired` → `kind=TIMEOUT` (salvaging the partial streams through `_decoded_partial_stream`) and `OSError` → `kind=UNAVAILABLE`. Step 4: `QualityGateResult.observation` (trailing, defaulted `None`) carries a `GateRunObservation` behind default-off `[loop] gate_observation_enabled` (`maestro/config.py`, `maestro/config_settings.py`, `maestro.toml`), registered in `EXPERIMENTAL_FLAGS` (`maestro/flag_lifecycle.py`) with `review_by = 2027-09-30` and `gate=None`; flag-off the argv, `raw_output` and the three booleans are byte-identical, pinned by `test_run_quality_gates_flag_off_is_byte_identical` (`tests/test_evolution.py`) alongside `tests/test_config.py`'s default/overlay/registration coverage. Step 5 **producer half**: the closed `Attribution` vocabulary and the pure `attribute(observation, changed_paths, baseline)` in `maestro/gate_observation.py`, plus the `EvolutionEngine._observe_baseline(sha)` seam (`maestro/evolution.py`) — a process-lifetime map bounded by `_MAX_GATE_BASELINES`, banked only from a clean tree whose `HEAD` already equals `sha`, never checking anything out — captured off the event loop at the two sites where that holds: `tend_evolution_prs` (the reconciled `origin/main` head, and only on a lap that has an open evolution PR to attribute) and `implement` (the plan's own `base_hash`). Allowlisted by name in `_GATE_ACCEPTANCE_GUARD` (`tests/structural_guards.py`); the verdict matrix and the seam are covered by `tests/test_gate_observation.py`, `tests/test_evolution_impl.py` and `tests/test_evolution_tend.py`. Step 6 **terminal binding**: `EvolutionEngine._convergence_terminal` (`maestro/evolution.py`) is now the sole constructor of `ConvergenceResult` in `converge_to_green`, re-reading the gate on the `not fixed` path so a branch a fix round turned green is no longer demoted on a stale red, classifying the (re-read) observation via `attribute` against `plan.files_affected` and the `_observe_baseline` cache, recording the verdict on the new `ConvergenceResult.attribution` field, and appending one `gate_convergence_terminal` activation row per exit; flag-off it is byte-identical (no re-read, no attribution, no row). The `EXPERIMENTAL_FLAGS` entry's `gate` is now `gate_convergence_terminal` (`maestro/flag_lifecycle.py`), and `_convergence_terminal` is allowlisted in `_GATE_ACCEPTANCE_GUARD`. Covered by `tests/test_agent.py` and `tests/test_gate_convergence.py`. Step 7 **progress at failure granularity**: `plan_next_round` (`maestro/gate_convergence.py`) now takes a keyword-only `all_clean: bool` and binds `"green"` to it — every observed gate CLEAN — rather than to an empty `curr_failures` set (a second, independent lock), and its `curr_failures`/`prev_failures` carry *failure-identity* signatures (one identity per failing test/diagnostic) when the caller supplies them, so a fix round clearing eleven of twelve failing tests is a strict subset — real progress — instead of an unchanged single-label set, and the 20-round `gate_convergence_ceiling` becomes the real budget it advertises. `EvolutionEngine._progress_signals(review)` (`maestro/evolution.py`) is the single seam picking the loop's progress vocabulary from the flag: flag-on `(observation.signature(), observation.all_clean())`, flag-off the byte-identical pre-124 `(review.failure_signature(), review.passed)`. Covered by `tests/test_gate_convergence.py` and `tests/test_agent.py`. Step 8 **the same treatment for the review loop**: `plan_next_review_round` (`maestro/gate_convergence.py`) now takes keyword-only `gate_clean` / `verify_grace_remaining` and, ranked below the approval and ceiling rules, returns `"continue"` for a round that neither approved nor committed while every observed gate is `CLEAN` and the bounded grace is unspent — so a verification-only round is no longer demoted on an undirtied tree. `EvolutionEngine._review_terminal` (`maestro/evolution.py`) is the review loop's sole terminal owner and the sibling of `_convergence_terminal`: flag-off a pure pass-through (`gate_clean=False`, `verify_grace_remaining=0`) that issues no gate run at all, flag-on re-reading the gate through `_progress_signals` **only** on the would-otherwise-be-stuck path, returning the decision plus the grace it consumed and appending one `self_review_terminal` activation row per exit; `self_review` hands it to `asyncio.to_thread` and bounds the grace at `_VERIFY_ONLY_REVIEW_ROUND_BUDGET` per call, flagging the grace-bought next round's prompt as running against a just-measured-green branch. `self_review` also counts a reviewer whose commit advanced `HEAD` past the round start as progress (flag-on), since the action-gate-scoped `Bash` can leave the working tree clean after a commit. Covered by `tests/test_gate_convergence.py`, `tests/test_evolution_impl.py` and `tests/test_agent.py`. Step 9 **an inherited red never spends a human approval**: on an `INHERITED` convergence terminal `implement_plan` (`maestro/agent.py`) no longer demotes or consumes an attempt while the hold budget is unspent — it `cleanup_branch`es as before, stamps the two new defaulted `EvolutionPlan` fields `inherited_hold_main_sha` (the `origin/main` head the base red was measured against) and `inherited_hold_count` through the new `Memory.update_plan_inherited_hold` (`maestro/memory.py`), journals an `evolution_note`, and fills an *empty* `next_task_directive` with the bounded `_INHERITED_BASE_REPAIR_DIRECTIVE` naming the inherited failure identities rendered by the new `gate_observation.render_identities`; `AutonomousAgent._plan_on_inherited_hold` skips such a plan in `_implement_approved_plans` while `origin/main` still equals the stamped head (mirroring `_in_conflict_cooldown`'s both-SHAs-truthy-and-equal guard, so an unmeasurable base never fires the skip), and once the count reaches `max_implementation_attempts` the next `INHERITED` terminal demotes as today with the rendered identities as `last_failure_reason`. `ATTRIBUTED` / `UNATTRIBUTABLE` and the flag-off `None` keep today's demote byte-for-byte. Covered by `tests/test_agent.py`, `tests/test_gate_observation.py`, `tests/test_memory.py` and `tests/test_memory_plans.py`. Step 10 **an inherited red never destroys work**: the three destructive arms now branch on `AutonomousAgent._attribute_owned_gate_red(review)` (`maestro/agent.py`) — the one shared reader that keys `attribute` off `merge_base(HEAD, "origin/main")`, `changed_paths(base, head)`, and the base-commit `_observe_baseline(base)` cache, via the two new best-effort `SubprocessVCS.merge_base` / `changed_paths` seams (`maestro/vcs.py`, the diff bounded by `_CHANGED_PATHS_MAX`) mirrored on `MockVCS` (`tests/component/mock_vcs.py`). On `INHERITED` `_land_uncommitted_owned_edit` skips `discard_changes()`, `_push_committed_unpushed_owned_head` skips `reset_hard(origin/<branch>)` (still returning `True`), and `_tend_single_pr`'s post-merge arm holds the clean merge (no `reset_hard(pre_merge_head)`) while still routing a `quality_gates_routed` remediation whose summary names the base identities via `render_identities` rather than the coarse two-word `_summarize_failed_gates` label; each records a `partial` carrying the new `Marker.INHERITED_RED` (`maestro/pr_remediation_state.py`), registered in `_NULL_LAP_MARKERS` (`maestro/remediation_tracker.py`) so such a lap still classifies `NULL`. `ATTRIBUTED` / `UNATTRIBUTABLE` and the flag-off `None` keep today's rewind and summary byte-for-byte. Covered by `tests/test_evolution_uncommitted_landing.py`, `tests/test_committed_unpushed_push.py`, `tests/test_evolution_tend.py` and `tests/test_vcs.py`. Step 11 **the fixer gets a diagnosis**: the new static `EvolutionEngine._auto_fix_diagnosis(review, attribution, cap)` (`maestro/evolution.py`) is the seam selecting the auto-fix prompt's diagnosis section — no observation (flag-off, or any caller that attached none) returns `review.raw_output[-cap:]` verbatim so the prompt stays byte-identical, otherwise it renders `gate_observation.render_failure_report` (bounded by `_AUTO_FIX_FAILURE_ROW_LIMIT` rows and the same `gate_output_cap` chars, so the long-context seam keeps its meaning) followed by an `Attribution: {verdict}` line and, on `INHERITED`, one sentence telling the fixer the failures are the base's and not the plan's to fix; `_run_auto_fix_query` takes the verdict through a new keyword-only `attribution` argument fed by `_attribute_review`, the single attribution reader `converge_to_green` and `_convergence_terminal` share so the fix rounds and the terminal cannot drift on *how* a red is classified. `implement_plan`'s non-inherited stalled-convergence demote (`maestro/agent.py`) composes `last_failure_reason` from `gate_observation.render_identities` under `_GATE_RECORD_IDENTITY_LIMIT` / `_GATE_RECORD_IDENTITY_CHARS` — the same bounds the inherited-demote sibling and the two record-time landing arms use — falling back to today's `raw_output[-500:]` tail only when no observation is present, so the failure corpus feeding rejection learning stops recording mid-word slices. Covered by `tests/test_evolution_tend.py` and `tests/test_gate_observation.py`. Step 12 **structural enforcement**: two guards in the house `ChokepointGuard` shape land in `tests/structural_guards.py` — `gate_observation` pins `maestro/gate_observation.py` as the sole owner of the local gate-failure vocabulary (`GateFailure` / `GateObservation` / `GateRunObservation` / `parse_gate_output` / `parse_non_answer` / `attribute`), so no rival `def attribute` or `GateFailure` anywhere under `maestro/` can fork the attributor the three destructive hold arms read; `gate_terminal` confines every `ConvergenceResult` construction to `EvolutionEngine._convergence_terminal` (allowlist `("evolution.py", "_convergence_terminal")` under the default `line_to_function` resolver), so the stale-observation demote is unrepresentable rather than merely discouraged. The registry population cap bumps 22→24 (`_MAX_STRUCTURAL_GUARDS`), both guards run regardless of the `gate_observation_enabled` flag, and `tests/test_structural_guards.py` adds a per-guard detector/negative-control test plus two non-vacuity floors (the live tree still constructs `ConvergenceResult` under `_convergence_terminal`, and the owner module still binds all six vocabulary names). One-line invariant records added to `CLAUDE.md` and `docs/modules.md`. |
| **What is missing** | Steps 13 to 14 — the retire/collapse phase. Step 3's non-answer naming reaches the *observation* only: `run_quality_gates`' `raw_output` report still carries the `"could not run"` label (deliberately, for the byte-identical flag-off guarantee) and the non-answer rows carry salvaged partial output rather than the elapsed budget or errno, so no consumer is yet free of that string. Phase C's retirement is untouched: `maestro/red_root.py` still carries `classify_mypy_provisioning_signature` / `_is_maestro_sdk_provisioning_error`, and `QualityGateResult.failure_signature` and `_summarize_failed_gates` still exist. |
| **Depends on** | 110 _(shipped)_, 087 _(shipped)_, 120 _(burn-in:ready)_ |
| **Derived from** | live production data via `/propose`, 2026-09-03 |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

The implementation lineage makes terminal, irreversible decisions — demote an
approved plan to `failed`, delete its branch, `discard_changes` a working tree,
`reset_hard` a committed head, mint a remediation task — from **proxy** and
**stale** observations of the quality gate rather than from the gate itself.
`auto_fix` reports success as "is the working tree dirty?"; `plan_next_round`
measures progress as "did the three-element failing-*label* set shrink?"; the
fixer's diagnosis is a 1000-character front-truncated tail of a `-x`-aborted
pytest run; and nothing anywhere asks the one question that decides whether a
red gate is the plan's fault at all — *did my diff cause this, or did I inherit
it?*

On 2026-09-03 this cost a plan that was already fixed. The gate went red because
`maestro_sdk` was not installed in the loop container, aborting collection of an
unrelated test file. The fix step diagnosed it correctly, ran `pip install`,
verified every gate green, and edited no file — so `has_changes()` returned
`False`, `converge_to_green` read that as "no progress is possible", returned
`stuck` carrying the *round-zero* red review it had never re-read, and the loop
demoted the plan and deleted the branch. The recorded `last_failure_reason` is a
diagnosis that had already stopped being true four minutes earlier. The same
mechanism has demoted nine distinct plans fifteen times in eighteen days, and
the last three things Maestro merged were all patches to this signal.

This proposal gives the gate one structured fact and makes every terminal bind
to a fresh read of it. A new `maestro/gate_observation.py` becomes the sole
owner of the gate-failure vocabulary — the local-gate sibling of the CI-truth
owner `maestro/ci_diagnosis.py`, which already does exactly this for remote
check logs. It parses the runner's output into an enumerated
`tuple[GateFailure, ...]`, gives progress a *failure*-granular signature so a
twelve-to-one improvement counts as progress, and attributes each failure
against the plan's diff and a cached base-branch baseline so an inherited
breakage routes to base repair instead of consuming a human approval. Every
`ConvergenceResult` is then constructed in one private terminal helper that
observes the gate itself, so a stale verdict becomes unrepresentable rather
than merely discouraged.

The end state: a plan fails only when *its own diff* is red, and only after the
gate has been read at the moment of the verdict. The ceiling of 20 rounds
becomes a real budget instead of a decorative one, the destructive rewind arms
stop firing on failures the branch did not cause, and the failure corpus that
feeds rejection learning stops recording diagnoses that were false when written.

## Relevant Findings

- `maestro/evolution.py` (`_run_auto_fix_query`) — returns `self._vcs.has_changes()`
  as its success signal. Any fix that repairs the *environment* rather than the
  tree (a `pip install`, a cache purge, a re-provisioned dependency) reports
  `False`. The action gate (`action_gate_hooks`) scopes the fix agent's `Bash` to
  the plan's own evolve branch, so an agent that commits its own fix also leaves a
  clean tree and also reports `False`.
- `maestro/evolution.py` (`converge_to_green`) — on `not fixed` it returns
  `ConvergenceResult("stuck", review, ...)` where `review` is the observation
  taken *before* the fix round. The gate is never re-run on that path, so a
  branch that is green at the moment of the verdict is demoted on a stale red.
- `maestro/gate_convergence.py` (`plan_next_round`) and `maestro/evolution.py`
  (`QualityGateResult.failure_signature`) — progress is `curr_failures <
  prev_failures` over a frozenset drawn from exactly three labels
  (`ruff` / `mypy` / `tests`). When one gate is red — the dominant case — the set
  can never *strictly* shrink without going fully green, so `plan_next_round`
  returns `"stuck"` on round 2 regardless of the `gate_convergence_ceiling` of 20.
- `maestro/evolution.py` (`run_quality_gates`) — runs pytest with `-x`, so the
  first failure or collection error aborts the suite and only ever one failure is
  visible; keeps `output[-1000:]` per gate, a front-truncated tail that routinely
  begins mid-word and carries deprecation warnings instead of the assertion; and
  collapses `TimeoutExpired`/`OSError` to the string `"could not run"`, which the
  fixer receives as its entire diagnosis.
- `maestro/evolution.py` (`QualityGateResult`) — carries `passed`, three booleans
  and one opaque `raw_output` string. There is no representation of *which*
  failures occurred, so no consumer can compare failure sets, attribute a failure
  to a file, or distinguish a timeout from an assertion.
- `maestro/ci_diagnosis.py` (`CIFailure`, `extract_first_errors`,
  `diagnose_ci_failure`, `render_ci_failure_section`) — the *remote* CI side
  already owns exactly this structure behind an AST-enforced chokepoint. The
  local gate, which is the one that demotes plans and destroys work, has no
  equivalent.
- `maestro/red_root.py` (`classify_mypy_provisioning_signature`,
  `_is_maestro_sdk_provisioning_error`) — the "this red is provisioning, not a
  defect" concept already exists, but hardcoded to one tool (mypy) and one library
  (`maestro_sdk`), and only on the red-root *diagnosis* path. It is unreachable
  from `run_quality_gates`, the reader that actually demotes plans.
- `maestro/agent.py` (`implement_plan`) — composes `last_failure_reason` as
  `f"Quality gate failed:\n{convergence.review.raw_output[-500:]}"` from the same
  stale review and passes it to `_demote_plan_to_failed`, so the failure corpus
  that feeds `learn_from_rejection` and `reflect` records phantom diagnoses.
- `maestro/agent.py` (`_land_uncommitted_owned_edit`, `_push_committed_unpushed_owned_head`)
  — both branch on a bare `run_quality_gates().passed` and, on red, call
  `discard_changes()` (reset + checkout + `git clean -fd`, sweeping untracked new
  files) or `reset_hard(f"origin/{pr_branch}")` (discarding committed work). An
  inherited red destroys work the branch did not break.
- `maestro/evolution.py` (`_tend_single_pr`, `_summarize_failed_gates`) — an
  unattributed post-merge red rewinds the clean main merge and appends to
  `quality_gates_routed`, minting a remediation task whose stated blocker is a
  two-word label (`"tests failed"`).
- `maestro/evolution.py` (`self_review`) and `maestro/gate_convergence.py`
  (`plan_next_review_round`) — the review loop carries the identical VCS proxy:
  `made_changes` stands in for progress, so a review round that verifies rather
  than edits reads as a stall.
- `tests/structural_guards.py` (`_GATE_ACCEPTANCE_GUARD`) — proposal 110 already
  pins `run_quality_gates` as a chokepoint with a named allowlist. The chokepoint
  exists; the *fact it produces* is too thin for its consumers to bind to, so each
  one substitutes a weaker locally-derived proxy.
- `tests/test_agent.py` (`test_converge_to_green_no_change_is_stuck`) — asserts
  `result.review is _FAIL_REVIEW`, pinning the stale-terminal behaviour as
  intended. `tests/test_gate_convergence.py`
  (`test_plan_next_round_no_shrink_equal_is_stuck`) pins the label-granularity
  stall. The only shrink-to-green test
  (`test_converge_to_green_shrinks_to_green`) exercises a two-gate → one-gate
  path that production produced once in twenty-five rounds.

## Production Evidence

Live data at the loop container's bind-mount (`/opt/maestro/data`), read
read-only. Windows are stated per figure.

**The decisive incident.** `logs/maestro.log`, plan `b8e2da8e4c2a` ("Flip
`pre_mint_generation_gate_enabled` default-on (proposal-120 gate cutover-flip
step)"), 2026-09-03:

```
02:29:24  Self-review round 1 (ceiling 20): LGTM
02:30:05  Gate convergence round 1 (ceiling 20) — failing gates: ['tests']
02:30:50  SDK tool result [Bash]: Successfully built maestro_sdk
02:34:46  SDK text: All quality gates are now green:
02:34:52  SDK call: caller=auto_fix ... cost=8.2974 duration_ms=287722 turns=10
02:34:52  Gate convergence: auto_fix made no changes on round 1 — stopping (stuck)
02:34:52  Gate convergence stalled with no progress after 0 round(s) — demoting plan b8e2da8e4c2a
```

The recorded `last_failure_reason` for that plan (`evolution_plans.jsonl`) is the
round-zero read — `"ERROR tests/test_provider_cli_paced.py"` — for a plan whose
`files_affected` are `maestro/config.py`, `maestro.toml`,
`maestro/flag_lifecycle.py`, `tests/test_agent.py`. The failing file is not in
the diff.

**Convergence never gets a second round.** `logs/maestro.log{,.1,.2}`,
2026-08-16 10:48 → 2026-09-03 05:19 (18 days): 25 `Gate convergence round`
lines, **all of them round 1**; 24 of 25 had exactly one failing gate
(`['tests']`). 15 `Gate convergence stalled` demotions across 9 distinct plans —
**11 at `after 0 round(s)`** (the `has_changes()` proxy) and 4 at `after 1
round(s)` (the label-granularity stall). Observed maximum `rounds_used` is 1
against a configured `gate_convergence_ceiling` of 20.

**The failure reasons are unattributed or unusable.** `evolution_plans.jsonl`,
all 7 plans in `failed` status: 2 name `ERROR tests/test_provider_cli_paced.py`
(a collection abort in a file neither diff touched), 2 carry the entire
diagnosis `"Tests: could not run"` (a gate timeout or `OSError` at
`selfcheck_timeout`), 2 name a single assertion each — one in
`tests/component/test_agent.py` for a plan touching only `maestro/cli.py` and
`tests/test_cli.py`, and `FAILED tests/test_cli.py::test_cmd_audit_flags_json_output`
alongside `3177 passed`, a genuinely coupled test demoted after one round — and
1 is the `self_review` `made_changes` stall. Four of the seven carry the
`stopping after 1 failures` banner, so at most one failure was ever visible.
Each of those four is exactly 521 characters — the 20-character
`Quality gate failed:` header, its newline, and `implement_plan`'s
`raw_output[-500:]` slice — so that record-time cap, not the per-gate
`output[-1000:]` tail, is the cut that sets the boundary; three of the four
begin mid-word and the fourth begins mid-path (`workspace/tests/…`). The
per-gate tail is what fills the surviving slice with deprecation warnings and
timing lines instead of the assertion.

**The class blocks its own repair.** Plan `16c8bc2f4db3` — "Guard
`test_provider_cli_paced` collection with `pytest.importorskip` so self-review's
pytest gate stops aborting when `maestro_sdk` is uninstalled" — was approved,
attempted 3 times, and demoted each time with `"Tests: could not run"`. Plan
`0c26cf4995cc` (`maestro gate-check --full`, aimed at the same CI-fidelity gap)
was attempted 3 times and demoted 3 times, twice on the very collection error it
existed to eliminate.

**Cost and yield.** `cost_samples.jsonl`, 2026-08-27 → 2026-09-03 (7 days):
`implement_plan` $— (n=28) + `self_review` $— (n=29) + `auto_fix`
$— (n=12) = **$—, 35% of the window's $— total**. Over
2026-08-20 → 2026-09-03 (14 days) the same three callers total **$— of
$— (22.5%)**. `version_archive.jsonl` records **zero merges since
2026-09-01**, and the last three merges before that — 2026-08-29, 2026-08-30,
2026-09-01 — were "Register the `browser_context_args` pytest marker…",
"Red-root mypy provisioning-signature carve-out…", and "Put `maestro_sdk/src`
on mypy's module-search path…": three consecutive patches to this same signal.

**Downstream cost of the blockage.** The specific plan being blocked is proposal
120's cutover flip. `evolution_plans.jsonl`, 2026-08-26 → 2026-09-03 (9 days):
**64 distinct plans** name `pre_mint_generation_gate` (53 `obsolete`, 9
`rejected`, 2 `failed`, 0 merged); **34** of them are titled as a flip/arm of
that flag. Meanwhile `logs/maestro.log*` records **338 `Admission gate refused
plan … (saturation)` firings** (2026-08-21 → 2026-09-03) — each one a
`reflect` + `propose_plan` pair spent *before* a door that then discards the
result, which is precisely the waste the un-flippable flag would stop.

**A co-occurring blocker this proposal does not cure.** The same log files
carry a second way an implementation attempt ends without a verdict:
`Transient SDK failure during implement_plan` (the resume-in-place path, which
leaves the plan's status untouched and never reaches a convergence terminal).
`logs/maestro.log{,.1}`, 2026-08-24 → 2026-09-03 (the older `maestro.log.2`
carries no such line): **22 events** — 7 `cli_crash`, 15 `stream_stall` —
across **9 distinct plans**, against the 15 convergence demotions across 9
plans above. On the decisive plan they compound: one `cli_crash` preceded the
02:34:52 demotion, and every one of the seven attempts after it — six
`cli_crash`, one `stream_stall` — died before the gate ran. So even with this
proposal's terminal fixed, `b8e2da8e4c2a` would still not have landed on
2026-09-03; the transient-SDK path is measured here so the Falsifiability
section below does not claim more than the observer can deliver, and is named
in *Out of scope*.

## Prior Point Fixes (whack-a-mole ledger)

| # | Identifier | What it patched | Why the class survived |
|---|---|---|---|
| 1 | `a5a52168` (2026-07-19) | Restored the `maestro_sdk` export/`pyproject` contract "to make quality gates green again"; its own message names "a base whose gates cannot go green" as "the disease beneath my treadmill" | Repaired one broken base. The gate still cannot tell a broken base from a broken diff, so the next drift reproduced it |
| 2 | plan `793f285e4efc` / `650a46cc` (merged 2026-08-16) | Fixed env-dependent evolve-cycle component tests "blocking the quality gate" | Deleted one environment-sensitive test; left the attribution gap that made it fatal |
| 3 | Proposal 110 (shipped, PRs #3349–#3393) | Replaced the fixed `auto_fix_max_rounds` terminal with `plan_next_round` + a ceiling of 20 | The new progress predicate reads a 3-label set and the terminal reads an actuator return, so the cap became *invisible* rather than removed — measured budget is still 1 round |
| 4 | plan `29581f6fdde4` (merged 2026-08-29) | Registered the `browser_context_args` pytest marker to remove a warning from gate output | Cleaned one noise source inside the truncated tail; the tail is still the diagnosis |
| 5 | plan `1420c3a39f2a` (merged 2026-08-30) | `red_root` mypy provisioning-signature carve-out — "don't record a phantom-RED as a real one" | Right concept, bound to one tool and one library, and installed on the diagnosis path only — unreachable from the reader that demotes plans |
| 6 | plan `96d886a6516e` (merged 2026-09-01) | Put `maestro_sdk/src` on mypy's module-search path so self-review stops reporting phantom errors | Third provisioning-specific patch in four days |
| 7 | plan `16c8bc2f4db3` (failed ×3, 2026-09-02) | Proposed `pytest.importorskip` on the aborting collection | Never landed — the broken gate demoted the fix to the broken gate |
| 8 | plan `0c26cf4995cc` (failed ×3, 2026-09-01→02) | Proposed `maestro gate-check --full` for CI-faithful local gates | Never landed — died twice on the collection error it existed to remove |

Entries 1, 2, 4, 5 and 6 are the same mole struck five times: an inherited red
that the gate reports as the plan's fault. Step 12 retires entry 5's mechanism
outright by generalizing it; entries 1, 2, 4 and 6 are subsumed — the design
makes their recurrence non-fatal rather than requiring each new instance to be
hand-patched. Entry 3's `failure_signature` and its two pinning tests are
replaced in Steps 6 and 13.

## Implementation Plan

### Phase A — one structured, attributed fact

1. [x] **The gate-observation vocabulary.** new `maestro/gate_observation.py` —
   frozen `GateFailure` (`tool`, `kind`, `location`, `identifier`, `message`),
   frozen `GateObservation` (per-gate status, `failures: tuple[GateFailure, ...]`,
   `kind` covering `CLEAN` / `FAILED` / `COLLECTION_ERROR` / `COVERAGE` /
   `TIMEOUT` / `UNAVAILABLE`), the pure parser
   `parse_gate_output(tool, stdout, stderr, returncode)`, and
   `GateObservation.signature() -> frozenset[str]` at *failure* identity
   granularity. A red gate can never yield an empty signature: a non-`CLEAN`
   gate whose parsed `failures` is empty — a `TIMEOUT`, an `UNAVAILABLE`, a
   coverage shortfall (pytest-cov's `FAIL Required test coverage of N% not
   reached` emits no `FAILED` line), or unparseable output — contributes one
   synthetic identity `<tool>:<kind>` (e.g. `tests:TIMEOUT`), so
   `signature()` is empty iff every gate is `CLEAN`. Pure, zero-I/O, zero-SDK,
   imported by nothing yet — the module is dark until Step 4.
   - *Depends on:* none.
   - *Acceptance:* `parse_gate_output` turns a captured pytest run reporting three
     `FAILED <nodeid>` lines into three `GateFailure` rows carrying those nodeids,
     a collection `ERROR <path>` into one row with `kind == COLLECTION_ERROR`, and
     a run whose only red signal is the coverage-threshold line into one row with
     `kind == COVERAGE`; a `TIMEOUT` observation's `signature()` is the
     non-empty `{"tests:TIMEOUT"}`.

2. [x] **Enumerate instead of aborting blind.** `maestro/evolution.py`
   (`run_quality_gates`) — behind the new flag, keep `-x` as the fast probe and,
   only when it returns red, re-run the suite once without `-x` so the observation
   carries the *whole* failing set. Enumeration is load-bearing, not a limit
   bump: `signature()` and `progress_against` are undefined over a set the runner
   refused to finish computing.
   - *Depends on:* 1.
   - *Acceptance:* a red pytest gate with four failing tests yields a
     `GateObservation` with four `GateFailure` rows; a green gate runs exactly once.

3. [x] **Name the non-answers.** `maestro/evolution.py` (`run_quality_gates`) —
   `TimeoutExpired` becomes `kind=TIMEOUT` carrying the elapsed budget and
   `OSError` becomes `kind=UNAVAILABLE` carrying the errno, replacing the
   `"could not run"` string. Retain the full captured output on the observation
   for parsing; the 1000-char tail stops being the carrier of meaning.
   - *Depends on:* 1.
   - *Acceptance:* a gate that exceeds `selfcheck_timeout` produces an observation
     whose `kind` is `TIMEOUT`, whose `failures` is empty, and whose
     `signature()` is `{"tests:TIMEOUT"}` (never empty, so it can never read as
     green downstream), and no consumer receives the literal string
     `"could not run"`.

4. [x] **Produce the observation alongside today's result.** `maestro/evolution.py`
   (`run_quality_gates`, `QualityGateResult`), `maestro/config.py`,
   `maestro/toml_config.py`, `maestro/flag_lifecycle.py`, `maestro.toml` — attach
   the `GateObservation` to the existing `QualityGateResult` (a new
   `observation: GateObservation | None` field, `None` flag-off) behind
   default-off `[loop] gate_observation_enabled`, registered as an
   `ExperimentalFlag` in `EXPERIMENTAL_FLAGS` with a dated `review_by`, a
   `note`, and `gate=None` until Step 6 wires the activation row, at which point
   the entry's `gate` becomes `"gate_convergence_terminal"` so `maestro
   audit-flags` counts it. Every existing field keeps its meaning, so no consumer
   changes shape and flag-off is byte-identical.
   - *Depends on:* 1, 2, 3.
   - *Acceptance:* with the flag off, `run_quality_gates` issues the same
     subprocess commands and returns the same `raw_output` as today, and the
     observation attribute is `None`.

5. [x] **Attribute the red against the diff and the base.** *(producer half only — `attribute()` and `_observe_baseline` exist and are tested; no consumer calls either yet, so the verdict is still inert.)*
   `maestro/gate_observation.py`
   — `attribute(observation, changed_paths, baseline) -> Attribution` returning
   `ATTRIBUTED` (a failure's location intersects the plan's changed paths),
   `INHERITED` (the identical failure identity appears in the base-branch
   baseline), or `UNATTRIBUTABLE`; a `baseline` of `None` can only yield
   `ATTRIBUTED` or `UNATTRIBUTABLE`. `ATTRIBUTED` outranks `INHERITED`: a failure
   in a file the diff touched is the diff's to answer for even when the base
   carried it too. Two things withhold `INHERITED`, and both read the
   **observation** side only. Step 1's synthetic `<tool>:<kind>` identities never
   take part in matching — a red this owner could not parse, or a gate that never
   answered, might be anything, including a genuine regression. And an
   observation whose enumeration is *known short* — the `_MAX_FAILURES` cap, or a
   `-x` probe whose `-x`-less re-run did not itself answer, both stamped
   `GateObservation.truncated` — is `UNATTRIBUTABLE` too, because the failures
   past the cut were never compared to the base at all and excusing them would be
   the exact false excuse this owner exists to refuse. The **baseline** side is
   deliberately not symmetric: a baseline that timed out or capped has a
   *smaller* signature, which makes the subset test harder rather than easier, so
   whatever rows it did salvage are honest evidence that the base already carried
   those identities and are matched as such. The baseline is produced by one new
   `EvolutionEngine._observe_baseline(sha)` seam that calls `run_quality_gates`
   **only while the checked-out `HEAD` is `sha` and the working tree is clean** —
   it never checks anything out itself, and a dirty tree is not that commit — from
   two capture sites where the `HEAD` condition already holds: `tend_evolution_prs`
   after it has healed `main` to `origin/main` and enumerated the open PRs, before
   it touches any PR branch (one run per new `origin/main` head, and only on a lap
   that has an open evolution PR to attribute, so a quiet lap of the top-priority
   mechanical phase pays nothing), and `implement` right after it reads `base_hash`
   and before the implementation session, on a cache miss only (a fresh-from-`main`
   branch normally finds the tend lap's entry; a `source_task_id` base is the case
   that pays). Both sites hand the sync seam to `asyncio.to_thread`, since a whole
   gate sweep would otherwise stall the loop for minutes. Baselines live in a small
   in-memory map on the engine keyed by SHA, bounded to the last four entries and
   lasting the process's life — the clean-tree condition is what makes the SHA a
   sound key for the *code* half, while the environment half is pinned only until
   a restart, which costs at most one re-run per base, never a wrong answer. Consumers key the
   lookup by the base they actually built on: `converge_to_green` by the plan's
   `base_hash`, the post-merge `_tend_single_pr` read by the `origin/main` head
   it just merged, and the two record-time landing arms by
   `merge-base(HEAD, origin/main)`. A key with no cached baseline yields
   `UNATTRIBUTABLE` — today's behaviour — and no consumer ever performs a
   mid-flight checkout to fill one, which is what keeps Step 10's hold-the-tree
   promise and the action gate's branch scoping intact. `_observe_baseline` is
   the single new `run_quality_gates` call site and enters proposal 110's
   `_GATE_ACCEPTANCE_GUARD` allowlist by name.
   - *Depends on:* 1, 4.
   - *Acceptance:* a red whose only failure is a collection error in a file absent
     from `changed_paths` and present in the baseline classifies `INHERITED`; the
     same failure absent from the baseline classifies `UNATTRIBUTABLE`; a
     baseline is recorded only when the `HEAD` read at capture equals its key and
     the tree is clean; a consumer whose key has no cached baseline classifies
     `UNATTRIBUTABLE` without any checkout; and an observation stamped
     `truncated` classifies `UNATTRIBUTABLE` even when every row it *did*
     enumerate is present in the baseline.

### Phase B — bind every terminal to a fresh, attributed read

6. [x] **No terminal on a stale observation.** `maestro/evolution.py`
   (`converge_to_green`), `maestro/gate_convergence.py` (`ConvergenceResult`),
   `maestro/flag_lifecycle.py` — route every exit through one private *method*
   `EvolutionEngine._convergence_terminal(...)` (a method rather than a nested
   closure, so the structural guard in Step 12 resolves its constructions with
   the default outermost-function line resolver) that accepts only an
   observation read *after* the round's last actuator call and constructs the
   `ConvergenceResult` from it. The post-round exits already hold such a read;
   the `not fixed` path holds none, so the helper re-reads the gate there —
   one extra deterministic run per stale-stuck terminal, zero SDK — and returns
   `green` when the branch is in fact green. The actuator's `has_changes()`
   return becomes advisory input, never the verdict — the same re-read
   invariant CLAUDE.md already states for merge truth, escalation delivery, and
   owned `mark_plan_obsolete`. `ConvergenceResult` gains
   `attribution: Attribution | None` (`None` flag-off), computed by Step 5's
   `attribute` inside the helper so Steps 9–11 branch on the result rather than
   re-deriving it; and the helper is the single writer of one
   `gate_convergence_terminal` row per terminal via the existing
   `log_gate_activation` (detail: outcome, attribution, `rounds_used`, the base
   SHA), which flips the Step 4 manifest entry's `gate` from `None` to that
   name.
   - *Depends on:* 4, 5.
   - *Acceptance:* an `auto_fix` that returns `False` while the gates are green
     yields `ConvergenceResult(outcome="green")`, the returned `review` is the
     post-fix observation, not the pre-fix one, and exactly one
     `gate_convergence_terminal` activation row is appended per terminal.

7. [x] **Progress at failure granularity.** `maestro/gate_convergence.py`
   (`plan_next_round`) — compare `GateObservation.signature()` sets. Twelve
   failures becoming one is `"continue"`; the identical failure set is `"stuck"`.
   The `"green"` rule binds to the observation's per-gate status — every gate
   `CLEAN` — never to an empty signature: Step 1's synthetic identities already
   make a `TIMEOUT` or coverage-only red non-empty, and this rule is the second,
   independent lock, so an unverified branch can never be PR'd on a gate that did
   not finish. The `gate_convergence_ceiling` of 20 becomes the real terminal it
   was advertised as.
   - *Depends on:* 1, 6.
   - *Acceptance:* `plan_next_round` returns `"continue"` for a strictly shrinking
     failure set drawn from a single gate label, `"stuck"` when the set is
     unchanged, and never `"green"` for an observation whose gates are not all
     `CLEAN` — including a `TIMEOUT` or coverage-shortfall observation whose
     parsed `failures` tuple is empty.

8. [x] **The same treatment for the review loop.** `maestro/evolution.py`
   (`self_review`), `maestro/gate_convergence.py` (`plan_next_review_round`) —
   a round that verifies without editing no longer reads as a stall; the terminal
   binds to the observation, mirroring Step 6.
   - *Depends on:* 6.
   - *Acceptance:* a self-review round that commits nothing while the branch is
     gate-clean and unreviewed does not return `"stuck"` on that ground alone.

9. [x] **An inherited red never spends a human approval — and never loops.**
   `maestro/agent.py` (`implement_plan`, `_implement_approved_plans`),
   `maestro/memory.py` (`EvolutionPlan`) — on an `INHERITED` convergence
   terminal, do not `_demote_plan_to_failed` and do not consume the attempt:
   leave the plan `approved` with `failure_count` unchanged, `cleanup_branch` as
   today (`implement` deletes and recreates the plan's branch from the repaired
   base on re-admission, so a kept branch would only be swept then), journal
   the inherited failure, and fill an empty `next_task_directive` with a bounded
   base-repair directive naming the failing identities. Removing the demote
   removes `max_implementation_attempts` as the lineage's only terminal, so the
   hold is bounded twice, mirroring the existing base-SHA-keyed
   `last_conflict_resolution_base_sha` cooldown: two defaulted `EvolutionPlan`
   fields, `inherited_hold_main_sha` (the `origin/main` head at the terminal)
   and `inherited_hold_count`. `_implement_approved_plans` skips a plan whose
   `inherited_hold_main_sha` still equals the current `origin/main` head — the
   base has not moved, so a re-attempt would only re-buy the same red — and
   re-admits it the moment `origin/main` advances (the base-repair task landing
   is the designed release); and once `inherited_hold_count` reaches
   `max_implementation_attempts`, the next `INHERITED` terminal demotes as
   today, carrying the inherited identities as `last_failure_reason`, so a base
   that stays red across three distinct heads still terminates.
   `UNATTRIBUTABLE` keeps today's demote.
   - *Depends on:* 5, 6.
   - *Acceptance:* an `INHERITED` terminal leaves the plan's status `approved`
     and its `failure_count` unchanged, stamps `inherited_hold_main_sha` and
     increments `inherited_hold_count`, and writes exactly one base-repair
     directive into an empty slot; `_implement_approved_plans` skips the plan
     while `origin/main` equals the stamped head and selects it again once the
     head differs; a third `INHERITED` terminal demotes the plan `failed`.

10. [x] **An inherited red never destroys work.** `maestro/agent.py`
    (`_land_uncommitted_owned_edit`, `_push_committed_unpushed_owned_head`),
    `maestro/evolution.py` (`_tend_single_pr`) — the three destructive arms
    (`discard_changes`, `reset_hard(origin/<branch>)`, the post-merge rewind) fire
    only on `ATTRIBUTED` or `UNATTRIBUTABLE`. On `INHERITED` they hold the tree,
    push nothing, and record a partial naming the inherited blocker.
    - *Depends on:* 5.
    - *Acceptance:* with an `INHERITED` observation, the uncommitted arm calls
      neither `discard_changes` nor any push seam, and the head SHA is unchanged.

11. [x] **Give the fixer a diagnosis.** `maestro/evolution.py`
    (`_run_auto_fix_query`) — the prompt carries the enumerated failure list and
    the attribution verdict in place of `review.raw_output[-gate_output_cap:]`,
    and states explicitly that an `INHERITED` failure is not the plan's to fix.
    `implement_plan`'s `last_failure_reason` is composed from the same rendered
    failures so the failure corpus stops recording truncated phantoms.
    - *Depends on:* 5, 6.
    - *Acceptance:* the auto-fix prompt for a four-failure red names all four
      identities, and no recorded `last_failure_reason` is a mid-word slice.

### Phase C — enforce, retire, collapse

12. [x] **Structural enforcement.** `tests/structural_guards.py` — two guards in
    the house `ChokepointGuard` shape: `gate_observation` pinning
    `maestro/gate_observation.py` as the sole owner of `GateFailure` /
    `parse_gate_output` / `attribute` (mirroring the `plan_grounding` and
    `frame_discharge` single-owner guards), and `gate_terminal` pinning that
    `ConvergenceResult(` is constructed under `maestro/` only inside
    `EvolutionEngine._convergence_terminal` — allowlist entry
    `("evolution.py", "_convergence_terminal")` under the default
    `line_to_function` resolver, which is exactly why Step 6 makes the helper a
    method: that resolver attributes a nested closure to its enclosing
    `converge_to_green`, and only `line_to_innermost_function` could see a
    closure — so a stale-observation verdict is unrepresentable rather than
    merely discouraged.
    - *Depends on:* 1, 6.
    - *Acceptance:* both guards red the build when a seeded edit constructs a
      `ConvergenceResult` outside `_convergence_terminal` or parses gate output
      outside the owner module.

13. [ ] **Retire the subsumed moles.** `maestro/red_root.py` — delete
    `classify_mypy_provisioning_signature`, `_is_maestro_sdk_provisioning_error`
    and their constants, routing `diagnose_red_root` through `attribute`;
    `maestro/evolution.py` — delete `QualityGateResult.failure_signature` and
    `_summarize_failed_gates`, both superseded by the observation. Rewrite the
    tests that pin the replaced behaviour: `tests/test_agent.py`
    (`test_converge_to_green_no_change_is_stuck`),
    `tests/test_gate_convergence.py`
    (`test_plan_next_round_no_shrink_equal_is_stuck` plus the five
    `test_failure_signature_*` tests, retargeted at
    `GateObservation.signature()`), `tests/test_evolution_tend.py` (the two
    `test_summarize_failed_gates_*` tests, retargeted at the rendered failure
    list the routed remediation task now carries), and `tests/test_red_root.py`
    (the `classify_mypy_provisioning_signature` block, retargeted at
    `attribute`).
    - *Depends on:* 5, 6, 7, 12.
    - *Acceptance:* `rg 'classify_mypy_provisioning_signature|failure_signature'`
      returns no hits under `maestro/`, and red-root still names a mypy
      provisioning gap — now via the general attributor, for any tool.

14. [ ] **Measure, then collapse the flag.** `maestro/flag_lifecycle.py`,
    `maestro/evolution.py` — the `gate_convergence_terminal` rows Step 6 already
    writes are the measurement, so `maestro audit-flags` reports the fire rate
    with no new store and no second writer. After a bounded observation window,
    move `gate_observation_enabled` from `EXPERIMENTAL_FLAGS` to `CUTOVER_FLAGS`
    (stage-tracked, `off_path_symbol` naming the retained off-branch) and delete
    the off-branch.
    - *Depends on:* 6, 9, 10.
    - *Acceptance:* `maestro audit-flags` lists the flag with a non-null
      activation count whose per-terminal detail names outcome and attribution,
      and the cut-over entry reds the build if the off-branch survives its
      collapse stage.

## Testing Strategy

All tests use `mem_fs`, mock every subprocess and SDK seam, and patch
`asyncio.sleep` / `time.sleep` — no real pytest, git, network, or clock.

- `tests/test_gate_observation.py` (new) — pure-parser coverage over captured
  fixture output: multi-failure pytest, a collection `ERROR`, a ruff diagnostic
  block, a mypy error block, a timeout, and an empty/garbage stream. Both
  `signature()` branches (shrink, unchanged) and all three `attribute` verdicts
  with a seeded baseline, including the guard branch where `changed_paths` is
  empty.
- `tests/test_agent.py` — the regression that names this proposal's incident:
  `auto_fix` returns `False` while a patched `run_quality_gates` returns green on
  the re-read; assert `outcome == "green"`, a PR is created, and
  `_demote_plan_to_failed` is never called. Plus the `INHERITED` arm (plan stays
  `approved`, `failure_count` unchanged, one base-repair directive written) and
  the `UNATTRIBUTABLE` arm (today's demote preserved).
- `tests/test_gate_convergence.py` — `plan_next_round` over failure-granular
  signatures: single-label shrink is `"continue"`, unchanged is `"stuck"`,
  ceiling still backstops, and a non-`CLEAN` observation with an empty parsed
  failure tuple is never `"green"`. Every test pinning the replaced behaviour is
  rewritten, not deleted, so the new contract is asserted in the same place:
  `test_plan_next_round_no_shrink_equal_is_stuck` and the five
  `test_failure_signature_*` tests here, `test_converge_to_green_no_change_is_stuck`
  in `tests/test_agent.py`, the two `test_summarize_failed_gates_*` tests in
  `tests/test_evolution_tend.py`, and the `classify_mypy_provisioning_signature`
  block in `tests/test_red_root.py` (Step 13).
- `tests/test_evolution_impl.py` (baseline seam) — `_observe_baseline` records
  only when the mocked `HEAD` equals its key, the map evicts past four entries,
  and a consumer whose key is absent classifies `UNATTRIBUTABLE` without any
  checkout call on the mocked VCS.
- `tests/test_agent.py` (inherited hold) — `_implement_approved_plans` skips a
  plan whose `inherited_hold_main_sha` equals the mocked `origin/main` head,
  selects it once the head differs, and a third `INHERITED` terminal demotes.
- `tests/test_evolution_impl.py` — `run_quality_gates` issues one subprocess run
  on green and the probe-plus-enumerate pair on red; the flag-off path is
  byte-identical (asserted on the recorded argv and `raw_output`).
- `tests/test_agent.py` (landing arms) — with an `INHERITED` observation,
  `discard_changes`, `reset_hard` and every push seam are asserted *not* called;
  with `ATTRIBUTED`, today's rewind still fires.
- `tests/test_structural_guards.py` — a seeded negative control per guard
  (a `ConvergenceResult` constructed outside `_convergence_terminal`; a
  `parse_gate_output` call outside the owner) plus a non-vacuity floor, following the
  `tests/test_settlement_reconciler.py` registry-guard pattern.
- `tests/test_config.py` — the flag's default, TOML overlay, and
  `EXPERIMENTAL_FLAGS` registration with a dated `review_by`.

## Risk Assessment

**Wall-clock and cost.** Three additions, every one a deterministic subprocess
run at zero SDK cost: Step 2's enumeration re-run (only on a red probe), Step
5's baseline (once per new `origin/main` head at the tend lap, plus a
cache-miss run in `implement` for a `source_task_id` base), and Step 6's
terminal re-read (only on the `not fixed` path — the post-round terminals
reuse the read they already hold). Measured full-suite runtime is ~103s
against a `selfcheck_timeout` of 300s, so a doubled red path fits the existing
budget; if it does not, the observation records `TIMEOUT` explicitly rather
than degrading to today's `"could not run"`. Against a measured $— of
implement-lineage spend per 7 days producing zero merges, one to three extra
deterministic runs per red gate is the cheap side of the trade.

**A wrong `INHERITED` verdict masks a real regression.** This is the sharp edge:
Step 9 declines to demote and Step 10 declines to rewind on `INHERITED`. The
attribution is deliberately conservative — a failure is `INHERITED` only when the
*identical identity* is present in a baseline observation of the branch's own
base, never inferred from a heuristic about the failure's shape. Anything the
baseline does not confirm falls to `UNATTRIBUTABLE`, which keeps today's
behaviour exactly. CI remains authoritative and unchanged, so a mis-attributed
regression is caught at the PR, not merged.

**A correct `INHERITED` verdict must not loop.** Declining to demote removes
`max_implementation_attempts` as the lineage's only terminal, and a base that
stays red would otherwise re-buy the same ~$— implementation every iteration.
Step 9's hold is bounded on both axes: a plan is not re-attempted while
`origin/main` still equals the head it was held at, and after
`max_implementation_attempts` distinct inherited holds it demotes exactly as
today, with the inherited identities recorded as the reason.

**Parse drift.** A pytest or ruff output-format change would silently empty
`failures`. Mitigated by treating an empty `failures` on a non-zero exit as
`UNATTRIBUTABLE` (never `INHERITED` — Step 1's synthetic identity keeps such a
red visible to progress and greenness, and Step 5 excludes it, and any
`truncated` enumeration, from baseline matching) — the fail-safe direction — and
by fixture
tests over captured real output, the same posture `maestro/ci_diagnosis.py` takes
for check logs.

**Rollback.** Every behaviour change sits behind default-off `[loop]
gate_observation_enabled`; flag-off is byte-identical, asserted in
`tests/test_evolution_impl.py`. Steps 1–5 are inert additions. Steps 13–14
(deletions and cutover) land only after the flag is on in production and the
`gate_convergence_terminal` activation counts confirm the terminals moved. No
state file moves and nothing is rewritten: the observation and its baselines
are in-memory, the only new persistence reuses the existing
`gate_activations.jsonl`, and Step 9's two defaulted `EvolutionPlan` fields
follow the `last_conflict_resolution_base_sha` precedent — absent on old rows,
normalised on read by the dataclass loaders under `ensure_schema()`.

**Trust chain.** The change touches no approval, sandbox, or public-API surface.
It *narrows* the conditions under which the loop destroys work, and Step 12's
`gate_terminal` guard is the deterministic control proving the narrowing holds.

**Falsifiability.** If the design is right, then over 14 days after the flip:
`stuck` terminals whose fresh re-read is green fall to zero (baseline: at least 1
of 15 demotions confirmed, with 11 of 15 at `0 round(s)` as candidates);
demotions attributed `INHERITED` fall to zero (baseline: at least 5 of 15); and
median `rounds_used` on a red gate rises above 1 (baseline: 1.0 over 25 observed
rounds, ceiling 20). The failure-identity signal is two-sided — a round that
clears one gate while a *new* failure surfaces in another now reads `stuck` where
the coarse label set read `continue` — so the `rounds_used` read must examine the
whole distribution the `gate_convergence_terminal` rows record, not only whether
attempts clear round 2, and a modest rise in early `stuck` terminals of that
clear-one-add-one shape is the honest cost, not a regression. If demotions per
approved plan stay flat *and*
`ATTRIBUTED` is the modal verdict, the design is wrong and the root lies in the
implementation writer rather than the observer — that result should send the
next investigation at `implement_plan`, not at another gate patch. All three
figures are measured over attempts that *reach a convergence terminal*: an
attempt aborted by a transient SDK failure reaches none and is excluded from
both baseline and prediction, so the prediction can hold while a plan still
never lands — and that outcome would indict the transient-SDK path (22 aborts
in the same window), not this design.

## Out of scope

- **Provisioning the loop container's `maestro_sdk` install.** A deployment
  concern (`docs/deployment.md`); this proposal makes such a gap non-fatal to a
  plan, it does not close the gap.
- **Local↔CI gate fidelity.** The local gate runs `cov_fail_under = 97` where CI
  requires 100%, and never runs the `maestro_review` / `maestro_sdk` sibling
  suites — so a locally green branch can still be CI-red. That is the *opposite*
  direction (local too lenient) and was plan `0c26cf4995cc`'s subject; it
  deserves its own proposal.
- **The remote CI-truth chokepoint.** `maestro/ci_diagnosis.py` already owns
  check-status classification and log error-extraction; this proposal builds its
  local sibling and deliberately does not merge the two vocabularies.
- **Proposal 120's Step 9 decommission.** Unblocked by this work but not
  performed here.
- **Transient SDK aborts during `implement_plan`.** The 22 `cli_crash` /
  `stream_stall` aborts measured above are handled by `run_loop`'s
  resume-in-place retry and never reach a convergence terminal; this proposal
  neither counts them as gate outcomes nor changes how they are retried. They
  deserve their own investigation at the SDK-resilience layer
  (`docs/sdk-internals.md`).
