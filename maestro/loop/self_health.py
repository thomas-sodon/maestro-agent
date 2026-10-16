"""The loop's seams onto the self-health detectors and evolution fuel (proposal 155).

:func:`run_evolve_prestep` gathers the reads the zero-SDK detectors in
:mod:`maestro.evolution_fuel` fold — the origin/main head (no fetch), its check
runs through the :class:`~maestro.vcs.CodeHost`, whether a live plan is on an
inherited hold against that head, and the source-budget scan — and runs them
as ``MaestroAgent.evolve``'s pre-step. :func:`open_inherited_gate` is the
inherited-hold seam ``record_inherited_hold`` calls. Both open the gap
store and close it in a ``finally``; neither runs a gate subprocess nor an SDK
call, and both return before any read when no GitHub repo is configured.

Step 4 makes fuel evolve's first act: :func:`admitted_evolution_fuel` runs the
detectors, :func:`select_fuel` picks one trusted gap issue, and the WIP and
pre-mint gates follow (a red gate bypasses both) — all at zero SDK cost, so a
tick without fuel opens no session and runs no gate subprocess.
:func:`evolve_with_fuel` then plans and submits the gap.

Step 5's stuck-gap tripwire: :func:`select_fuel` queues each gap the selection
labelled ``needs-decision``, :func:`deliver_gap_decision_asks` asks Thomas
once per gap with a plain marked question, and
:func:`apply_gap_decision_reply` applies the first threaded reply.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from maestro import conscious_workspace
from maestro.evolution import ProposalParseError, pin_gap_plan
from maestro.evolution_fuel import (
    RED_MAIN_REF,
    DetectorOutcome,
    FuelSelection,
    RedMainReading,
    SelfHealthReport,
    apply_gap_decision,
    effective_operator_logins,
    format_gap_decision_ask,
    format_gap_decision_followup,
    gap_decision_note,
    gap_decision_reply_stamp,
    gap_issue_url,
    open_gate_for_inherited_hold,
    parse_gap_decision_question,
    parse_gap_decision_reply,
    record_detector_outcome,
    render_gap_block,
    run_self_health_detectors,
    select_evolution_fuel,
)
from maestro.gap_store import build_gap_store
from maestro.generation_gate import GENERATION_STANDDOWN_DIRECTIVE
from maestro.loop import plan_holds
from maestro.messaging import Message
from maestro.source_budget import read_source_budget, scan_target

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.consciousness import SelfModel
    from maestro.memory import EvolutionPlan
    from maestro.providers.base import Session

logger = logging.getLogger("maestro")


def run_evolve_prestep(agent: MaestroAgent, now: datetime) -> SelfHealthReport | None:
    """Run the self-health detectors once; ``None`` when no repo is configured."""
    config = agent.config
    if not config.work_target().github_repo:
        return None
    code_host = agent.evolution.code_host
    sha = agent._vcs.rev_parse("origin/main") or ""
    ref = code_host.get_ref_check_runs(sha) if sha else None
    hold_live = any(
        plan_holds.plan_on_inherited_hold(plan, sha or None)
        for plan in agent.memory.get_live_plan_records()
    )
    readings = read_source_budget(*scan_target(config))
    logins = effective_operator_logins(config, code_host.resolve_owner())
    store = build_gap_store(config)
    try:
        return run_self_health_detectors(
            sha=sha,
            check_runs=ref.runs if ref is not None else None,
            inherited_hold_live=hold_live,
            readings=readings,
            grace_hours=config.evolution_fuel_red_main_grace_hours,
            tracker=agent.issues,
            store=store,
            operator_logins=logins,
            journal=agent.journal,
            now=now,
        )
    finally:
        store.close()


def open_inherited_gate(
    agent: MaestroAgent,
    plan: EvolutionPlan,
    identities: str,
    held_sha: str | None,
) -> DetectorOutcome | None:
    """Open or reuse the ``maestro:gate`` issue for *plan*'s inherited hold.

    *held_sha* is the ``origin/main`` head the caller stamped the hold against
    (``None`` when unmeasured), so the issue names exactly that head.

    Returns ``None`` without opening anything while ``[loop] evolve_every`` is
    ``0``: the only closer of a ``maestro:gate`` issue is the red-main
    detector's green arm in :func:`run_evolve_prestep`, which runs only on
    evolve's cadence, so an issue opened with that cadence off would stay open
    against a stale head. The open goes live together with its closer.
    """
    config = agent.config
    if config.loop_evolve_every <= 0:
        return None
    if not config.work_target().github_repo:
        return None
    logins = effective_operator_logins(
        config, agent.evolution.code_host.resolve_owner(),
    )
    store = build_gap_store(config)
    try:
        outcome = open_gate_for_inherited_hold(
            plan.plan_id,
            held_sha,
            identities,
            tracker=agent.issues,
            store=store,
            operator_logins=logins,
            now=datetime.now(UTC),
        )
    finally:
        store.close()
    record_detector_outcome(agent.journal, outcome)
    return outcome


def _red_main_excerpt(reading: RedMainReading) -> str:
    """The failing-gate excerpt a ``maestro:gate`` fuel carries into its prompt."""
    if not reading.failing_checks:
        return ""
    return (
        f"{RED_MAIN_REF} {reading.sha or '(unknown head)'} is red on: "
        f"{', '.join(reading.failing_checks)}"
    )


def select_fuel(
    agent: MaestroAgent, report: SelfHealthReport | None, now: datetime,
) -> FuelSelection:
    """Select this tick's evolution fuel (proposal 155 step 4); zero SDK.

    An empty selection when no repo is configured (*report* is ``None``).
    Each gap the tripwire parked is queued on ``agent._pending_gap_asks``.
    """
    if report is None:
        return FuelSelection()
    config = agent.config
    own_login = agent.evolution.code_host.resolve_owner()
    logins = effective_operator_logins(config, own_login)
    store = build_gap_store(config)
    try:
        selection = select_evolution_fuel(
            now,
            tracker=agent.issues,
            store=store,
            operator_logins=logins,
            journal=agent.journal,
            plans=agent.memory.get_all_plans(),
            max_attempts=config.evolution_fuel_max_plans_per_gap,
            retry_budget=config.max_implementation_attempts,
            failed_excerpt=_red_main_excerpt(report.red_main),
            own_login=own_login,
        )
    finally:
        store.close()
    agent._pending_gap_asks.extend(selection.tripped)
    return selection


async def deliver_gap_decision_asks(agent: MaestroAgent) -> None:
    """Ask Thomas once per gap the tripwire parked, then drain the queue.

    Each ask is one ``[gap-decision #N]`` question through
    :meth:`MaestroAgent.ask_thomas`, so a threaded reply routes back to
    :func:`apply_gap_decision_reply`. An ask
    that cannot be delivered (no message store) is logged and journaled as
    ``gap-decision-undelivered``; the tripwire's issue comment stays the
    fallback channel, since the ``needs-decision`` label already fences a
    second ask (a crash between selection and delivery loses it the same way).
    """
    pending, agent._pending_gap_asks = agent._pending_gap_asks, []
    if not pending:
        return
    config = agent.config
    repo = config.work_target().github_repo
    if not repo:
        logger.warning("Gap decision asks dropped: no GitHub repo is configured")
        return
    for trip in pending:
        message_id = await agent.ask_thomas(
            format_gap_decision_ask(
                trip.issue_number, gap_issue_url(repo, trip.issue_number),
            ),
        )
        if not message_id:
            logger.warning(
                "Gap #%d decision ask undelivered; the issue comment is the only signal",
                trip.issue_number,
            )
            agent.journal.record_event(
                f"gap-decision-undelivered #{trip.issue_number}: ask not stored; "
                "the tripwire's issue comment is the fallback channel",
                entry_type="audit_observation",
                triggered_by="evolution_fuel",
            )


async def apply_gap_decision_reply(agent: MaestroAgent, msg: Message) -> bool:
    """Apply a Thomas reply threaded to a gap-decision question.

    ``False`` when *msg* does not reply to one (the caller handles it as
    before). ``True`` means consumed, never enqueued: the first Thomas reply
    to the question that names build or buy is applied (an earlier
    unparseable, unapplied or ``drop`` reply does not block a corrective one);
    a later one, or an unparseable word, is only logged. Text after the
    decision word is journaled with it. A reply already journalled as applied
    (a crash between apply and ``mark_read_many``) is not applied again.

    A reply naming no option, a ``drop`` reply, and a decision whose host
    write failed, are answered by
    a fresh marked question through :meth:`MaestroAgent.ask_thomas`, so the
    notice reaches Thomas on the notifier and a reply to it routes here.
    """
    store = agent.message_store
    if store is None or not msg.reply_to:
        return False
    thread = store.get_thread(msg.reply_to)
    question = next(
        (m for m in thread if m.message_id == msg.reply_to and m.sender == "maestro"),
        None,
    )
    number = parse_gap_decision_question(question.content) if question else None
    if number is None:
        return False
    decision = parse_gap_decision_reply(msg.content)
    events = [e.content for e in agent.journal.read_events_by_type("audit_observation")]
    applied_head = f"gap-decision #{number}: "
    if any(
        c.startswith(applied_head) and gap_decision_reply_stamp(msg.message_id) in c
        for c in events
    ):
        logger.info("Gap #%d reply %s already applied; not re-applied", number, msg.message_id)
        return True
    unapplied_head = f"gap-decision-unapplied #{number}: "
    unapplied = {
        c[len(unapplied_head):].split(" ", 1)[0]
        for c in events
        if c.startswith(unapplied_head)
    }
    first = next(
        (
            m for m in thread
            if m.sender == "thomas" and m.reply_to == msg.reply_to
            and m.message_id not in unapplied
            and parse_gap_decision_reply(m.content) not in (None, "drop")
        ),
        None,
    )
    if decision is None:
        logger.info("Gap #%d decision reply names no option: %r", number, msg.content[:80])
        noop_stamp = f"gap-decision-noop #{number}: {msg.message_id}"
        if first is not None or noop_stamp in events:
            # Already answered (e.g. 'thanks' after an applied build), or a
            # crash re-drain of a reply already nagged: do not ask again.
            return True
        agent.journal.record_event(
            noop_stamp, entry_type="audit_observation", triggered_by="evolution_fuel",
        )
        await agent.ask_thomas(format_gap_decision_followup(
            number,
            f"Your reply on gap #{number} names no option. Reply to this message "
            "with build or buy.",
        ))
        return True
    if decision != "drop" and first is not None and first.message_id != msg.message_id:
        logger.info("Gap #%d decision already answered; ignoring %s", number, msg.message_id)
        return True
    if not apply_gap_decision(
        agent.issues, agent.journal, number, decision,
        note=gap_decision_note(msg.content), reply_id=msg.message_id,
    ):
        # A failed write must not win the thread: a repeated reply applies.
        agent.journal.record_event(
            f"{unapplied_head}{msg.message_id} — host write failed; a repeated "
            "reply will be applied",
            entry_type="audit_observation",
            triggered_by="evolution_fuel",
        )
        await agent.ask_thomas(format_gap_decision_followup(
            number,
            f"Your '{decision}' on gap #{number} did not apply (the issue label "
            "could not be changed). Reply to this message with build or buy to retry.",
        ))
    elif decision == "drop":
        await agent.ask_thomas(format_gap_decision_followup(
            number,
            f"'drop' on gap #{number} changes nothing by itself: close issue "
            f"#{number} yourself to drop it, or reply to this message with "
            "build or buy.",
        ))
    return True


def _live_backlog_saturated(agent: MaestroAgent) -> bool:
    """Whether the door's ``pending_plan_submit_block_threshold`` would refuse a mint."""
    cap = agent.config.loop_pending_plan_submit_block_threshold
    return cap > 0 and len(agent.memory.get_live_plan_records()) >= cap


def admitted_evolution_fuel(agent: MaestroAgent, now: datetime) -> FuelSelection | None:
    """Evolve's zero-SDK first act: detectors, fuel, then the mint gates.

    ``None`` means stand down this tick — no fuel, or (unless the fuel is a
    red gate, which bypasses all three) WIP primacy owns the slot, the pre-mint
    gate refuses, or the live backlog is saturated. A pre-mint refusal fills an empty ``next_task_directive``
    with the stand-down directive, as before.
    """
    selection = select_fuel(agent, run_evolve_prestep(agent, now), now)
    fuel = selection.selected
    if fuel is None:
        logger.info(
            "Evolution fuel: no admissible gap issue (%d skipped) — standing down",
            len(selection.skipped),
        )
        return None
    if fuel.bypasses_gates:
        return selection
    if agent._wip_owns_slot():
        logger.info(
            "WIP primacy: owned work is pending (%r) — deferring gap #%d",
            agent._current_task_focus(), fuel.issue.number,
        )
        return None
    verdict = agent._pre_mint_generation_verdict()
    if verdict is not None and not verdict.admit:
        logger.info(
            "Pre-mint generation gate refused a net-new mint (%s) — deferring gap #%d",
            verdict.reason, fuel.issue.number,
        )
        agent.consciousness.mutate_self_model(_arm_standdown_directive)
        return None
    if _live_backlog_saturated(agent):
        # Runs after the pre-mint verdict (which owns the stand-down
        # directive) so it only matters with that gate off. Zero-SDK mirror
        # of the door's submit-block: deferring here keeps a saturated
        # backlog from re-paying a refused propose_plan every tick.
        logger.info(
            "Live-work backlog saturated — deferring gap #%d", fuel.issue.number,
        )
        return None
    return selection


def _arm_standdown_directive(model: SelfModel) -> None:
    """Fill an empty ``next_task_directive`` with the generation stand-down."""
    if not model.next_task_directive:
        model.next_task_directive = GENERATION_STANDDOWN_DIRECTIVE


async def evolve_with_fuel(
    agent: MaestroAgent, selection: FuelSelection, session: Session | None,
) -> None:
    """Plan the selected gap and submit it (proposal 155 step 4).

    One ``propose_plan`` (or, under ``multi_pr_proposals_enabled``,
    ``propose_plan_chain``) call over the capped gap block; every plan it
    returns is pinned to the gap, and the lone plan's conscious-access
    citation is re-checked (proposal 130) before ``submit_for_review``.
    """
    fuel = selection.selected
    if fuel is None:
        return
    evolution = agent.evolution

    def tr(caller: str) -> bool:  # proposal 143
        return evolution.tree_reads_for(caller, throttled=agent.budget_throttle_active)

    cited_moment: conscious_workspace.ConsciousMoment | None = None
    try:
        if agent.config.multi_pr_proposals_enabled:
            block = render_gap_block(fuel, evolution._gap_context_inputs(fuel))
            result = await evolution.propose_plan_chain(
                f"Close this capability gap:\n{block}", session=session,
                tree_reads=tr("propose_plan_chain"), gap_pinned=True,
            )
        else:
            # Deliver the active moment (proposal 130): a triple only when
            # broadcast is on and a moment is active.
            delivery = agent._deliver_moment_to_consumer("evolution")
            moment, moment_id, admitted = "", None, frozenset[str]()
            if delivery is not None:
                moment, cited_moment, admitted = delivery
                moment_id = cited_moment.moment_id
            result = await evolution.propose_plan(
                gap=fuel, session=session,
                workspace_moment=moment, workspace_moment_id=moment_id,
                admitted_item_ids=admitted, tree_reads=tr("propose_plan"),
            )
    except ProposalParseError as exc:
        logger.warning("Evolution proposal parse failure — skipping submission: %s", exc)
        agent.journal.record_event(
            content=(
                "Skipped evolution proposal this iteration: the LLM returned an "
                f"unparseable response. Detail: {exc}"
            ),
            entry_type="evolution_note",
            triggered_by="evolution_skip",
        )
        return

    if isinstance(result, tuple):
        proposal, slices = result
        await evolution.submit_proposal_chain(
            proposal, [pin_gap_plan(s, fuel) for s in slices],
            exempt_saturation=fuel.bypasses_gates,
        )
        return
    plan = pin_gap_plan(result, fuel)
    # Fail-closed citation guard (proposal 130): an invalid citation is
    # nulled, never a reason to drop the plan.
    if cited_moment is not None:
        verdict = conscious_workspace.validate_citation(
            moment_id=plan.workspace_moment_id,
            item_ids=plan.workspace_item_ids,
            moment=cited_moment,
            consumer="evolution",
        )
        if verdict.valid:
            agent._active_cited_plan_ids.append(plan.plan_id)
        else:
            logger.warning(
                "Nulling invalid conscious-access citation on plan %s "
                "(reason=%s); plan submitted uncited", plan.plan_id, verdict.reason,
            )
            plan = replace(plan, workspace_moment_id=None, workspace_item_ids=None)
    await evolution.submit_for_review(
        plan, self_initiated=True,
        exempt_saturation=fuel.bypasses_gates,
    )
