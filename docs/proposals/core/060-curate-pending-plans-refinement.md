# 060: `curate_pending_plans` — Refinement of Stale Pending Plans

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/evolution.py`, `maestro/agent.py`, `maestro/audit_checks.py`, `maestro/notifications/base.py`, `maestro/notifications/telegram.py`, `maestro/prompts/`, `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `docs/self-evolution.md`, `CLAUDE.md`, tests |
| **Depends on** | _(none — completes infrastructure already in `memory.py`)_ |
| **What landed** | Step 1: `select_curation_target()` in `maestro/audit_checks.py` (oldest-unrefined `pending_review` selector), with tests in `tests/test_audit_checks.py`. Step 2: `notify_plan_refined` notifier surface — `Notifier.notify_plan_refined` default in `maestro/notifications/base.py` and the Telegram override in `maestro/notifications/telegram.py`, with tests in `tests/test_notifications.py` and `tests/test_telegram.py`. Step 3: `EvolutionEngine.curate_pending_plans()` in `maestro/evolution.py` with its two verdict handlers (`_apply_curate_refine`, `_apply_curate_obsolete`), wired to call `Memory.refine_plan()` on refine verdicts, with tests in `tests/test_evolution.py`. Step 4: `CURATE_PLAN_PROMPT` in `maestro/prompts/curate_plan.py`, with tests in `tests/test_prompts.py`. Step 5: the `_curate_pending_plans` loop-phase body in `maestro/agent.py`, `"curate_pending_plans"` added to the phase-ordering tuple and `_BUDGET_GATED_PHASES`, the `_phase_will_run` opt-in cadence gate (`curate_enabled and curate_every > 0` with `_last_phase_iteration` dedup), and `curate_every`/`curate_enabled` threaded through `run`/`run_loop` and the slot-B dispatch table, with tests in `tests/test_curate_phase.py`. Step 6: the `[loop] curate_every` (default `0`) / `curate_enabled` (default `false`) config keys in `maestro/config.py` (`loop_curate_every`/`loop_curate_enabled` with validation) and `maestro/toml_config.py`, the commented defaults in `maestro.toml`, and the CLI wiring sourcing `run_loop`'s params from `Config` (PR #2556). Step 7: the "Pending-Plan Curation" section in `docs/self-evolution.md`, the one-line invariant in `CLAUDE.md`, the phase entry in `docs/loop-lifecycle.md`, and the `maestro.toml` note that the dashboard's existing `Refined:` column surfaces results with no UI change. |
| **What is missing** | _Nothing — all seven steps have landed._ |

## Description

`Memory.refine_plan()` already exists, stamps `refined_at`, and the dashboard
already renders `| Refined: <date>` when that field is set — but **no production
code path ever calls `refine_plan()`**. It is invoked only from tests. Three
separate comments in the tree (`memory.py`, `types.py`, `evolution.py`) defer the
caller to a "future `curate_pending_plans` phase" that was never built. The
consequence is that `refined_at` is always `None`, no refinement dates appear on
the dashboard, and stale `pending_review` plans accumulate without ever being
refreshed or self-pruned. The operator's question — *"is plan refinement
occurring?"* — has a definite answer today: **no.**

This proposal builds that missing phase. `curate_pending_plans` is a cadence-gated
loop phase that selects the **oldest unrefined** `pending_review` plan, asks the
LLM to either improve its `rationale` / `proposed_changes` / `acceptance_criteria`
or recommend it `obsolete`, applies the verdict through the already-validated
`refine_plan()` / `mark_plan_obsolete()` write boundary, journals the action, and
**alerts Thomas** that a pending plan's body changed. The end state: pending plans
get one bounded refresh pass per cadence tick, `refined_at` populates, the operator
is notified on every refinement, and the existing dashboard column finally shows
live data.

The phase is opt-in (disabled by default) so an autonomous LLM rewriting
human-reviewable plans never turns on without the operator's say-so — mirroring how
proposal 044's audit-stall actuator shipped behind `[audit] stall_block_enabled`.

