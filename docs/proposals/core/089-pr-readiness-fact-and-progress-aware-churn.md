# 089: Churn Is the Job Until the PR Is Ready — One Observed Ready-and-Waiting Fact and Progress-Aware Lap Accounting

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | new `maestro/pr_readiness.py`, `maestro/agent.py` (`observe_pr_readiness`, `_gate_pr_remediation_ready`), `maestro/evolution.py` (`_eligible_bug_comments`, `_eligible_overflow_bug_findings`, `_confirm_ready_for_review`), `maestro/remediation_tracker.py` (`classify_merge_block`, new `pr_readiness_improved` / `measure_no_progress_laps`), `maestro/escalation_bridge.py` (`detect_unlanded_cure_handoff`), `maestro/loop/pr_remediation.py` (`dequeue_gates`), `maestro/pr_remediation_state.py` (`PrRemediationState`, `Marker`, `_STATE_TO_DISPOSITION`), `maestro/prompts/remediation.py` (`MERGE_TRUTH_FIRST_PREAMBLE`), `maestro/audit_checks.py` (`format_known_state_summary`), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml` (`[loop]`), tests |
| **Depends on** | _(composes with 070, 080, 087, and the 064/083 comment-reply machinery)_ |
| **Derived from** | Operator framing of the loop's recurring PR-remediation frustration: a PR churns *legitimately* while sibling PRs merge, jobs flake, and reviewers comment — the only honest terminal is "nothing left to do but Thomas's approval" |

## Description

> Status updated to shipped on 2026-06-24 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

A pull request on a fast-moving repository **churns by design**, not by malfunction.
While Maestro's PR is open, other PRs keep merging — so its branch falls behind
and sometimes conflicts; CI jobs flake and must be re-chased to green; reviewers
keep adding comments, inline and overflow. Each of those is a *new, real* unit of
work that arrived after the last lap. Re-attending to it is the chase to a green
pipeline, which is the job — not a treadmill. There is exactly one point at which
the chase is genuinely over: every blocker Maestro *can* clear is clear, and the
sole remaining gate is Thomas's approval (or Tend's mechanical push). **Only then
may Maestro stand down on a PR.** Until then there is work, and standing down early
is the real avoidance.

The loop's current machinery gets the *epistemics* of standing down right but the
*frame* wrong, and it has no single observer of the terminal condition. The
stand-down decision is **narrated**: the remediation preamble asks the LLM to read
`AWAITING_APPROVAL` and assert "no actionable review thread," then choose a
`[stood-down]` `partial` (`maestro/prompts/remediation.py`). That preamble's
language — "the re-lap is the avoidance," "the treadmill," "empty hands chosen on
purpose" — treats *all* re-attention as pathological, leaving Maestro no vocabulary
for the legitimate, progress-making churn that a busy main forces. And the budgets
that decide when to escalate count **gross** laps, not progress: a PR that clears a
fresh conflict every lap because main keeps moving will cross
`[loop] pr_remediation_lap_budget` (default 6) and earn a "merge-or-close" handoff
(`maestro/escalation_bridge.py`) — telling Thomas to *close a healthy, advancing
PR*. That is the frustration: necessary work reported as failure.

This proposal makes the terminal an **observed fact** instead of a narrated one,
in the exact mold 087 built for merge-truth (which explicitly named CI status and
issue state as its natural successors). A new `PRReadiness` value aggregates every
blocker dimension the loop already observes piecemeal — branch behind/conflicting,
CI green/failing/pending, unresolved inline threads, unaddressed overflow findings,
merge-state — into one provenance-stamped observation, deriving a single
`ready_and_waiting` boolean: *every code-clearable blocker is clear, the only thing
left is approval*. One stand-down gate keys on that fact, recomputed live each
dequeue, so a blocker reappearing on the same head (a sibling PR merging, a new
comment) re-admits the PR immediately — churn handled by construction. Then it
re-bases the escalation budget on **consecutive no-progress laps** rather than gross
laps, reusing the no-op doctrine the repetition gate already encodes, so a PR that
keeps clearing newly-arrived blockers never escalates while a genuinely stuck one
still does. Finally it reframes the contemplation and remediation surfaces so
churn-with-progress reads as the expected cost of a fast-moving main, and the clean
`[ready]` stand-down reads as completed work — not as the same defeat as a stuck
loop.

## Relevant Findings

- `maestro/remediation_tracker.py` (`classify_merge_block`) — orders blockers
  `CONFLICTING` > `BEHIND_MAIN` > `CI_FAILING` > `CI_PENDING` > `AWAITING_APPROVAL`
  > `NONE`, the closest thing to a unified blocker view today. But it ranks only the
  **merge-state and CI** dimensions; it is blind to unresolved inline review threads
  and unaddressed overflow findings. So it can return `AWAITING_APPROVAL` for a PR
  that still has open comment threads — the "only waiting on approval" condition has
  **no single, complete observer**.
- `maestro/evolution.py` (`_confirm_ready_for_review`, `ready_for_review_pending_sha`)
  — the one place that *does* aggregate readiness (branch synced with main + all
  review comments addressed + CI green), but it fires a ready-for-review **ping** and
  is computed independently of the remediation / stand-down decision; the two
  readiness judgments can disagree. This is the prior art the readiness fact converges
  and generalizes from "ping trigger" to "stand-down decision."
- `maestro/evolution.py` (`_eligible_bug_comments`, `_eligible_overflow_bug_findings`,
  `TendPRsResult.bug_comments_routed` / `bug_overflow_routed`) — inline-thread and
  overflow-finding blockers are observed only inside `tend_evolution_prs` and routed
  straight to remediation work (064, 083). Their **counts** are never folded into a
  readiness verdict, so the stand-down decision cannot see them.
- `maestro/vcs.py` (`get_pr_check_conclusion`, `get_pr_failing_check_names`,
  `get_pr_review_threads`) — the CI-rollup and review-thread observers already exist
  and are host-API-grounded; the readiness fact composes them rather than adding new
  I/O.
- `maestro/world_state.py` (`MergeTruth`, `observe_pr_merge_truth`) and
  `maestro/agent.py` (`is_pr_settled`) — 087's provenance-stamped, fetch-first
  observation chokepoint for *merge* state. Its **Out of scope** section explicitly
  names CI status, branch existence, and issue state as the pattern's natural
  successors. This proposal is that successor: `PRReadiness` is built in the same
  mold and reuses `is_pr_settled` for the merge dimension rather than re-deriving it.
- `maestro/escalation_bridge.py` (`detect_unlanded_cure_handoff`,
  `HANDOFF_STAMP_THRESHOLD`) consuming
  `maestro/remediation_tracker.py` (`measure_unlanded_remediations`) — the handoff
  trigger counts **cumulative** unlanded laps and crosses the budget regardless of
  whether each lap cleared a newly-arrived blocker. A healthily-churning PR therefore
  earns the same merge-or-close knock as a stuck one.
- `maestro/audit_checks.py` (`classify_noop_task`, `classify_settled_relap`,
  `measure_relap_rate`) and `maestro/remediation_tracker.py` (the per-PR
  repetition gate, which counts **consecutive no-ops**) — the loop already
  distinguishes a no-op lap from a progress-making one in several sensors and in the
  repetition gate, but the **lap budget and escalation bridge do not consume that
  distinction.** Phase 3 closes that gap with the same doctrine.
- `maestro/prompts/remediation.py` (`MERGE_TRUTH_FIRST_PREAMBLE`, the
  `CHOSEN STAND-DOWN` and `STAND-DOWN-ON-RESOLVED` clauses) — the stand-down is
  authored from LLM **narration** of `AWAITING_APPROVAL` / `AWAITING_PUSH`, and the
  surrounding language frames every re-lap as avoidance. There is no clause for
  legitimate churn, and no observed fact the verdict is bound to — exactly the
  narration-outruns-reality leak 087 closed for merge-truth, still open for readiness.
- `maestro/pr_remediation_state.py` (`PrRemediationState.STOOD_DOWN`, `Marker`,
  `classify_pr_remediation_disposition`, `_STATE_TO_DISPOSITION`) — the existing
  stand-down state is gated on a prose-authored `stand_down_verdicts.jsonl` verdict
  keyed `(pr, head_sha)`; it **cannot re-arm when a blocker reappears on the same
  head** (a sibling PR merging makes this PR `BEHIND_MAIN` with no head change here).
  A live-recomputed readiness gate fixes that without a new ledger.

## Implementation Plan

### Phase 1 — One aggregated, provenance-stamped readiness fact

1. [x] **Add `maestro/pr_readiness.py` with `PRReadiness` and one observer.**
   `maestro/pr_readiness.py` — a frozen `PRReadiness` dataclass
   (`pr_number: int`, `branch_landed: bool`, `branch_behind: bool`,
   `branch_conflicting: bool`,
   `ci_state: Literal["GREEN","FAILING","PENDING","NONE"]`,
   `unresolved_threads: int`, `unaddressed_overflow: int`,
   `awaiting_approval: bool`, `ready_and_waiting: bool`,
   `source: Literal["host_api","git","mixed"]`, `observed_at: float`) and a pure
   `observe_pr_readiness(pr_number, *, merge_truth, ci_oracle, thread_oracle,
   overflow_oracle, merge_block_oracle, now)` that composes the injected oracles and
   derives `ready_and_waiting` = the PR is still open (`not branch_landed`) **and**
   no code-clearable blocker remains: `not branch_behind`, `not branch_conflicting`,
   `ci_state == "GREEN"`, `unresolved_threads == 0`, `unaddressed_overflow == 0`.
   Every false-making field names the one blocker that keeps the PR from ready.
   Fully injectable, consumed by nobody yet — additive and inert.
   - *Depends on:* none.
   - *Acceptance:* with stub oracles and an injected clock, `ready_and_waiting` is
     `True` iff every code-clearable blocker is clear and the PR is open; setting any
     single blocker (one unresolved thread, `ci_state="FAILING"`, `branch_behind`,
     one overflow finding) flips it `False`; `source` / `observed_at` are stamped.

### Phase 2 — One observed stand-down, recomputed live

2. [x] **Add `MaestroAgent.observe_pr_readiness()` wiring the real oracles, memoized
   per iteration.** `maestro/agent.py` — `observe_pr_readiness(pr_number) ->
   PRReadiness` calls Phase 1's observer with `is_pr_settled` (087) as the
   `merge_truth` oracle, `VCS.get_pr_check_conclusion` / `get_pr_failing_check_names`
   as `ci_oracle`, the `_eligible_bug_comments` count as `thread_oracle`, the
   `_eligible_overflow_bug_findings` count as `overflow_oracle`, and
   `classify_merge_block` as `merge_block_oracle`. Memoize per iteration (cleared in
   `run_loop`) with the same TTL discipline `is_pr_settled` uses, so a fresh
   observation re-fetches rather than serving a stale fact.
   - *Depends on:* 1.
   - *Acceptance:* a PR is observed at most once per iteration within the TTL (the
     memo is hit on the second consult); past the TTL the oracles are re-invoked.

3. [x] **Add the `READY_AND_WAITING` state and the live stand-down gate.**
   `maestro/pr_remediation_state.py`, `maestro/agent.py`,
   `maestro/loop/pr_remediation.py` — add `PrRemediationState.READY_AND_WAITING`,
   `Marker.READY = "[ready]"`, and map the new state to
   `PrRemediationDisposition.HANDOFF` in `_STATE_TO_DISPOSITION` (a terminal,
   zero-cost skip — the work is done, the PR is in Thomas's court); the disposition
   therefore stays in 080 Phase 4's unified `classify_pr_remediation_disposition`
   (which reads `_STATE_TO_DISPOSITION`) — the new gate only *detects* the state, so
   the gate-vs-classifier split 080 collapsed is not reintroduced. Add a dequeue-face
   gate `_gate_pr_remediation_ready` that, when `observe_pr_readiness(pr)
   .ready_and_waiting` holds, records a `[ready]` `partial` (a clean terminal
   stand-down, **never `success`** — nothing landed) and skips. Crucially the gate
   re-checks readiness **live** each dequeue, so a blocker reappearing on the same
   head re-admits the PR immediately — this is the re-arm the `(pr, head_sha)`-keyed
   `STOOD_DOWN` verdict cannot do. Register `_gate_pr_remediation_ready` in the
   order-pinned `_DEQUEUE_GATE_TABLE` (070) **after** `MERGED` / `LANDED` (a
   merged/landed PR is already terminal, and `ready_and_waiting` is `False` for a
   landed branch by construction) and **immediately before** the legacy `STOOD_DOWN`
   gate, so the live, freshly-observed `[ready]` fact supersedes the stale
   `(pr, head_sha)`-keyed verdict it generalizes rather than double-firing behind it.
   It sits ahead of `MERGE_BLOCKED` / `BRANCH_RESOLVED` / `PREFLIGHT_CLEAN`, which are
   mutually exclusive with readiness — any unresolved code-clearable blocker makes
   `ready_and_waiting` `False`, so control falls through to them. Re-pin the new slot
   in `tests/test_loop_pr_remediation.py` (`test_dequeue_gate_table_order_is_pinned`).
   Behind `[loop] observed_ready_standdown_enabled` (default off).
   - *Depends on:* 2.
   - *Acceptance:* a PR whose readiness shows only-awaiting-approval is skipped with a
     `[ready]` `partial`; the *same* PR after one new unresolved thread appears on the
     *same head* is **not** skipped and re-admits as remediable; with the flag off the
     dequeue chain is byte-identical to today.

### Phase 3 — Progress-aware lap accounting (escalate the stuck, not the busy)

4. [x] **Define a productive lap and count consecutive no-progress laps.**
   `maestro/remediation_tracker.py` — add `pr_readiness_improved(prev, cur)` returning
   `True` when at least one code-clearable blocker cleared between two `PRReadiness`
   snapshots (fewer unresolved threads/overflow, CI advanced toward green, a conflict
   or behind-state resolved) or `cur.ready_and_waiting`; persist each lap's readiness
   snapshot on the remediation `TaskRecord` (or recompute from the trailing history),
   and add `measure_no_progress_laps(data_dir, n)` counting **consecutive** laps per
   PR with no improvement and not yet ready-and-waiting. Reuses the repetition gate's
   no-op doctrine for the budget dimension.
   - *Depends on:* 1.
   - *Acceptance:* a lap sequence that clears one blocker each lap reports `0`
     consecutive no-progress laps; a sequence of identical (no-improvement)
     observations accumulates the count; a `ready_and_waiting` observation resets it.

5. [x] **Re-base the merge-or-close handoff on no-progress, not gross laps.**
   `maestro/escalation_bridge.py` — when `[loop] progress_aware_lap_budget_enabled`
   (default off) is on, `detect_unlanded_cure_handoff` triggers on
   `measure_no_progress_laps >= pr_remediation_lap_budget` instead of cumulative
   unlanded laps. A PR clearing newly-arrived blockers never crosses the budget; a PR
   stuck on identical observations still does. The handoff message and durable
   `pr_number`-keyed standdown are unchanged — only the *trigger* condition moves.
   - *Depends on:* 4.
   - *Acceptance:* a PR driven well past the gross budget but clearing a blocker every
     lap receives **zero** handoffs; a PR stuck on identical observations past the
     no-progress budget receives **exactly one**; with the flag off, the gross-count
     trigger is unchanged.

### Phase 4 — Reframe churn in contemplation and the remediation prompt

6. [x] **Surface done / stuck / healthily-churning in known-state.**
   `maestro/audit_checks.py` (`format_known_state_summary`),
   `maestro/remediation_tracker.py` — add `format_pr_churn_summary` folding, per
   active PR, the productive-vs-no-progress lap split and the ready-and-waiting state
   into the contemplation context: a `PR-READY #N` line when an open PR is
   ready-and-waiting (a clean terminal to recognize, not chase), and a distinct
   `PR-STUCK #N` line when no-progress laps cross the budget. A PR churning *with*
   progress raises neither alarm — the absence is the signal that churn is normal.
   Pure display.
   - *Depends on:* 4.
   - *Acceptance:* an open ready-and-waiting PR yields exactly one `PR-READY` line; a
     no-progress-stuck PR yields exactly one `PR-STUCK` line; a PR clearing a blocker
     each lap yields neither.

