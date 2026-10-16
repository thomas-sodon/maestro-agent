# 131: Attempt Disposition Oracle — One Owner of What Happens When an Implementation Doesn't Reach Green, Adding Resume-With-Memory and Ask-The-Human to the Give-Up Terminal

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:observing |
| **Affects** | `maestro/agent.py`, `maestro/evolution.py`, `maestro/memory.py`, `maestro/types.py`, new `maestro/attempt_disposition.py`, `maestro/config.py`, `maestro/config_settings.py`, `maestro/toml_config.py`, `maestro/flag_lifecycle.py`, `maestro.toml`, `tests/test_structural_guards.py`, tests |
| **Depends on** | 110 _(shipped)_, 124 _(burn-in:observing)_ |
| **What landed** | Step 1 — the pure disposition oracle: new `maestro/attempt_disposition.py` (`classify_attempt` / `AttemptDisposition` / `AttemptVerdict`) with full unit coverage in `tests/test_attempt_disposition.py`. Step 5 — the dormant data model: `EvolutionPlan.resume_count` / `EvolutionPlan.blocked_question_id` and the `blocked` `PlanStatus`, excluded from every live/pending/eligibility selector exactly as `sliced`. Steps 3–4 — the `RESUME` arm: `implement_plan`'s non-inherited stuck terminal routes through `classify_attempt`, and on a strictly-progressing stall preserves the evolution branch, persists the failing baseline plus `resume_branch_head` and an incremented `resume_count` (with `failure_count` frozen) via `update_plan_resume`, while `evolution.implement` checks out the preserved branch, merges `origin/main`, and prepends `_resume_preamble`; behind `[loop] attempt_disposition_enabled` (registered in `EXPERIMENTAL_FLAGS`, default-off), with `max_resume_attempts` bounding continuations. Step 6 — the `ASK_HUMAN` arm: on a recurring non-code-fixable environment red (`classify_attempt` → `ASK_HUMAN`), the same non-inherited stuck terminal asks Thomas one question via `ask_thomas` and moves the plan to the inert `blocked` status keyed to that question's `message_id` (persisted through `update_plan_status`'s new `blocked_question_id`), spending no `failure_count` and opening no GitHub issue; a blank or unreachable message channel falls through to today's demote so no plan is stranded `blocked`. Flag-gated alongside `RESUME`. Step 7 — the answer-threaded re-admission: an inbound reply from Thomas that threads (via its `reply_to`) back to a `blocked` plan's `blocked_question_id` disposes of the plan in `_process_messages` — `_readmit_blocked_plan` returns it to `approved` carrying the answer forward as `blocked_answer` (which `implement` prepends via `_blocked_answer_preamble`, then clears), while an explicit abandonment reply (`_is_abandonment_reply`) retires it `obsolete` via `_abandon_blocked_plan`; persisted before the reply is marked read so the transition is crash-idempotent. Flag-gated. Step 2 — the structural chokepoint guard: `_ATTEMPT_DISPOSITION_GUARD` pins `attempt_disposition.py` as the sole definer of `classify_attempt` / `AttemptDisposition` / `AttemptVerdict` and, via a `terminals_consult_disposition_oracle` `co_occurrences` requirement, binds every `implement_plan` demote/hold terminal to an oracle consult (registry cap 24→25); runs regardless of the flag. Step 8 — the terminal collapse: the four post-implementation `_demote_plan_to_failed` terminals (`self_review`, uncalled-symbol gate, `converge_to_green` "stuck", the non-transient SDK-error handler) and the inline `INHERITED` hold now route through `classify_attempt`, reaching `_demote_plan_to_failed` only on `ABANDON` and the hold only on `HOLD_INHERITED`; the three observation-less terminals assert the oracle can only ABANDON. The pre-implementation scope gate is a deliberate carve-out (it demotes via `update_plan_status` before any attempt runs, so it is not an attempt-disposition terminal). Flag-off every observable effect is byte-identical to the pre-131 scattered terminals. |
| **What is missing** | Observing the tripwires, then collapsing `[loop] attempt_disposition_enabled` via `CUTOVER_FLAGS` and deleting the dead flag-off branch (Step 9). The production `classify_attempt` call now handles every verdict — `RESUME`, `ASK_HUMAN`, answer-threaded re-admission, `HOLD_INHERITED`, and `ABANDON` — at every post-implementation terminal, and the flag is now enabled in `MAESTRO_ENV=production` only as a `PROD_ONLY_CANARIES` canary (base `maestro.toml` stays default-off), opening the observation window; graduating it to a base default and the cutover decommission remain. |
| **Derived from** | live production data via /propose, 2026-09-09 |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

Proposal 110 gave Maestro one owner of "grind this branch to green" and two
honest in-session terminals: **green** (open the PR) or **no-progress stuck**
(fail loud, demote). Proposal 124 made the stuck detector attribution-aware so
it stops blaming the plan for the base's red. Neither changed what happens
*after* a stuck terminal — and that is where the concern that prompted this
proposal lives: plans keep failing implementation, and the agent never iterates
until it succeeds nor pauses to ask its human a question. It does the opposite.
When the in-session grind hits the ceiling, the branch is thrown away, the plan
is marked `failed`, and a *later* iteration re-runs `implement` with a prompt
**byte-identical to the first attempt** — no record of what was tried, no gate
output, no diff-so-far. Three cold restarts of the same wall, then a generic
GitHub issue, then abandonment. There is no third terminal for "blocked on a
decision only Thomas can make," even though the `ask_thomas` primitive already
exists and is wired only to the forced-arm handoff/retirement actuators.

This proposal introduces the successor terminal owner: a single **attempt
disposition oracle** that every non-green exit of `implement_plan` routes
through. It reads the structured gate fact 124 already produces plus the plan's
own history, and returns exactly one verdict from a closed vocabulary —
`LAND` / `RESUME` / `ASK_HUMAN` / `HOLD_INHERITED` / `ABANDON`. Two of those are
new capabilities the concern demands. `RESUME` keeps the branch and carries the
failing-identity set, the gate-output tail, and the diff-so-far into the next
admission's prompt, so a stalled-but-*progressing* plan continues where it left
off instead of cold-restarting — "iterate until it succeeds." `ASK_HUMAN`
formulates one specific question via `ask_thomas`, moves the plan to a new
`blocked` status keyed to that question's `message_id`, and stops burning
attempts; when Thomas answers, the reply threading already in `messaging.py`
re-admits the plan with the answer injected — "or has a question for its human."

The end state: the ceiling backstop becomes a checkpoint rather than a cliff.
The six scattered demote/hold sites in `implement_plan` collapse into one owner
whose default disposition for a progressing-but-unfinished attempt is to
continue, whose disposition for a human-decidable block is to ask, and whose
`ABANDON` — the only path that still spends the plan and opens an issue — is the
rare exception, not the reflex.

## Relevant Findings

- `maestro/evolution.py` (`implement`) — the implementation prompt is built once
  from `plan.title`, `plan.rationale`, `plan.proposed_changes`, and
  `plan.files_affected`. It reads nothing from `last_failure_reason` or any prior
  attempt, and `implement` deletes any stale evolution branch and recreates it
  fresh from `main` (or the `source_task_id` base). A retry is therefore a
  cold-start amnesiac replay, not a continuation.
- `maestro/agent.py` (`implement_plan`) — the post-attempt terminal is a binary:
  green falls through to `create_pr`; every non-green exit calls
  `_demote_plan_to_failed` (scope-gate rejection, `self_review` returning
  `False`, the uncalled-symbol gate, `converge_to_green` returning `"stuck"`, and
  the outer SDK-error handler) with one narrow special case — 124's `INHERITED`
  inline `hold` branch — bolted on. There is no `ASK_HUMAN`/`RESUME` disposition.
- `maestro/agent.py` (`_implement_approved_plans`) — a `failed` plan is retry-
  eligible only while `failure_count < max_implementation_attempts` (default
  `3`, `maestro/config.py`). Once exhausted the plan is terminal `failed`
  forever; nothing re-admits it and nothing carried its learning forward.
- `maestro/agent.py` (`_demote_plan_to_failed`) — on the terminal attempt it
  opens a generic `[Maestro] Implementation failed` / `Quality gate failed`
  GitHub issue with the raw gate tail. This notifies the human but asks no
  answerable question and pauses no specific plan.
- `maestro/agent.py` (`ask_thomas`) — a working agent→human question channel
  (`Message` with `message_type="question"`, saved and pushed via
  `notifier.ask_thomas`). Its only in-tree callers are the forced-arm
  `deliver_handoff` / `deliver_retirement_disposition` actuators; the
  implementation stuck path never calls it.
- `maestro/messaging.py` (reply resolution) — replies are already threaded by
  `reply_to`, and the resolution already special-cases
  `message_type == "question"` (the inbound poll that feeds it,
  `maestro/notifications/telegram.py`'s `get_updates`, is a separate seam). This
  is where an `ASK_HUMAN` re-admission binds; no new inbound channel is needed.
- `maestro/gate_convergence.py` (`plan_next_round`, `plan_next_review_round`,
  `ConvergenceResult`) — the pure stuck detectors already distinguish
  strictly-shrinking failing sets ("continue") from stalls ("stuck") and carry
  the `Attribution` verdict. The oracle consumes these outputs; it does not
  re-derive them.
- `maestro/types.py` (`PlanStatus`) — a closed `Literal`; `sliced` (proposal 118)
  is the precedent for a status deliberately excluded from the eligibility and
  live-plan allowlists. `blocked` follows the same pattern.
- `tests/test_structural_guards.py` (`ChokepointGuard`) — the house mechanism for
  pinning a single owner (used for `is_pr_settled`, `ci_diagnosis`,
  `gate_observation`). The disposition owner is enforced the same way.

## Production Evidence

Live data via the host bind-mount at `/opt/maestro/data` (loop
container `maestro-loop`), strictly read-only. Window **2026-08-16 → 2026-09-09**
(24 days) — the full extent of `evolution_plans.jsonl` on disk.

**Attempts spent versus PRs produced** (`evolution_plans.jsonl`):

- 11 plans are terminal `failed`; together they consumed **29 implementation
  attempts** and produced **0 PRs** — every attempt discarded.
- Over the same window 17 plans `merged` and 11 terminal-`failed` — ~39% of
  plans that reached the implement lineage and terminated did so as failures,
  most burning the full 3-attempt budget.
- Turn-share-weighted implement-lineage SDK spend (`usage.jsonl`, callers
  `implement_plan` / `self_review` / `auto_fix` / `replan_for_scope` /
  `assess_criteria_outcome`) is ≈ **$—** over the 24 days, corroborating 124's
  independently-measured "$— of implement-lineage spend per 7 days
  producing zero merges."

**Failure classes** (`evolution_plans.jsonl`, all 14 rows with `failure_count > 0`):

```
 5  Quality gate failed: Ruff clean / Mypy clean / Tests: could not run   (env/infra)
 3  Self-review did not converge to LGTM (no-progress stall or ceiling reached)
 2  Quality gate failed: …StarletteDeprecationWarning…                    (warning-as-fail)
 2  Quality gate failed: tests:TIMEOUT
 1  Quality gate failed: (other)
 1  Implementation failed: CLIJSONDecodeError: JSON message exceeded max   (SDK transport)
```

**8 of 14** failing plans had a root that no amount of code-editing iteration can
fix — an environment/collection failure ("could not run" ×5), a warning treated
as a hard gate failure (×2), or an SDK-transport error (×1). Each still burned
its full 3-attempt cold-start budget grinding a wall the diff did not cause.

**The agent never asked** (`messages.jsonl`): of 422 messages, Maestro sent 10
`question`-type messages to Thomas. **All 10** are "Handoff — PR #N …" or
"Retirement — …" from the forced arms. **Zero** are "I am blocked implementing
plan X; here is the decision I need." The concern's "or has a question for its
human" has literally never happened for a stuck implementation.

## Prior Point Fixes (whack-a-mole ledger)

| # | Identifier | What it patched | Why the class survived |
|---|---|---|---|
| 1 | Proposal 110 (shipped, PRs #3349–#3393) | Replaced the fixed `auto_fix_max_rounds` / "proceed without LGTM" terminals with `converge_to_green` + a progress predicate + generous ceiling | Made the grind *persistent within one session*, but its "stuck" terminal still demotes and discards; the ceiling is a cliff, and the next attempt is a cold restart |
| 2 | Proposal 124 (burn-in:observing) | Attributed the red (`INHERITED` vs the diff's), added an inline `hold` so an inherited red never spends an approval or an attempt | Fixed *whose fault* the red is; did not change what a genuinely-stuck attempt does next, added a *second* bespoke terminal branch, and explicitly left the transient-SDK path and the "iterate/ask" gap out of scope |
| 3 | `4ecf4afc` — plan-scope gate false-fails (#3769) | Stopped the scope gate rejecting a plan on a symbol/paired-test verification mismatch | One false-positive demote source removed; the demote *terminal* it fed is unchanged and still cold-restarts on the true positives |
| 4 | plans `16c8bc2f` / `0c26cf49` (each failed ×3, never landed) | Proposed `pytest.importorskip` / `maestro gate-check --full` to remove the aborting collection error | Never landed — the broken gate demoted the very fix meant to remove it; three cold restarts could not escape a block that was never the diff's to fix |
| 5 | `max_implementation_attempts` (config, default 3) | Bounds cold-restart retries so a bad plan cannot loop forever | A count cap on amnesiac replays; raising it multiplies waste, lowering it abandons sooner — neither adds memory or an ask |

Entries 1 and 2 are this proposal's direct parents: it reuses their machinery
and changes only the terminal disposition. Entry 2's inline `INHERITED` hold is
**retired** by folding it into the oracle's `HOLD_INHERITED` disposition (Step 8).
Entry 4 is the shape the design makes non-fatal: `RESUME` carries the collection
error forward and `ASK_HUMAN` surfaces it as a decision instead of burning three
blind retries. Entry 5's cold-restart retry is **retired** by `RESUME`'s
context-carrying re-admission (Step 3–4).

## Implementation Plan

### Phase A — the disposition owner (no behaviour change)

1. [x] **The disposition vocabulary + pure classifier.** new
   `maestro/attempt_disposition.py` — a frozen `AttemptDisposition` carrying a
   closed `verdict: Literal["LAND","RESUME","ASK_HUMAN","HOLD_INHERITED","ABANDON"]`
   plus the fields each verdict needs (`question: str | None`,
   `carry_context: bool`, `reason: str`), and a pure `classify_attempt(...)`
   that reads the `ConvergenceResult` / `GateRunObservation` / `Attribution`
   (proposal 124), the plan's `failure_count` / `inherited_hold_count` /
   `resume_count`, and the resume budget. Zero I/O, zero SDK.
   - *Depends on:* none.
   - *Acceptance:* a unit test proves each distinct input shape maps to exactly
     one verdict (progressing-at-ceiling → `RESUME`; recurring-unchanged
     env/collection/`TIMEOUT` red → `ASK_HUMAN`; `INHERITED` within budget →
     `HOLD_INHERITED`; no progress + budget spent → `ABANDON`; all-clean →
     `LAND`), with guard-branch coverage.
2. [x] **Structural chokepoint guard.** `tests/structural_guards.py` — a
   `ChokepointGuard` (`_ATTEMPT_DISPOSITION_GUARD`) pinning
   `maestro/attempt_disposition.py` as the sole definer of `classify_attempt` /
   `AttemptDisposition` / `AttemptVerdict`, and — through a
   `terminals_consult_disposition_oracle` `co_occurrences` requirement — asserting
   every `implement_plan` terminal that demotes (`_demote_plan_to_failed`) or holds
   (`update_plan_inherited_hold`) also consults `classify_attempt` in that scope.
   The registry cap rises 24→25 (`tests/test_structural_guards.py`). Runs
   regardless of the flag.
   - *Depends on:* 1.
   - *Acceptance:* a synthetic `implement_plan` that calls `_demote_plan_to_failed`
     without a disposition verdict reds the guard; the live tree is clean now that
     Step 8 has landed.

### Phase B — RESUME: context-carrying re-admission (flag-gated)

3. [x] **Preserve the branch and persist the carry-context on `RESUME`.**
   `maestro/agent.py` (`implement_plan`), `maestro/memory.py` (`EvolutionPlan`,
   `update_plan_status`) — when `classify_attempt` returns `RESUME`, do **not**
   `cleanup_branch`; persist the sorted failing-identity set, the
   `last_failure_reason`, and the branch head under a new `resume_count` bounded
   by `[loop] max_resume_attempts`, and do **not** increment `failure_count`
   (progress is not failure). Fully behind `[loop] attempt_disposition_enabled`.
   - *Depends on:* 1.
   - *Acceptance:* a stalled-but-strictly-progressing plan re-admits with its
     branch intact and `resume_count` incremented (not `failure_count`); a
     `RESUME` past `max_resume_attempts` degrades to `ABANDON`.
4. [x] **Inject the carry-context into the implement/converge prompt.**
   `maestro/evolution.py` (`implement`, `converge_to_green`) — when a re-admitted
   plan carries resume context, prepend the prior failing-identity set, the gate
   output tail, and a "continue from the existing branch HEAD; do not restart"
   instruction to the prompt, under the same context-bloat caps. Flag-off the
   prompt is byte-identical to today's static build.
   - *Depends on:* 3.
   - *Acceptance:* a resumption prompt contains the prior failing identities and
     bases on the preserved branch; the flag-off prompt is unchanged (asserted
     against the current static string).

### Phase C — ASK_HUMAN: question-and-pause (flag-gated)

5. [x] **The `blocked` plan status.** `maestro/types.py` (`PlanStatus`),
   `maestro/memory.py` (`EvolutionPlan.blocked_question_id`, `update_plan_status`)
   — add `blocked`, excluded from `_implement_approved_plans` eligibility and the
   `LIVE_STATUSES` / pending allowlists exactly as `sliced` is, so a blocked plan
   is inert until re-admitted.
   - *Depends on:* none.
   - *Acceptance:* a `blocked` plan is skipped by `_implement_approved_plans` and
     absent from the pending/live corpora; `VALID_PLAN_STATUSES` picks it up via
     `get_args(PlanStatus)`.
6. [x] **Wire `ASK_HUMAN` to `ask_thomas` and the `blocked` disposition.**
   `maestro/agent.py` (`implement_plan`) — on an `ASK_HUMAN` verdict, call
   `ask_thomas(question)`, store the returned `message_id` on the plan as
   `blocked_question_id`, set status `blocked`, and **do not** open the generic
   terminal GitHub issue or spend a `failure_count` for this class. Flag-gated.
   - *Depends on:* 1, 5.
   - *Acceptance:* a human-decidable stall produces exactly one `question`
     message and one `blocked` plan and consumes no `failure_count`.
7. [x] **Re-admit a `blocked` plan on Thomas's answer.** `maestro/agent.py`
   (the inbound-reply path in `_respond_to_messages`), `maestro/messaging.py` —
   when an inbound reply threads (`reply_to`) to a `blocked` plan's
   `blocked_question_id`, clear `blocked` and re-admit the plan with the answer
   injected as resume context (reusing Step 4's carry-context channel).
   - *Depends on:* 4, 6.
   - *Acceptance:* a reply to the question moves the plan off `blocked` and its
     next implement prompt contains the answer; an unanswered blocked plan stays
     inert.

### Phase D — retire the moles, then collapse

8. [x] **Route every scattered terminal through the oracle; fold in the
   inherited hold.** `maestro/agent.py` (`implement_plan`) — the four
   post-implementation `_demote_plan_to_failed` terminals (`self_review` false,
   uncalled-symbol gate, `converge_to_green` "stuck", and the non-transient
   SDK-error handler) and the inline `INHERITED` hold now route through
   `classify_attempt`; `_demote_plan_to_failed` is reached only via an `ABANDON`
   disposition, and the inherited hold via `HOLD_INHERITED`. The three
   observation-less terminals assert the oracle can only return `ABANDON` (fail
   loud rather than silently skip the demote). The pre-implementation scope gate
   is a deliberate carve-out: it rejects a plan *before* any attempt runs and
   demotes via `update_plan_status(..., "failed")` (not `_demote_plan_to_failed`),
   so it is not an attempt-disposition terminal and stays outside the oracle and
   the Step-2 guard's trigger set.
   - *Depends on:* 3, 6.
   - *Acceptance:* the Step-2 guard is green against the live tree; the only
     caller of `_demote_plan_to_failed` inside `implement_plan` is the `ABANDON`
     arm.
9. [ ] **Flip on, observe, collapse.** `maestro/config.py`,
   `maestro/config_settings.py`, `maestro/toml_config.py`, `maestro.toml`,
   `maestro/flag_lifecycle.py` — register `[loop] attempt_disposition_enabled`
   in `EXPERIMENTAL_FLAGS` with a dated `review_by`; flip on in one reversible
   step; observe the tripwires (below); collapse via `CUTOVER_FLAGS`, deleting
   the flag and the dead flag-off branch.
   - *Depends on:* 3, 4, 6, 7, 8.
   - *Acceptance:* the flag is registered with a `review_by`; after collapse the
     flag and its off-branch are gone and the cutover tripwires are green.

## Testing Strategy

All tests honour CLAUDE.md principles — `mem_fs` for file I/O, mocked SDK /
notifier / VCS / code-host, no real network, no real `sleep`.

- **`tests/test_attempt_disposition.py`** (new, unit) — the pure classifier: one
  case per verdict plus the guards (budget-exhausted `RESUME`→`ABANDON`,
  non-recurring red does not `ASK_HUMAN`, `INHERITED` within/over budget). No I/O.
- **`tests/test_structural_guards.py`** — the new `ChokepointGuard` (Step 2):
  the seeded negative control (a demote without a verdict reds it) and the
  live-tree clean scan. This is the named deterministic enforcement test.
- **Component (`tests/test_agent.py`, `tests/component/`)** — `RESUME` preserves
  the branch and increments `resume_count` not `failure_count`; the resumption
  prompt carries the prior failing identities (Step 3–4). `ASK_HUMAN` sends
  exactly one `question`, sets `blocked`, opens no issue, spends no
  `failure_count` (Step 6). A threaded reply re-admits the blocked plan with the
  answer injected (Step 7). Flag-off: `implement_plan` is byte-identical to
  today (demote path, static prompt, no `blocked`/`RESUME`).
- **`tests/test_memory.py` / `tests/test_dashboard.py`** — `blocked` round-trips
  through `update_plan_status`, is excluded from eligibility and the live/pending
  corpora, and is accepted by `VALID_PLAN_STATUSES` (Step 5).

## Risk Assessment

- **Over-asking (Thomas flooded with questions).** Mitigated by binding
  `ASK_HUMAN` to a closed set of human-decidable signals (a recurring-unchanged
  env/collection/`TIMEOUT` red across ≥2 attempts, or a self-review stall citing
  acceptance-criteria ambiguity) and at most one question per plan per block; a
  blocked plan is inert until answered, so it cannot re-ask.
- **`RESUME` looping without converging.** Bounded by `[loop] max_resume_attempts`
  and gated on *measured strict progress* each resume (the failing-identity set
  must shrink); a resume that does not progress degrades to `ABANDON`. The budget
  is self-extinguishing.
- **A preserved branch drifting from `origin/main`.** `RESUME` re-admission
  merges `origin/main` before grinding, exactly as `implement`'s
  `source_task_id` path already does; on conflict it falls back to a fresh branch
  (losing the memory channel but never wedging).
- **`blocked` leaking into a surface that treats it as actionable.** Mitigated by
  mirroring `sliced`'s allowlist exclusions and the `test_memory` / `test_dashboard`
  coverage above.
- **Rollback.** Every behaviour change is behind `[loop] attempt_disposition_enabled`
  (default-off, four-stage flag lifecycle). Flag-off, `implement_plan` and
  `implement` are byte-identical to today. The Phase-A oracle and its guard carry
  no behaviour and are always safe. No state migration: `resume_count` and
  `blocked_question_id` default to zero/`None` and normalise on read (schema
  fingerprint bump per `ensure_schema()`), and `blocked` is additive to the
  `PlanStatus` `Literal`.

## Falsifiability

The design is right if, over `evolution_plans.jsonl` after the flip:

- the terminal-`failed` count and attempts-per-failed-plan **fall**, and
  `merged` rises (previously-stalled-but-progressing plans now converge via
  `RESUME`), lifting merges-per-implement-dollar (the 124 metric); and
- a new `blocked` cohort shows a high **answer→re-admit→merge** conversion, with
  the non-code-fixable classes (the 8/14 above) surfacing one question rather
  than burning three attempts.

It has failed if `blocked` plans accumulate unanswered (the questions are not
answerable or Thomas is overwhelmed), or if `RESUME` re-admissions grind without
the failing set shrinking (the progress signal lies) — either would show as a
rising blocked/resume backlog with no merge lift.

## Out of scope

- **Why the environment red happens** ("Tests: could not run", `TIMEOUT`). The
  provisioning/CI root is operational — proposal 125's territory and `maestro
  gate-check`. This proposal only stops that class from silently eating three
  cold-start attempts; it does not repair the environment.
- **The SDK-*classified* transient path** — an `SDKError` whose category is
  `cli_crash` / `stream_stall` / `stale_session`. These are re-raised for
  iteration-level resume-in-place before any terminal, so the disposition oracle
  never sees them; 124 already named this class out of scope. This is the
  classified path only: an **unwrapped** `CLIJSONDecodeError` is *not* in it — it
  reaches `implement_plan`'s generic `except Exception` as a non-`SDKError`, is
  not classified transient, and is exactly the single terminal-`failed` row in
  the Production Evidence above (which is why it burned its cold-start budget
  instead of resuming). Step 8 routes that generic demote through the oracle, so
  the oracle **does** own this transport class and disposes of it as one of the
  non-code-fixable failures (`ASK_HUMAN` / `RESUME`, never a blind 3× cold-restart
  `ABANDON`). Only *repairing the transport root itself* is out of scope, and
  stays operational.
- **Why plans that cannot be implemented get generated.** Governing the
  generator is proposals 116 and 120; this proposal governs only what happens
  once a plan reaches the implement lineage.
