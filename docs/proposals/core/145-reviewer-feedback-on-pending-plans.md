# 145: Reviewer Feedback on Pending Plans — A Third Decision Between Approve and Reject, Owned by One Ledger and Serviced by the Curator

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | burn-in:observing |
| **Affects** | `maestro/plan_feedback.py` (new), `maestro/memory.py`, `maestro/evolution.py`, `maestro/prompts/curate_plan.py`, `maestro/audit_checks.py`, `maestro/loop/lifecycle.py`, `maestro/agent.py`, `maestro/notifications/telegram.py`, `maestro/dashboard/routes.py`, `maestro/dashboard/static/index.js`, `maestro/config.py`, `maestro/config_settings.py`, `maestro/flag_lifecycle.py`, `maestro.toml`, `docs/self-evolution.md`, `CLAUDE.md`, tests |
| **Depends on** | 060 (the `curate_pending_plans` phase this feeds), 021 (plan content-hash binding the refined plan re-binds to), 131 (the shipped precedent for folding a threaded Thomas reply into a plan row inside `_process_messages`) |
| **Derived from** | live production data via /propose, 2026-10-03 (no live data available — evidence is code + git history only) |
| **What landed** | Step 1 — `maestro/plan_feedback.py` (`PlanFeedback`, `coerce_feedback`, `unaddressed_feedback`, `has_unaddressed_feedback`, the four caps), `EvolutionPlan.reviewer_feedback` (excluded from `compute_plan_content_hash`, coerced in `_parse_plan_line`), `Memory.add_plan_feedback` / `Memory.mark_feedback_addressed`, `[loop] plan_feedback_enabled` (default `false`) registered in `EXPERIMENTAL_FLAGS`. Step 2 — `select_curation_target(prioritize_feedback=...)` puts unaddressed-feedback plans first past the grace floor, wired from `EvolutionEngine.curate_pending_plans` and `MaestroAgent._curate_pending_plans` (PR #4455). Steps 3–4 — `curate_pending_plans` appends `render_feedback_section` for unaddressed feedback (prompt body via `render_curate_prompt`), and `maestro/plan_feedback_curation.py::curate_with_feedback` requires a `response`, downgrades `obsolete` to `keep`, stamps only the rendered entries addressed, and counts failed passes per entry via `Memory.record_feedback_attempt`, releasing each at `MAX_FEEDBACK_CURATE_ATTEMPTS` with `FEEDBACK_UNSERVICEABLE_RESPONSE`. Step 5 — `LifecycleController.phase_will_run` fires `curate_pending_plans` off-cadence under `plan_feedback_enabled and curate_enabled` while a cached `any_pending_feedback` bit holds (re-read at startup and after each curate pass, or on `invalidate_feedback_pending`), keeping smoke exclusion, the burn-rate gate, and `_last_phase_iteration` dedup (PR #4477). Step 6 — review bodies lead with a `[plan <id>@<hash prefix>]` marker (`format_plan_ref`, composed in `maestro/plan_review_message.py`); `_poll_loop` passes `parse_plan_ref`'s result to `on_message`, `build_telegram_inbound_message` saves a `plan_feedback` row when the flag, curation, and focus allow a pass to service it, and `_process_messages` folds those via `plan_feedback_fold.fold_feedback_messages` into `Memory.add_plan_feedback` (idempotent on `feedback_id`), acknowledging once and invalidating the feedback-pending bit. Step 7 — `POST /api/plans/{plan_id}/feedback` (`PlanFeedbackBody`, `extra="forbid"`) writes only a `plan_feedback` `Message` via `build_dashboard_feedback_message`, refusing with 409 when `feedback_serviceable` is false or the unaddressed cap is reached; the pending card gains a "Request changes" control (`requestPlanChanges`), a "Feedback pending" badge, and the latest response (`renderPlanFeedback`) (PR #4488). Step 8 — the `### Reviewer feedback on pending plans` subsection in `docs/self-evolution.md`, the extended **Bidirectional messaging** invariant in `CLAUDE.md`, and the static guard `tests/test_plan_feedback_doc.py`. |
| **What is missing** | Step 9 — the observation window, running via the `maestro.production.toml` canary enabling `plan_feedback_enabled` (criteria in the "Reviewer feedback canary (proposal 145)" subsection of `docs/self-evolution.md`), the falsifiability reading, and graduating the flag. |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

A reviewer looking at a pending evolution plan has exactly two moves: approve it
as written or reject it. When a plan is mostly right — wrong acceptance
criterion, one change too many, a missed file — the only way to say so is to
reject it with a reason. The plan then dies, the reason goes to the
rejection classifier (`learn_from_rejection`), which turns it into a general
self-model lesson rather than an edit to that plan, and the corrected
re-proposal Maestro mints next is refused for seven days by the rejection
cooldown, because it restates the plan just rejected. That restatement is
exactly what the reviewer asked for. On Telegram it is worse: the Reject
button records the fixed string `"Rejected via Telegram"`, and a text reply
to the plan's review message lands in `messages.jsonl` as an unthreaded
`instruction`, which `_process_messages` then appends to
`SelfModel.pending_user_requests` — so it becomes a top-priority task stripped
of the plan it was about, rather than an edit to that plan.

This proposal adds a third decision, **request changes**. Reviewer feedback
becomes a persisted, per-plan ledger on `EvolutionPlan`, written through one
`Memory` door that only accepts `pending_review` plans. Both surfaces deliver
feedback as a `Message` threaded to the plan — the dashboard (a "Request
changes" textarea on each pending plan card) writes a message, keeping it a
thin status layer, and Telegram does the same for a text reply to the plan's
review message. The loop folds such a message into the plan's ledger inside
`_process_messages`, exactly as proposal 131 already folds a reply to a
blocked plan's question into `blocked_answer` (`_readmit_blocked_plan`).
All new logic lives in a new `maestro/plan_feedback.py`, so the size-pinned
`agent.py` / `evolution.py` gain only call sites. The existing
`curate_pending_plans` phase services the feedback: a plan with unaddressed
feedback goes to the front of `select_curation_target`, skips the
min-age grace period, and makes the phase fire on the next iteration
regardless of `[loop] curate_every`. It still requires
`[loop] curate_enabled` and still pauses under the burn-rate throttle. The
curator prompt gets a "Reviewer feedback — address it" section. Every verdict
on a plan with feedback must carry a short `response` to the reviewer, and
the entries are stamped addressed only after the verdict is applied. A
curator `obsolete` on a plan with feedback is downgraded to `keep` plus the
response: only the human may retire a plan they asked to change.

End state: the reviewer writes "drop change 3, and criterion 2 should be
measurable", and within one iteration a refined plan arrives on Telegram and
the dashboard, preceded by a separate message carrying Maestro's response; its
Approve/Reject buttons are bound (proposal 021) to the new content hash. The
plan was never rejected, so neither the rejection cooldown nor rejection
learning ever sees it.

## Relevant Findings

- `maestro/evolution.py` (`EvolutionEngine.curate_pending_plans`) — the only
  production caller of `Memory.refine_plan`. It renders `CURATE_PLAN_PROMPT`
  from the plan body alone, so no human input reaches it, and it applies a
  `refine` / `obsolete` / `keep` verdict through `_apply_curate_refine` /
  `_apply_curate_obsolete`. A `keep` only calls `Memory.touch_plan_curated`.
- `maestro/evolution.py` (`_apply_curate_refine`) — already re-reads the plan
  after `refine_plan`, re-stamps `notified_content_hash`, and sends
  `Notifier.notify_plan_refined` with the refined body (#4146). The feedback
  path reuses that tail unchanged and prepends the reviewer response.
- `maestro/prompts/curate_plan.py` (`CURATE_PLAN_PROMPT`) — tells the model to
  "prefer `keep` when in doubt" and to "not broaden the scope". Both are right
  for autonomous curation and wrong when a human has asked for a specific
  change, so the feedback section has to override them explicitly.
- `maestro/audit_checks.py` (`select_curation_target`) — orders pending plans
  by `(refined_at is not None, last_curated_at, created_at, plan_id)` and drops
  plans younger than `loop_curate_min_plan_age_hours`. A plan the human is
  actively waiting on can therefore sit behind the whole never-refined
  backlog.
- `maestro/loop/lifecycle.py` (`LifecycleController.phase_will_run`, the
  `curate_pending_plans` arm) — fires only when `curate_enabled and
  curate_every > 0 and iteration % curate_every == 0`, deduped through
  `_last_phase_iteration`. The phase is in `_BUDGET_GATED_PHASES`, so the
  burn-rate gate skips it earlier in the same function.
- `maestro/memory.py` (`Memory.refine_plan`) — the validated write boundary
  for `rationale` / `proposed_changes` / `acceptance_criteria`, refusing
  anything not `pending_review` under `file_lock`. It does not accept `title`
  or `files_affected` (see Out of scope).
- `maestro/memory.py` (`compute_plan_content_hash`) — hashes reviewable body
  fields only, and bookkeeping (`refined_at`, `last_curated_at`) is excluded.
  A feedback ledger is bookkeeping in the same sense: adding feedback must not
  invalidate the hash a reviewer is about to approve.
- `maestro/memory.py` (`Memory._parse_plan_line`) — filters raw keys to
  `fields(EvolutionPlan)` and coerces individual fields. A nested list of
  records needs one coercion branch there (the `purpose.py`
  `_coerce_evidence` precedent), and legacy rows default to an empty list.
- `maestro/evolution.py` (`submit_for_review`, rejection cooldown M1) — refuses
  for `_REJECTION_COOLDOWN_DAYS` a plan whose title matches a rejected plan,
  or whose content `is_plan_restatement` matches one. A corrected re-mint of a
  plan rejected as a change request trips this by construction.
- `maestro/plan_decision.py` (`telegram_plan_handlers`, `on_reject`) — records
  `rejection_reason="Rejected via Telegram"`, so Telegram rejections carry no
  reviewer signal at all.
- `maestro/agent.py` (`run_loop`, the `_on_message` closure passed to
  `TelegramNotifier.start_approval_polling`) — a Telegram text reply is saved
  as a `Message` with `reply_to` set only when `parse_question_ref` finds an
  `ask_thomas` `[ref …]` marker. A reply to a plan message matches nothing and
  becomes an unthreaded `instruction`, which `_process_messages` appends to
  `SelfModel.pending_user_requests`.
- `maestro/agent.py` (`_process_messages`, `_readmit_blocked_plan`) — proposal
  131's shipped precedent: a Thomas reply whose `reply_to` matches a blocked
  plan's question is folded into that plan row (`blocked_answer`, capped at
  `message_max_length`) and never enqueued. Plan feedback reuses this shape.
- `maestro/loop/lifecycle.py` (`_FOCUS_SUPPRESSED_PHASES`) —
  `curate_pending_plans` is suppressed under the `external_delivery` focus
  profile, so feedback accepted there would never be serviced.
- `maestro/notifications/telegram.py` (`TelegramNotifier._truncate`) — cuts
  over-long messages from the end, and `_compose_review_message` does not cap
  `proposed_changes`, which is why `ask_thomas` refs lead the text
  (`_QUESTION_REF_RE` is anchored at `^`).
- `maestro/evolution.py` (`_compose_review_message`) — the single source of the
  plan review body for `submit_for_review`, `promote_slice`, and the curator's
  refine notice. The body does not include the `plan_id`, so a reply cannot
  name its plan. `TelegramNotifier._plan_messages` maps plan to message id,
  but it is in-memory and lost on restart, so it cannot be the binding.
- `maestro/dashboard/routes.py` (`reject_plan_action`, `PlanRejectBody`,
  `_raise_decision_loss`) — the established shape for a plan-decision route
  (the feedback route reuses its validation but writes only a `Message`):
  `extra="forbid"` body with a required `content_hash`, fast 404/400 before
  reading the body, a locked re-check, and 409 on content drift. Requests
  already pass through the auth-gated CSRF and `MutationRateLimit`
  middleware.
- `maestro/dashboard/static/index.js` (pending plan card render, `rejectPlan`,
  the delegated `data-action` click dispatch) — the pattern a "Request
  changes" control plugs into.

## Production Evidence

No live data available. No `maestro-loop` container exists on the authoring
host, and the repo-local `data/` has no `lifecycle.json`, so the evidence is
code and git history only. The quantification the proposal needs comes from
the falsifiability reading in Step 9 once the canary runs. Before opening the
canary, read the baseline from the production `evolution_plans.jsonl`:
human-rejection rate over the trailing 30 days, and the share of those
rejections whose `rejection_reason` names a concrete edit rather than a
verdict on the idea. `maestro measure-rejection-classes --json` covers the
already-shipped / false-premise / not-needed classes and is the existing
sensor for the rest of the rejection mix.

## Prior Point Fixes (whack-a-mole ledger)

Every entry below hardens the **reject → re-mint** path. That path is the
only way a reviewer can steer a plan today, so it absorbs what are really
change requests:

| Identifier | What it patched | Why the class survived |
|---|---|---|
| `a4da7de0` | Widened the rejection cooldown from exact-title to content-restatement (`is_plan_restatement` against within-cooldown rejected plans) | Correctly blocks re-mint floods, and with them the corrected re-mint a change-request rejection asked for |
| `522e2182` (#4068) | Timed the rejection cooldown from a PR close, not the approval | Cooldown timing, not the missing request-changes channel |
| `4999806c` (#3803) | Gave rejection learning its own high-effort self-model session | Learning from rejections works better, but it still generalises an edit into a lesson instead of applying it |
| `fc20c4e8` | Narrowed the rejection-classifier swallow to transient SDK failures | Same path. Classifier robustness |
| `af9fe72f` | Auto-obsoleted stale `pending_review` plans past an age horizon | Plans the reviewer neither approves nor rejects age out, because no move between the two exists |
| `015f0cbf`, `1371f6e6` (#4104) | Bound curator refine output to the acceptance-criteria count and item caps | Curator hardening for autonomous rewrites, which run blind to the reviewer |

This is a modest ledger. The fixes are correct and none of them is the bug.
They show the pressure the missing channel puts on adjacent machinery, not
repeated failed attempts at this feature. The design **retires none of them**:
the cooldown and classifier keep their job for genuine rejections. What it
retires is the use of rejection as a change request, and Step 9 measures
whether that use falls.

## The failure class

**A reviewer's only write into a pending plan is a terminal decision, so
plan-shaping input has no owner. It either kills the plan (reject) or is
discarded (approve as-is, or a Telegram reply that lands unthreaded in the
inbox).**

Manifestations and how the design closes each:

1. **Reject-as-change-request kills a salvageable plan.** Closed by the
   feedback ledger plus feedback-driven refine (Steps 1, 3, 4): the plan
   stays `pending_review` and is edited in place.
2. **The corrected re-mint is refused by the rejection cooldown.** Closed
   because no rejection occurs, so the M1 cooldown never sees the plan.
3. **Rejection learning generalises a plan-specific edit into a self-model
   lesson.** Closed for the same reason. Feedback reaches the curator for
   that plan only and never enters `learn_from_rejection`.
4. **A Telegram reply to a plan message lands unthreaded in
   `messages.jsonl` and is enqueued as a context-free user request.** Closed
   by the plan-ref marker in `_compose_review_message` plus reply threading
   and the `_process_messages` fold (Step 6).
5. **The curator refines blind to the reviewer and may `keep` a plan the
   human wants changed.** Closed by the prompt section and the
   response-required verdict contract (Step 3).
6. **A plan the human is waiting on can sit behind the curation backlog or
   the cadence.** Closed by selector priority and the off-cadence trigger
   (Steps 2, 5).
7. **Telegram rejections carry no reason** (`"Rejected via Telegram"`).
   Partially closed: a Telegram reviewer can now reply with feedback instead
   of rejecting. Reasoned Telegram *rejections* are listed under Out of scope.

## Implementation Plan

### Phase 1 — Ledger and curator (dark)

1. [x] **Feedback ledger on `EvolutionPlan` and one write door.**
   `maestro/plan_feedback.py` (new), `maestro/memory.py`, `maestro/config.py`,
   `maestro/config_settings.py`, `maestro/flag_lifecycle.py`, `maestro.toml`.
   In `plan_feedback.py` define the `PlanFeedback` record (`feedback_id`,
   `text`, `submitted_at`, `source` ∈ `dashboard`/`telegram`,
   `content_hash_seen` — the hash *prefix* the reviewer saw, always truncated
   to `CALLBACK_HASH_PREFIX_LEN` so both surfaces compare alike —
   `curate_attempts`, `addressed_at`, `response`), its coercer, and the caps
   as module constants (Memory takes no config): `FEEDBACK_TEXT_MAX_CHARS =
   1000`, `FEEDBACK_RESPONSE_MAX_CHARS = 500`, `MAX_UNADDRESSED_FEEDBACK = 3`,
   `MAX_ADDRESSED_FEEDBACK_KEPT = 3`. Add
   `EvolutionPlan.reviewer_feedback: list[PlanFeedback]`, excluded from
   `compute_plan_content_hash` with a docstring line saying why, coerced in
   `_parse_plan_line`. Add
   `Memory.add_plan_feedback(plan_id, text, *, source, content_hash_seen)`.
   It works under `file_lock`, refuses anything not `pending_review`, caps
   `text` at `FEEDBACK_TEXT_MAX_CHARS`, and refuses a fourth *unaddressed*
   entry, so the prompt input stays bounded. Add
   `Memory.mark_feedback_addressed(plan_id, feedback_ids, response)`, which
   stamps only the ids it is given and then drops addressed entries beyond the
   newest `MAX_ADDRESSED_FEEDBACK_KEPT`, so the row cannot grow without bound.
   Register `[loop] plan_feedback_enabled` (default `false`) in
   `EXPERIMENTAL_FLAGS` with a `review_by` horizon.
   - *Depends on:* none.
   - *Acceptance:* `add_plan_feedback` on a `pending_review` plan persists one
     entry and leaves `compute_plan_content_hash(plan)` unchanged. On an
     `approved` plan it returns `False` without writing. A fourth unaddressed
     entry is refused. A legacy row with no `reviewer_feedback` key loads as
     `[]`.

2. [x] **Selector priority for unaddressed feedback.**
   `maestro/audit_checks.py` (`select_curation_target`). Plans with any
   entry where `addressed_at is None` sort first and skip the
   `min_age_hours` grace period, oldest unaddressed `submitted_at` first.
   With no feedback anywhere the existing ordering is unchanged. The single
   predicate `has_unaddressed_feedback(plan)` lives in `plan_feedback.py`
   and Steps 3, 5, and 6 reuse it.
   - *Depends on:* 1.
   - *Acceptance:* given one never-refined old plan and one refined plan with
     unaddressed feedback created a minute ago (under a 24 h grace),
     the selector returns the feedback plan. With no feedback present its
     output matches the pre-change ordering on the existing test fixtures.

3. [x] **Curator consumes feedback and must answer it.**
   `maestro/prompts/curate_plan.py`, `maestro/evolution.py`
   (`curate_pending_plans`, `_apply_curate_refine`, `_apply_curate_obsolete`).
   `maestro/plan_feedback.py` renders the section and validates the verdict;
   `evolution.py` gains only the call sites. When the target has unaddressed
   feedback, render a `Reviewer feedback — address it` section with each
   entry's text and timestamp, the whole section capped at
   `FEEDBACK_TEXT_MAX_CHARS × MAX_UNADDRESSED_FEEDBACK`. Entries written
   against an earlier body (where `content_hash_seen` is not a prefix of the
   current hash) are flagged as such, never refused. The section
   overrides "prefer keep" and "do not broaden scope" only as far as the
   feedback asks. The verdict schema gains `response` (≤
   `FEEDBACK_RESPONSE_MAX_CHARS`), required whenever feedback was
   rendered. An `obsolete` verdict on a plan with rendered feedback is
   downgraded to `keep` with the response: the curator may not retire a plan
   the human asked to change, so `_apply_curate_obsolete` never runs on it.
   After the verdict is applied, call `mark_feedback_addressed`
   with the ids that were rendered, so feedback arriving mid-call stays
   unaddressed for the next pass. The response always goes out as its own
   `Notifier.notify` message sent before any refine notice, so the existing
   `notify_plan_refined` message (#4146 hash-field visibility) is unchanged;
   on a `keep` the hash is unchanged and existing buttons stay valid. A
   feedback verdict missing `response` is logged at WARNING and treated as
   malformed: no write, the cursor is touched, the feedback stays
   unaddressed, and Step 4's retry bound applies. Without feedback the prompt and the code path
   are byte-identical to today.
   - *Depends on:* 1, 2.
   - *Acceptance:* with an SDK mock returning
     `{"action":"refine","acceptance_criteria":[…],"response":"…"}` for a plan
     with one unaddressed entry, the plan is refined, the entry is stamped
     with that response, and a response notification precedes the unchanged
     refined notification. An `obsolete` verdict with feedback leaves the plan
     `pending_review`. The same mock without `response` leaves the plan and
     the entry untouched.

4. [x] **Bounded retry for unserviceable feedback.** `maestro/plan_feedback.py`,
   `maestro/memory.py`. Increment `PlanFeedback.curate_attempts` per rendered
   entry under `file_lock` on each malformed or failed pass. After
   three malformed or failed passes, stamp the entries addressed with a
   fixed system response ("Maestro could not apply this feedback — approve,
   reject, or rephrase") and notify. Selector priority then cannot pin
   one plan at the head forever.
   - *Depends on:* 3.
   - *Acceptance:* three consecutive malformed verdicts on the same entry
     produce exactly one system-response notification, and the fourth
     selection no longer prioritises that plan.

5. [x] **Off-cadence trigger.** `maestro/loop/lifecycle.py`
   (`phase_will_run`, `curate_pending_plans` arm), `maestro/plan_feedback.py`. `phase_will_run` must not read the plans file
   every iteration, so it reads an in-process
   `feedback_pending` bit: set by the Step 6 fold, cleared once a pass
   leaves no plan satisfying `has_unaddressed_feedback`, and recomputed once
   at startup. When `plan_feedback_enabled and curate_enabled` and the bit
   is set, the arm returns `True` regardless of `curate_every`. It keeps `--smoke`
   exclusion, the burn-rate gate above it, and `_last_phase_iteration`
   dedup. The phase body's existing zero-cost early return (no target, no
   session) is unchanged.
   - *Depends on:* 2.
   - *Acceptance:* the `phase_will_run` matrix gains the cases
     feedback-pending + off-cadence ⇒ run, feedback-pending +
     `curate_enabled=false` ⇒ skip, feedback-pending + throttle ⇒ skip,
     and same-iteration re-entry ⇒ skip. With the flag off every
     pre-existing case is unchanged.

### Phase 2 — Surfaces

6. [x] **Telegram threading and the message fold.** `maestro/plan_review_message.py`
   (the review-body compose, extracted from `maestro/evolution.py`), `maestro/notifications/telegram.py`,
   `maestro/agent.py` (`_on_message`, `_process_messages`),
   `maestro/plan_feedback.py`. Put a `[plan <plan_id>@<hash prefix>]` marker
   in the review body's *heading line*, not its tail, because
   `TelegramNotifier._truncate` cuts from the end; the prefix length is
   `CALLBACK_HASH_PREFIX_LEN`. Add `parse_plan_ref` in `plan_feedback.py`,
   and have `_poll_loop` pass the parsed plan ref to `on_message` — still
   after the `_authorized_chat` check — which saves the reply as a
   `plan_feedback` `Message` threaded to the plan. In `_process_messages`, a
   Thomas `plan_feedback` message is folded (one `plan_feedback_fold.fold_feedback_messages`
   call, mirroring the proposal-131 `_readmit_blocked_plan` branch) into
   `add_plan_feedback`, sets the `feedback_pending` bit, acknowledges by
   `notify` ("Feedback recorded — Maestro will revise plan X") or names the
   refusal (not pending, too many pending entries), and is never enqueued.
   With the flag off, with curation off or focus-suppressed (no pass would
   service it), or with no plan ref, the reply is saved and processed
   exactly as today.
   - *Depends on:* 1.
   - *Acceptance:* an authorised reply whose `reply_to_message.text` carries
     a plan marker records one feedback entry after `_process_messages` and
     adds no `pending_user_requests` entry. A review body longer than the
     Telegram limit still carries the marker after truncation. An
     unauthorised reply records nothing. With the flag off the same reply
     yields today's `instruction` row and enqueue.

7. [x] **Dashboard endpoint and UI.** `maestro/dashboard/routes.py`,
   `maestro/dashboard/static/index.js`. Add
   `POST /api/plans/{plan_id}/feedback` with a `PlanFeedbackBody`
   (`content_hash`, `text`; `extra="forbid"`). The dashboard stays a thin
   layer: the route writes only a `plan_feedback` `Message` (the same write
   it already makes for messages), and the loop's Step 6 fold owns the
   plan-row write. It follows `reject_plan_action`'s validation shape: fast
   404, 400 for a plan not `pending_review` or for empty or over-long text.
   A stale `content_hash` is accepted and recorded as `content_hash_seen`,
   flagged in the prompt like Telegram's — one stale-hash rule for both
   surfaces. It returns 409 with an explanatory error when
   `plan_feedback_enabled` or `curate_enabled` is off, or when the active
   focus profile lists `curate_pending_plans` in `_FOCUS_SUPPRESSED_PHASES`,
   so feedback never queues where nothing will service it. The pending card gains a
   "Request changes" button that opens a textarea dialog through the
   existing confirm-dialog helper, and a "feedback pending" badge plus the
   latest response once one exists.
   - *Depends on:* 1.
   - *Acceptance:* a route test posts feedback for a pending plan (200, one
     `Message`, no plan-row write), with a stale hash (200, hash recorded),
     for an approved plan (400), with the flag off or under
     `external_delivery` (409, nothing written). A browser test submits feedback
     from the card and sees the pending badge.

### Phase 3 — Canary, measure, graduate

8. [x] **Docs and invariant.** `docs/self-evolution.md`, `CLAUDE.md`. Add a
   "Reviewer feedback on pending plans" subsection: ledger, trigger,
   response contract, retry bound. Add one line to the **Bidirectional
   messaging** invariant noting that a `plan_feedback` message is folded
   into the plan's feedback ledger instead of being enqueued. The
   **Dashboard as thin status layer** invariant needs no change, because the
   route writes only a message.
   - *Depends on:* 6, 7.
   - *Acceptance:* the CLAUDE.md line names the routing rule and points at
     the docs subsection. `tests/test_no_line_citations_in_docs.py` passes.

9. [ ] **Canary and falsifiability reading.** `maestro.production.toml`,
   `maestro/flag_lifecycle.py` (`PROD_ONLY_CANARIES`). Enable
   `plan_feedback_enabled` in production for a window of at least 30
   human-decided plans. Read two numbers from `evolution_plans.jsonl`: the
   approval rate of plans that received feedback, and the overall
   human-rejection rate against the pre-canary baseline from Production
   Evidence. Graduate to the base default and add a `CUTOVER_FLAGS` entry
   only if the tripwires hold.
   - *Depends on:* 3, 4, 5, 6, 7.
   - *Acceptance:* the canary entry carries a written justification and the
     two tripwires below, and the graduation PR cites both readings.

## Testing Strategy

All tests use `mem_fs`, `SDKMock`, and mocked Telegram/HTTP. Nothing uses
real sleep, subprocesses, or network.

New assertions go to new files or to modules with size headroom, because
`tests/test_audit_checks.py` (like `agent.py` and `evolution.py`) is near
its `_SIZE_BASELINE` pin in `tests/test_source_file_size.py`.

- `tests/test_plan_feedback.py` (new) — `add_plan_feedback` covers the happy
  path, a non-pending refusal, the text cap, the unaddressed-count cap, and
  that the hash is unchanged. `mark_feedback_addressed` stamps only the given
  ids and prunes old addressed entries. Legacy-row coercion and round-trip
  through `_parse_plan_line`. `select_curation_target` puts feedback first,
  skips the grace period for feedback only, and keeps the no-feedback
  ordering identical (regression pin). `parse_plan_ref` positive, negative,
  malformed, and post-truncation cases.
- `tests/test_curate_phase.py` — refine/keep with feedback each stamp the
  entries and send the response notification; obsolete-with-feedback is
  downgraded to keep. A missing
  `response` produces no write and leaves the entry unaddressed. Feedback
  added during the SDK call stays unaddressed. The prompt is byte-identical
  when no feedback exists (golden). Three-strike system response.
- `tests/test_curate_phase.py` — the `phase_will_run` matrix rows from
  Step 5, including that the arm reads the bit, not the plans file.
- `tests/test_telegram.py` / `tests/test_telegram_approval.py` — reply
  threading when authorised, unauthorised, flag-off, and when the plan is no
  longer pending; the fold adds no `pending_user_requests` entry.
- `tests/test_dashboard_routes.py` — feedback route status matrix plus
  CSRF/auth coverage inherited from the middleware tests.
  `tests/browser/` — the card flow.
- Enforcement: the golden prompt test and the no-feedback selector
  regression pin are the deterministic guards that flag-off behaviour is
  unchanged. `tests/test_no_silenced_exceptions.py` covers the new notify
  call sites.

## Risk Assessment

- **Trust boundary.** Feedback is a new human-to-LLM input on two
  authenticated surfaces: dashboard bearer auth plus CSRF and the mutation
  rate limit, and Telegram `_authorized_chat`. It can only cause a rewrite of
  a still-`pending_review` plan, and the human still gives the final
  Approve/Reject on the rewritten body. That body is re-bound by content
  hash, so an approval can never land on content the reviewer did not see.
  No new path writes code, pushes, or approves.
- **Churn.** A reviewer could loop feedback → refine → feedback. Each loop
  costs one curate call. It is bounded by the three-unaddressed-entry cap,
  the three-strike retry bound, and the burn-rate gate, and every loop is
  human-initiated. The curator can never retire a plan with feedback.
- **Notification noise.** One acknowledgement per Telegram feedback and one
  response per serviced pass.
- **Hash drift between submit and service.** Feedback written against an
  earlier body is flagged in the prompt rather than refused, on both
  surfaces, because the reviewer's intent usually survives a refine.
- **Rollback.** `plan_feedback_enabled = false` restores today's behaviour:
  the selector, trigger, prompt, and Telegram routing are all byte-identical
  off, and the endpoint refuses. Stored `reviewer_feedback` entries stay
  inert on the plan row. No migration is needed, because the field defaults
  to `[]` on read.
- **Tripwires (Step 9).** Halt graduation if feedback-refined plans are
  approved no more often than the baseline approval rate. That would mean
  feedback is not producing acceptable rewrites. Also halt if the
  three-strike system response fires on more than 20% of feedback entries.
  That would mean the curator cannot act on real feedback, most likely
  because the change lies outside the fields `refine_plan` can rewrite (see
  Out of scope).

## Out of scope

- **Rewriting `title` or `files_affected` from feedback.** `refine_plan`
  rewrites three fields, and `files_affected` feeds `check_plan_scope` and the
  CLI-grounding guards, which run only at `submit_for_review`. Widening the
  refine boundary is a follow-up if the Step 9 three-strike rate shows that
  feedback often asks for file-set changes. Until then the curator answers
  such feedback with `keep` and a response that says so.
- **Reasoned Telegram rejections** (manifestation 7). The Reject button keeps
  recording `"Rejected via Telegram"`. Collecting a reason through a reply is
  a separate small change.
- **Feedback on `approved` or `implemented` plans or open PRs.** PR review
  threads are the channel there, already serviced by remediation.
- **Feeding addressed feedback into rejection learning or the self-model.**
  The ledger is per-plan by design. Folding it into reflection would recreate
  manifestation 3.
