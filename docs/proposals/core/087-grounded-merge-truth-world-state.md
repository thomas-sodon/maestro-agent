# 087: A Grounded Merge-Truth World-State — One Provenance-Stamped Fact, One Chokepoint, One AST-Enforced Door

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | new `maestro/world_state.py`, `maestro/agent.py` (`_host_or_git_pr_merged`, `_pr_merged_in_git`, `_branch_merged_into_main`, `_gate_pr_remediation_merged`, `_gate_pr_remediation_landed`, `_preflight_merge_truth_standdown`, `_record_remediation_outcome`), `maestro/audit_checks.py` (`is_branch_merged_into_main`, `_git_pr_merged_default`), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml` (`[loop]`), new `tests/test_world_state.py`, new `tests/test_merge_truth_chokepoint.py`, and updates to `tests/test_agent.py`, `tests/test_loop_pr_remediation.py`, `tests/test_run_task_phase.py`, `tests/test_config.py` |
| **Depends on** | _(composes with 070 and 080)_ |
| **What landed** | All five steps. **Step 1 (Phase 1)** — `maestro/world_state.py` defines the frozen `MergeTruth` dataclass (`pr_number` / `settled` / `state` / `branch_landed` / `source` / `observed_at`) and the single fetch-first observer `observe_pr_merge_truth` (host oracle first, git `branch_oracle` fallback only when the host call raises, fail-loud `fetch`, optional `close_oracle`), pure and fully injectable. Tested in `tests/test_world_state.py`. **Step 2 (Phase 2)** — `MaestroAgent.is_pr_settled()` wires the real oracles (the **fail-loud** host API `code_host.is_pr_merged_strict` — corrected from the best-effort `code_host.is_pr_merged` by #2954, whose swallowed `VCSError` made the `branch_oracle` fallback below dead code and laundered every transient `gh` failure into a confident `host_api` / not-landed stamp — the git-subject `_git_pr_merged` fallback, `code_host.is_pr_closed`) through `observe_pr_merge_truth` with a per-iteration memo (`_merge_truth_memo`, cleared each fresh iteration in `run_loop`); all four PR-servicing faces now read merge state through it — admission `route_feedback` and dequeue-by-status `gate_merged` (`maestro/loop/pr_remediation.py`), dequeue-by-ancestry `_gate_pr_remediation_landed` and task-entry `_preflight_merge_truth_standdown` (`maestro/agent.py`). The `[loop] unified_merge_truth_enabled` rollout flag was retired once byte-identical parity was proven, so routing is unconditional. Tested in `tests/test_agent_merge_truth.py`. **Step 3 (Phase 3)** — `tests/test_merge_truth_chokepoint.py` AST-enforces the chokepoint as the only door over `maestro/agent.py` and `maestro/loop/pr_remediation.py`; `_pr_merged_in_git` is removed (retained only as a tripwire in the guard's primitive set), and the git-subject oracle enters only as the observer's `branch_oracle`. **Step 4 (Phase 4)** — `MergeTruth.is_fresh(now, ttl_seconds)` plus `[loop] merge_truth_ttl_seconds` (default 300); `is_pr_settled` re-observes (re-fetches) a memoized fact past its TTL rather than returning it stale. **Step 5 (Phase 5)** — the pre-stamp gate in `_record_remediation_outcome` behind `[loop] prestamp_merge_truth_gate_enabled` (default off) downgrades a `success` naming `#N` to `partial` when `is_pr_settled` reports it unmerged, stamping the outcome with the observation's `source` / `observed_at`. |
| **What is missing** | Nothing — all five steps landed and the `is_pr_settled` chokepoint is AST-enforced as the only door. |
| **Derived from** | Architecture review of the merge-recognition failure class — 070, 080, audit rules 10/12/13/14/15, and the prose-verdict / confirmed-landing sensors |

## Description

> Status updated to shipped on 2026-06-24 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

The loop keeps re-discovering that it cannot reliably tell a merged PR from an
open one, and each rediscovery has been fixed *locally* — a new gate face, a new
audit rule, a new sensor. 070 named the lifecycle states; 080 converged the
subsystem on one host-API-first oracle and added a per-PR lap budget. Both were
right and both shipped. What neither did is make "is this PR settled?" a
**single, typed, provenance-stamped fact that every code path is structurally
required to consult.** Today the merge-deciding primitives
(`_host_or_git_pr_merged`, `is_branch_merged_into_main`, the host API, raw
`git merge-base --is-ancestor`) are still called independently at four separate
faces — admission (`_branch_merged_into_main`), dequeue-by-status
(`_gate_pr_remediation_merged`), dequeue-by-ancestry
(`_gate_pr_remediation_landed`), and task-entry
(`_preflight_merge_truth_standdown`). The fourth face existed *only because*
#2579 entered through a channel the first three did not cover. That is the
signature of a whack-a-mole class: every new entry point that turns a `#NNNN`
into work re-introduces the bug unless someone remembers to add the guard.

