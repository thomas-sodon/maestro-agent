"""Lifecycle/throttle cluster carved out of ``maestro.agent`` (proposal 068, step 3).

Holds the phase-cadence gate (``phase_will_run``), the lifecycle-cursor
reader, the iteration status update, the budget-throttle multiplier, and
the throttle / peak-hours / maintenance transition notifiers behind
:class:`LifecycleController`. ``MaestroAgent`` keeps thin delegating
shims with the historical method names. Seam functions (``build_cost_rates``,
``latest_plan_headroom``, ``create_session``) are imported from their defining
modules and patched by tests on this module; the pure window predicate
``_is_in_peak_throttle_window`` lives here beside its callers.
``_is_in_standby_window`` and the ``run_loop`` body live in
``maestro.loop.iteration``.

The module also owns ``MaestroAgent.run``'s task-run lifecycle stages at
both ends: the run-entry trio ``resolve_preflight_standdown`` (the
merge-truth-first preflight stand-down), ``begin_task_run`` (the entry-time
setup block), and ``open_run_session`` (the run-scoped SDK session open),
paired with ``teardown_task_run`` (the outer ``finally``). Each takes the
agent as a first positional argument.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from maestro.audit_checks_calibration import (
    record_prediction,
)
from maestro.cost_tracking import build_cost_rates
from maestro.memory import TaskRecord
from maestro.plan_feedback import any_pending_feedback
from maestro.sdk import (
    IterationUsage,
    QueryOptions,
    create_session,
    disconnect_session,
    latest_plan_headroom,
)

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.config import Config
    from maestro.cost_tracking import CostRateSnapshot
    from maestro.diagnostics import DiagnosticSnapshot
    from maestro.providers.base import Session
    from maestro.types import TaskOutcome, TaskSource

logger = logging.getLogger("maestro")


def _is_in_peak_throttle_window(
    config: Config, now: datetime | None = None,
) -> bool:
    """Return True when the current time is inside the peak-throttle window.

    Like :func:`_is_in_standby_window` but layered on top of
    ``budget_throttle_active`` instead of skipping the iteration body — phase
    gating fires (heavy LLM phases skip) but the inter-iteration sleep
    multiplier stays at 1.0 so responsive phases keep running at base pace.

    Hours and weekday are evaluated in ``config.peak_throttle_timezone`` (an
    IANA zone name), so DST transitions are handled automatically — a window
    set to 5-11 in ``America/Los_Angeles`` fires at 5-11 AM Pacific Time
    year-round regardless of whether PDT or PST is in effect.

    The window is interpreted as ``[start_hour, end_hour)`` and supports
    midnight-wrap (``start > end``). When ``peak_throttle_weekdays_only`` is
    True the helper returns False on Saturday/Sunday in the configured
    timezone. Returns False immediately when the feature is disabled.
    """
    if not config.peak_throttle_enabled:
        return False
    tz = ZoneInfo(config.peak_throttle_timezone)
    current = (now or datetime.now(UTC)).astimezone(tz)
    if config.peak_throttle_weekdays_only and current.weekday() >= 5:
        return False  # 5=Saturday, 6=Sunday
    hour = current.hour
    start = config.peak_throttle_start_hour
    end = config.peak_throttle_end_hour
    if start < end:
        return start <= hour < end
    # Wraparound (start > end). Equal hours are rejected by Config.validate().
    return hour >= start or hour < end


# Phases that skip during a burn-rate throttle. ``respond_to_messages``
# is intentionally excluded so Maestro stays responsive to Thomas during
# throttle (it runs with an empty reflection). ``tend_evolution_prs`` is
# excluded because it is purely mechanical — merge / reminder / subprocess
# gates — and never spends tokens.
_BUDGET_GATED_PHASES: frozenset[str] = frozenset({
    "contemplate",
    "implement_approved_plans",
    "process_rejected_plans",
    "generate_task",
    "run_task",
    "evolve",
    "curate_pending_plans",
})


# Cadence phases ``run_loop`` stamps into ``_last_phase_iteration`` after their
# body runs, so a frozen ``iteration % N == 0`` under throttle does not re-fire
# them; ``phase_will_run`` reads the stamp back.
DEDUPED_CADENCE_PHASES: frozenset[str] = frozenset({
    "review_artifacts",
    "tend_evolution_prs",
    "audit_self_model",
    "dream",
    "curate_pending_plans",
})


# The fully-introspective phases the ``external_delivery`` focus profile
# (proposal 107) drops so the loop's iteration slots flow to delivery work
# (``generate_task`` / ``run_task`` / ``tend_evolution_prs``) instead of
# self-reflection. Mirrors ``_BUDGET_GATED_PHASES``: a membership set gated at
# the top of ``phase_will_run``. Inert under the default ``self_evolution``
# profile — the guard is a no-op and every cadence behaves exactly as before.
_FOCUS_SUPPRESSED_PHASES: frozenset[str] = frozenset({
    "evolve",
    "audit_self_model",
    "curate_pending_plans",
})


def read_maintenance_state(path: Path) -> bool:
    """Return the persisted maintenance-mode flag stored at *path*.

    The single parse of the maintenance sidecar, shared by the loop
    (``MaestroAgent._read_maintenance_state``) and the dashboard, which runs
    in a separate container and so cannot read the loop's in-memory flag.
    Defaults to ``False`` when the sidecar is missing or corrupt — a missing
    file is the normal cold-start case, and a corrupt file should not block
    the loop from starting.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    active = data.get("active")
    return active if isinstance(active, bool) else False