7. [x] **Teach the remediation preamble that churn-with-progress is the job.**
   `maestro/prompts/remediation.py` (`MERGE_TRUTH_FIRST_PREAMBLE`) — add an
   `EXPECTED-CHURN` clause naming the distinction the budgets now encode: re-attending
   to a *newly-arrived* blocker (a fresh conflict from a sibling merge, a new failing
   job, a new comment) is the expected chase to green and is **not** avoidance;
   re-doing *already-finished* work (re-resolving resolved threads, a phantom
   conflict) is. Name the single clean terminal explicitly: when
   `observe_pr_readiness` reports ready-and-waiting, the move is the `[ready]`
   stand-down — completed work handed to Thomas, recorded `partial`, never `success`.
   Token-pinned in `tests/test_remediation_prompt.py` alongside the existing clauses.
   - *Depends on:* 3.
   - *Acceptance:* the golden-string test asserts the `EXPECTED-CHURN` clause
     distinguishes new-blocker re-attention from finished-work repetition and names
     the `[ready]` terminal; the existing clause assertions still pass.

## Testing Strategy

- **Phase 1** — `tests/test_pr_readiness.py`: a table over each blocker dimension
  asserting `ready_and_waiting` is `True` only when all are clear and the PR is open;
  the `branch_landed` case (already merged) is **not** ready-and-waiting; provenance
  and clock are stamped from injected stubs. No real git or network.