The deeper reason this is hard is structural, not incidental. Merge state is a
fact about remote git that (a) lives outside the agent's context, (b) drifts
asynchronously as other actors merge/squash/close, and (c) cannot even be
*observed* without a network fetch. An agent built on a language model defaults
to *narrating* such a fact rather than *observing* it, so the system must make
narration non-load-bearing by routing every decision through an observation it
cannot fake. The verdict stamps are already fetch-gated and honest; the leak is
that the observation behind them has no shared representation — no record of
*how* it was known or *when* — so freshness and provenance cannot be enforced,
and the success-shaped failures (a `success` claiming `#N` resolved while git
shows it unmerged) are caught only *after* the wrong stamp is written, by
post-hoc audit rules 13/14 and the prose-verdict sensor.

The end state: a `maestro/world_state.py` module defining a frozen `MergeTruth`
value — `settled` / `state` / `branch_landed` / `source` (provenance) /
`observed_at` (as-of time) — produced by exactly one fetch-first observer; a
single `MaestroAgent.is_pr_settled()` chokepoint that every PR-servicing face
consults instead of re-deriving merge state itself; an **AST guard** (in the
family of `tests/test_session_attachment.py` and
`tests/test_no_silenced_exceptions.py`) that fails the build when a merge
decision is made outside that door; a TTL so a stale observation may not author
a verdict; and the promotion of the success-shaped check from a post-hoc audit
finding to a *pre-stamp* gate. This converts merge-recognition from "fixed each
incident" into "fixed by construction."

## Relevant Findings

- `maestro/agent.py` (`_host_or_git_pr_merged`) — 080's unified oracle: host
  API first, `_git_pr_merged` fallback only when the host call raises. It
  returns a bare `bool` with no record of which oracle answered or when, so a
  caller cannot reason about provenance or freshness of the answer.
- `maestro/agent.py` (`_branch_merged_into_main`, `_gate_pr_remediation_merged`,
  `_gate_pr_remediation_landed`, `_preflight_merge_truth_standdown`) — four
  faces that each independently fetch and/or call a merge oracle. The
  task-entry face was added specifically because the dequeue faces never covered
  the user-request channel that #2579 arrived through — direct evidence of the
  per-face proliferation this proposal closes.
- `maestro/audit_checks.py` (`is_branch_merged_into_main`, `_git_pr_merged_default`)
  — the git fallbacks. 080 records their residual failure modes: the
  subject-only contract false-positives on an unrelated `(#N)` subject and
  false-negatives on rebase-merges. A single observer is the place to encode
  "host wins, git only on host failure" once rather than per call site.
- `maestro/pr_remediation_state.py` (`PrRemediationState`, `Marker`) — 070 made
  the *states* explicit and data-driven, but the *truth that drives the
  transitions* is still observed ad hoc inside each gate; the state machine has
  a typed vocabulary with an untyped, unshared input.
- `maestro/remediation_tracker.py` (`classify_remediation_outcome_state`,
  `classify_landing_attribution`, `measure_confirmed_landing_rate`) — already
  type the post-task outcome and separate *caused* from *confirmed* landings;
  these are natural consumers of a shared `MergeTruth` rather than re-deriving
  it from `result_summary` prose.
- `maestro/audit_checks.py` (`measure_false_success_recurrence`,
  `check_phantom_resolution` (rule 13), `check_unconfirmed_own_landing`
  (rule 14)) and `maestro/stats.py` (`measure_prose_verdict_gap`) — the
  success-shaped failure detectors. They are all *post-hoc*: they fire cycles
  after the false `success` stamp is already written and acted upon. The leak
  they patch can be moved upstream into a pre-stamp gate.
- `tests/test_session_attachment.py`, `tests/test_no_silenced_exceptions.py` —
  the existing AST-invariant pattern (a deterministic, zero-cost walk that fails
  the build on a forbidden call shape) this proposal reuses to enforce the
  chokepoint.
- `maestro/audit_checks.py` (`record_prediction`, `measure_prediction_accuracy`,
  `PREDICTIONS_FILENAME`) — the existing predict-then-check substrate the
  calibration tie-in (Phase 5, optional) composes with.

## Implementation Plan

### Phase 1 — A typed, provenance-stamped merge-truth fact