def _coerce_lifecycle_int(raw: object, field: str, default: int = 0) -> int:
    """Coerce a persisted lifecycle integer field, defaulting loudly on corruption.

    ``lifecycle.json`` is designed to degrade **per field**: a single
    corrupt value defaults without destroying the rest of the resume
    record. A plain ``int(raw)`` breaks that contract — a non-numeric
    string such as ``"abc"`` raises ``ValueError`` (and a dict / ``None``
    a ``TypeError``), which propagates to
    :meth:`LifecycleController.read_lifecycle_progress`'s last-resort
    ``except`` and discards the whole tuple, including a valid
    ``last_shutdown``. ``OverflowError`` joins them: ``json.loads`` parses
    ``Infinity`` by default, and ``int(float('inf'))`` raises
    ``OverflowError`` (which the old ``isinstance(..., int | str)`` guard
    routed to the ``else 0`` branch). Confine that failure here: return
    *default* and emit a ``logger.warning`` naming the corrupt *field*,
    mirroring the sibling ``iteration_usage`` fail-loud handler in that
    method.
    """
    try:
        return int(raw)  # type: ignore[call-overload, no-any-return]
    except (ValueError, TypeError, OverflowError):
        logger.warning(
            "Corrupt %s in lifecycle.json (got %r); defaulting to %d",
            field, raw, default,
        )
        return default


def _format_status_message(
    iteration: int,
    cost_rate: CostRateSnapshot | None,
    health_summary: str,
) -> str:
    from html import escape

    header = f"<b>Iter {iteration}</b> — Starting"
    if cost_rate is not None:
        cost_line = f"<b>${cost_rate.cost_per_hour:.2f}/hr</b>"
    else:
        cost_line = "Cost: unknown"
    fragments = [f.strip() for f in health_summary.split(", ") if f.strip()]
    if len(fragments) > 1:
        bullets = "\n".join(f"  • {escape(f)}" for f in fragments)
        health_line = f"<b>Health:</b>\n{bullets}"
    else:
        health_line = f"<b>Health:</b> {escape(health_summary)}"
    return f"{header}\n{cost_line}\n{health_line}"