## Relevant Findings

_Anchors verified against `main` at HEAD (2026-06-05); all citations resolve cleanly._

- `maestro/memory.py` — `refine_plan(plan_id, *, rationale, proposed_changes, acceptance_criteria)` partial-rewrites a `pending_review` plan, stamps `p.refined_at = datetime.now(UTC).isoformat()`, returns `False` (no mutation) for missing or non-`pending_review` plans, and enforces per-field length caps. **This proposal supplies its first non-test caller.**
- `maestro/memory.py` — `EvolutionPlan.refined_at: str | None = None`; "`None` until a plan is first refined."
- `maestro/memory.py` — caps for curator rewrites, with an explicit note that "the curator runs every iteration while initial proposals are rare and human-reviewed" — i.e. the size-sensitive boundary is already curator-side, anticipating this phase.
- `maestro/memory.py` — `get_pending_plans()` returns `[p for p in ... if p.status == "pending_review"]`; `get_all_plans()` returns every row. The oldest-unrefined selector now lives in `maestro/audit_checks.py` as `select_curation_target()` (Step 1, shipped).
- `maestro/memory.py` — `mark_plan_obsolete(plan_id, reason)` transitions a `pending_review` plan to terminal `obsolete`, skips `rejection_reason` so a self-prune never feeds rejection-learning. The curate verdict's "drop" branch reuses this.
- `maestro/types.py` — `PlanStatus` documents `obsolete` as "agent-pruned `pending_review` plan from the future `curate_pending_plans` phase."
- `maestro/evolution.py` — `submit_for_review()` is the model for this phase's notify path: dedup → `save_evolution_plan` → journal `evolution_note` → `await self._notifier.notify_plan_for_review(plan.plan_id, msg)` wrapped in a best-effort `try/except`. It again defers an obsolete-loop guard to "the future `curate_pending_plans` phase."
- `maestro/dashboard/static/index.js` — pending-plan card already renders ``${p.refined_at ? ` | Refined: ${p.refined_at.split('T')[0]}` : ''}``. **No dashboard change is needed** for the operator to see refinement dates once the field populates.
- `maestro/agent.py` — `_phase_will_run()` is the single source of truth for cadence gating. The `audit_self_model` and `tend_evolution_prs` branches are the exact template for a new `*_every` + `*_enabled` + `_last_phase_iteration` dedup gate.
- `maestro/agent.py` — `_BUDGET_GATED_PHASES`; an LLM-spending phase must be a member so the burn-rate throttle can gate it.
- `maestro/agent.py` — `run_loop` threads `audit_self_every` as a keyword param into the dispatch table and into `_phase_will_run`; `curate_every` follows the same wiring.
- `maestro/config.py` — `loop_audit_self_every: int = 10` and `tend_prs_every: int = 1` defaults, plus the validation idiom ("`0 is valid (means disabled)`"). `curate_every` reuses this.
- `maestro/toml_config.py` — `("loop", "audit_self_every"): ("loop_audit_self_every", int)` mapping pattern.
- `maestro/notifications/base.py` — `Notifier.notify()` abstract + `notify_plan_for_review()` defaulting to plain `notify()`. A new `notify_plan_refined()` follows the same "default falls back to plain text" contract.
- `CLAUDE.md` "Main-phase SDK calls must be session-attached" — the curate phase must call Claude via `query_on_session(session, ...)`, and (per #1725) prepend any specialized instruction to the user prompt since the session carries its creation-time system prompt.

## Implementation Plan

Ordered so an interrupted run leaves a working tree: the deterministic selector and
notifier surface (1–2) ship inert; the engine and prompt (3–4) are unreferenced
until the phase is wired (5); the phase stays dark until config enables it (6).

1. [x] **Oldest-unrefined selector.** `maestro/audit_checks.py` — add a pure
   `select_curation_target(plans: list[EvolutionPlan]) -> EvolutionPlan | None`
   that filters `status == "pending_review"`, prefers rows with `refined_at is
   None`, and sorts ascending by `created_at` (stable tie-break on `plan_id`).
   Returns `None` when no pending plan exists. Zero SDK cost; lives beside the
   other deterministic plan analyzers.
   - *Depends on:* none.
   - *Acceptance:* given a mix of pending/non-pending and refined/unrefined plans,
     returns the oldest `refined_at is None` `pending_review` plan; returns `None`
     when none are pending; never returns an `approved`/`merged`/`obsolete` plan.

2. [x] **Refinement notification surface.** `maestro/notifications/base.py` +
   `telegram.py` — add `async def notify_plan_refined(self, plan_id: str, message:
   str) -> None` to `Notifier`, defaulting to `await self.notify(message)`; the
   Telegram override may reuse the existing Approve/Reject keyboard (the plan is
   still `pending_review` after refinement). `NullNotifier` inherits the default.
   - *Depends on:* none.
   - *Acceptance:* `Notifier.notify_plan_refined` default delegates to `notify`;
     the Telegram subclass sends a message tagged for the refined plan; both are
     covered by tests with a mocked bot.

3. [x] **`EvolutionEngine.curate_pending_plans()`.** `maestro/evolution.py` —
   session-attached method that: takes the selector's target, formats the curate
   prompt (step 4), runs one `query_on_session` call, parses a structured verdict
   (`action: "refine" | "obsolete" | "keep"` plus optional rewritten fields),
   then either calls `Memory.refine_plan(...)` with only the changed fields,
   `Memory.mark_plan_obsolete(plan_id, reason)`, or no-ops on `keep`. On a
   successful `refine`, journals an `evolution_note` and awaits
   `notify_plan_refined` (best-effort `try/except`, mirroring `submit_for_review`).
   A `ProposalParseError`-style malformed response is logged and skipped (no
   mutation), consistent with `propose_plan`'s parse-failure handling.
   - *Depends on:* 1, 2, 4.
   - *Acceptance:* with a pending plan and a mocked SDK returning a `refine`
     verdict, `refine_plan` is called with exactly the changed fields, `refined_at`
     is stamped, and `notify_plan_refined` fires once; an `obsolete` verdict calls
     `mark_plan_obsolete` and does **not** notify-as-refined; a `keep` verdict and a
     malformed response both leave the plan unmutated.

4. [x] **Curate prompt.** `maestro/prompts/` — add `CURATE_PLAN_PROMPT` describing
   the task (sharpen a stale pending plan or recommend dropping it), the JSON
   response shape, and the field caps so a confused LLM cannot exceed
   `_PLAN_RATIONALE_MAX_CHARS` / `_PLAN_CHANGE_ITEM_MAX_CHARS`. Follows the existing
   prompt-module conventions (versioned, importable constant).
   - *Depends on:* none.
   - *Acceptance:* the constant exists and a test asserts the rendered prompt carries
     the target plan's title/rationale and the response-shape contract. (Wiring it
     into `curate_pending_plans` is part of Step 3's import — tracked there, not as a
     gate on this step.)