1. [x] **Add `maestro/world_state.py` with `MergeTruth` and one observer.**
   `maestro/world_state.py` — a frozen `MergeTruth` dataclass
   (`pr_number: int`, `settled: bool`, `state: Literal["MERGED","CLOSED","OPEN"]`,
   `branch_landed: bool`, `source: Literal["host_api","git_ancestry","git_subject"]`,
   `observed_at: float`) and `observe_pr_merge_truth(pr_number, *, fetch,
   host_oracle, branch_oracle, now)` that runs `fetch` first (fail-loud), then
   asks `host_oracle` and falls back to git only when it raises, stamping
   `source` with the oracle that actually answered and `observed_at` with `now`.
   Pure, fully injectable, consumed by nobody yet — additive and inert.
   - *Depends on:* none.
   - *Acceptance:* with stub oracles and an injected clock,
     `observe_pr_merge_truth` returns a `MergeTruth` whose `source` reflects the
     answering oracle and whose `observed_at` equals the injected clock; the host
     verdict wins over a git `(#N)` name-drop; a raising `fetch` propagates.

### Phase 2 — One chokepoint every face consults

2. [x] **Add `MaestroAgent.is_pr_settled()` and route the four faces through it.**
   `maestro/agent.py` — `is_pr_settled(pr_number) -> MergeTruth` calls Phase 1's
   observer (wiring `_host_or_git_pr_merged` / `is_branch_merged_into_main` as
   the oracles and the existing fetch helper) and memoizes per iteration.
   Re-point `_branch_merged_into_main`, `_gate_pr_remediation_merged`,
   `_gate_pr_remediation_landed`, and `_preflight_merge_truth_standdown` to read
   the shared `MergeTruth` instead of fetching/observing themselves — each face
   keeps its own *decision*, only the *observation* is shared. Behind
   `[loop] unified_merge_truth_enabled` (default off until parity is proven).
   - *Depends on:* 1.
   - *Acceptance:* with the flag on, a replay of recorded dequeue / preflight
     inputs produces byte-identical evictions and outcome stamps to the legacy
     per-face logic, and each PR is observed at most once per iteration (the
     memo is hit on the second face).

### Phase 3 — Make the door the only door (AST guard)

3. [x] **AST-enforce that merge decisions go through the chokepoint.**
   `tests/test_merge_truth_chokepoint.py` — a deterministic AST walk asserting
   that the merge-deciding primitives (`_host_or_git_pr_merged`,
   `is_branch_merged_into_main`, `code_host.is_pr_merged`, and a
   `git merge-base --is-ancestor` subprocess) appear only inside
   `maestro/world_state.py` and a small explicit allowlist (the observer's own
   oracle wiring); every other PR-servicing site must reach merge state via
   `is_pr_settled`. The two merge-deciding *wrappers* are brought inside the
   door rather than left as bypasses: `_pr_merged_in_git` (the
   fetch-then-`_host_or_git_pr_merged` seam `_gate_pr_remediation_merged`
   calls) has its fetch-first-then-oracle role subsumed by Phase 1's
   `observe_pr_merge_truth` and is removed, and `_git_pr_merged_default` (the
   git fallback) is allowlisted *only* as the observer's `branch_oracle`
   wiring — so neither survives as a call shape a new face could re-derive
   merge state through. Mirrors `tests/test_session_attachment.py`.
   - *Depends on:* 2.
   - *Acceptance:* introducing a new merge-deciding call site outside the
     chokepoint and its allowlist fails the test; the current tree (post Phase 2)
     passes.

### Phase 4 — Freshness: a stale observation may not author a verdict

4. [x] **Add a TTL and refuse to gate on a stale fact.** `maestro/world_state.py`,
   `maestro/agent.py`, `maestro/config.py`, `maestro/toml_config.py`,
   `maestro.toml` — give `MergeTruth` an `is_fresh(now, ttl_seconds)` predicate
   and have `is_pr_settled` re-observe (re-fetch) when the memoized fact is older
   than `[loop] merge_truth_ttl_seconds` rather than returning a stale value.
   Generalizes 080's "fetch-first" from a per-call ritual into a property of the
   fact: no `MergeTruth` past its TTL may drive a decision.
   - *Depends on:* 2.
   - *Acceptance:* a memoized fact past the TTL triggers a re-observation
     (`fetch` invoked again) before it is returned; within the TTL the memo is
     reused and `fetch` is not re-invoked.

### Phase 5 — Promote the success-shaped check from audit to pre-stamp gate

