"""Loop iteration body carved out of ``maestro.agent`` (proposal 147, Step 8).

Holds the ``run_loop`` body formerly on ``MaestroAgent``: the iteration
driver that sequences the registered phases, the standby / peak-throttle /
warning-hold / rate-limit gates, usage flushing, and the crash-aware
shutdown unwind, plus the module-level ``_is_in_standby_window`` helper that
only it uses. It moves intact to bring ``maestro/agent.py`` under its source
budget.

The extraction is behaviour-preserving: the same body, the same call order,
the same effects — only the receiver changed from ``self`` to a first
positional ``agent`` argument. ``MaestroAgent.run_loop`` is a one-line
delegator, and every helper the body calls is still reached as
``agent.<name>`` so instance-level patches keep intercepting. Every module
global the body reads is imported from its defining module, so a test
patches it as ``maestro.loop.iteration.<name>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from maestro import diagnostics, host_capacity
from maestro.config import Config
from maestro.loop.lifecycle import DEDUPED_CADENCE_PHASES
from maestro.messaging import build_telegram_inbound_message
from maestro.plan_decision import telegram_plan_handlers
from maestro.plan_feedback_fold import feedback_serviceable
from maestro.sdk import (
    ErrorCategory,
    IterationUsage,
    SDKError,
    clear_usage_sink,
    disconnect_session,
    is_warning_hold_active,
    latest_context_window,
    set_usage_sink,
)

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.plan_feedback import PlanRef

logger = logging.getLogger("maestro")

# Ordered list of phases that make up a single loop iteration. After each
# phase finishes, the loop persists ``(iteration, next_phase)`` to
# ``lifecycle.json`` so a graceful shutdown (or hard kill) can resume at the
# next phase on restart instead of restarting the iteration from scratch.
_LOOP_PHASES: tuple[str, ...] = (
    "ensure_main_branch",
    "check_contributions",
    "process_messages",
    "tend_evolution_prs",
    "attend",
    "contemplate",
    "respond_to_messages",
    "implement_approved_plans",
    "check_merged_plans",
    "review_artifacts",
    "execute_approved_publications",
    "process_rejected_plans",
    "generate_task",
    "run_task",
    "evolve",
    "audit_self_model",
    "curate_pending_plans",
    "dream",
)


# Hard ceiling on consecutive in-process CLI-crash retries on the same
# iteration. Lower than the rate-limit ceiling: CLI crashes are unexpected
# rather than capacity-driven, so we fail fast into crash triage once the
# fault looks deterministic.
_MAX_CONSECUTIVE_CLI_CRASHES = 3


# Threshold for fast in-process rate-limit retries on the same iteration.
# Once exceeded, the loop hands off from the fast-retry path (300s cooldown
# between attempts) to the extended-idle path (exponential-backoff probe
# loop, persisted to ``lifecycle.json``) instead of crashing — sustained
# Anthropic-side capacity pressure is an unavailability, not a bug.
_MAX_CONSECUTIVE_RATE_LIMITS = 5


# Hard ceiling on consecutive in-process stale-session retries on the same
# iteration. Like CLI_CRASH/STREAM_STALL, STALE_SESSION is non-capacity:
# the retry creates fresh sessions, so an unbounded loop is not currently
# reachable. This is a defensive ceiling against a future SDK classifier
# regression that could otherwise spin the resume-in-place loop forever.
_MAX_CONSECUTIVE_STALE_SESSIONS = 3


# Hard ceiling on consecutive in-process stream-stall retries on the same
# iteration. STREAM_STALL is the safety-net classification raised when the
# SSE stream goes silent for the configured idle timeout; like a stale
# session, the retry creates a fresh client so we cap it the same way.
_MAX_CONSECUTIVE_STREAM_STALLS = 3


_RATE_LIMIT_COOLDOWN_SECONDS = 300


# Maximum sleep between standby re-checks. Bounded so we still respond
# promptly to shutdown signals while paused.
_STANDBY_MAX_SLEEP_SECONDS = 300


def _is_in_standby_window(
    config: Config, now: datetime | None = None,
) -> bool:
    """Return True when the current UTC hour is inside the standby window.

    The window is interpreted as ``[start_hour, end_hour)``. When
    ``start_hour`` is greater than ``end_hour`` the window wraps around
    midnight UTC (e.g. ``22..2`` covers 22:00-01:59 UTC). Returns ``False``
    immediately when the feature is disabled.
    """
    if not config.standby_enabled:
        return False
    current = (now or datetime.now(UTC)).astimezone(UTC)
    hour = current.hour
    start = config.standby_start_hour_utc
    end = config.standby_end_hour_utc
    if start < end:
        return start <= hour < end
    # Wraparound (start > end). Equal hours are rejected by Config.validate().
    return hour >= start or hour < end


async def run_loop(
    agent: MaestroAgent,
    interval: int = 60,
    max_iters: int = 0,
    evolve_every: int = 5,
    task_every: int = 3,
    dream_every: int = 10,
    audit_self_every: int = 10,
    contemplate_every: int = 1,
    curate_every: int = 0,
    curate_enabled: bool = False,
    smoke: bool = False,
) -> bool:
    """Continuous autonomous operation.

    Returns True when the loop exits normally (max_iters reached or
    shutdown requested). Critical failures never return False — they
    propagate as exceptions through the crash path (``shutdown(crashed=True)``
    re-raises after the ``finally`` block) unless resume-in-place
    absorbs them as transient SDK failures.

    When ``smoke`` is ``True``, the loop runs in deploy-smoke mode:
    ``contemplate`` runs with the same settings as production (so the
    smoke iteration validates the real configuration end-to-end), but
    every phase that does external work or downstream evolution is
    skipped. Concretely, ``respond_to_messages`` (Telegram side effect),
    ``implement_approved_plans`` (writes throwaway code into the snapshot),
    ``check_merged_plans``, ``process_rejected_plans``, ``generate_task``,
    ``evolve``, and ``dream`` are all skipped. The remaining phases still
    run so the smoke iteration exercises startup, wiring, phase
    orchestration, real SDK integration, and the shutdown path.
    """
    if max_iters < 0:
        raise ValueError(
            f"max_iters must be >= 0 (0 means unlimited), got {max_iters}"
        )
    for name, val in (
        ("task_every", task_every),
        ("evolve_every", evolve_every),
        ("dream_every", dream_every),
        ("audit_self_every", audit_self_every),
        ("contemplate_every", contemplate_every),
        ("curate_every", curate_every),
    ):
        if val < 0:
            raise ValueError(
                f"{name} must be >= 0 (0 means disabled), got {val}"
            )
    agent._running = True
    agent._shutdown_initiated = False
    agent._ensure_shutdown_event().clear()
    agent._install_signal_handlers()
    agent._write_pid_file()

    # Write the heartbeat immediately so the Docker healthcheck can pass
    # before crash reporting, the startup Telegram call, or the first
    # iteration's LLM phases have a chance to delay it.
    agent._write_heartbeat()

    # Immediate feedback that the loop (and Telegram) is working
    await agent._notify_best_effort(
        f"Maestro loop started (interval={interval}s, "
        f"task_every={task_every}, evolve_every={evolve_every})",
        failure_log="Startup notification failed",
    )

    # Detect previous shutdown + load resume cursor + restore ephemeral
    # state in one pass. Ephemeral fields ride into the iteration via
    # ``self._iter_*`` so the resumed phase sees the same context the
    # failed pass had. ``iteration_usage`` is handed off to
    # ``self._iteration_usage`` below so the accumulator survives the
    # restart.
    (
        last_shutdown_ts,
        resume_iteration,
        resume_phase,
        resume_exit_code,
        crashed,
        restored_message_context,
        restored_reflection,
        restored_iteration_usage,
        restored_session_a_id,
        restored_session_b_id,
        persisted_throttle_entered_at,
        persisted_peak_throttle_active,
        persisted_rate_limit_idle_entered_at,
        restored_consecutive_rate_limits,
        restored_consecutive_cli_crashes,
        restored_consecutive_stream_stalls,
        restored_consecutive_stale_sessions,
        persisted_maintenance_milestone,
    ) = agent._read_lifecycle_progress()
    restored_reply_context = agent._lifecycle.read_reply_context()
    agent._budget_throttle_entered_at = persisted_throttle_entered_at
    agent._peak_throttle_active_persisted = persisted_peak_throttle_active
    agent._maintenance_milestone_persisted = persisted_maintenance_milestone
    # Maintenance flag lives in its own sidecar so the Telegram command
    # handler can mutate it without racing the lifecycle writer.
    agent._maintenance_mode_active = agent._read_maintenance_state()
    agent._rate_limit_idle_entered_at = persisted_rate_limit_idle_entered_at
    # Restore the three consecutive-failure counters so a deploy or
    # restart during a back-off window keeps the in-process counters
    # marching toward the extended-idle threshold instead of silently
    # resetting to 0 and giving the restarted process a full fresh
    # window of retries against an already-stressed upstream.
    agent._consecutive_rate_limits = restored_consecutive_rate_limits
    # CLI-crash and stream-stall counters survive restart but are
    # clamped to ``ceiling - 1`` so a fresh process gets exactly one
    # more attempt before tripping the ceiling. A full reset defeats
    # the purpose of the counter; a full re-raise traps the user when
    # the underlying fault is in fact transient (e.g. a freshly
    # deployed CLI binary). One-more-attempt is the minimal safe
    # compromise.
    agent._consecutive_cli_crashes = min(
        restored_consecutive_cli_crashes,
        max(0, _MAX_CONSECUTIVE_CLI_CRASHES - 1),
    )
    agent._consecutive_stream_stalls = min(
        restored_consecutive_stream_stalls,
        max(0, _MAX_CONSECUTIVE_STREAM_STALLS - 1),
    )
    agent._consecutive_stale_sessions = min(
        restored_consecutive_stale_sessions,
        max(0, _MAX_CONSECUTIVE_STALE_SESSIONS - 1),
    )
    # Sync the resume cursor early so all startup gates
    # (_probe_rate_limit, crash detection, idle-state restoration) see the
    # lifecycle-restored iteration count instead of the constructor default 0.
    # The later assignment at the iteration-variable sync block writes the
    # final cursor value: same ``resume_iteration`` on normal resume,
    # ``resume_iteration - 1`` on the unknown-phase fallback so the loop body's
    # first ``iteration += 1`` lands on ``resume_iteration``.
    agent._resume_iteration = resume_iteration
    # Conscious-access workspace continuity (proposal 130). Restore the last
    # recorded moment id so the next ``attend`` pass can prove ``linked``, and
    # arm the one-shot crash-gap marker from the same startup ``crashed`` flag
    # that drives crash detection, so a moment recorded after a hard kill is
    # classified ``crash_gap`` rather than ``linked``.
    agent._active_conscious_moment_id = (
        agent._lifecycle.read_active_conscious_moment_id()
    )
    agent._pending_crash_gap = crashed
    # Hoisted out of the ``if crashed:`` block so the startup-cooldown
    # gate below can read the prior crash's SDK category.
    crash_sdk_error: SDKError | None = agent._read_crash_sdk_context()
    if crashed:
        try:
            from maestro.crash import build_crash_event, handle_crash
            event = build_crash_event(
                last_iteration=resume_iteration,
                last_phase=resume_phase,
                log_path=agent.config.app_log_path,
                exit_code=resume_exit_code,
                sdk_error=crash_sdk_error,
            )
            await handle_crash(
                event,
                notifier=agent.notifier,
                journal=agent.journal,
            )
        except Exception:
            logger.warning("Crash reporting failed", exc_info=True)
        logger.warning(
            "Crash detected and reported. Continuing with normal startup."
        )
    try:
        if last_shutdown_ts:
            now = datetime.now(UTC)
            shutdown_dt = datetime.fromisoformat(last_shutdown_ts)
            downtime = now - shutdown_dt
            total_secs = max(0, int(downtime.total_seconds()))
            hours, remainder = divmod(total_secs, 3600)
            minutes, secs = divmod(remainder, 60)
            if hours > 0:
                duration_str = f"{hours}h {minutes}m {secs}s"
            elif minutes > 0:
                duration_str = f"{minutes}m {secs}s"
            else:
                duration_str = f"{secs}s"
            agent.journal.record_event(
                content=(
                    f"Waking up after {duration_str} of downtime. "
                    f"Last shutdown: {last_shutdown_ts}. "
                    f"What has changed while I was away?"
                ),
                entry_type="milestone",
                triggered_by="startup",
            )
    except Exception:
        logger.warning("Startup lifecycle detection failed", exc_info=True)

    # Startup cooldown — when the prior run crashed with a rate-limit
    # SDK error, give the upstream rate-limit window time to clear
    # before this fresh process makes its first SDK call. The gate
    # runs *before* the restored-idle-state check below so that a
    # fast crash before ``_enter_rate_limit_idle`` ran (no idle flag
    # persisted) still pays a minimum cooldown. ``0`` disables.
    cooldown = agent.config.startup_rate_limit_cooldown_seconds
    if (
        not smoke
        and crashed
        and crash_sdk_error is not None
        and crash_sdk_error.category == ErrorCategory.RATE_LIMIT
        and cooldown > 0
    ):
        logger.warning(
            "Prior run crashed with rate-limit error — sleeping %ds "
            "before first SDK call to let the upstream window clear",
            cooldown,
        )
        try:
            agent.journal.record_event(
                content=(
                    f"Prior run crashed with rate-limit error. "
                    f"Sleeping {cooldown}s before resuming SDK calls."
                ),
                entry_type="milestone",
                triggered_by="startup_rate_limit_cooldown",
            )
        except Exception:
            logger.warning(
                "Failed to journal startup rate-limit cooldown",
                exc_info=True,
            )
        await agent._safe_shutdown_wait(cooldown)

    # Honor restored rate-limit state before the first SDK call.
    # Either the prior run persisted ``rate_limit_idle_entered_at`` or
    # the consecutive-failure counter restored at or above the
    # ceiling — in both cases the new process should re-enter the
    # extended-idle probe loop directly instead of issuing a startup
    # probe that is almost guaranteed to fail. Smoke mode bypasses
    # idle entirely so deploy validation stays deterministic.
    if not smoke and (
        agent._rate_limit_idle_entered_at is not None
        or agent._consecutive_rate_limits >= _MAX_CONSECUTIVE_RATE_LIMITS
    ):
        if agent._rate_limit_idle_entered_at is None:
            await agent._enter_rate_limit_idle(
                iteration=resume_iteration,
                phase=resume_phase or "startup",
            )
        await agent._run_rate_limit_idle_probe()
        if not agent._ensure_shutdown_event().is_set():
            await agent._exit_rate_limit_idle()
        # If shutdown fired during the probe, fall through — the main
        # loop's first ``shutdown.is_set()`` check breaks before any
        # iteration runs and the ``finally`` block cleans up.
    else:
        await agent._probe_rate_limit()

    # Start Telegram polling for approvals, rejections, and messages
    telegram_polling_started = False
    from maestro.notifications.telegram import TelegramNotifier
    if isinstance(agent.notifier, TelegramNotifier):
        _on_approve, _on_reject = telegram_plan_handlers(agent.memory)

        # Hoisted out of the ``message_store is not None`` gate so
        # ``/maintenance`` toggling works even with messaging disabled.
        async def _on_message(
            text: str, reply_to: str | None = None,
            *, plan_ref: PlanRef | None = None,
        ) -> None:
            if await agent._handle_maintenance_command(text):
                return
            if agent.message_store is None:
                return
            agent.message_store.save_message(build_telegram_inbound_message(
                text, reply_to, plan_ref,
                max_length=agent.config.message_max_length,
                feedback_serviceable=feedback_serviceable(agent.config, curate_enabled),
            ))

        on_message = _on_message

        try:
            await agent.notifier.start_approval_polling(
                _on_approve, _on_reject,
                on_message=on_message,
                on_publication_approve=agent._on_publication_approve,
                on_publication_reject=agent._on_publication_reject,
            )
            telegram_polling_started = True
            logger.info("Telegram approval polling started")
        except Exception:
            logger.exception("Failed to start Telegram approval polling")

    # Restore the resume cursor from disk. ``resume_phase is None`` means
    # the previous run either never started or completed an iteration
    # cleanly — in both cases the next iteration starts fresh from the
    # first phase. ``resume_phase is not None`` means the previous run
    # was interrupted between phases and we should re-enter the same
    # iteration at the saved phase instead of from the beginning.
    if resume_phase is not None and resume_iteration > 0:
        try:
            start_phase_index = _LOOP_PHASES.index(resume_phase)
        except ValueError:
            logger.warning(
                "Unknown resume phase %r in lifecycle file; restarting iteration",
                resume_phase,
            )
            # Re-run the saved iteration from scratch — we want
            # ``iteration`` to land on ``resume_iteration`` after the
            # loop's first ``iteration += 1``.
            start_phase_index = 0
            iteration = resume_iteration - 1
        else:
            # Resume mid-iteration — the loop body must NOT increment on
            # entry, so set ``iteration`` to its target value directly,
            # and seed the ephemeral-state mirrors from disk so the
            # resumed phase sees the same context the failed pass had.
            iteration = resume_iteration
            agent._resumed_mid_iteration = True
            agent._iter_message_context = restored_message_context
            agent._iter_reply_context = restored_reply_context
            agent._iter_reflection = restored_reflection
            agent._iteration_usage = restored_iteration_usage
            agent._iter_session_a_id = restored_session_a_id
            agent._iter_session_b_id = restored_session_b_id
            logger.info(
                "Resuming loop at iteration %d phase %s",
                resume_iteration, resume_phase,
            )
    else:
        # Fresh start (no lifecycle file) or last iteration completed
        # cleanly (``next_phase is None``). The loop's first
        # ``iteration += 1`` advances us to the next iteration.
        iteration = resume_iteration
        start_phase_index = 0

    # Sync instance vars so _write_lifecycle_shutdown() has the correct
    # cursor even if shutdown fires before the first _persist_progress().
    agent._resume_iteration = iteration
    agent._resume_phase = _LOOP_PHASES[start_phase_index] if start_phase_index > 0 else None

    # Startup orphan sweep (proposal 006): delete task checkpoints older
    # than the configured age so files left behind by manually deleted
    # branches or schema drift cannot accumulate. Runs once per process
    # start — the natural point for orphan cleanup.
    agent.memory.cleanup_stale_checkpoints(
        agent.config.checkpoint_max_age_days,
    )

    first_iteration_after_restart = True
    # ``iterations_run`` counts loop bodies executed in this *process*,
    # used for the ``max_iters`` boundary. ``iteration`` is the absolute
    # iteration counter persisted across restarts. They diverge whenever
    # we resume mid-iteration after a restart.
    iterations_run = 0
    set_usage_sink(agent._on_query_usage)
    try:
        while max_iters == 0 or iterations_run < max_iters:
            if agent._ensure_shutdown_event().is_set():
                break

            # Crash loud if the fire-and-forget Telegram poll task died on
            # a handler exception. Probed here (after the shutdown check so
            # a requested stop still exits gracefully, before the standby
            # gate so a dead channel surfaces even while paused) it
            # re-raises past the per-phase guards into the crash boundary.
            agent._raise_pending_poll_failure()

            # Standby gate — pause SDK loop body during configured peak
            # hours. We sleep in bounded chunks (responsive to shutdown)
            # without bumping ``iteration``, ``iterations_run``, or the
            # persisted phase cursor, so any mid-iteration resume from a
            # prior restart is preserved across the standby period.
            if _is_in_standby_window(agent.config):
                logger.info(
                    "In standby window (%02d:00-%02d:00 UTC); "
                    "pausing SDK loop",
                    agent.config.standby_start_hour_utc,
                    agent.config.standby_end_hour_utc,
                )
                standby_sleep = min(
                    interval * 5, _STANDBY_MAX_SLEEP_SECONDS,
                )
                await agent._safe_shutdown_wait(standby_sleep)
                continue

            if is_warning_hold_active():
                try:
                    agent.journal.record_event(
                        content=(
                            f"Warning hold activated at iteration "
                            f"{iteration}. API under sustained "
                            f"pressure — pausing until clear."
                        ),
                        entry_type="milestone",
                        triggered_by="warning_hold",
                    )
                except Exception:
                    logger.warning(
                        "Failed to journal warning-hold",
                        exc_info=True,
                    )

                await agent._run_warning_hold_probe()

                if agent._ensure_shutdown_event().is_set():
                    break
                logger.info(
                    "Warning hold cleared — resuming loop",
                )
                continue

            # Capture the resume flag for the gates below, then clear it
            # immediately so subsequent iterations (after this one
            # finishes, cleanly or via retry) treat themselves as fresh.
            is_resuming = agent._resumed_mid_iteration
            agent._resumed_mid_iteration = False
            # Freeze the counter under budget throttle so cadence survives
            # the window; the body still runs. Non-gated cadence phases
            # dedup per frozen value; gated ones bind to this body-scoped
            # snapshot (``body_frozen``), so a mid-body release can't
            # re-fire them (#4150).
            freeze = agent.budget_throttle_active
            if start_phase_index == 0 and not is_resuming and not freeze:
                iteration += 1
                logger.info("Loop iteration %d", iteration)
            elif start_phase_index == 0 and not is_resuming and freeze:
                logger.info(
                    "Loop iteration %d (frozen — budget throttle active)",
                    iteration,
                )
            else:
                logger.info(
                    "Loop iteration %d (resuming at %s)",
                    iteration, _LOOP_PHASES[start_phase_index],
                )

            # Per-iteration marker for SDK telemetry aggregation, emitted
            # at the *start* of the iteration body so it precedes that
            # iteration's ``SDK call:`` log lines. Diagnostics groups SDK
            # call lines between successive markers without keeping
            # in-memory state. Emitted unconditionally on every pass that
            # opens the body (fresh / frozen / resumed): a frozen or
            # resumed pass may re-emit the same ``iter=N``, which is
            # harmless — each marker still delimits the calls that follow
            # it.
            logger.info("SDK iteration: iter=%d", iteration)

            # Fresh usage accumulator — only when starting an iteration
            # from scratch. On a mid-iteration resume we keep the prior
            # pass's accumulator (restored from disk at startup or still
            # in memory for the in-process rate-limit retry) so
            # ``usage.jsonl`` gets one record per iteration, not per pass.
            # Under a freeze the body still flushes its own usage row at
            # end-of-iteration; consecutive frozen bodies therefore emit
            # one row each, all sharing the same frozen iteration value.
            if start_phase_index == 0 and not is_resuming and not freeze:
                agent._iteration_usage = IterationUsage(
                    iteration=iteration,
                    started_at=datetime.now(UTC).isoformat(),
                )
            elif agent._iteration_usage is None:
                # Defensive — resumed mid-iteration but no usage was
                # restored. Start a fresh accumulator so the sink has a
                # place to land; the loss of earlier counts is accepted.
                agent._iteration_usage = IterationUsage(
                    iteration=iteration,
                    started_at=datetime.now(UTC).isoformat(),
                )

            if agent._last_status_iteration != iteration:
                _cached_diag: diagnostics.DiagnosticSnapshot | None = None
                try:
                    # Thread ``context_window`` and the host-capacity reading
                    # so the hoisted snapshot carries the same enrichment
                    # ``contemplate`` builds today (proposal 062 step 4,
                    # proposal 125); it then reuses this one via
                    # ``self._iter_diag_snapshot`` (#1211). Threading the
                    # reading here (not only at contemplate's fallback build)
                    # is what makes the sensor reach the loop path when the
                    # flag is on, mirroring how ``context_window`` is wired.
                    _cached_diag = diagnostics.build_diagnostic_snapshot(
                        agent.config.app_log_path,
                        memory_dir=agent.config.memory_dir,
                        context_window=latest_context_window(),
                        host_capacity=(
                            host_capacity.read_host_capacity()
                            if agent.config.host_capacity_sensor_enabled
                            else None
                        ),
                    )
                except Exception:
                    logger.warning(
                        "Failed to build diagnostic snapshot for status",
                        exc_info=True,
                    )

                agent._iter_diag_snapshot = _cached_diag
                await agent._send_status_update(
                    iteration=iteration,
                    diag_snapshot=_cached_diag,
                )
                agent._last_status_iteration = iteration

            # Per-iteration ephemeral state lives on ``self`` so
            # ``_persist_progress`` and ``_write_lifecycle_shutdown`` can
            # mirror it to ``lifecycle.json``. Resets are gated on
            # ``start_phase_index == 0`` (fresh iteration) AND not
            # resuming mid-iteration — on a rate-limit retry or crash
            # restart we restore from disk instead of wiping.
            if start_phase_index == 0 and not is_resuming:
                agent._iter_message_context = ""
                agent._iter_reply_context = ""
                agent._iter_reflection = ""
                # Drop the per-iteration merge-truth memo so the first
                # PR-servicing face this iteration re-observes (proposal 087
                # step 3). Only on a genuinely fresh iteration — a
                # resume-in-place keeps the same iteration's observations.
                agent._merge_truth_memo.clear()
                # Drop the per-iteration PR-readiness memo too (089 step 2),
                # so the first readiness consult this iteration re-observes.
                agent._pr_readiness_memo.clear()
                # Drop the per-iteration CI-failure-oracle surfaced-line cache
                # (102) so a stale surfaced fact can't author this lap's verdict.
                agent._ci_oracle_surfaced.clear()
                # Drop the per-iteration ahead-of-origin reachability memo so
                # the chosen-stand-down guard re-fetches + re-counts fresh.
                agent._ahead_of_origin_memo.clear()
                # Drop the per-iteration open-owned-PR enumeration snapshot
                # (092) so the first WIP-focus consult this iteration re-lists.
                agent._open_owned_pr_states_memo = None
                agent._open_owned_pr_states_error = None
                # Persist the bumped iteration cursor immediately, before
                # any phase runs. ``_persist_progress`` only fires at
                # phase boundaries, so without this the long first phase
                # (e.g. evolve, minutes) crashing mid-run would leave the
                # on-disk cursor at ``iteration-1`` and the next startup
                # would reset the counter. One extra atomic write per
                # fresh iteration, and it also refreshes the heartbeat.
                agent._persist_progress(iteration, _LOOP_PHASES[0])

            # Phase session state for prompt-cache reuse across phases,
            # promoted to instance attributes (proposal 106) so the
            # migrated ``_phase_*`` runners — bound methods that cannot
            # touch a caller's locals — can share them. Reset at the top of
            # each iteration body; created lazily by
            # ``_get_or_create_session`` (which gates on
            # ``config.enable_session_reuse`` internally). Smoke mode keeps
            # session reuse on so the deploy iteration exercises the same
            # SDK code path production runs.
            agent._session_a = None  # contemplation
            agent._session_b = None  # evolution

            stop_for_shutdown = False
            retry_phase_index: int | None = None
            try:
                for phase_index in range(start_phase_index, len(_LOOP_PHASES)):
                    phase = _LOOP_PHASES[phase_index]
                    agent._current_phase = phase

                    # Persist the live phase before its handler runs so
                    # the dashboard container (separate process) can see
                    # the currently executing phase via ``lifecycle.json``.
                    # ``next_phase=phase`` keeps crash-resume semantics
                    # correct: if we crash before the body executes, the
                    # next startup retries this same phase.
                    agent._persist_progress(iteration, phase)

                    will_run = agent._phase_will_run(
                        phase,
                        iteration=iteration,
                        smoke=smoke,
                        first_iteration_after_restart=first_iteration_after_restart,
                        task_every=task_every,
                        evolve_every=evolve_every,
                        dream_every=dream_every,
                        audit_self_every=audit_self_every,
                        contemplate_every=contemplate_every,
                        curate_every=curate_every,
                        curate_enabled=curate_enabled,
                        body_frozen=freeze,
                    )

                    if will_run:
                        # Data-driven dispatch (proposal 106): every phase
                        # resolves to its bound ``_phase_*`` runner through
                        # the registry table and is invoked with one uniform
                        # call — phase order and phase bodies are both data.
                        # There is no inline ``elif phase == "..."`` ladder
                        # any more; a ``KeyError`` on an unregistered phase
                        # fails loud here instead of silently skipping.
                        await agent._phase_runners[phase](
                            iteration=iteration,
                        )

                        # Dedup marker for ``_last_phase_iteration``-gated
                        # cadence phases, so a frozen ``iteration % N == 0``
                        # does not re-fire them during a throttle window.
                        if phase in DEDUPED_CADENCE_PHASES:
                            agent._last_phase_iteration[phase] = iteration

                    # Phase finished — capture the latest session ids
                    # from the live clients so the next ``_persist_progress``
                    # writes them to ``lifecycle.json`` (allowing a crash
                    # or rate-limit retry to resume the same SDK session).
                    # Bookkeeping always runs, whether or not the body
                    # ran, so the next phase always sees a consistent
                    # cursor.
                    agent._capture_session_id("a", agent._session_a)
                    agent._capture_session_id("b", agent._session_b)

                    # Persist the cursor pointing at the next phase, then
                    # check for shutdown. The current phase always runs to
                    # completion before we exit.
                    if phase_index + 1 < len(_LOOP_PHASES):
                        next_phase: str | None = _LOOP_PHASES[phase_index + 1]
                    else:
                        next_phase = None
                    agent._persist_progress(iteration, next_phase)

                    if agent._ensure_shutdown_event().is_set():
                        stop_for_shutdown = True
                        break
            except SDKError as exc:
                if exc.category == ErrorCategory.STALE_SESSION:
                    # A session opened with ``resume=<id>`` failed on
                    # its first real turn — the server-side transcript
                    # is gone (likely expired during cooldown). Clear
                    # the persisted ids so the retry creates fresh
                    # sessions, then re-enter the same phase. No
                    # cooldown: this is not a capacity failure, and the
                    # consecutive-rate-limit counter is NOT bumped. Like
                    # CLI_CRASH/STREAM_STALL the retry carries a small
                    # ceiling so a deterministic stale-session loop fails
                    # fast into crash triage rather than spinning forever.
                    retry_phase_index = phase_index
                    agent._consecutive_stale_sessions += 1
                    if (
                        agent._consecutive_stale_sessions
                        > _MAX_CONSECUTIVE_STALE_SESSIONS
                    ):
                        logger.error(
                            "Consecutive stale sessions exceeded ceiling "
                            "(%d); re-raising to trigger crash path",
                            _MAX_CONSECUTIVE_STALE_SESSIONS,
                        )
                        raise
                    agent._iter_session_a_id = None
                    agent._iter_session_b_id = None
                    agent._persist_progress(
                        iteration, _LOOP_PHASES[retry_phase_index],
                    )
                    logger.warning(
                        "Stale session resume at iteration %d phase %s "
                        "(consecutive=%d) — clearing persisted ids and "
                        "retrying fresh",
                        iteration,
                        _LOOP_PHASES[retry_phase_index],
                        agent._consecutive_stale_sessions,
                    )
                    # Fall through to ``finally`` (disconnects the
                    # dead clients) and then to the
                    # ``retry_phase_index`` post-iteration
                    # branch which re-enters mid-iteration.
                elif exc.category == ErrorCategory.STREAM_STALL:
                    # The SDK's SSE stream went silent past
                    # ``sdk_stream_idle_timeout_seconds``. The dead
                    # client is disconnected in ``finally``; like a
                    # stale-session retry the persisted ids are
                    # cleared so the retry creates fresh sessions.
                    # No cooldown: the failure is local rather than
                    # capacity-driven, but it does carry a small
                    # ceiling to fail fast on a deterministic stall.
                    retry_phase_index = phase_index
                    agent._consecutive_stream_stalls += 1
                    if (
                        agent._consecutive_stream_stalls
                        > _MAX_CONSECUTIVE_STREAM_STALLS
                    ):
                        logger.error(
                            "Consecutive stream stalls exceeded ceiling "
                            "(%d); re-raising to trigger crash path",
                            _MAX_CONSECUTIVE_STREAM_STALLS,
                        )
                        raise
                    agent._iter_session_a_id = None
                    agent._iter_session_b_id = None
                    agent._persist_progress(
                        iteration,
                        _LOOP_PHASES[retry_phase_index],
                    )
                    logger.warning(
                        "Stream stall at iteration %d phase %s "
                        "(caller=%s, consecutive=%d) — clearing persisted "
                        "ids and retrying fresh",
                        iteration,
                        _LOOP_PHASES[retry_phase_index],
                        exc.caller or "unknown",
                        agent._consecutive_stream_stalls,
                    )
                    try:
                        agent.journal.record_event(
                            content=(
                                f"Stream stall during iteration "
                                f"{iteration} at phase "
                                f"{_LOOP_PHASES[retry_phase_index]}."
                                f" Retrying with fresh sessions."
                            ),
                            entry_type="milestone",
                            triggered_by="stream_stall",
                        )
                    except Exception:
                        logger.warning(
                            "Failed to journal stream-stall re-entry",
                            exc_info=True,
                        )
                elif exc.category == ErrorCategory.CLI_CRASH:
                    # The Claude CLI subprocess died mid-conversation
                    # (typically ``ProcessError: exit code 1``). The
                    # dead client is disconnected in ``finally``; the
                    # retry opens a fresh session with
                    # ``resume=<last_session_id>`` so the server-side
                    # transcript picks up where the crash happened.
                    # No cooldown: CLI crashes are not capacity-driven.
                    retry_phase_index = phase_index
                    agent._consecutive_cli_crashes += 1
                    if (
                        agent._consecutive_cli_crashes
                        > _MAX_CONSECUTIVE_CLI_CRASHES
                    ):
                        logger.error(
                            "Consecutive CLI crashes exceeded ceiling "
                            "(%d); re-raising to trigger crash path",
                            _MAX_CONSECUTIVE_CLI_CRASHES,
                        )
                        raise
                    agent._capture_session_id("a", agent._session_a)
                    agent._capture_session_id("b", agent._session_b)
                    agent._persist_progress(
                        iteration,
                        _LOOP_PHASES[retry_phase_index],
                    )
                    logger.warning(
                        "CLI crash at iteration %d phase %s "
                        "(consecutive=%d) — re-entering phase with "
                        "resumed session",
                        iteration,
                        _LOOP_PHASES[retry_phase_index],
                        agent._consecutive_cli_crashes,
                    )
                    try:
                        agent.journal.record_event(
                            content=(
                                f"CLI crash during iteration "
                                f"{iteration} at phase "
                                f"{_LOOP_PHASES[retry_phase_index]}."
                                f" Resuming with last known session ids."
                            ),
                            entry_type="milestone",
                            triggered_by="cli_crash",
                        )
                    except Exception:
                        logger.warning(
                            "Failed to journal CLI-crash re-entry",
                            exc_info=True,
                        )
                elif exc.category != ErrorCategory.RATE_LIMIT:
                    raise
                else:
                    # The for-loop variable ``phase_index`` is still in
                    # scope here — it holds the index of the phase
                    # that raised.
                    retry_phase_index = phase_index
                    agent._consecutive_rate_limits += 1

                    # Capture the latest session ids from live clients
                    # and persist the cursor pointing at the failed
                    # phase so a hard kill during cooldown still
                    # resumes at the right spot. ``_persist_progress``
                    # mirrors the ephemeral per-iteration state
                    # (message_context, reflection, iteration_usage,
                    # session_a_id, session_b_id) so the retry
                    # re-enters the failed phase with the same context.
                    agent._capture_session_id("a", agent._session_a)
                    agent._capture_session_id("b", agent._session_b)

                    if (
                        agent._consecutive_rate_limits
                        > _MAX_CONSECUTIVE_RATE_LIMITS
                    ):
                        # Sustained rate-limiting: enter extended idle
                        # with exponential-backoff probes instead of
                        # crashing. The counter is reset on probe
                        # success so a subsequent burst gets the full
                        # fast-retry window again before re-entering
                        # idle.
                        await agent._enter_rate_limit_idle(
                            iteration,
                            _LOOP_PHASES[retry_phase_index],
                        )
                        await agent._run_rate_limit_idle_probe()
                        if agent._ensure_shutdown_event().is_set():
                            stop_for_shutdown = True
                        else:
                            await agent._exit_rate_limit_idle()
                    else:
                        agent._persist_progress(
                            iteration,
                            _LOOP_PHASES[retry_phase_index],
                        )

                        logger.warning(
                            "Rate-limited at iteration %d phase %s "
                            "(consecutive=%d) — cooling down %ds",
                            iteration,
                            _LOOP_PHASES[retry_phase_index],
                            agent._consecutive_rate_limits,
                            _RATE_LIMIT_COOLDOWN_SECONDS,
                        )
                        try:
                            agent.journal.record_event(
                                content=(
                                    f"Rate-limited during iteration "
                                    f"{iteration} at phase "
                                    f"{_LOOP_PHASES[retry_phase_index]}."
                                    f" Cooling down for "
                                    f"{_RATE_LIMIT_COOLDOWN_SECONDS}s."
                                ),
                                entry_type="milestone",
                                triggered_by="rate_limit",
                            )
                        except Exception:
                            logger.warning(
                                "Failed to journal rate-limit cooldown",
                                exc_info=True,
                            )
                        await agent._safe_shutdown_wait(
                            _RATE_LIMIT_COOLDOWN_SECONDS,
                        )
            finally:
                # Disconnect phase sessions at iteration end (normal,
                # rate-limit, or error path). The session ids were
                # already captured into ``self._iter_session_*_id``
                # before this finally runs, so disconnect is safe.
                for _sess in (agent._session_a, agent._session_b):
                    if _sess is not None:
                        with contextlib.suppress(Exception):
                            await disconnect_session(_sess)
                agent._session_a = agent._session_b = None

            if retry_phase_index is not None:
                # Rate-limit retry — the cooldown has elapsed (or was
                # cut short by shutdown). Re-enter the same iteration
                # at the failed phase on the next loop body. Do NOT
                # bump ``iterations_run`` (retries don't count toward
                # ``max_iters``) and do NOT flush usage (we keep
                # accumulating into the same ``IterationUsage``
                # record so ``usage.jsonl`` gets one row per iteration
                # — counts may overshoot the failed phase's actual
                # work by one phase-retry's worth, documented in
                # docs/loop-lifecycle.md).
                if agent._ensure_shutdown_event().is_set():
                    break
                start_phase_index = retry_phase_index
                agent._resumed_mid_iteration = True
                # ``check_merged_plans`` still counts as "first after
                # restart" because the iteration has not completed.
                continue

            # Either the iteration finished cleanly or we are bailing
            # out for shutdown. Subsequent iterations always start at
            # phase 0 and ``check_merged_plans`` is no longer "first
            # after restart".
            start_phase_index = 0
            if not stop_for_shutdown:
                first_iteration_after_restart = False
                iterations_run += 1
                # Clean iteration end — clear ephemeral per-iteration
                # state so the next iteration starts truly fresh even
                # if ``_resumed_mid_iteration`` is somehow left true
                # by a future bug. ``_persist_progress`` on the next
                # phase will overwrite lifecycle.json anyway.
                agent._consecutive_rate_limits = 0
                agent._consecutive_cli_crashes = 0
                agent._consecutive_stream_stalls = 0
                agent._consecutive_stale_sessions = 0
                agent._rate_limit_idle_entered_at = None
                agent._iter_message_context = ""
                agent._iter_reply_context = ""
                agent._iter_reflection = ""
                agent._iter_session_a_id = None
                agent._iter_session_b_id = None
                agent._resume_iteration = iteration
                agent._resume_phase = None
                agent._current_phase = None
                # Flush the idle-between-iterations state to lifecycle.json
                # so the dashboard's public visualizer renders 'idle'
                # during the inter-iteration sleep instead of the last
                # phase that ran.
                agent._persist_progress(iteration, None)

            if stop_for_shutdown:
                # A finished iteration books now; a mid-iteration one
                # rides lifecycle.json to the resume (#4149).
                if agent._resume_phase is None:
                    agent._flush_iteration_usage()
                break

            # Persist this iteration's conscious-workspace broadcast receipts
            # (proposal 130). Runs only on a normally completing iteration —
            # the ``stop_for_shutdown`` break above skips it, so on a shutdown
            # break or crash the ``attend``-time bootstrap row survives without
            # receipts, the honest degraded state. Best-effort so a ledger
            # write error never crashes the loop: it catches ``OSError`` (the
            # JSONL backend) *and* ``sqlite3.Error`` (the
            # ``SqliteConsciousEpisodeLedger`` under ``[paths]
            # sqlite_state_enabled``, whose ``append`` raises the sqlite3
            # family, not ``OSError``). No-op unless the broadcast flag is on
            # and receipts were collected.
            try:
                agent._finalize_conscious_episode()
            except (OSError, sqlite3.Error):
                logger.debug(
                    "Failed to finalize conscious episode", exc_info=True,
                )

            agent._flush_iteration_usage()

            # Burn-rate throttle — end-of-iteration so the rate is based
            # on the window that now includes this iteration's SDK spend.
            # The transition check runs every iteration (even the last
            # one, when no sleep follows) so a released throttle fires
            # its exit notification promptly.
            gating_multiplier, sleep_multiplier, snapshot = (
                agent._compute_budget_throttle_multiplier(smoke)
            )
            # The gating multiplier (untightened limit) drives phase
            # gating; the sleep multiplier (peak-tightened) drives the
            # inter-iteration gap below.
            await agent._maybe_notify_throttle_transition(
                gating_multiplier, snapshot,
                sleep_multiplier=sleep_multiplier,
            )
            # Peak-hours scheduled throttle — the window slows the loop by
            # tightening the burn-rate budget (folded into the sleep
            # multiplier above), so its entry/exit transition must be
            # observed independently of the burn-rate gating check.
            await agent._maybe_notify_peak_throttle_transition()
            await agent._maybe_notify_maintenance_transition()

            # Sleep between iterations.  Soft-warning throttling
            # (``allowed_warning``) and rate limits are handled
            # internally by the SDK (per-``query()`` sleep), so the
            # loop just sleeps the base interval, optionally stretched
            # by the self-regulating burn-rate throttle above (and more
            # so inside the peak-hours window, which tightens the budget
            # via the sleep multiplier). All phases keep running in
            # throttled mode — only the inter-iteration gap stretches. As
            # samples age out of the rolling window, the rate falls, the
            # multiplier falls, and the loop returns to base pace on its
            # own. Rate-limit ``SDKError``s trigger a cooldown; other
            # failures crash the loop and crash detection restarts it.
            if max_iters == 0 or iterations_run < max_iters:
                effective_interval = interval * sleep_multiplier
                if sleep_multiplier > 1.0:
                    agent._current_phase = "budget_throttle"
                    # Display-only: cursor stays None (#4148).
                    agent._persist_progress(iteration, None)
                await agent._heartbeat_wait(effective_interval)
                if sleep_multiplier > 1.0:
                    agent._current_phase = None
    finally:
        # ``sys.exc_info()`` reports the exception unwinding us, if any.
        # An unhandled one (e.g. ``CancelledError``) passes
        # ``crashed=True`` so ``shutdown()`` skips ``last_shutdown`` and
        # the next startup's crash detection fires. ``SystemExit`` /
        # ``KeyboardInterrupt`` stay graceful (explicit exit requests).
        # The usage accumulator survives until ``shutdown()`` mirrors it
        # into lifecycle.json (#4149).
        clear_usage_sink()
        exc_type, exc_val, exc_tb = sys.exc_info()
        crashed_unwind = (
            exc_type is not None
            and not isinstance(exc_val, (SystemExit, KeyboardInterrupt))
        )
        # Fail loud at the outermost crash boundary: emit the
        # originating exception's type, message, and traceback to
        # ``maestro.log`` before ``shutdown()`` runs. Without this
        # an unhandled ``BaseException`` (e.g. ``CancelledError``
        # from a stray ``async_generator_athrow`` orphan) escapes
        # with no record in the loop's primary log — leaving only
        # the generic shutdown WARNING and forcing operators to
        # rely on docker stdout for the originating traceback.
        if crashed_unwind and exc_type is not None and exc_val is not None:
            logger.error(
                "run_loop crashing on unhandled %s: %s",
                exc_type.__name__,
                exc_val,
                exc_info=(exc_type, exc_val, exc_tb),
            )
        # When the unwinding exception carries a structured exit code
        # (SDKError wrapping ProcessError), merge it into the lifecycle
        # cursor so the next startup's crash report includes it.
        # The full SDKError is passed so category, stderr tail and
        # retries survive into the one startup hands ``build_crash_event``.
        if crashed_unwind and isinstance(exc_val, SDKError):
            agent._persist_crash_exit_code(
                exc_val.exit_code, sdk_error=exc_val,
            )
        # Stop Telegram polling before shutdown. The teardown is wrapped in
        # its own ``try`` so ``shutdown()`` runs in the ``finally`` even when
        # ``stop_approval_polling()`` raises ``SystemExit`` /
        # ``KeyboardInterrupt``: the exit request still propagates (honoring
        # the #398 contract), but only after shutdown unlinks the PID file
        # and writes ``last_shutdown``, so the next startup does not misread
        # this graceful exit as a crash.
        try:
            if isinstance(agent.notifier, TelegramNotifier) and telegram_polling_started:
                try:
                    await agent.notifier.stop_approval_polling()
                    logger.info("Telegram approval polling stopped")
                except (Exception, asyncio.CancelledError):
                    # Narrow catch (not ``BaseException``): a network error
                    # or ``asyncio.CancelledError`` during teardown must not
                    # block shutdown, but ``SystemExit`` / ``KeyboardInterrupt``
                    # are explicit exit requests and must propagate (after the
                    # ``finally`` below runs shutdown), not be swallowed here.
                    logger.exception("Error stopping Telegram approval polling")
        finally:
            await agent.shutdown(crashed=crashed_unwind)
            agent._iteration_usage = None
    return True