class LifecycleController:
    """Lifecycle/throttle collaborator for :class:`~maestro.agent.MaestroAgent`.

    Constructed with the agent and dispatching through its attributes at
    call time: the cluster reads and mutates the agent's persisted
    edge-tracker fields (``_budget_throttle_entered_at``,
    ``_peak_throttle_active_persisted``,
    ``_maintenance_milestone_persisted``, ``_last_phase_iteration``) and
    calls ``_notify_best_effort``, which the test suite monkey-patches on
    the agent instance after construction.
    """

    def __init__(self, agent: MaestroAgent) -> None:
        self._agent = agent
        # Proposal 145 off-cadence curate trigger: cached "a pending plan holds
        # unaddressed feedback" bit (``None`` = unknown) and the curate-pass
        # marker it was computed against, so the plan file is re-read once at
        # startup and once per curate pass, never per idle iteration.
        self._feedback_pending: bool | None = None
        self._feedback_checked_after_curate: int | None = None

    def invalidate_feedback_pending(self) -> None:
        """Force the next off-cadence check to re-read pending plans.

        The hook a feedback writer calls after ``add_plan_feedback`` succeeds.
        """
        self._feedback_pending = None

    def _plan_feedback_pending(self) -> bool:
        agent = self._agent
        marker = agent._last_phase_iteration.get("curate_pending_plans")
        if self._feedback_pending is None or marker != self._feedback_checked_after_curate:
            self._feedback_pending = any_pending_feedback(agent.memory.get_pending_plans())
            self._feedback_checked_after_curate = marker
        return self._feedback_pending

    def compute_budget_throttle_multiplier(
        self, smoke: bool,
    ) -> tuple[float, float, CostRateSnapshot | None]:
        """Return ``(gating_multiplier, sleep_multiplier, snapshot)``.

        ``gating_multiplier`` is computed against the untightened limit and
        drives the burn-rate transition / phase-gating flag (unchanged
        behaviour). ``sleep_multiplier`` drives the inter-iteration sleep;
        inside the peak-hours window the effective limit is tightened by
        ``peak_throttle_rate_multiplier`` so the loop slows sooner without
        gating phases. Off-peak the two multipliers are equal.

        Returns ``(1.0, 1.0, None)`` when smoke mode is active or the
        throttle is disabled; ``(1.0, 1.0, snapshot)`` when total spend in
        the window is below the minimum-cost floor
        (``budget_rate_min_cost_usd``). An empty window is not one of those
        cases — ``build_cost_rates`` returns a zeroed snapshot for it — so a
        builder failure is a defect and propagates into the crash path rather
        than being mapped onto the neutral tuple, which would release an
        active throttle on a measurement fault. The shared snapshot lets the
        caller log / notify with the same data that drove the decision.
        """
        agent = self._agent
        if smoke:
            return 1.0, 1.0, None
        limit = agent.config.max_loop_budget_usd_per_hour
        if limit <= 0:
            return 1.0, 1.0, None
        snapshot = build_cost_rates(
            agent.cost_samples,
            window_hours=agent.config.budget_rate_window_hours,
        )
        if snapshot.total_cost_usd < agent.config.budget_rate_min_cost_usd:
            return 1.0, 1.0, snapshot
        cap = agent.config.budget_throttle_max_multiplier
        rate = snapshot.cost_per_hour
        gating = min(cap, max(1.0, rate / limit))
        sleep_limit = limit
        if (
            agent.config.peak_throttle_enabled
            and _is_in_peak_throttle_window(agent.config)
        ):
            sleep_limit = limit * agent.config.peak_throttle_rate_multiplier
        sleep = (
            min(cap, max(1.0, rate / sleep_limit))
            if sleep_limit > 0
            else cap
        )
        return gating, sleep, snapshot

    async def maybe_notify_throttle_transition(
        self,
        multiplier: float,
        snapshot: CostRateSnapshot | None,
        sleep_multiplier: float | None = None,
    ) -> None:
        """Fire a one-shot Telegram + journal entry on throttle entry/exit.

        The transition is detected by comparing the gating ``multiplier``
        (computed against the untightened limit) against the persisted
        ``_budget_throttle_entered_at`` field. Re-entering a sustained
        throttle does not re-notify — the Telegram message fires once on
        the crossing into throttled mode and once on the crossing back to
        base pace.

        ``sleep_multiplier`` is the factor that actually governs the inter-
        iteration sleep; inside the peak-hours window it is computed against
        the *tightened* limit and so can be materially larger than the gating
        multiplier. The entry message reports it (falling back to the gating
        multiplier off-peak, where the two are equal) so operators read the
        real slowdown, not the smaller gating factor.
        """
        agent = self._agent
        if sleep_multiplier is None:
            sleep_multiplier = multiplier
        entering = multiplier > 1.0
        # Track burn-rate transitions only — the combined ``budget_throttle_active``
        # also includes the scheduled peak-hours window, which would suppress
        # a legitimate burn-rate entry notification while peak-throttle is on.
        was_throttled = agent.burn_rate_throttle_active
        if entering and not was_throttled:
            agent._budget_throttle_entered_at = datetime.now(UTC).isoformat()
            rate = snapshot.cost_per_hour if snapshot is not None else 0.0
            limit = agent.config.max_loop_budget_usd_per_hour
            # Plan rate-limit headroom from the cli_paced /usage probe
            # (proposal 062 Step 5) — a read-only awareness signal appended to
            # the entry report. It never feeds the gating math (deferred by the
            # proposal); absent (default anthropic provider, probe off, or a
            # parse miss) the report is unchanged.
            headroom = latest_plan_headroom()
            headroom_note = ""
            if headroom is not None and headroom.percent_used is not None:
                headroom_note = (
                    f" Plan headroom ({headroom.label or 'plan'}): "
                    f"{headroom.percent_used:.0f}% used."
                )
            logger.warning(
                "Entering budget throttle — burn rate $%.4f/hr vs $%.2f/hr "
                "limit (sleep multiplier %.2fx)%s",
                rate, limit, sleep_multiplier, headroom_note,
            )
            msg = (
                f"Entering budget throttle — burn rate ${rate:.4f}/hr vs "
                f"${limit:.2f}/hr limit. Inter-iteration sleep stretched "
                f"{sleep_multiplier:.2f}x. Self-regulating as cost samples "
                f"age out.{headroom_note}"
            )
            await agent._notify_best_effort(
                msg,
                failure_log="Budget throttle notification failed",
            )
            agent.journal.record_event(
                content=msg,
                entry_type="milestone",
                triggered_by="budget_throttle_enter",
            )
        elif not entering and was_throttled:
            agent._budget_throttle_entered_at = None
            logger.info(
                "Budget throttle released — rate back below limit",
            )
            msg = "Budget throttle released — inter-iteration pace restored."
            await agent._notify_best_effort(
                msg,
                failure_log="Budget throttle exit notification failed",
            )
            agent.journal.record_event(
                content=msg,
                entry_type="milestone",
                triggered_by="budget_throttle_exit",
            )

    async def maybe_notify_peak_throttle_transition(self) -> None:
        """Fire a one-shot Telegram + journal entry on peak-throttle entry/exit.

        Sibling of :meth:`maybe_notify_throttle_transition`. Tracks edge
        transitions of :func:`_is_in_peak_throttle_window` against
        the persisted ``_peak_throttle_active_persisted`` field so a restart
        inside the configured window does not re-fire entry, matching the
        burn-rate side's one-shot semantics. Peak hours no longer gate
        phases; they tighten the burn-rate budget by
        ``peak_throttle_rate_multiplier`` so the loop slows (longer inter-
        iteration sleep) while heavy phases keep running. That slowdown rides
        on the burn-rate machinery, so it has no effect unless
        ``max_loop_budget_usd_per_hour > 0`` — the entry path logs a warning
        when the window fires against a disabled budget so an inert config is
        visible.
        """
        agent = self._agent
        active = _is_in_peak_throttle_window(agent.config)
        was_active = agent._peak_throttle_active_persisted
        if active and not was_active:
            agent._peak_throttle_active_persisted = True
            start = agent.config.peak_throttle_start_hour
            end = agent.config.peak_throttle_end_hour
            tz = agent.config.peak_throttle_timezone
            weekday_clause = (
                ", weekdays"
                if agent.config.peak_throttle_weekdays_only
                else ""
            )
            window_label = f"{start:02d}:00-{end:02d}:00 {tz}{weekday_clause}"
            factor = agent.config.peak_throttle_rate_multiplier
            if agent.config.max_loop_budget_usd_per_hour <= 0:
                logger.warning(
                    "Entering peak-hours throttle (%s) but "
                    "max_loop_budget_usd_per_hour is disabled — no slowdown "
                    "will apply.",
                    window_label,
                )
                msg = (
                    f"Entering peak-hours throttle ({window_label}), but no "
                    "budget limit is set (max_loop_budget_usd_per_hour = 0), "
                    "so no slowdown applies."
                )
            else:
                logger.warning(
                    "Entering peak-hours throttle (%s); budget rate tightened "
                    "%.2fx, loop slows but heavy phases keep running.",
                    window_label, factor,
                )
                msg = (
                    f"Entering peak-hours throttle ({window_label}). Budget "
                    f"rate tightened {factor:.2f}x; loop slows but heavy "
                    "phases keep running."
                )
            await agent._notify_best_effort(
                msg,
                failure_log="Peak throttle notification failed",
            )
            try:
                agent.journal.record_event(
                    content=msg,
                    entry_type="milestone",
                    triggered_by="peak_throttle_enter",
                )
            except Exception:
                logger.warning(
                    "Peak throttle journal entry failed", exc_info=True,
                )
        elif not active and was_active:
            agent._peak_throttle_active_persisted = False
            logger.info(
                "Peak-hours throttle released — loop pace restored.",
            )
            msg = (
                "Peak-hours throttle released — loop pace restored."
            )
            await agent._notify_best_effort(
                msg,
                failure_log="Peak throttle exit notification failed",
            )
            try:
                agent.journal.record_event(
                    content=msg,
                    entry_type="milestone",
                    triggered_by="peak_throttle_exit",
                )
            except Exception:
                logger.warning(
                    "Peak throttle exit journal entry failed", exc_info=True,
                )

    async def maybe_notify_maintenance_transition(self) -> None:
        """Fire a one-shot Telegram + journal entry on maintenance entry/exit.

        Sibling of :meth:`maybe_notify_peak_throttle_transition`. Tracks
        edge transitions of ``MaestroAgent._maintenance_mode_active`` against
        the persisted ``_maintenance_milestone_persisted`` field so a restart
        inside maintenance mode does not re-fire entry.
        """
        agent = self._agent
        active = agent._maintenance_mode_active
        was_active = agent._maintenance_milestone_persisted
        if active and not was_active:
            agent._maintenance_milestone_persisted = True
            logger.warning(
                "Maintenance mode engaged — high-cost phases gated.",
            )
            msg = (
                "Maintenance mode engaged — high-cost phases gated; "
                "messaging stays live."
            )
            await agent._notify_best_effort(
                msg,
                failure_log="Maintenance enter notification failed",
            )
            try:
                agent.journal.record_event(
                    content=msg,
                    entry_type="milestone",
                    triggered_by="maintenance_enter",
                )
            except Exception:
                logger.warning(
                    "Maintenance enter journal entry failed", exc_info=True,
                )
        elif not active and was_active:
            agent._maintenance_milestone_persisted = False
            logger.info(
                "Maintenance mode released — high-cost phases resumed.",
            )
            msg = (
                "Maintenance mode released — high-cost phases resumed."
            )
            await agent._notify_best_effort(
                msg,
                failure_log="Maintenance exit notification failed",
            )
            try:
                agent.journal.record_event(
                    content=msg,
                    entry_type="milestone",
                    triggered_by="maintenance_exit",
                )
            except Exception:
                logger.warning(
                    "Maintenance exit journal entry failed", exc_info=True,
                )

    async def send_status_update(
        self,
        iteration: int,
        diag_snapshot: DiagnosticSnapshot | None,
    ) -> None:
        agent = self._agent
        cost_rate: CostRateSnapshot | None = None
        try:
            window_hours = agent.config.budget_rate_window_hours
            cost_rate = build_cost_rates(
                agent.cost_samples, window_hours=window_hours,
            )
        except Exception:
            logger.debug("Failed to build cost rates for status", exc_info=True)
        health = diag_snapshot.summary if diag_snapshot is not None else "Unknown"
        msg = _format_status_message(
            iteration=iteration,
            cost_rate=cost_rate,
            health_summary=health,
        )
        await agent._notify_best_effort(
            msg,
            parse_mode="HTML",
            failure_log="Status update notification failed",
            log_level=logging.DEBUG,
        )

    def phase_will_run(
        self,
        phase: str,
        *,
        iteration: int,
        smoke: bool,
        first_iteration_after_restart: bool,
        task_every: int,
        evolve_every: int,
        dream_every: int,
        audit_self_every: int,
        body_frozen: bool,
        contemplate_every: int = 1,
        curate_every: int = 0,
        curate_enabled: bool = False,
    ) -> bool:
        """Single source of truth for whether this phase's body runs this iteration.

        Any new gated phase must be reflected here. Unknown phase names
        return ``True`` defensively so a missing entry surfaces as a noisy
        execution rather than a silent skip.

        ``body_frozen`` is the body-top freeze snapshot: once a body starts
        frozen, the budget gate stays closed until the body ends, so a
        mid-body release (``/maintenance off``) cannot re-fire a cadence
        phase at an iteration value that already fired (#4150). The live
        ``budget_throttle_active`` read is kept so a mid-body engage still
        gates at once — the union only tightens within a body.
        """
        agent = self._agent
        if (
            (body_frozen or agent.budget_throttle_active)
            and phase in _BUDGET_GATED_PHASES
        ):
            return False
        # Focus dial (proposal 107): the ``external_delivery`` profile drops the
        # introspective phases so delivery keeps its slots. Membership is checked
        # first so ``work_target()`` is resolved only for a suppressed phase, and
        # the read goes through the ``WorkTarget`` owner (``Config.work_target()``)
        # rather than the raw ``work_focus_profile`` scalar.
        if (
            phase in _FOCUS_SUPPRESSED_PHASES
            and agent.config.work_target().focus_profile == "external_delivery"
        ):
            return False
        if phase in (
            "ensure_main_branch",
            "check_contributions",
            "process_messages",
        ):
            return True
        if phase == "respond_to_messages":
            return not smoke and bool(agent._iter_reply_context)
        if phase == "implement_approved_plans":
            return not smoke
        if phase == "check_merged_plans":
            # Proposal 155 step 4: also every iteration an implemented plan
            # exists, so a merged gap plan is marked merged within one tick.
            return not smoke and (
                first_iteration_after_restart
                or any(
                    p.status == "implemented"
                    for p in agent.memory.get_live_plan_records()
                )
            )
        if phase == "review_artifacts":
            base = (
                not smoke
                and agent.config.artifact_review_enabled
                and (
                    first_iteration_after_restart
                    or (
                        agent.config.artifact_review_every > 0
                        and iteration % agent.config.artifact_review_every == 0
                    )
                )
            )
            if not base:
                return False
            # ``first_iteration_after_restart`` is a one-shot signal that
            # bypasses the dedup; otherwise short-circuit when this phase
            # already fired at the current (possibly frozen) iteration value.
            if first_iteration_after_restart:
                return True
            return agent._last_phase_iteration.get(phase) != iteration
        if phase == "execute_approved_publications":
            # Zero-SDK-cost executor that turns approved proposals into
            # public repos. Not budget-gated: the queue must drain on
            # every iteration so a burn-rate throttle never strands an
            # approved proposal indefinitely.
            return not smoke and agent.config.publication_enabled
        if phase == "tend_evolution_prs":
            base = (
                not smoke
                and agent.config.tend_prs_enabled
                and (
                    first_iteration_after_restart
                    or (
                        agent.config.tend_prs_every > 0
                        and iteration % agent.config.tend_prs_every == 0
                    )
                )
            )
            if not base:
                return False
            if first_iteration_after_restart:
                return True
            return agent._last_phase_iteration.get(phase) != iteration
        if phase == "attend":
            # Observation-only conscious-access workspace (proposal 130): a
            # zero-SDK phase gated purely on the opt-in flag. Without this
            # explicit arm the unknown-phase fall-through returns True and the
            # phase would run flag-off, breaking the byte-identity guarantee.
            # Skipped under --smoke so the deploy-validation iteration stays
            # byte-identical, matching the audit_self_model posture.
            return not smoke and agent.config.conscious_workspace_observe_enabled
        if phase == "process_rejected_plans":
            return not smoke
        if phase == "generate_task":
            # Proposal 116 master switch: flag-off starves the self-directed
            # channel by never scheduling ``generate_task``. ``run_task`` and
            # every external / PR-remediation path stay untouched below.
            return (
                not smoke
                and agent.config.self_directed_tasks_enabled
                and task_every > 0
                and iteration % task_every == 0
            )
        if phase == "run_task":
            # Runs every iteration the loop is otherwise live — budget /
            # peak-throttle / smoke gates handled above. ``_has_runnable_work``
            # keeps the phase quiet when no source has anything pending.
            return not smoke and agent._has_runnable_work()
        if phase == "evolve":
            return (
                not smoke
                and evolve_every > 0
                and iteration % evolve_every == 0
            )
        if phase == "audit_self_model":
            # Zero-SDK-cost phase: not in ``_BUDGET_GATED_PHASES``. Skipped
            # under ``--smoke`` (deploy-validation iteration) so it mirrors
            # the ``dream`` posture.
            base = (
                not smoke
                and audit_self_every > 0
                and iteration % audit_self_every == 0
            )
            if not base:
                return False
            return agent._last_phase_iteration.get(phase) != iteration
        if phase == "curate_pending_plans":
            # SDK-spending phase (member of ``_BUDGET_GATED_PHASES``, so the
            # burn-rate gate at the top already skips it under throttle).
            # Opt-in by design (proposal 060): both ``curate_enabled`` and a
            # positive ``curate_every`` must hold before an autonomous LLM
            # rewrites human-reviewable pending plans. Mirrors
            # ``audit_self_model``'s cadence + ``_last_phase_iteration`` dedup.
            # Off-cadence, ``[loop] plan_feedback_enabled`` (proposal 145) fires
            # it anyway — regardless of ``curate_every``, so ``curate_every = 0``
            # still services explicit feedback — while a pending plan holds
            # unaddressed reviewer feedback; the cached bit is consulted only after the cheap conjuncts pass.
            base = (
                not smoke
                and curate_enabled
                and curate_every > 0
                and iteration % curate_every == 0
            )
            if not base and not (
                not smoke
                and curate_enabled
                and agent.config.loop_plan_feedback_enabled
                and self._plan_feedback_pending()
            ):
                return False
            return agent._last_phase_iteration.get(phase) != iteration
        if phase == "dream":
            # Zero-SDK since proposal 149 Step 5, so neither budget-gated nor
            # focus-suppressed; deduped like ``audit_self_model`` so a frozen
            # throttled iteration runs the passes once.
            base = (
                not smoke
                and dream_every > 0
                and iteration % dream_every == 0
            )
            if not base:
                return False
            return agent._last_phase_iteration.get(phase) != iteration
        if phase == "contemplate":
            # Priced introspection (introspect + update_self_model). Budget-gated
            # above against the body-scoped freeze snapshot (not only the live
            # flag), so a freeze skips it for the whole body; a resume-in-place
            # retry at the same iteration is safe because contemplate keys its once-per-cycle
            # writes (bet, backlog snapshot, sensor row) to *iteration* (#4106),
            # so no _last_phase_iteration dedup is needed. Always exercised under --smoke so the deploy iteration
            # validates the contemplate SDK path end-to-end; otherwise cadenced.
            if smoke:
                return True
            return contemplate_every > 0 and iteration % contemplate_every == 0
        return True

    def read_lifecycle_progress(self) -> tuple[
        str | None,
        int,
        str | None,
        int | None,
        bool,
        str,
        str,
        IterationUsage | None,
        str | None,
        str | None,
        str | None,
        bool,
        str | None,
        int,
        int,
        int,
        int,
        bool,
    ]:
        """Read the lifecycle file.

        Returns ``(last_shutdown, iteration, next_phase, last_exit_code,
        crashed, message_context, reflection, iteration_usage, session_a_id,
        session_b_id, budget_throttle_entered_at,
        peak_throttle_active_persisted, rate_limit_idle_entered_at,
        consecutive_rate_limits, consecutive_cli_crashes,
        consecutive_stream_stalls, consecutive_stale_sessions,
        maintenance_milestone_persisted)``.
        Missing fields default to ``None``/``0``/``""``. A missing or
        corrupt file yields the all-empty tuple. Legacy keys
        ``budget_standby_entered_at`` / ``budget_standby_anchor_message_id``
        left on disk by the prior message-gated standby implementation are
        silently ignored so an in-flight deploy does not crash startup —
        new writes emit ``budget_throttle_entered_at`` only. The four
        consecutive-failure counters were added later (the stale-session
        counter latest); lifecycle files written by older builds default to
        0 with no warning.

        Does not delete the file. The next ``_persist_progress`` (phase
        boundary) or ``_write_lifecycle_shutdown`` (graceful shutdown) call
        overwrites it atomically. Leaving the file on disk means that if this
        startup itself crashes mid-phase before reaching a phase boundary,
        the previous cursor is still readable on the next startup, so the
        iteration counter and crash-detection signal survive.

        ``crashed`` is True when the file exists (i.e. the previous run left
        state behind) but ``last_shutdown`` is absent *and* there is evidence
        the loop was in progress (non-zero iteration or a pending next_phase).
        A clean ``shutdown()`` always writes ``last_shutdown``, so its absence
        signals a hard kill / unhandled exception / OOM between phase writes.

        On corrupt or malformed ephemeral fields (e.g. a non-dict
        ``iteration_usage``), log a warning and return the empty default for
        that field rather than crashing startup — the resumed iteration will
        still pick up at the correct phase, just without restored context
        for that one field.
        """
        agent = self._agent
        path = agent.config.lifecycle_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                logger.warning(
                    "Non-dict top-level lifecycle.json (got %s); "
                    "falling back to empty defaults",
                    type(data).__name__,
                )
                raise ValueError("lifecycle.json is not a JSON object")
            last_shutdown: str | None = data.get("last_shutdown")
            iteration_raw = data.get("iteration", 0)
            iteration = _coerce_lifecycle_int(iteration_raw, "iteration")
            next_phase = data.get("next_phase")
            if not isinstance(next_phase, str):
                next_phase = None
            exit_code_raw = data.get("last_exit_code")
            last_exit_code = exit_code_raw if isinstance(exit_code_raw, int) else None
            crashed = (
                last_shutdown is None
                and (iteration > 0 or next_phase is not None)
            )

            msg_raw = data.get("message_context", "")
            message_context = msg_raw if isinstance(msg_raw, str) else ""
            refl_raw = data.get("reflection", "")
            reflection = refl_raw if isinstance(refl_raw, str) else ""

            iu_raw = data.get("iteration_usage")
            iteration_usage: IterationUsage | None = None
            if iu_raw is not None:
                if isinstance(iu_raw, dict):
                    try:
                        iteration_usage = IterationUsage.from_dict(iu_raw)
                    except Exception:
                        logger.warning(
                            "Corrupt iteration_usage in lifecycle.json; "
                            "starting iteration with empty accumulator",
                            exc_info=True,
                        )
                        iteration_usage = None
                else:
                    logger.warning(
                        "Non-dict iteration_usage in lifecycle.json "
                        "(got %s); ignoring",
                        type(iu_raw).__name__,
                    )

            if (
                iteration_usage is not None
                and iteration_usage.iteration != iteration
            ):
                logger.warning(
                    "Persisted iteration_usage.iteration=%d mismatches "
                    "cursor iteration=%d; trusting cursor and discarding "
                    "the stale accumulator",
                    iteration_usage.iteration, iteration,
                )
                iteration_usage = None

            sa_raw = data.get("session_a_id")
            session_a_id = sa_raw if isinstance(sa_raw, str) and sa_raw else None
            if sa_raw is not None and session_a_id is None:
                logger.warning(
                    "Non-string session_a_id in lifecycle.json; ignoring",
                )
            sb_raw = data.get("session_b_id")
            session_b_id = sb_raw if isinstance(sb_raw, str) and sb_raw else None
            if sb_raw is not None and session_b_id is None:
                logger.warning(
                    "Non-string session_b_id in lifecycle.json; ignoring",
                )

            throttle_entered_at = data.get("budget_throttle_entered_at")
            if not isinstance(throttle_entered_at, str):
                throttle_entered_at = None
            peak_persisted_raw = data.get("peak_throttle_active_persisted")
            peak_throttle_active_persisted = (
                peak_persisted_raw if isinstance(peak_persisted_raw, bool) else False
            )
            rate_limit_idle_entered_at = data.get(
                "rate_limit_idle_entered_at",
            )
            if not isinstance(rate_limit_idle_entered_at, str):
                rate_limit_idle_entered_at = None

            rl_raw = data.get("consecutive_rate_limits", 0)
            consecutive_rate_limits = _coerce_lifecycle_int(
                rl_raw, "consecutive_rate_limits",
            )
            cc_raw = data.get("consecutive_cli_crashes", 0)
            consecutive_cli_crashes = _coerce_lifecycle_int(
                cc_raw, "consecutive_cli_crashes",
            )
            ss_raw = data.get("consecutive_stream_stalls", 0)
            consecutive_stream_stalls = _coerce_lifecycle_int(
                ss_raw, "consecutive_stream_stalls",
            )
            sss_raw = data.get("consecutive_stale_sessions", 0)
            consecutive_stale_sessions = _coerce_lifecycle_int(
                sss_raw, "consecutive_stale_sessions",
            )

            maintenance_persisted_raw = data.get(
                "maintenance_milestone_persisted",
            )
            maintenance_milestone_persisted = (
                maintenance_persisted_raw
                if isinstance(maintenance_persisted_raw, bool)
                else False
            )

            return (
                last_shutdown, iteration, next_phase, last_exit_code, crashed,
                message_context, reflection, iteration_usage,
                session_a_id, session_b_id,
                throttle_entered_at,
                peak_throttle_active_persisted,
                rate_limit_idle_entered_at,
                consecutive_rate_limits,
                consecutive_cli_crashes,
                consecutive_stream_stalls,
                consecutive_stale_sessions,
                maintenance_milestone_persisted,
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, TypeError):
            return (
                None, 0, None, None, False, "", "", None,
                None, None, None, False, None,
                0, 0, 0, 0,
                False,
            )

    def read_reply_context(self) -> str:
        """Read the persisted reply context (pre-deploy notices excluded).

        A single-field accessor, like :meth:`read_active_conscious_moment_id`,
        rather than widening :meth:`read_lifecycle_progress`. A file without
        ``reply_context`` (an older build) falls back to ``message_context``
        so an in-flight upgrade never drops a real reply; a missing, corrupt,
        or non-dict file yields ``""``.
        """
        path = self._agent.config.lifecycle_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return ""
        if not isinstance(data, dict):
            return ""
        value = data.get("reply_context")
        if isinstance(value, str):
            return value
        legacy = data.get("message_context", "")
        return legacy if isinstance(legacy, str) else ""

    def read_active_conscious_moment_id(self) -> str | None:
        """Read the last recorded conscious-moment id from the lifecycle file.

        A single-field accessor (proposal 130) rather than widening the already
        19-wide :meth:`read_lifecycle_progress` tuple. Returns the persisted
        ``active_conscious_moment_id`` — the id ``attend`` stamped on the last
        moment it recorded — or ``None`` when the file is missing, corrupt, or
        carries no such key (a fresh install or a build predating this field).
        The restored value is what makes the next ``attend`` pass's ``linked``
        continuity classification provable.
        """
        path = self._agent.config.lifecycle_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("active_conscious_moment_id")
        return value if isinstance(value, str) and value else None