5. [x] **Refuse a `success` stamp git-merge-truth can't confirm, at write time.**
   `maestro/agent.py` (`_record_remediation_outcome`) — before writing a
   `success` outcome for a task naming `#N`, consult `is_pr_settled(N)`; when it
   reports the PR unmerged, downgrade to `partial` and record the refusal,
   stamping the outcome record with the observation's `source` and `observed_at`.
   This moves the catch that rules 13/14 and `measure_prose_verdict_gap` make
   *post-hoc* to *pre-stamp*, so the false success never becomes load-bearing;
   the audit rules remain as the backstop for paths this gate does not cover.
   Behind `[loop] prestamp_merge_truth_gate_enabled` (default off).
   - *Depends on:* 2.
   - *Acceptance:* a task attempting a `success` stamp naming `#N` whose
     `is_pr_settled` reports unmerged is recorded `partial` with a provenance
     stamp; rule 13 finds nothing to flag for that record because the stamp was
     never written.

## Testing Strategy

- **Phase 1** — `tests/test_world_state.py`: stub `host_oracle` / `branch_oracle`
  and inject a clock; assert `source` and `observed_at`, the host-wins-over-git
  name-drop case (the #2579 reproduction at the value-object level), the
  git-fallback-only-on-host-raise branch, and fail-loud `fetch` propagation. No
  real git or network; `mem_fs` where files are touched.
- **Phase 2** — `tests/test_agent.py` / `tests/test_loop_pr_remediation.py`: a
  parity harness replays recorded `pending_pr_requests` and a preflight goal
  through the legacy faces and through the `is_pr_settled`-routed faces, asserting
  identical dispositions and stamps; an observation-count assertion proves the
  per-iteration memo. Cover the flag-off branch (legacy path unchanged).
- **Phase 3** — `tests/test_merge_truth_chokepoint.py`: the AST guard itself,
  plus a negative fixture (an in-test module string with a bypassing call site)
  asserting the walk rejects it.
- **Phase 4** — `tests/test_world_state.py`: drive the clock past
  `merge_truth_ttl_seconds` and assert re-observation; within the window assert
  the memo is reused. `tests/test_config.py` round-trips the new flag and TTL
  with default assertions.
- **Phase 5** — `tests/test_run_task_phase.py`: seed a `success`-shaped outcome
  naming an unmerged `#N` and assert the recorded outcome is `partial` with the
  provenance stamp; assert the corresponding rule-13 check then finds nothing.
  Cover the flag-off branch (today's behaviour). `tests/test_config.py` round-trip.
- All new config flags get a `tests/test_config.py` round-trip and a default
  assertion, per the project's flag-lifecycle convention.

## Risk Assessment

- **Phase 1** is pure addition consumed by nobody; zero behavioural risk.
- **Phase 2** carries the behavioural weight but is parity-gated: the flag stays
  off until the replay harness proves byte-identical dispositions, and rollback
  is the flag. The faces keep their own decision logic — only the observation is
  centralized — so a routing regression is contained to "which fact each face
  reads," which the parity test pins.
- **Phase 3** is a test-only guard; its risk is over-strictness (a legitimate
  call site flagged). The explicit allowlist is the escape valve, and the guard
  lands only after Phase 2 makes the tree already conform.
- **Phase 4** changes when a re-fetch happens; the danger is *over*-fetching
  (cost) on a too-small TTL, mitigated by a conservative default and the
  per-iteration memo. A too-large TTL degrades to today's within-iteration
  staleness, no worse than current behaviour.
- **Phase 5** changes a verdict at write time; the danger is *under*-crediting a
  genuine landing the chokepoint cannot yet see (host API down, ref not fetched).
  Mitigated by Phase 4 freshness and by recording the refusal honestly as
  `partial` (never silently dropping the task) so the existing
  confirmed-vs-caused sensors stay accurate. Flag-gated; rollback is the flag.
- **Trust chain:** none of these phases touch approvals, the action gate, or the
  sole-pusher invariant — Tend remains the only pusher and all code changes still
  require human merge. This proposal only changes *how the loop reads remote
  state*, never how it writes it.

## Out of scope

The `MergeTruth` world-state is deliberately scoped to PR merge facts in v1. The
same pattern — a typed, provenance-and-freshness-stamped, observation-only fact
behind one chokepoint — generalizes to other external truths the loop currently
narrates (CI status, branch existence, issue state), but those are left to follow-up
once the merge-truth instance proves the shape. A full predict-then-verify
calibration tie-in over `record_prediction` / `measure_prediction_accuracy` (force
a checkable merge prediction at each consequential action and feed the calibration
back) is likewise a natural successor, not part of this proposal.