- **Phase 2** — `tests/test_agent.py`: stub the five oracles; assert the per-iteration
  memo (second consult hits the memo, oracles not re-invoked) and TTL re-observation;
  cover the flag-off branch (gate inert).
- **Phase 3 gate** — `tests/test_loop_pr_remediation.py`: a parity replay of recorded
  `pending_pr_requests` proves byte-identical dispositions with the flag off; with it
  on, a ready-and-waiting PR is `[ready]`-skipped and the same PR with a fresh thread
  on the same head re-admits. Assert the `[ready]` outcome is `partial`, never
  `success`.
- **Phase 3 budget** — `tests/test_pr_lap_budget.py` / `tests/test_escalation_bridge.py`:
  drive a PR past the gross budget while clearing one blocker per lap and assert zero
  handoffs; drive a PR on identical observations past the no-progress budget and assert
  exactly one handoff and one durable standdown; assert the flag-off path keeps the
  gross-count trigger.
- **Phase 4** — `tests/test_audit_checks.py`: assert the `PR-READY` / `PR-STUCK` lines
  appear under exactly their conditions and that a healthily-churning PR yields no
  line; `tests/test_remediation_prompt.py` golden-strings the `EXPECTED-CHURN` clause.
- All new config flags get a `tests/test_config.py` round-trip and default assertion,
  per the flag-lifecycle convention. All tests use `mem_fs`, mock the host/git oracles
  via the seams the merge-truth tests already use, and make no real I/O, network,
  subprocess, or `sleep`.

