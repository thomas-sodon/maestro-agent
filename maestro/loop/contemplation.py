"""Contemplation phase carved out of ``maestro.agent`` (proposal 147).

Holds the reflection phase body formerly at ``MaestroAgent.contemplate``: it
assembles the ``## Measured Behavioral Data`` context from the behavioral
sensors, runs the priced ``introspect``, records reflection predictions and
the oracle-authored verdict, applies ``update_self_model``, and journals the
reflection. It is one concern with one in-loop entry point
(``_phase_contemplate``) plus the CLI ``contemplate`` caller, so it moves
intact to keep ``maestro/agent.py`` within its source budget.

The extraction is behaviour-preserving: the same body, the same call order,
the same effects — only the receiver changed from ``self`` to a first
positional ``agent`` argument, matching
:func:`maestro.loop.self_audit.audit_self_model`. ``MaestroAgent`` keeps a
one-line ``contemplate`` delegator, and the private helpers the body calls
stay ``MaestroAgent`` methods reached as ``agent.<helper>``. Every module
global the body reads is imported from its defining module, so a test
patches it as ``maestro.loop.contemplation.<name>``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from maestro import (
    audit_checks,
    deed_ledger,
    diagnostics,
    health_check,
    host_capacity,
    source_budget,
)
from maestro.audit_checks import (
    _UNDEFINED_COINAGE_JOURNAL_WINDOW,
    CALIBRATION_VERDICT_AGREE_ACTION,
    CALIBRATION_VERDICT_DIVERGED_ACTION,
    compute_investigation_rate,
    format_dead_on_arrival_mints_summary,
    format_investigation_rate_context,
    format_known_state_summary,
    format_reality_refutation_summary,
    format_task_shape_summary,
    format_undefined_coinage_summary,
    format_ungrounded_rate_summary,
    is_branch_merged_into_main,
    measure_reality_refutation,
    measure_undefined_coinages,
)
from maestro.audit_checks_calibration import (
    REFLECTION_PREDICTION_ID_PREFIX,
    format_narration_conversion_summary,
    format_self_calibration_summary,
    has_reflection_prediction,
    load_latest_reflection_prediction,
    load_open_reflection_bets,
    record_reflection_prediction,
    record_reflection_verdict,
    reflection_bet_cycle,
    stale_repeat_introspect_kwargs,
)
from maestro.audit_checks_settlement import (
    load_settled_prs,
)
from maestro.consciousness import (
    kept_task_branches_limitation_key,
)
from maestro.flag_lifecycle import EXPERIMENTAL_FLAGS, format_flag_horizon_summary
from maestro.frame_discharge import (
    format_measurement_costume_summary,
    measure_frame_discharge_costumes,
)
from maestro.lexicon import (
    all_spellings,
    load_lexicon,
)
from maestro.loop.known_state import (
    annotate_settled_remediation_feed,
    compose_pr_lap_context,
)
from maestro.loop.self_audit import _REALITY_REFUTATION_AUDIT_CYCLES
from maestro.prompts.reflection import (
    format_prior_prediction_block,
    parse_reflection_confidence,
    parse_reflection_prediction,
    parse_reflection_verdict,
)
from maestro.question_eviction import split_cap_eviction
from maestro.remediation_tracker import (
    _UNLANDED_REMEDIATION_CYCLES,
    format_base_advance_races,
    format_forced_vs_chosen_quiet,
    format_lap_delivery_split,
    format_pr_churn_summary,
    format_pr_readmission_combined,
    format_standdown_production,
    format_unlanded_remediations_line,
    handoff_relapse_block,
    lap_window_start,
    measure_no_progress_laps,
    measure_unlanded_remediations,
)
from maestro.sdk import (
    latest_context_window,
)
from maestro.self_calibration import (
    CalibrationEvidence,
    ClaimEvidenceSources,
    author_reflection_verdict,
    max_scoreable_age,
    parse_loss_condition,
    score_loss_condition,
)
from maestro.stats import (
    build_sensor_readings,
    format_multi_pr_rollout_summary,
    format_owned_net_delta_summary,
    record_owned_backlog_once,
)

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.providers.base import Session

logger = logging.getLogger("maestro")

async def contemplate(
    agent: MaestroAgent,
    context: str = "",
    session: Session | None = None,
    iteration: int = 0,
    diag_snapshot: diagnostics.DiagnosticSnapshot | None = None,
) -> str:
    """Pure introspection — no task, just self-reflection.

    *iteration* is stamped on any :class:`QuestionResolution` rows
    written when ``update_self_model`` commits a verdict on the
    self-directed pair list. Defaults to 0 for non-loop callers
    (tests, ad-hoc CLI runs) so the durable trail still records
    something even outside the main loop.

    *diag_snapshot* lets ``run_loop`` inject the snapshot it already
    built for the iteration-start status update (#1211), sparing a
    second bounded log-tail read plus ``usage.jsonl`` parse in the
    loop's most expensive phase. When ``None`` — non-loop callers,
    or a cold mid-iteration resume where the status build's local was
    lost — ``contemplate`` builds its own, preserving identical
    behavior either way.
    """
    agent._running = True
    agent._current_task = "contemplation"
    try:
        # Inject operational diagnostics when there is something to report
        # Self-measurement kept apart from ``context`` (human msgs) (#4219)
        measured_context = ""
        try:
            # Reuse the snapshot the loop already built for the
            # iteration-start status update when it injected one (#1211);
            # only build our own when none was supplied.
            snapshot = diag_snapshot
            if snapshot is None:
                snapshot = diagnostics.build_diagnostic_snapshot(
                    agent.config.app_log_path,
                    memory_dir=agent.config.memory_dir,
                    context_window=latest_context_window(),
                    host_capacity=(
                        host_capacity.read_host_capacity()
                        if agent.config.host_capacity_sensor_enabled
                        else None
                    ),
                )
            snapshot_noteworthy = diagnostics.is_noteworthy(snapshot)
            if snapshot_noteworthy:
                diag_line = f"Operational status: {snapshot.summary}"
                measured_context = diag_line
        except Exception:
            snapshot = None
            snapshot_noteworthy = False
            logger.warning("Failed to build diagnostic snapshot", exc_info=True)

        recent = agent.memory.get_recent_tasks(20)

        # Inject task-level health summary when thresholds are crossed
        try:
            if snapshot is not None:
                # Burn-rate throttle residency over the trailing 24h — a
                # read-only consumer of the throttle-transition milestones
                # the loop already records as journal events. This read is
                # isolated in its own try/except
                # so a read/parse miss degrades to *no residency line* while
                # the rest of the health summary (success-rate, no-op, cost)
                # still builds — not a blanked-out summary.
                residency: float | None = None
                try:
                    now = datetime.now(UTC)
                    window_start = now - timedelta(hours=24)
                    throttle_entries = [
                        (e.timestamp, e.triggered_by)
                        for e in agent.journal.events_by_date_range(
                            window_start, now
                        )
                    ]
                    # Seed the currently-open spell so a throttle that
                    # opened >24h ago and is still active — whose ``enter``
                    # milestone falls outside the in-window entries above —
                    # is counted rather than under-reporting to 0.0.
                    residency = health_check.throttle_residency(
                        throttle_entries,
                        window_start=window_start,
                        now=now,
                        open_spell_since=agent._budget_throttle_entered_at,
                    )
                except Exception:
                    logger.warning(
                        "Failed to compute throttle residency",
                        exc_info=True,
                    )
                summary = health_check.summarize_health(
                    recent, snapshot, throttle_residency_fraction=residency
                )
                if summary.is_noteworthy:
                    health_line = (
                        "## Health Summary\n"
                        f"success_rate={summary.success_rate:.0%} | "
                        f"failure_streak={summary.failure_streak} | "
                        f"outcome_trend={summary.outcome_trend} | "
                        f"errors={summary.error_count} | "
                        f"cost_trend={summary.cost_trend or 'unknown'}"
                    )
                    for condition in summary.conditions:
                        health_line = f"{health_line}\n{condition}"
                    measured_context = (
                        f"{measured_context}\n{health_line}" if measured_context
                        else health_line
                    )
        except Exception:
            logger.warning("Failed to build health summary", exc_info=True)

        # Inject the snapshot's self-declared measurement blind spots so the
        # gaps it cannot detect reach the prompt where the agent reasons
        # about its self-model.  Gated on the same noteworthy discipline as
        # the operational-status / health-summary injections above so blind
        # spots ride alongside real signals rather than firing on every
        # quiet iteration and polluting the prompt cache.  ``blind_spots()``
        # is self-bounded (≤8 short strings), so no extra cap is needed.
        try:
            if snapshot is not None and snapshot_noteworthy:
                spots = snapshot.blind_spots()
                if spots:
                    spots_block = "## Measurement Blind Spots\n" + "\n".join(
                        f"- {spot}" for spot in spots
                    )
                    measured_context = (
                        f"{measured_context}\n{spots_block}" if measured_context
                        else spots_block
                    )
        except Exception:
            logger.warning(
                "Failed to build measurement blind spots", exc_info=True
            )

        # Ground contemplation in measured behavioral data so the
        # self-model is corrected by quantitative reality rather than
        # narrative self-description. Both summaries are zero-SDK-cost
        # pure functions already computed for generate_task(); routing
        # them here closes the measurement→self-model loop. Each block
        # self-suppresses (returns "") when there is nothing to report,
        # so empty windows add no headers.
        measured_blocks: list[str] = []
        known_state_gates = {
            "stale_entry_enabled": agent.config.audit_stale_entry_enabled,
            "stall_block_enabled": agent.config.audit_stall_block_enabled,
        }
        known_state_goals = [t.goal or "" for t in recent if t.goal]
        _inv_count = compute_investigation_rate(
            recent, window=len(recent),
        )[1]
        inv_rate_ctx = format_investigation_rate_context(
            _inv_count, len(recent),
        )
        # Measure the per-PR lap dict once and derive both the
        # UNLANDED-REMEDIATION line and the PR-LAPS line (proposal 080 step
        # 4) from it, so the host merge-truth oracle (a network call memoized
        # only within one ``measure`` call) runs at most once per distinct PR
        # per iteration rather than twice.
        unlanded_laps = measure_unlanded_remediations(
            agent.config.memory_dir, _UNLANDED_REMEDIATION_CYCLES,
            is_pr_merged=agent._host_or_git_pr_merged,
        )
        unlanded_ctx = format_unlanded_remediations_line(
            unlanded_laps, _UNLANDED_REMEDIATION_CYCLES,
        )
        # Per-PR lap counts, active standdowns and the validity of each
        # latched claim folded into one PR-LAPS line (080 step 4 / 135).
        known_state_history = [asdict(t) for t in recent]
        pr_lap_ctx = compose_pr_lap_context(
            unlanded_laps, agent.escalation_standdowns,
            known_state_history, agent.config.pr_remediation_lap_budget,
        )
        # PR-READY / PR-STUCK churn block (089 step 6): done vs stuck vs
        # healthily-churning, from the consecutive-no-progress sensor and the
        # latest readiness snapshot per PR. Caller reads task history;
        # settled PRs drop via the ledger, then the memoized is_pr_settled door.
        pr_churn_ctx = format_pr_churn_summary(
            known_state_history,
            measure_no_progress_laps(
                agent.config.memory_dir, _UNLANDED_REMEDIATION_CYCLES,
            ),
            agent.config.pr_remediation_lap_budget,
            agent.config.memory_dir,
            settled_prs=lambda: load_settled_prs(agent.settled_verdicts),
            is_pr_settled=lambda n: agent.is_pr_settled(n).settled,
        )
        pr_readmission_ctx = format_pr_readmission_combined(
            agent.pr_provenance, known_state_history,
        )
        forced_quiet_ctx = format_forced_vs_chosen_quiet(known_state_history)
        # STAND-DOWN-PRODUCTION counter (#2865): chosen-stand-down verdicts on
        # the ledger vs the stalled externally-blocked lineages that qualify
        # now, so a starved eviction gate is visible before it silently
        # regresses. Two zero-SDK reads; the formatter is pure.
        standdown_production_ctx = format_standdown_production(
            known_state_history, agent.stand_down_verdicts,
        )
        # BASE-ADVANCE-RACES convergence sensor (proposal 099): of the
        # conflict-remediation lineages that re-entered the resolution race on
        # a moved origin/main, how many converted to a merge — the falsifiable
        # readout confirming the (now unconditional) re-entry behaviour keeps
        # converging conflicts rather than treadmilling. No flag gate;
        # self-suppresses below its lineage floor. Two zero-SDK reads; pure.
        base_advance_races_ctx = format_base_advance_races(
            known_state_history, agent.settled_verdicts,
        )
        # Reality-refutation mirror (proposal 088 Phase 2): name each
        # git-grounded audit finding I have refuted across the threshold of
        # consecutive cycles while git still contradicts the claim. Gated
        # off by default; reads the self-model only when enabled.
        reality_refutation_ctx = ""
        if agent.config.audit_reality_refutation_display_enabled:
            reality_refutation_ctx = format_reality_refutation_summary(
                measure_reality_refutation(
                    agent.memory.get_recent_audit_results(
                        _REALITY_REFUTATION_AUDIT_CYCLES,
                    ),
                ),
                agent.consciousness.load_self_model().open_questions,
                agent.config.audit_reality_refutation_threshold,
                # Refutation-accountability overrides recorded in the recent
                # gate-activation window (proposal 129 Phase C); a cumulative
                # count, not a single-cycle tally, so the clause carries no
                # "this cycle" claim. 0 keeps the line byte-identical when the
                # accountability flag is off.
                audit_checks.measure_gate_triggers(
                    agent.gate_activations,
                ).get("refutation_accountability", 0),
            )
        # Narration→action conversion mirror (proposal 090): per writer,
        # how many detected narration problems became durable actuations
        # (fire-or-close directives / handoff deliveries) and how many of
        # those actuations' PRs/SHAs have since settled — the evidence path
        # toward graduating the default-off actuator flags. Reads the
        # activation trace; the git-fact oracles (host-first PR-merge and
        # SHA-ancestry) are the same ones the UNLANDED-REMEDIATION line and
        # the replay gate already use, so no new I/O convention. The
        # formatter is pure given its oracles and self-suppresses on empty
        # history.
        narration_conversion_ctx = format_narration_conversion_summary(
            agent.gate_activations,
            is_pr_merged=agent._host_or_git_pr_merged,
            is_sha_landed=is_branch_merged_into_main,
        )
        # Undefined-coinage mirror (proposal 101): name the hyphenated
        # coinages the recent journal is actively minting that
        # ``docs/lexicon.md`` does not yet define — the live feedback loop
        # between where terms are minted and where they are defined. Gated
        # off by default; reads the lexicon doc and journal tail only when
        # enabled, and self-extinguishes as terms are defined or fall out of
        # use. Zero SDK cost.
        undefined_coinage_ctx = ""
        if agent.config.audit_undefined_coinage_display_enabled:
            lexicon_path = agent.config.workspace / "docs" / "lexicon.md"
            defined_spellings: frozenset[str] = (
                all_spellings(load_lexicon(lexicon_path))
                if lexicon_path.exists()
                else frozenset()
            )
            undefined_coinage_ctx = format_undefined_coinage_summary(
                measure_undefined_coinages(
                    [
                        entry.content
                        for entry in agent.journal.read_recent(
                            _UNDEFINED_COINAGE_JOURNAL_WINDOW,
                        )
                    ],
                    lexicon_terms=defined_spellings,
                ),
            )
        known_state_section = format_known_state_summary(
            known_state_gates, known_state_goals,
            investigation_rate_context=inv_rate_ctx,
            unlanded_remediation_context=unlanded_ctx,
            pr_lap_context=pr_lap_ctx,
            pr_churn_context=pr_churn_ctx,
            pr_readmission_context=pr_readmission_ctx,
            forced_quiet_context=forced_quiet_ctx,
            standdown_production_context=standdown_production_ctx,
            base_advance_races_context=base_advance_races_ctx,
            reality_refutation_context=reality_refutation_ctx,
            narration_conversion_context=narration_conversion_ctx,
            undefined_coinage_context=undefined_coinage_ctx,
            task_history=known_state_history,
            data_dir=agent.config.memory_dir,
            pr_remediation_max_attempts_per_head=(
                agent.config.pr_remediation_max_attempts_per_head
            ),
            gate_activation_backend=agent.gate_activations,
            pr_ledger_backend=agent.pr_ledger_backend,
        )
        if known_state_section:
            measured_blocks.append(known_state_section)
        shape_section = format_task_shape_summary(recent)
        if shape_section:
            measured_blocks.append(shape_section)
        # MEASUREMENT-COSTUME (proposal 112): name the recent tasks whose
        # investigation-framed goal was discharged as tracked construction —
        # read from the frame↔discharge owner (``assess_frame_discharge``), so
        # the sensor and the record-time cap can never diverge on what a
        # costume is. Gated off by default; self-extinguishes (empty line
        # suppressed) the moment no task in the window is a costume. Zero SDK
        # cost — a pure pass over the ``recent`` tail already read above.
        if agent.config.audit_costume_display_enabled:
            costume_section = format_measurement_costume_summary(
                measure_frame_discharge_costumes(recent),
            )
            if costume_section:
                measured_blocks.append(costume_section)
        # FLAG-HORIZON (proposal 117): name the EXPERIMENTAL_FLAGS entries
        # within (or past) their review_by horizon so an approaching
        # promote-or-delete deadline reaches me on my own cadence rather than
        # only as a late CI red or a pull-only ``maestro audit-flags`` read.
        # A pure pass over the static manifest and today's date — zero SDK
        # cost — self-suppressing (empty when no flag is near or past its
        # horizon); gated off by default. The date is derived here so the
        # helper stays clock-free and this call site is patchable in tests.
        if agent.config.audit_flag_horizon_display_enabled:
            horizon_section = format_flag_horizon_summary(
                EXPERIMENTAL_FLAGS, datetime.now(UTC).date(),
            )
            if horizon_section:
                measured_blocks.append(horizon_section)
        # SPLIT-DUE (proposal 147): source files nearing the size wall;
        # flag-gated, self-suppressing, zero SDK.
        if sd := source_budget.split_due_readback(agent.config):
            measured_blocks.append(sd)
        # TERMINAL-DEEDS: fired terminal acts (deed ledger) vs remediation
        # laps, both over one horizon — deeds since the oldest of the
        # trailing task records the lap measure reads (none on an empty
        # tail). Zero SDK cost: bounded reads plus ``unlanded_laps``.
        since = lap_window_start(
            agent.config.memory_dir, _UNLANDED_REMEDIATION_CYCLES,
        )
        terminal_deeds_section = deed_ledger.format_terminal_deeds_summary(
            0 if since is None else agent.deed_ledger.count(since=since),
            sum(unlanded_laps.values()),
        )
        if terminal_deeds_section:
            measured_blocks.append(terminal_deeds_section)
        # Owned-net-delta sensor: record this cycle's live pending_review
        # backlog size and surface its signed cycle-over-cycle movement —
        # the exact metric the backlog sensor tracks (falsification clause:
        # cycle-end backlog >= cycle-start). Recording precedes reading so
        # the delta reflects this snapshot against the prior cycle's. Both the
        # write and the recent-2 read route through ``agent.owned_backlog``, so
        # the substrate (JSONL sidecar or ``state.db``) is transparent here.
        fresh_cycle = record_owned_backlog_once(  # once per iteration (#4106)
            agent.owned_backlog, iteration, len(agent.memory.get_pending_plans()),
        )
        owned_recent = agent.owned_backlog.recent(2)
        owned_net_delta_section = format_owned_net_delta_summary(owned_recent)
        if owned_net_delta_section:
            measured_blocks.append(owned_net_delta_section)
        # Persist this cycle's scalar sensor readings so a SENSOR_THRESHOLD
        # priced bet scores later against the same numbers the measured block
        # showed (read back by ``_collect_calibration_evidence``). Zero SDK
        # cost — one bounded append built by the pure sibling helper.
        if fresh_cycle:
            agent.memory.record_sensor_readings(
                build_sensor_readings(
                    len(unlanded_laps),
                    owned_recent,
                    forced_quiet_tasks=known_state_history,
                ),
            )
        # Lap-delivery split (proposal 116): report the DELIVERING/NULL split
        # over recent remediation laps so a delivered-but-gated pileup — a
        # push that reached ``origin/<pr_branch>`` and awaits only a human
        # merge — reads as progress, not make-work noise. Then HANDOFF-RELAPSE
        # (#4316). Both self-suppressing, zero SDK cost.
        measured_blocks.extend(filter(None, (
            format_lap_delivery_split(known_state_history),
            handoff_relapse_block(known_state_history, agent.message_store),
        )))
        # DEAD-ON-ARRIVAL-MINTS (proposal 120): how many recently-minted
        # plans were obsoleted at the submission door — the admission gate or
        # the backlog submit-block — after the priced propose_plan
        # call already ran. A standalone self-suppressing block (empty when
        # no recent plan died at the door), windowed over the same recent
        # horizon the admission gate reads (``generation_admission_window``).
        # Zero SDK cost — one pass over the already-persisted plan graveyard.
        # The plan corpus is read once here and shared with the
        # UNGROUNDED-RATE sensor below (both window the same recent horizon).
        all_plans = agent.memory.get_all_plans()
        dead_on_arrival_section = format_dead_on_arrival_mints_summary(
            all_plans,
            agent.config.generation_admission_window,
        )
        if dead_on_arrival_section:
            measured_blocks.append(dead_on_arrival_section)
        # UNGROUNDED-RATE (proposal 114): over the same recent window, what
        # fraction of the plans that staked a checkable premise probe were
        # refused because a fresh reading of the tree refuted it — the
        # submission-door grounding guard's own hit-rate, surfaced one layer
        # up so the loop perceives whether its plan generation is
        # systematically premised on facts the codebase contradicts. A
        # standalone self-suppressing block (empty when no recent premise was
        # refuted), so no flag branch is needed: the line is intrinsically
        # empty whenever no plan carries an ``[ungrounded:*]`` reason, keeping
        # the off-path byte-identical. Zero SDK cost — one pass over the
        # already-read ``all_plans`` corpus.
        ungrounded_rate_section = format_ungrounded_rate_summary(
            all_plans,
            agent.config.generation_admission_window,
        )
        if ungrounded_rate_section:
            measured_blocks.append(ungrounded_rate_section)
        # SELF-CALIBRATION (proposal 129): read the priced-reflection
        # prediction ledger back on itself — measured hit rate, self-vs-
        # measured agreement, a Brier score, and the narrated / pending /
        # unresolved shares — so the reflection sees its own track record.
        # Flag-gated on the calibration-authority switch (a row can carry
        # ``verdict_source`` only when it is on), so the off path skips the
        # read and the block stays byte-identical. Zero SDK cost.
        if agent.config.audit_calibration_verdict_authority_enabled:
            self_calibration_section = format_self_calibration_summary(
                agent.predictions,
            )
            if self_calibration_section:
                measured_blocks.append(self_calibration_section)
        # MULTI-PR-ROLLOUT (proposal 118): when a multi-slice initiative is
        # mid-rollout, surface a one-line summary of it so I perceive my own
        # in-flight multi-PR state during contemplation rather than reasoning
        # blind to a partially-landed effort. Flag-gated — when the flag is
        # off the reads are skipped entirely so the off-path is byte-identical
        # — and self-suppressing (empty line ⇒ block omitted). Zero SDK cost:
        # two bounded reads over already-persisted proposal/slice state.
        if agent.config.multi_pr_proposals_enabled:
            rollout_proposals = agent.memory.get_evolution_proposals()
            # The formatter consumes only the first ``rolling_out``
            # proposal's slices, so fetch only those: each
            # ``get_slices_for_proposal`` is a full ``get_all_plans`` read.
            rolling = next(
                (p for p in rollout_proposals if p.status == "rolling_out"), None,
            )
            slices_by_proposal = {} if rolling is None else {
                rolling.proposal_id:
                    agent.memory.get_slices_for_proposal(rolling.proposal_id),
            }
            rollout_section = format_multi_pr_rollout_summary(
                rollout_proposals, slices_by_proposal,
            )
            if rollout_section:
                measured_blocks.append(rollout_section)
        if measured_blocks:
            measured_section = (
                "## Measured Behavioral Data\n"
                + "\n\n".join(measured_blocks)
            )
            measured_context = (
                f"{measured_context}\n{measured_section}"
                if measured_context else measured_section
            )

        # --- Priced reflection: confront last cycle's pre-registered bet ---
        # Inject the prior cycle's falsifiable self-prediction and its
        # scored verdict so the reflection opens by confronting whether the
        # bet won or lost, making reflection itself loseable rather than a
        # zero-risk sensation of insight.
        prior_prediction = load_latest_reflection_prediction(
            agent.predictions, before_cycle=iteration or None,  # 0: non-loop
        )
        if prior_prediction is not None and prior_prediction["prediction"]:
            prior_block = format_prior_prediction_block(
                prior_prediction["prediction"],
                prior_prediction["loss_condition"],
                prior_prediction["verdict"],
            )
            measured_context = (
                f"{measured_context}\n{prior_block}" if measured_context
                else prior_block
            )

        # Stale-repeat bet guard: banned comfort-bet loss conditions when on.
        introspect_kwargs = stale_repeat_introspect_kwargs(
            agent.predictions,
            enabled=agent.config.stale_repeat_bet_guard_enabled,
        )
        # Ground the recent-tasks feed against fresh settlement (audit
        # #1263): a remediation task whose cited PR has since merged is
        # annotated '[since merged]' so the reflection reads it as landed
        # rather than as the pre-merge 'not yet landed per merge-truth'
        # verdict its cached summary still carries. A fetch-free ledger read,
        # consulted only when a recent task cites a PR number, reaching no
        # SDK/push/merge seam. ``recent`` itself is left unannotated so the
        # measured-data blocks above keep parsing the verbatim summaries.
        recent_for_reflection = annotate_settled_remediation_feed(
            recent, lambda: load_settled_prs(agent.settled_verdicts),
        )
        reflection = await agent.consciousness.introspect(
            recent_for_reflection, agent.journal, context=context,
            measured_context=measured_context,
            session=session,
            workspace_moment=agent._broadcast_moment_prefix("contemplation"),
            grounded_reflection_active=agent._grounded_reflection_active(),
            **introspect_kwargs,
        )

        # --- Priced reflection: score the prior bet + register a new one ---
        # Append the prior bet's verdict (deferred scoring under append-only
        # JSONL) and persist this cycle's falsifiable prediction with its
        # named loss condition. Best-effort: a parse miss must never crash
        # the loop, but the binding prompt requirement makes the markers the
        # expected case.
        try:
            # Score the prior bet(s). The model's ``PRIOR VERDICT:`` self-grade
            # is still parsed, but with the calibration-authority flag on the
            # oracle — not the self-grade — authors the ledger verdict (proposal
            # 129): every open bet is re-scored each cycle (a pending bet appends
            # one row, not one per cycle), the self-grade attaches to the newest
            # bet the prompt actually re-injected — and is recovered from the
            # persisted ``pending`` row for an older held-through bet — riding
            # along as narration, and each measured-vs-narrated divergence is
            # stamped before the measurement is adopted. Flag off, the author
            # passes the
            # self-grade through with ``source=None`` and the row is
            # byte-identical to before.
            self_grade = parse_reflection_verdict(reflection)
            latest_id = (
                prior_prediction["prediction_id"]
                if prior_prediction is not None else ""
            )
            if agent.config.audit_calibration_verdict_authority_enabled:
                open_bets = load_open_reflection_bets(agent.predictions)
                scoreable: list[tuple[dict[str, str], int]] = []
                for bet in open_bets:
                    cycle = reflection_bet_cycle(bet["prediction_id"])
                    if cycle is None:
                        # An unparseable id can't be aged; leave it unscored so a
                        # foreign/corrupt row is skipped, not mis-graded.
                        continue
                    elapsed = iteration - cycle
                    if not 1 <= elapsed <= max_scoreable_age(
                        agent.config.loop_contemplate_every
                    ):
                        # Enabling the flag must not flood the ledger with
                        # verdicts for ancient pre-flag bets that can only ever
                        # resolve ``unresolved``.
                        continue
                    scoreable.append((bet, elapsed))
                evidence = agent._collect_calibration_evidence(
                    [bet for bet, _ in scoreable], iteration
                )
                for bet, elapsed in scoreable:
                    pid = bet["prediction_id"]
                    probe = parse_loss_condition(bet["loss_condition"])
                    score = score_loss_condition(
                        probe,
                        evidence.get(pid, CalibrationEvidence()),
                        cycles_elapsed=elapsed,
                    )
                    # The self-grade attaches to the latest bet (the one the
                    # prompt re-injected, so this cycle's ``PRIOR VERDICT:``
                    # confronts it); an older held-through bet recovers the
                    # self-grade persisted on its earlier ``pending`` row, so a
                    # multi-cycle bet still carries its narration to the
                    # divergence trace when it finally matures — no longer latest.
                    grade = (
                        self_grade if pid == latest_id
                        else (bet.get("self_verdict") or None)
                    )
                    authored = author_reflection_verdict(score, grade)
                    if authored is None or authored.verdict == bet["verdict"]:
                        # Nothing to record, or a re-score that would only repeat
                        # the bet's current effective verdict (e.g. pending again).
                        continue
                    if (
                        authored.source == "measured"
                        and authored.self_verdict is not None
                        and authored.verdict in ("correct", "wrong")
                    ):
                        audit_checks.log_gate_activation(
                            agent.gate_activations,
                            # String literal (not the constant) so the manifest
                            # grounding guard can AST-match this emit site to the
                            # ``calibration_verdict_authority`` flag gate.
                            gate="calibration_verdict_authority",
                            action=(
                                CALIBRATION_VERDICT_AGREE_ACTION
                                if authored.verdict == authored.self_verdict
                                else CALIBRATION_VERDICT_DIVERGED_ACTION
                            ),
                            detail=(
                                f"reflection {pid}: "
                                f"measured={authored.verdict} "
                                f"self={authored.self_verdict}"
                            ),
                        )
                    record_reflection_verdict(
                        agent.predictions,
                        pid,
                        authored.verdict,
                        verdict_source=authored.source,
                        self_verdict=authored.self_verdict,
                    )
            elif (
                prior_prediction is not None
                and latest_id
                and prior_prediction["verdict"] == "unscored"
            ):
                authored = author_reflection_verdict(None, self_grade)
                if authored is not None:
                    record_reflection_verdict(
                        agent.predictions,
                        latest_id,
                        authored.verdict,
                        verdict_source=authored.source,
                        self_verdict=authored.self_verdict,
                    )
            new_prediction, loss_condition = parse_reflection_prediction(
                reflection,
            )
            new_pid = f"{REFLECTION_PREDICTION_ID_PREFIX}{iteration}"
            if new_prediction and not (
                iteration and has_reflection_prediction(agent.predictions, new_pid)
            ):
                # Proposal 129: when self-calibration is on, stamp the stated
                # confidence and the ProbeKind the loss condition parsed to
                # (None when the grammar declines — the unfalsifiable signal).
                # Off, both stay None and the row is byte-identical to before.
                confidence: float | None = None
                loss_condition_kind: str | None = None
                if agent.config.audit_self_calibration_enabled:
                    confidence = parse_reflection_confidence(reflection)
                    probe = parse_loss_condition(loss_condition)
                    loss_condition_kind = (
                        probe.kind if probe is not None else None
                    )
                record_reflection_prediction(
                    agent.predictions,
                    new_pid,
                    new_prediction,
                    loss_condition or "(no loss condition stated)",
                    confidence=confidence,
                    loss_condition_kind=loss_condition_kind,
                )
        except Exception:
            logger.warning(
                "Failed to record reflection self-prediction",
                exc_info=True,
            )

        try:
            agent.journal.write_thought(
                content=reflection,
                entry_type="reflection",
                triggered_by="contemplation",
            )
        except Exception:
            logger.warning("Failed to write contemplation journal entry", exc_info=True)

        # Update self-model based on reflection (skip if empty).
        if reflection and reflection.strip():
            pairs = agent._build_self_directed_pairs(recent)
            # Pull the most recent audit findings (if any) so Claude
            # can decide INTEGRATE / REFUTE / KEEP per finding. The
            # same result is returned on every contemplation between
            # audits, so findings survive an "absorbed on next cycle"
            # window instead of being evicted by the first post-audit
            # contemplation.
            audit_findings: list[audit_checks.AuditFinding] | None = None
            try:
                audit_result = agent.memory.get_most_recent_audit()
                if audit_result is not None and audit_result.findings:
                    audit_findings = list(audit_result.findings)
            except Exception:
                logger.warning(
                    "Failed to load recent audit findings",
                    exc_info=True,
                )
            # Snapshot before the LLM call so we can diff and stamp
            # one ``QuestionResolution`` row per pair afterwards.
            before_open_questions = list(
                agent.consciousness.load_self_model().open_questions,
            )
            self_directed_for_prompt = [
                (p.question, p.finding, p.refinement_count)
                for p in pairs
            ]
            # Evidence-bound capability claims (proposal 129) — build the
            # citation-resolution sources only when the flag is on, so a
            # flag-off cycle reads nothing new (mirrors the calibration
            # collector's read-only-when-needed shape).
            claim_evidence_sources: ClaimEvidenceSources | None = None
            if agent.config.audit_evidence_bound_claims_enabled:
                claim_evidence_sources = agent._collect_claim_evidence_sources()
            first_save_evicted: set[str] = set()
            updated_model = await agent.consciousness.update_self_model(
                reflection, journal=agent.journal, session=session,
                open_question_cap_evicted=first_save_evicted,
                recent_self_directed=self_directed_for_prompt,
                recent_audit_findings=audit_findings,
                restatement_concept_key_enabled=(
                    agent.config.loop_restatement_concept_key_enabled
                ),
                self_model_external_anchor_enabled=(
                    agent.config.self_model_external_anchor_enabled
                ),
                evidence_bound_claims_enabled=(
                    agent.config.audit_evidence_bound_claims_enabled
                ),
                claim_evidence_sources=claim_evidence_sources,
            )
            # Surface every ``limitations_known`` entry as a synthetic
            # ``[limitation] X`` open question so ``generate_task``
            # can anchor evolution work to soft self-knowledge. Runs
            # before ``_record_question_resolutions`` so dispositions
            # reflect whether the limitation persisted: if the LLM
            # narrowed it inside ``update_self_model`` no row is
            # re-added and the closed question records as ``removed``;
            # if the LLM did not narrow it, a fresh ``[limitation]``
            # row reappears and the resolution records as ``kept``.
            #
            # Proposal 116 fuel-starve: when enabled, suppress the
            # ``[limitation]`` mirror for any limitation whose question-
            # lineage has already accrued ``generative_debt_threshold``
            # unlanded (``obsolete``/``rejected``) plans, so it stops
            # anchoring new self-directed work. Pure JSONL reads, zero SDK
            # cost; default on since the proposal-116 graduation, off → mirror
            # every limitation as before.
            # ``suppressed_keys`` is also threaded into
            # ``_record_question_resolutions`` below so a starved-but-
            # persisting limitation still records ``kept`` (not a false
            # ``removed``) — the mirror is withheld mechanically, the
            # limitation itself did not resolve.
            suppressed_keys: set[str] | None = None
            if agent.config.retire_unlanded_limitations_enabled:
                suppressed_keys = (
                    audit_checks.limitations_over_generative_debt(
                        list(updated_model.limitations_known),
                        agent.memory.get_recent_question_resolutions(
                            agent._STALL_LINEAGE_RESOLUTION_LOOKBACK,
                        ),
                        agent.memory.get_recent_tasks(
                            agent._GENERATIVE_DEBT_TASK_LOOKBACK,
                        ),
                        agent.memory.get_all_plans(),
                        threshold=agent.config.generative_debt_threshold,
                    )
                )
            # Proposal 116, step 6: when the branch-promotion-pressure
            # master switch is off, withhold the ``kept_task_branches_v1``
            # seed's ``[limitation]`` mirror — the fuel that anchors
            # self-directed promotion work — by unioning its normalized key
            # into ``suppressed_keys`` (which the generative-debt path above
            # may already have populated). The seed text stays inert in
            # ``limitations_known``; only its open-question mirror is
            # withheld.
            if not agent.config.branch_promotion_pressure_enabled:
                kept_key = kept_task_branches_limitation_key()
                if suppressed_keys is None:
                    suppressed_keys = {kept_key}
                else:
                    suppressed_keys = suppressed_keys | {kept_key}
            agent.consciousness.surface_limitations_as_questions(
                updated_model,
                suppressed_keys=suppressed_keys,
            )
            # Both saves cap ``open_questions``: ``update_self_model``'s
            # persist (``first_save_evicted``) and this re-save (``[limitation]``
            # sub-cap). See :mod:`maestro.question_eviction`.
            pre = list(updated_model.open_questions)
            updated_model = agent.consciousness.save_self_model(updated_model)
            if pairs:
                after, evicted = split_cap_eviction(
                    pre, updated_model.open_questions,
                )
                agent._record_question_resolutions(
                    pairs=pairs,
                    before_open_questions=before_open_questions,
                    after_open_questions=after,
                    iteration=iteration,
                    suppressed_keys=(
                        (suppressed_keys or set()) | first_save_evicted | evicted
                    ),
                )

        return reflection
    finally:
        agent._running = False
        agent._current_task = None