5. [x] **Loop phase + cadence gate.** `maestro/agent.py` — add a session-attached
   `_curate_pending_plans` phase body that early-returns when
   `select_curation_target` yields `None`; add `"curate_pending_plans"` to
   `_BUDGET_GATED_PHASES`; add its branch to `_phase_will_run` (mirroring
   `audit_self_model`: `not smoke and curate_every > 0 and iteration %
   curate_every == 0 and config.curate_enabled`, with the `_last_phase_iteration`
   dedup so frozen ticks don't re-fire); thread `curate_every` through `run_loop`'s
   keyword params and dispatch table.
   - *Depends on:* 3.
   - *Acceptance:* `_phase_will_run("curate_pending_plans", ...)` is `True` only when
     enabled, on-cadence, not smoke, and not already fired at this iteration value;
     `False` under `budget_throttle_active`; the phase body early-returns without an
     SDK call when no pending plan exists.

6. [x] **Config.** `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml` —
   add `[loop] curate_every` (default `0` = disabled) and `[loop] curate_enabled`
   (default `false`), validated like `loop_audit_self_every` (`0` is valid). Inline
   `maestro.toml` comment documents that this turns on an autonomous LLM phase that
   rewrites pending plans before human review.
   - *Depends on:* 5.
   - *Acceptance:* both keys load from `maestro.toml` with the stated defaults, are
     exposed on `Config`, validate (`curate_every >= 0`), and gate the phase such
     that the default config never runs it.

7. [x] **Docs + invariant.** `docs/self-evolution.md` (a "Pending-plan curation"
   subsection) and one line in `CLAUDE.md`'s self-evolution bullets. Note in
   `maestro.toml` that the dashboard's existing `Refined:` column surfaces the
   result with no UI change.
   - *Depends on:* 5, 6.
   - *Acceptance:* `docs/self-evolution.md` and `CLAUDE.md` describe the
     `curate_pending_plans` phase and its opt-in gate; `maestro.toml` carries the
     inline comment.

## Testing Strategy

All tests use `mem_fs` and a mocked SDK/notifier per CLAUDE.md (no real I/O,
network, or `sleep`).

- **Selector** — oldest unrefined chosen over a newer unrefined and over an older
  *refined* plan; `None` when no `pending_review` exists; non-pending statuses
  never returned.
- **Notifier** — `Notifier.notify_plan_refined` default delegates to `notify`;
  Telegram override sends via a mocked bot; `NullNotifier` is a quiet no-op.
- **Engine `curate_pending_plans`** — `refine` verdict calls `refine_plan` with
  only changed fields and fires one notification; `obsolete` verdict calls
  `mark_plan_obsolete` and skips the refined-notification; `keep` and a malformed
  response leave the plan unmutated and send nothing; an oversized rewritten field
  is rejected by `refine_plan`'s caps (asserted via the raised `ValueError`).
- **Phase gate** — `_phase_will_run` matrix: enabled+on-cadence ⇒ run; disabled ⇒
  skip; `budget_throttle_active` ⇒ skip; same-iteration re-entry ⇒ skip (dedup);
  `--smoke` ⇒ skip. Phase body early-returns with no SDK call when no pending plan.
- **Config** — defaults load (`curate_every == 0`, `curate_enabled is False`);
  negative `curate_every` rejected by validation; values round-trip from a TOML
  overlay.

## Risk Assessment

- **Autonomous rewrite of human-reviewable plans** — the primary risk. Mitigated by
  the opt-in gate (`curate_every = 0` / `curate_enabled = false` by default), by
  `refine_plan` only touching `pending_review` rows (never `approved`/`merged`), by
  the existing field caps at the write boundary, and by the human still giving the
  final Approve/Reject on the refined plan.
- **Notification noise** — one refinement per cadence tick, gated by
  `_last_phase_iteration` dedup against frozen iterations; a slow cadence
  (`curate_every` ≫ 1) bounds volume. No notification on `keep`/`obsolete`-as-refined.
- **Refine ↔ propose churn loop** (a plan endlessly refined, or `mark_obsolete`
  then re-proposed) — bounded per tick (one target) and visible to the operator via
  the notification; `submit_for_review`'s dedup still blocks duplicate titles. A
  hard per-plan refine-count cap is deliberately **out of scope** (see below).
- **Rollback posture** — `curate_every = 0` (or `curate_enabled = false`) fully
  disables the phase without code revert; the selector and notifier additions are
  inert when the phase never runs.

## Out of scope

- A per-plan refinement-count cap / cool-down (analogous to the rejection cooldown)
  to bound repeated refinement of the same plan — a sensible follow-up once
  real cadence data shows whether churn occurs.
- Surfacing "oldest unrefined" or refine-count as a dedicated dashboard sort/badge;
  the existing `Refined:` column (`index.js`) is sufficient for the first ship.
- The default-on question. This proposal recommends opt-in; flipping
  `curate_enabled` to `true` at a conservative cadence is a one-line config change
  the operator can make after observing the phase on a low `curate_every`.