async def teardown_task_run(
    agent: MaestroAgent,
    *,
    run_session: Session | None,
    branch_name: str | None,
    kept_branch: str | None,
    preserve_branch: bool,
    deferred: TaskRecord | None = None,
    handed_off: bool = False,
) -> None:
    """Tear down a task run — ``MaestroAgent.run``'s outer ``finally`` block.

    A behaviour-preserving lift of the four-statement teardown that ``run``'s
    outer ``finally`` executed on every exit path (a normal return, or any
    escaping exception). When a run-scoped session was opened it is
    disconnected best-effort — the ``contextlib.suppress(Exception)`` around
    ``disconnect_session`` is the documented best-effort SDK-teardown contract
    (a teardown failure must never displace the run's real outcome, the same
    contract as ``_disconnect_best_effort`` / ``_phase_implement_approved_plans``).
    The working-tree/branch restoration then runs via the already-extracted
    ``_teardown_task_branch`` (its own log-and-fall-through contract), and the
    ``agent._running`` / ``agent._current_task`` flags reset last.

    Called from ``run``'s ``finally`` with the agent and the run-scoped locals
    passed as arguments, so they are still evaluated at teardown time regardless
    of how ``run`` exits. Reassigns nothing in the caller's scope — the two
    flag writes are instance attributes, matching ``_begin_task_run``'s
    precedent. With this lift ``run`` carries no broad ``except``/``suppress``
    of its own, so its broad-suppress allowlist exemption moves here.

    A *deferred* record (``run(defer_persist=True)``, #4062) is appended to
    task history here unless ``run`` *handed_off* it and teardown completes:
    an exception from the run's tail or from teardown itself means the caller
    never receives the record, so its single row must not be lost.
    """
    try:
        if run_session is not None:
            with contextlib.suppress(Exception):
                await disconnect_session(run_session)
        if agent._iteration_usage is not None:
            agent._iteration_usage._task_scope_active = False
        agent._teardown_task_branch(branch_name, kept_branch, preserve_branch)
        agent._running = False
        agent._current_task = None
    except BaseException:
        handed_off = False
        raise
    finally:
        if deferred is not None and not handed_off:
            agent.memory.record_task(deferred)
            agent.memory.clear_checkpoint(deferred.task_id)


