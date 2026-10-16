# 091: PR Delivery Truth — retire the "awaiting tend push" costume and stop orphaning open PRs

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/agent.py`, `maestro/evolution.py`, `maestro/pr_remediation_state.py`, `maestro/remediation_tracker.py`, `maestro/loop/pr_remediation.py`, `maestro/pr_readiness.py`, `maestro/memory.py`, `maestro/vcs.py`, `maestro/consciousness.py`, `maestro/config.py`, `maestro.toml`, tests |
| **What landed** | Step 1 — push-outcome truth: `push_delivered` / `push_error` on `TaskRecord` (`maestro/memory.py`), recorded by §5-push's fetch-and-retry-once path with a `PUSH FAILED` line in `result_summary` on genuine failure (`maestro/agent.py`). Step 2 — the marker split: `Marker.DELIVERED_AWAITING_MERGE` ("delivered to pr, awaiting review/merge") in `maestro/pr_remediation_state.py`, added to `_FETCH_REQUIRED_NARRATIVES` (`maestro/remediation_tracker.py`), leaving `AWAITING_TEND_PUSH` to mean only the undelivered case. Step 3 — tend keys on the open PR, not the live plan: `tend_evolution_prs` (`maestro/evolution.py`) admits an open `maestro/evolve/*` PR whose plan has left `implemented` (retired/obsolete) behind `[loop] tend_planless_prs_enabled` (default on; `maestro/config.py` / `maestro/toml_config.py` / `maestro.toml`), covered by `tests/test_evolution_tend.py`. Step 4 — retiring an open-PR plan requires a delivered disposition: `sweep_orphaned_cures` (`maestro/memory.py`, the sole `allow_implemented=True` caller) fires `mark_plan_obsolete` only after the injected `deliver_disposition` callable returns `True`; the `_deliver_disposition` closure (`maestro/agent.py`) routes to `deliver_retirement_disposition` (`maestro/escalation_bridge.py`), which lands a close-or-keep PR comment + durable Thomas message + `pr_number`-keyed standdown, so a `False` return leaves the plan `implemented` and tend keeps servicing it. Step 5 — "serviced" means reply confirmed delivered: `mark_comment_ids_serviced` now fires only in §4c-ii's reply-delivery loop over the confirmed `delivered_ids` (`maestro/agent.py`), no longer at `route_feedback`'s force-admit enqueue, and `reconcile_serviced_comment_ids` (`maestro/escalation_bridge.py`) drops falsely-stamped ids at tend time (`maestro/loop/pr_remediation.py`). Step 6 — thread resolution: `resolve_review_thread` (GraphQL `resolveReviewThread`) on `SubprocessCodeHost` (`maestro/vcs.py`), invoked by run()'s §5-push-ii loop over `_resolvable_thread_ids` (`maestro/evolution.py`) behind `[loop] resolve_addressed_threads_enabled` (default on). Step 7 — user messages referencing a PR get the remediation plumbing: `PrRemediationController.route_user_pr_message` (`maestro/loop/pr_remediation.py`) checks out the PR branch and releases its stand-down verdicts (`release_stand_downs`, `maestro/remediation_tracker.py`) + escalation standdown (`clear_standdown`, `maestro/escalation_bridge.py`), wired into `_run_task_phase` (`maestro/agent.py`) behind `[loop] user_pr_message_remediation_enabled` (default on). Step 8 — stand-downs refused while owned thread-work remains: `MERGE_BLOCK_THREADS_UNRESOLVED` / `classify_owned_blocker` (`maestro/remediation_tracker.py`) name the owned `THREADS_UNRESOLVED` blocker, and the chosen and mechanical stand-down writers refuse while `observe_pr_readiness` (`maestro/pr_readiness.py`) reports unresolved threads (`[loop] threads_block_standdown_enabled`) or a `pending_user_requests` entry names the PR (`[loop] user_request_blocks_standdown_enabled`), both default on. |
| **What is missing** | None — all eight steps have landed. |
| **Derived from** | Live incident data 2026-06-25 → 2026-07-07: `task_history.jsonl` (28 consecutive `partial` "awaiting tend push" remediation laps across PRs #2808–#2884), `stand_down_verdicts.jsonl`, `escalation_handoffs.json`, `lever_attempts.jsonl`, `evolution_plans.jsonl`, git reflogs of `maestro/evolve/fb29ca933941`, and the GitHub state of open PRs #2884/#2886/#2892 |

## Description

> Status updated to shipped on 2026-07-11 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

The operator asked why Maestro waited for the Tend push instead of pushing
itself. The live
data answers: **Maestro was pushing.** The reflog of `maestro/evolve/fb29ca933941`
(PR #2884) shows seven pushes on 2026-07-03 alone. What Maestro was not doing was
telling the truth about it — and then, one by one, its own anti-treadmill
mechanisms severed every remaining path by which the PR could ever be finished.

The phrase "Resolved on branch, awaiting tend push" is stamped by `run()`'s
host-state gate whenever a remediation lap finishes on a PR that is not yet
*merged into main*. Merging requires human approval, so the condition is
structurally false for every mid-review PR: even a lap that delivered its
commits, replied to threads, and left CI green records `partial` + "awaiting
tend push". The narration reads as "I didn't deliver" when delivery succeeded —
and the misnamed marker then feeds the costume-signature counters, the
FORCED-QUIET ratio, and ultimately the self-model, which learned that the only
"owned terminal act" left was to stand down or retire the plan. On 2026-07-04
the agent fired `mark_plan_obsolete` on the *implemented* plan behind open PR
#2884 (journaled as "Owned act landed at my keystroke"); the mechanical
`sweep_orphaned_cures` did the same to the plans behind open PRs #2886 and
#2892. Because `tend_evolution_prs` iterates *plans*, all three open PRs became
invisible to the tend system: no more merge-main freshening, no comment
routing, no ready-for-review pings, no reply-completion — zombie PRs, open on
GitHub, dead inside Maestro, with a silent "the PR stays open for human
disposition" note that no human was ever sent.

The end state after this proposal: a remediation lap's recorded verdict states
what was actually delivered (pushed / replied / resolved) and what was not; an
open evolution PR can never fall outside the tend set regardless of plan
status; "serviced" can only mean "reply confirmed delivered"; addressed threads
get resolved on GitHub instead of rotting `isResolved: false`; a human message
referencing a PR overrides every treadmill-suppression gate instead of being
answered with a stand-down verdict; and retiring a plan whose PR is open
requires an explicit, delivered disposition handoff.

## Live evidence (read 2026-07-07, before the c0e3ca2e deploy)

Why each open PR is stuck:

- **PR #2884** (`maestro/evolve/fb29ca933941`, "Enable progress-aware lap budget
  by default") — head `272ea704`, `BEHIND` main by ~48 commits, CI all green,
  no approval, 3 review threads all `isResolved: false` and all `isOutdated: true`.
  Thread `3520501289` and `3520774044` carry Maestro replies (delivered by the
  loop's reply-manifest path on the 07-03 laps); thread `3520622254` (a 🐛
  finding posted 14:53, mid-lap) **has zero replies** — it was born between lap
  admissions, went outdated when tend pushed, and `_eligible_bug_comments`
  drops outdated threads, so no lap's problem statement ever contained it. Yet
  after the escalation handoff of 07-03 17:38 seeded the standdown (the
  deliverer seeds `serviced_comment_ids` empty), the next force-admit
  admission blanket-stamped all three ids into `serviced_comment_ids`
  (`route_feedback` marks every currently-unserviced id at enqueue, before any
  reply is delivered), permanently closing the one-lap escape valve (#2798).
  Two stand-down verdicts for the current head were then authored **during
  Thomas's complaint tasks** ("PR2884 has unresolved comments" → verdict
  07-06 18:01; "PR2884 is still not ready for approval" → verdict 07-07 11:05).
  The plan was retired obsolete on 07-04. Content note: the 07-03 remediation
  commit ("Keep progress-aware lap budget OFF — revert counterproductive
  default flip") reverted the PR's own headline change to satisfy the config
  🐛 finding; the surviving diff vs main is a config comment plus tests — the
  PR no longer does what its title says.
- **PR #2886 / #2892** (`maestro/evolve/1b541589a55b`, `c7685744a558`) — both
  `DIRTY` (conflicting) since birth on 07-03, zero review threads, **zero
  remediation laps ever ran** (conflict-cooldown skipped them all evening
  07-03, per the loop log; their tend-route admissions never won a task-phase
  pop). Their plans were retired by `sweep_orphaned_cures` with the reason
  "the PR stays open for human disposition" — a disposition request that was
  never posted to the PR nor messaged to Thomas. Since retirement, tend logs
  contain no further mention of either PR.
- **PR #2902 / #2908** — authored by `thomas-sodon`, not `maestro/evolve/*`
  branches; correctly outside the tend/remediation scope. Not stuck on Maestro.

Why the fixes already deployed on 2026-07-07 (`c0e3ca2e`) do not resolve this:
the reply-completion route (#2893) and the mechanical stand-down repair (#2865)
both ride `tend_evolution_prs`, which never visits a retired-plan PR, and the
#2798 valve they honor keys on `serviced_comment_ids`, which already (wrongly)
contains the unreplied thread. The narration neutralizer (#2866) sanitizes
prose but delivers nothing.

## Relevant Findings

- `maestro/agent.py` (`run()` §4c host-state stamp gate) — on a finished
  remediation lap it downgrades `success` → `partial` and authors
  `Marker.AWAITING_TEND_PUSH` whenever `is_pr_settled(pr).branch_landed` is
  false, i.e. whenever the PR has not merged to main. This runs **before** the
  §5 commit and §5 push, so the recorded verdict cannot reflect whether
  delivery succeeded; "awaiting tend push" is stamped on laps whose push then
  lands seconds later.
- `maestro/agent.py` (§5 push, `SubprocessVCS.push`) — the direct-to-PR-branch
  push (#2817) fires only when `branch_name == pr_branch` and outcome is
  `success`/`partial`; any exception is caught, logged at WARNING ("leaving
  delivery to Tend PR"), and **never surfaces into the task record**. There is
  no retry and no `push_delivered` fact persisted.
- `maestro/tool_gating.py` (`_git_deny`) — the task gate blocks `git push` to
  `main`/foreign refs but **allows** pushing the run's own `allowed_branch`,
  which for a remediation lap *is* the PR branch. "The LLM does not push" is a
  prompt convention (`_DELIVERY_NOTE`), not a gate constraint; delivery is by
  design owned by deterministic loop code.
- `maestro/evolution.py` (`tend_evolution_prs`) — iterates **plans** ("for
  every plan whose PR is still open upstream"). A plan retired via
  `Memory.mark_plan_obsolete` (agent-fired with `allow_implemented`, or the
  `sweep_orphaned_cures` actuator) exits the iteration set while its PR remains
  open. `_reconcile_orphan_branches` covers the inverse case only (remote
  branches *without* PRs), so an open PR with a retired plan is tended by
  nothing.
- `maestro/evolution.py` (`_tend_single_pr`, `_branch_has_local_commits_ahead`,
  `_has_unconsumed_awaiting_push_verdict`) — the mechanical freshen/push path
  works when it runs: the 07-03 reflog shows repeated merge-main + push cycles.
  All of it is unreachable for a planless open PR.
- `maestro/loop/pr_remediation.py` (`_eligible_bug_comments`,
  `read_thread_reply_manifest`) — outdated threads are excluded from fix
  routing; the reply manifest is delivered per-thread best-effort by `run()`
  §4c-ii. A thread that goes outdated before any lap sees it is never routed
  and never replied.
- `maestro/escalation_bridge.py` (`serviced_comment_ids`,
  `mark_comment_ids_serviced`) / `maestro/loop/pr_remediation.py`
  (`route_feedback`) — the handoff deliverers (`deliver_handoff`,
  `deliver_reality_refutation`) seed `serviced_comment_ids` empty by design
  (#2798); the sole stamping site is `route_feedback`'s force-admit enqueue,
  which marks every currently-unserviced id serviced at admission — before any
  reply is delivered, and including outdated threads that
  `_eligible_bug_comments` excludes from the lap's problem statement. The
  #2798 escape valve keys on this set, so an id stamped at admission but never
  actually replied to can never earn its reply lap.
- `maestro/remediation_tracker.py` (`_FORCED_QUIET_MARKERS`,
  `count_branch_resolved_partials`) — `Marker.AWAITING_TEND_PUSH` is counted as
  a *forced* quiet and as the branch-resolved costume signature. The costume
  counter already discounts delivered laps when its calling gate injects the
  `is_delivered` git-ancestry predicate (a recorded `pr_head_sha` that is a
  proper ancestor of the PR's current origin head proves Tend pushed that
  lap's resolution), so it is not delivery-blind; `_FORCED_QUIET_MARKERS` is —
  the FORCED-QUIET ratio reads a delivered lap and an undelivered lap
  identically, which is what let "deliver honestly" collapse into "stand
  down" in the self-model's incentive gradient.
- `maestro/pr_readiness.py` (`observe_pr_readiness`, `PRReadiness`) — blocker
  vocabulary classifies `AWAITING_APPROVAL` as external with no notion of
  "own review threads unresolved/unreplied", so a PR the human refuses to
  approve *because of unresolved threads* reads as external-only, which is
  precisely the precondition for chosen/mechanical stand-downs.
- `maestro/consciousness.py` / `maestro/agent.py` (`pending_user_requests`,
  `_run_task_phase`) — instruction messages run as generic tasks with
  `pr_branch=None`; none of the remediation plumbing (PR checkout, reply
  manifest delivery, §5 push, fetch-truth stamping) engages. The 07-04→07-07
  tasks spawned by Thomas's #2884 complaints produced artifacts, GitHub issues,
  and two stand-down verdicts — zero deliveries.
- `maestro/vcs.py` (`SubprocessCodeHost`) — has `reply_to_review_comment` but
  no thread-resolution capability; nothing in the system can flip
  `isResolved`, while the human's approval gate (and the dashboard's mental
  model) is thread resolution.
- Composition with 090's landed steps — the #2866 write-time neutralizer
  (`outcome_grounding.neutralize_unlanded_resolution_narration`) placeholder-
  protects every `Marker` enum member by iterating the enum, so step 2's
  `Marker.DELIVERED_AWAITING_MERGE` is auto-protected with no neutralizer
  change; `delivered` and `push` sit outside
  `narration_cues.RESOLUTION_KEYWORDS`, so the new delivery narrations cannot
  trip rules 13/15. Conversely, step 2's sensor reclassification shifts the
  FORCED-QUIET ratio that 090's remaining steps consume — re-baseline that
  mirror after the marker split lands.

## Implementation Plan

### Phase 1 — Verdicts tell delivery truth

1. [x] **Persist the push outcome and retry once.** `maestro/agent.py`,
   `maestro/memory.py` — §5 push: on failure, `fetch("origin")` and retry once;
   record `push_delivered: bool` (origin head advanced to local head) and
   `push_error: str | None` on the task record; append a loud
   `PUSH FAILED: <err>` line to `result_summary` when delivery genuinely
   failed. No behavior change on success beyond the recorded fact.
   - *Depends on:* none.
   - *Acceptance:* a remediation task whose push raises records
     `push_delivered=False` with a non-empty `push_error`, and its
     `result_summary` contains `PUSH FAILED`; a successful push records
     `push_delivered=True`.
2. [x] **Split the marker: delivered vs undelivered.**
   `maestro/pr_remediation_state.py`, `maestro/agent.py`,
   `maestro/remediation_tracker.py`, `maestro/evolution.py` — add
   `Marker.DELIVERED_AWAITING_MERGE` ("delivered to PR, awaiting review/merge").
   After §5 push, rewrite the §4c note: `push_delivered` →
   `DELIVERED_AWAITING_MERGE`; push failed/skipped → keep
   `AWAITING_TEND_PUSH` (which now means only what it says). Update consumers:
   `count_branch_resolved_partials` and `_FORCED_QUIET_MARKERS` treat only
   `AWAITING_TEND_PUSH` as the costume, reconciled with the existing
   `is_delivered` SHA discount so the marker split and the git-ancestry
   predicate never classify the same lap twice: the marker decides at write
   time, the predicate stays as the read-time backstop that discounts
   pre-split legacy `awaiting tend push` records whose push in fact landed;
   `_has_unconsumed_awaiting_push_verdict`
   matches both (a delivered-then-reset head still deserves the cooldown
   bypass); `_FETCH_REQUIRED_NARRATIVES` gains the new marker.
   - *Depends on:* 1.
   - *Acceptance:* a lap that pushes successfully never persists
     `awaiting tend push`; sensors count it under the delivered marker; a
     legacy `awaiting tend push` record with a delivered `pr_head_sha` is
     discounted exactly once (by the predicate, not double-counted against the
     marker), and `tests/test_remediation_tracker.py` covers both markers
     through the costume gate.

### Phase 2 — No zombie PRs

3. [x] **Tend keys on open PRs, not live plans.** `maestro/evolution.py` —
   derive the tend worklist from the code host's open `maestro/evolve/*` PRs
   (the `pr_states` snapshot `_reconcile_orphan_branches` already consumes),
   joining the plan record when one is live. A PR whose plan is retired or
   missing still receives the mechanical passes: merge-main freshening,
   reminders, bug-comment routing, reply-completion (#2893). Gate behind
   `[loop] tend_planless_prs_enabled` (default on).
   - *Depends on:* none.
   - *Acceptance:* with a plan forced to `obsolete` while its PR is open, a
     tend pass still merges `origin/main` into the branch and routes its
     un-replied threads (component test with mocked code host).
4. [x] **Retiring a plan with an open PR requires a delivered disposition.**
   `maestro/memory.py` (`mark_plan_obsolete`, `sweep_orphaned_cures`),
   `maestro/messaging.py` — when the plan's PR is
   open, the retire path must (a) post a PR comment stating the retirement and
   asking close-or-keep, (b) send Thomas a message with the same ask, and
   (c) record the pending disposition so tend keeps reminding until the PR is
   actually merged or closed. Refuse silent retirement.
   - *Depends on:* 3.
   - *Acceptance:* `mark_plan_obsolete` on an open-PR plan with the notifier
     mocked asserts both deliveries happened; the sweep's reason string "stays
     open for human disposition" can no longer be written without them.

### Phase 3 — Reply and resolution honesty

5. [x] **"Serviced" means "reply confirmed delivered".**
   `maestro/escalation_bridge.py`, `maestro/loop/pr_remediation.py`,
   `maestro/agent.py` §4c-ii — stamp a comment id serviced only when
   `reply_to_review_comment` returned success for it: move the
   `mark_comment_ids_serviced` call out of `route_feedback`'s force-admit
   enqueue (which today stamps every currently-unserviced id at admission,
   before any reply exists) into §4c-ii's reply-delivery loop, which stamps
   exactly the ids whose replies delivered. At tend time, reconcile: drop any
   serviced id whose thread shows no Maestro reply on GitHub, reopening the
   #2798 valve for it.
   - *Depends on:* none.
   - *Acceptance:* replaying the #2884 shape — three known threads, two
     delivered replies — yields a serviced set of exactly two, and the third
     id earns a reply-completion lap.
6. [x] **Resolve addressed threads on GitHub.** `maestro/vcs.py`
   (`SubprocessCodeHost`), `maestro/agent.py` — add
   `resolve_review_thread(thread_id)` (GraphQL `resolveReviewThread` via `gh`).
   After §4c-ii delivers a terminal reply for a thread whose remediation
   commit is on the branch, resolve the thread. Gate behind
   `[loop] resolve_addressed_threads_enabled` (default on; own PRs only).
   Human comments (non-bot authors) are never auto-resolved.
   - *Depends on:* 5.
   - *Acceptance:* a delivered reply with a landed fix flips the mocked
     thread to resolved; a thread with a human comment in it is left
     untouched.

### Phase 4 — The human outranks the treadmill gates

7. [x] **User messages referencing a PR get the remediation plumbing and
   clear suppressions.** `maestro/agent.py` (`_run_task_phase`),
   `maestro/escalation_bridge.py`, `maestro/remediation_tracker.py` — when an
   instruction message references an open own PR (`#NNNN` / `PRNNNN`), run it
   as a PR-remediation task (`pr_branch` set, fetch-truth, reply manifest,
   §5 push all active) and, first, release that PR's stand-down verdicts and
   escalation-ledger suppression: an explicit human demand is the strongest
   possible "not settled" signal.
   - *Depends on:* 1, 5.
   - *Acceptance:* a queued message "PR #N has unresolved comments" produces a
     task whose record carries the PR branch and delivery fields, and the
     stand-down store no longer holds a live verdict for that PR.
8. [x] **Stand-downs are refused while owned thread-work remains.**
   `maestro/pr_readiness.py`, `maestro/remediation_tracker.py`
   (`mechanical_standdown_verdict` and the verdict writer) — add
   `THREADS_UNRESOLVED` (own PR, un-replied or unresolved AI threads) to the
   blocker vocabulary as **owned/code-clearable**; the chosen and mechanical
   stand-down paths must refuse to author a verdict while it is present or
   while an unconsumed `pending_user_requests` entry references the PR.
   `AWAITING_APPROVAL` alone is external; "approval withheld because threads
   are open" no longer is.
   - *Depends on:* 6, 7.
   - *Acceptance:* with one un-replied thread mocked, both stand-down writers
     refuse (no row appended) and readiness reports the owned blocker; with
     all threads replied+resolved, the verdict writes as today.

## Testing Strategy

- Unit (all `mem_fs`, mocked VCS/code host, no network): marker split and
  sensor reclassification (`tests/test_remediation_tracker.py`); push
  retry/`push_delivered` persistence with a raising-then-passing VCS mock;
  serviced-set threading and GitHub reconciliation; stand-down writer refusal
  branches; readiness `THREADS_UNRESOLVED` classification; retire-notification
  refusal path.
- Component: a tend pass over a mocked host holding one open PR with a retired
  plan asserts freshen + comment routing + reply-completion all fire (step 3),
  and that a silent retire is impossible (step 4).
- Regression fixture reproducing the #2884 lineage: three threads (one born
  mid-lap, outdated), two delivered replies, blanket-stamped serviced set —
  asserts the post-proposal pipeline replies to the third, resolves the two
  addressed ones, and never authors a stand-down while any remain.
- Every step honors the fail-loud invariant: no new swallowed exceptions
  (`tests/test_no_silenced_exceptions.py` stays green).

## Risk Assessment

- **Auto-resolve is outward-facing.** Wrongly resolving a thread hides a
  reviewer's concern. Mitigations: only threads whose root author is the AI
  reviewer bot, only after a delivered terminal reply plus a landed commit,
  never when any human commented in the thread; `[loop]
  resolve_addressed_threads_enabled` is the rollback switch.
- **Tending planless PRs widens tend's blast radius.** A PR the operator
  deliberately parked would get freshened again. Mitigation: the step-4
  disposition ledger gives the operator an explicit "keep/close" answer that
  parks it; `[loop] tend_planless_prs_enabled` off restores today's behavior.
- **Marker split touches many sensors.** Miscounting could mute the costume
  gates that stop real treadmills. Mitigation: step 2 changes classification
  only where a push verifiably succeeded; undelivered laps keep today's
  costume semantics; tracker tests cover both markers through every gate.
- **Human-override releasing stand-downs could re-open a settled treadmill.**
  Bounded: release happens only for the specific PR named in a human message,
  which is already the escalation bridge's addressee — the human explicitly
  reclaimed it.
- Rollback posture: each phase is independently revertible; steps 3, 6 and 7
  are flag-gated; steps 1–2 are additive fields plus a marker rename with
  both spellings accepted on read.

## Out of scope

- Approving or merging PRs autonomously (human-in-the-loop evolution is a
  hard invariant).
- The `pending_review` backlog-pruning policy (37 plans) and the mint-ceiling
  work tracked by PR #2892's lineage.
- Disposition of the three currently stuck PRs themselves — recommended
  operator actions, from the live evidence: close #2884 (its default-flip was
  reverted on its own branch; the surviving diff is a comment plus tests for
  a flag that stays off — proposal 089's flag remains reachable by a fresh,
  honest plan), and close-or-relaunch #2886/#2892, which conflicted at birth
  and were never serviced. Step 4's handoff exists precisely so this decision
  reaches the operator explicitly next time.