## Risk Assessment

- **Phase 1** is pure addition consumed by nobody; zero behavioural risk.
- **Phase 2** carries the observation cost — a readiness observation composes a CI
  rollup call, a review-thread query, and an overflow scan. Mitigated by the
  per-iteration memo and TTL (one observation per PR per iteration), and by the
  default-off gate flag: nothing observes readiness until Phase 3's gate is enabled.
- **Phase 3 gate** changes which items the dequeue chain skips. The danger is
  **standing down too early** — declaring ready-and-waiting while a blocker is in fact
  open. Mitigated by deriving the fact from the *same* host-API observers the existing
  gates trust (`get_pr_check_conclusion`, `_eligible_bug_comments`,
  `_eligible_overflow_bug_findings`, `is_pr_settled`), by recomputing live each
  dequeue so a reappearing blocker re-admits immediately, and by the parity-gated flag.
  Rollback is the flag.
- **Phase 3 budget** changes when the merge-or-close handoff fires. The danger is the
  inverse — **never escalating** a PR that is genuinely stuck but keeps producing a
  cosmetic blocker change. Mitigated by `pr_readiness_improved` requiring a
  *code-clearable* blocker to actually clear (not merely a head change), and by keeping
  the gross-count trigger one flag-flip away. The opposite failure (escalating a
  healthy PR) is the bug this phase removes.
- **Phase 4** is pure display plus a prompt-clause addition; its only risk is prompt
  drift, pinned by the golden-string test.
- **Trust chain:** none of these phases touch approvals, the action gate, or the
  sole-pusher invariant — Tend remains the only pusher and every code change still
  requires Thomas's merge. The `[ready]` stand-down is a *skip and a message-free
  record*, not an auto-merge; the only outward escalation (Phase 3) reuses the
  existing one-knock bridge.

## Out of scope

- **An AST single-door for readiness.** Mirroring 087's Phase 3 chokepoint guard —
  forbidding any merge/CI/thread decision outside `observe_pr_readiness` — is the
  natural hardening once the fact proves out, but is deferred until a second consumer
  exists to justify the guard.
- **LLM-judged thread actionability.** Whether an inline thread is *genuinely*
  actionable stays with the existing `_eligible_bug_comments` heuristics (terminal
  markers, AI/human discrimination); replacing that with a per-thread LLM judgment is
  a separate, costlier increment.
- **Reframing the broader self-model frustration.** This proposal reframes churn at
  the PR-remediation surface only. Whether the same observed-vs-narrated move should
  temper other places the loop reports necessary work as failure is left to a
  follow-up once this instance lands.