async def finish_task_phase(
    agent: MaestroAgent,
    record: TaskRecord | None,
    predicted_outcome: TaskOutcome,
    *,
    goal: str,
    pr_branch: str | None,
    head_before: str | None,
    defer_persist: bool,
) -> None:
    """Tail of ``_run_task_phase``: prediction, remediation arms, deferred row.

    Remediation arms re-stamp *record*; under *defer_persist* its single
    task-history row lands here, after them, even if they raise (#4062).
    """
    try:
        # Persist the pre-execution prediction with the real task_id.
        if (
            record is not None
            and isinstance(getattr(record, "task_id", None), str)
            and record.task_id
        ):
            record_prediction(
                agent.predictions, record.task_id, predicted_outcome,
            )

        await agent._record_remediation_outcome(
            goal=goal,
            pr_branch=pr_branch,
            record=record,
            head_before=head_before,
        )
    finally:
        # isinstance: phase tests stub ``run()`` with mocks.
        if defer_persist and isinstance(record, TaskRecord):
            agent.memory.record_task(record)
            agent.memory.clear_checkpoint(record.task_id)


def resolve_preflight_standdown(
    agent: MaestroAgent,
    *,
    goal: str,
    pr_branch: str | None,
    source: TaskSource,
    linked_question: str | None,
    workspace_moment_id: str | None = None,
    workspace_item_ids: list[str] | None = None,
) -> TaskRecord | None:
    """Stand down before servicing a settled PR — ``run``'s merge-truth-first
    preflight (extracted).

    A behaviour-preserving lift of the inline top-of-``run`` merge-truth-first
    stand-down block: before any planning, branch creation, or SDK servicing,
    it reads git/host merge-truth for a PR *goal* references — only on the
    user-request / queue channel (``pr_branch is None``; the remediation
    channel already reads merge-truth at its dequeue-face guards and stamp
    gate, so this closes only the channel those guards never covered, #2579)
    — via ``agent._preflight_merge_truth_standdown``, the inner merge-truth
    reader that does its own fetch-first read through the ``is_pr_settled``
    chokepoint. When that PR is already merged or closed the honest move is
    silence: it emits the ``[stood-down]`` chosen-quiet marker and returns a
    no-op :class:`~maestro.memory.TaskRecord` (``outcome="partial"``,
    ``result_summary=_CHOSEN_QUIET_MARKER``, empty plan, ``branch=None``) after
    persisting it via ``memory.record_task``, threading *source*,
    *linked_question*, and the conscious-access citation
    (*workspace_moment_id* / *workspace_item_ids*, proposal 130) onto the
    record. Otherwise it returns ``None`` and ``run`` falls through to normal
    servicing.

    Keyword-only after the agent, reassigns nothing in the caller's scope, and
    hands back exactly the record ``run`` returns early on. The record id is
    minted through ``uuid.uuid4()``.
    Behaviour-preservation of the whole block is proven by the full-``run``
    regression harness (the stand-down tests in ``tests/test_pr_remediation_preflight.py``).
    """
    from maestro.remediation_tracker import _CHOSEN_QUIET_MARKER

    standdown_pr = (
        agent._preflight_merge_truth_standdown(goal)
        if pr_branch is None
        else None
    )
    if standdown_pr is not None:
        logger.info(
            "run_task: standing down on already-settled PR #%s referenced "
            "by goal %r — merge-truth-first, no servicing",
            standdown_pr, goal[:80],
        )
        standdown_record = TaskRecord(
            task_id=uuid.uuid4().hex[:12],
            goal=goal,
            plan=[],
            outcome="partial",
            result_summary=_CHOSEN_QUIET_MARKER,
            errors=[],
            duration_secs=0.0,
            timestamp=datetime.now(UTC).isoformat(),
            source=source,
            branch=None,
            key_findings=None,
            artifacts_dir=None,
            replans=0,
            linked_question=linked_question,
            workspace_moment_id=workspace_moment_id,
            workspace_item_ids=workspace_item_ids,
        )
        agent.memory.record_task(standdown_record)
        return standdown_record
    return None


def begin_task_run(
    agent: MaestroAgent,
    *,
    goal: str,
    task_id: str | None,
) -> tuple[str, float, str]:
    """Initialize a task run — ``run``'s entry-time setup block (extracted).

    A behaviour-preserving lift of the inline entry block that runs after the
    pre-flight stand-down check and before the resume/plan/branch stages. It
    flips ``agent._running``, records the current task *goal*, resets the four
    ``agent._last_remediation_*`` observation fields to ``None``, installs the
    loop's signal handlers, records the *start* timestamp, mints a 12-char hex
    ``task_id`` when the caller did not supply one (the ``resume_task`` path
    supplies its own so a surviving checkpoint can be reloaded), stamps that
    id onto the per-iteration usage accumulator under the ``is not None``
    guard, and builds the identity/system-prompt context.

    Returns the ``(task_id, start, identity_context)`` triple ``run`` reads
    back. The ``str`` ``task_id`` return is guaranteed non-``None`` on exit
    (minted when absent), which keeps mypy happy at the call site. Runs before
    ``run``'s ``try`` block, so a failure while building the identity context
    leaves ``agent._running`` set exactly as the inline block did. It writes only instance attributes on
    *agent*, matching ``teardown_task_run``'s precedent.

    It also opens the accumulator's task scope (#4066) so only calls made until
    ``teardown_task_run`` closes it are credited to the task's cost; opening a
    scope that is already open raises ``RuntimeError`` rather than silently
    merging two tasks' spend. The scope opens last, after the identity context
    is built, because ``teardown_task_run`` only runs once ``run``'s ``try``
    is entered.
    """
    iu = agent._iteration_usage
    if iu is not None and iu._task_scope_active:
        raise RuntimeError(
            f"begin_task_run: task scope already open for {iu.task_id!r}",
        )
    agent._running = True
    agent._current_task = goal
    agent._last_remediation_merge_block = None
    agent._last_remediation_merge_source = None
    agent._last_remediation_merge_observed_at = None
    agent._last_remediation_items_remaining = None
    agent._install_signal_handlers()
    start = time.time()
    if task_id is None:
        task_id = uuid.uuid4().hex[:12]
    if agent._iteration_usage is not None:
        agent._iteration_usage.task_id = task_id
    identity_context = agent._build_identity_context()
    if iu is not None:
        iu._task_scope_active = True
    return task_id, start, identity_context


async def open_run_session(
    agent: MaestroAgent,
    *,
    identity_context: str,
) -> Session | None:
    """Open ``run``'s run-scoped SDK session (extracted).

    A behaviour-preserving lift of the inline session-open block at the top
    of ``run``'s ``try``: when :attr:`Config.enable_session_reuse` is set it
    opens the run-scoped session (``planner.decompose``, ``planner.replan``,
    and ``_distill_findings`` share one warm transcript so the identity
    prompt and shared context stay in the prompt cache) via
    :func:`maestro.sdk.create_session` — otherwise it returns ``None``. It
    reassigns nothing in the caller's scope — ``run`` assigns the returned
    ``Session | None`` to ``run_session`` — and the paired
    ``disconnect_session`` teardown stays in ``teardown_task_run``, because
    the session must be disposed regardless of how ``run`` exits and that
    lifecycle deliberately spans the ``try``/``finally``.
    """
    if agent.config.enable_session_reuse:
        session: Session = await create_session(
            system=identity_context,
            options=QueryOptions(
                effort="medium",
                tools=[],
                caller="run_session",
            ),
            phase="run_task",
        )
        return session
    return None
