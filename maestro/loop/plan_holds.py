"""The approved-plan selector's keyed holds, carved out of ``maestro.agent``.

``MaestroAgent._implement_approved_plans`` declines to spend an implementation
attempt on a plan whose red is already decided by a fact outside the diff, and
it now does so against two such facts, both keyed rather than clocked:

* the **inherited-base hold** (proposal 124) — an ``INHERITED`` convergence
  terminal held the plan ``approved`` against the ``origin/main`` head the base
  red was measured at, and :func:`plan_on_inherited_hold` declines it while that
  head has not moved;
* the **environment hold** (proposal 133) — :mod:`maestro.gate_environment`
  answers, purely and at zero SDK cost, whether the local gate can reach a
  verdict at all, and on a non-``SOUND`` verdict
  :func:`hold_plans_on_environment` stamps every plan the selector would have
  taken instead of buying a red no diff can clear. The same hold has a
  mid-attempt twin at the *other* end of an attempt —
  :func:`stamp_terminal_environment_hold`, which ``MaestroAgent.implement_plan``
  calls on a ``HOLD_ENVIRONMENT`` convergence terminal for an environment that
  broke while the attempt was running.

Both are *stamps*, never demotions — a plan's ``status`` and ``failure_count``
are exactly what these paths exist to preserve — and both self-extinguish,
because nothing filters on the stamp: the base advancing, or the next ``SOUND``
verdict, simply makes the plan selectable again.

They live here rather than on the agent for the reason every sibling under
``maestro/loop/`` does: they were carved out while ``maestro/agent.py``
carried a shrink-only size pin, and the agent stays thin, so new selector machinery lands in a
sibling and the agent keeps only the call. The receiver is a first positional
``agent: MaestroAgent`` argument, matching
:func:`maestro.loop.task_record.compose_task_summary`; the pure predicate takes
its two facts directly.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from maestro import audit_checks, gate_capacity, gate_environment
from maestro.consciousness import Consciousness
from maestro.gap_store import GapStoreSchemaError

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.memory import EvolutionPlan

logger = logging.getLogger("maestro")


_INHERITED_BASE_REPAIR_DIRECTIVE = (
    "Base repair: this plan's gate red is inherited from origin/main, not its "
    "diff: {identities}. Fix the base on main first; the plan then re-admits."
)


def record_inherited_hold(
    agent: MaestroAgent,
    plan: EvolutionPlan,
    directive_identities: str,
    hold_count: int,
    held_main_sha: str | None,
) -> None:
    """Record a just-stamped proposal-124 inherited hold in the self-model.

    Four guarded writes, each behind its *own* narrow handler rather than one
    broad one (#3814): the ``maestro:gate`` gap issue opened or reused through
    :func:`maestro.loop.self_health.open_inherited_gate` against
    *held_main_sha*, the head the hold was just stamped against (``except
    (sqlite3.Error, OSError, GapStoreSchemaError)``, proposal 155 step 2 —
    the schema error covers a gaps.db written by a newer schema, as a
    reverted later step leaves it), then — each under
    ``except (OSError, ValueError)`` — the self-model load, the
    ``evolution_note`` journal entry, and the base-repair ``next_task_directive``
    that is the hold's designed release mechanism. The directive stays until gap
    issues become evolution fuel: until then it is the hold's only release task. They fail for different
    reasons and cost different things, so each names what was lost. A *caught*
    failure never propagates — ``implement_plan``'s outer handler demotes the
    plan before re-raising, which would destroy the hold stamped just above —
    and because the hold stamp is already persisted, the plan stays held across
    every one of those state-file failures. That guarantee is scoped to the
    caught cases: any other exception (say a ``RuntimeError``) escapes
    deliberately and fails loud, and the outer handler will demote the
    just-held plan. Swallowing the caught cases is a deliberate divergence from
    #3814's own wording, which asked for the directive write to reach the
    per-plan handler; the loud, named WARNING below is the correction instead of
    the raise.
    """
    # Imported here, not at module top: ``self_health`` imports this module.
    from maestro.loop import self_health

    plan_id = plan.plan_id
    try:
        self_health.open_inherited_gate(
            agent, plan, directive_identities, held_main_sha,
        )
    except (sqlite3.Error, OSError, GapStoreSchemaError):
        # The gap issue is the hold's future fuel, not its release today; a
        # gaps.db failure must never demote the plan held just above.
        logger.warning(
            "Could not open the maestro:gate issue for the inherited hold on "
            "plan %s — the hold stands without its gap issue",
            plan_id,
            exc_info=True,
        )
    try:
        model = agent.consciousness.load_self_model()
    except (OSError, ValueError):
        logger.warning(
            "Could not load self-model for the inherited hold on plan %s — "
            "hold journal note and base-repair directive skipped; the hold "
            "has no release task queued",
            plan_id,
            exc_info=True,
        )
        return
    try:
        agent.journal.record_event(
            content=(
                f"Held plan {plan.title}: quality gate red inherited from "
                f"origin/main, not this diff — {directive_identities}"
            ),
            entry_type="evolution_note",
            triggered_by=f"inherited_hold:{plan_id}",
        )
    except (OSError, ValueError):
        # The note is decoration: it explains the hold to a later reader.
        # Losing it costs nothing operational, so it stays best-effort — but
        # under its own guard, so its failure can never be mistaken for the
        # directive's below.
        logger.warning(
            "Failed to write inherited-hold journal entry",
            exc_info=True,
        )
    if model.next_task_directive:
        return
    model.next_task_directive = _INHERITED_BASE_REPAIR_DIRECTIVE.format(
        identities=directive_identities,
    )
    try:
        agent.consciousness.save_self_model(model)
    except (OSError, ValueError):
        # The directive is the hold's release mechanism, not decoration:
        # without it no base-repair task is minted and the plan burns its
        # hold budget in silence.
        logger.warning(
            "Failed to write the base-repair directive for the inherited "
            "hold on plan %s — the hold has no release task queued and will "
            "burn its budget (%d/%d) unless origin/main is fixed by other "
            "means",
            plan_id,
            hold_count,
            agent.config.max_implementation_attempts - 1,
            exc_info=True,
        )


def plan_on_inherited_hold(
    plan: EvolutionPlan, current_main: str | None,
) -> bool:
    """Whether *plan* is on a proposal-124 inherited hold against *current_main*.

    An ``INHERITED`` convergence terminal holds a plan ``approved`` rather than
    demoting it (``MaestroAgent.implement_plan``), stamping the ``origin/main``
    head the base red was measured against on ``inherited_hold_main_sha``. While
    the base has not moved, re-implementing the plan would only re-buy the same
    base red, so the selector declines it — mirroring
    :func:`~maestro.evolution_tend._in_conflict_cooldown`'s
    both-SHAs-truthy-and-equal guard, so an unmeasurable current base (``None``)
    never fires the skip and the hold releases the moment the base advances.
    Pure and zero-SDK; *current_main* is resolved once per selector call.
    """
    return bool(
        current_main
        and plan.inherited_hold_main_sha
        and current_main == plan.inherited_hold_main_sha
    )


def environment_repair_directive(
    verdict: gate_environment.GateRunnabilityVerdict,
) -> str:
    """Render a bounded environment-repair directive for *verdict*.

    Bounded to :data:`~maestro.consciousness.Consciousness.MAX_DIRECTIVE_CHARS`
    at the source rather than trusting the writer, because the detail string is
    the oracle's and may name an arbitrary gate identity. Pure.
    """
    directive = f"Environment repair: {verdict.kind} — {verdict.detail}"
    return directive[:Consciousness.MAX_DIRECTIVE_CHARS]


def stamp_terminal_environment_hold(
    agent: MaestroAgent,
    plan_id: str,
    verdict: gate_environment.GateRunnabilityVerdict | None,
) -> None:
    """Stamp *plan_id*'s mid-attempt environment hold and record its ledger row.

    The terminal twin of :func:`hold_plans_on_environment`: same stamp, same
    ledger gate (``gate_environment_hold``), one plan instead of the whole
    eligible set, and it neither asks Thomas nor writes the journal note — that is
    ``implement_plan``'s own, since the terminal already has the observation's
    identities to say it with.

    A ``None`` *verdict* **skips the stamp entirely** rather than writing it.
    ``None`` is unreachable by construction — the ``ENVIRONMENT`` attribution is
    *derived* from the verdict, which
    :attr:`~maestro.gate_convergence.ConvergenceResult.runnability` carries to the
    caller — but the direction matters if it ever became reachable:
    :meth:`~maestro.memory.Memory.update_plan_environment_hold` defines a ``None``
    fingerprint as *clearing* the stamp, so writing one would make the arm that
    exists to create a hold silently dissolve the one already on the plan. With no
    verdict there is no fact to key a hold to, so the right answer is to leave the
    plan exactly as it was.

    The ledger row is best-effort in the shape
    :func:`hold_plans_on_environment` already uses: it is what makes the hold
    visible to ``maestro audit-flags``' promote-or-delete horizon, but the *stamp*
    is what protects the attempt budget, so a ledger that cannot be written must
    never cost the hold. Written after the stamp for the same reason.
    """
    if verdict is None:
        return
    agent.memory.update_plan_environment_hold(
        plan_id, environment_hold_fingerprint=verdict.fingerprint,
    )
    try:
        audit_checks.log_gate_activation(
            agent.gate_activations,
            gate="gate_environment_hold",
            action=verdict.kind,
            detail=f"terminal plan={plan_id} {verdict.detail}",
        )
    except Exception:
        logger.debug(
            "terminal gate_environment_hold activation row failed", exc_info=True,
        )


def gate_environment_verdict(
    agent: MaestroAgent,
    iteration: int | None,
    plans: list[EvolutionPlan] | None = None,
) -> gate_environment.GateRunnabilityVerdict | None:
    """Refresh the proposal-133 runnability verdict, or ``None`` while off.

    ``None`` is the flag-off answer *and* the "nothing to say" answer, so the
    caller has one branch: with ``[loop] gate_environment_oracle_enabled`` off no
    probe is issued at all and the selector is byte-identical to today.

    On, this consults :func:`maestro.gate_environment.refresh_verdict`, which
    probes at most once per ``iteration_key`` — the collection run is a whole
    ``pytest --collect-only``, so paying it once per iteration rather than once
    per phase is the point. Every piece of evidence the probes need is assembled
    here, from the owners that already hold it: the telemetry rows and the
    planned width and budget from :mod:`maestro.gate_capacity` (this is the seam
    the oracle's one-way import edge exists to keep out of the oracle itself),
    the suite collector from the evolution engine, and the ``origin/main`` head
    from the remote-tracking ref — resolved without a fetch, exactly as the
    inherited-hold read does. ``in_flight=0`` because the question is what a run
    *would* be granted if it started now, not what a contended one was.

    *plans* is the selector's eligible set, and an *empty* one is also ``None``:
    the probe is a whole ``pytest --collect-only``, so an iteration that would
    implement nothing must not pay for it — and a hold recorded over zero plans
    would hold nothing while still occupying the directive slot and asking Thomas
    about plans that do not exist. Omitting *plans* skips that check, which is
    what the direct-evidence tests want.
    """
    if not agent.config.gate_environment_oracle_enabled:
        return None
    if plans is not None and not plans:
        return None
    if iteration is None:
        # The cache key *is* the iteration. Without one, ``str(None)`` would be a
        # literal ``"None"`` key that matches itself forever, so the first probe's
        # verdict — including a stale non-``SOUND`` one that holds every plan —
        # would never be re-measured. A caller that cannot name its iteration gets
        # no probe rather than a frozen one; the compat default is the only such
        # caller, and it is the flag-off shape anyway.
        return None
    readings = gate_capacity.read_gate_runs(agent.config.gate_runs_path)
    workers = gate_capacity.uncontended_width(
        gate_capacity.measure_host_capacity(),
        cgroup_reserve=agent.config.gate_lease_cgroup_reserve_enabled,
    )
    # Proposal 134: the oracle must judge the suite against the budget that was
    # actually enforced. ``_run_gate_command`` spends the lease-derived deadline
    # only under ``[loop] gate_budget_lease_owned_enabled``; off, every run is
    # killed at ``selfcheck``, so measuring a killed-at-selfcheck suite against a
    # never-enforced derived budget would read a budget no run ever had. Flag-off
    # therefore skips the derivation entirely and names no cap — there is no
    # escalation to wait out when the ceiling never moves.
    if agent.config.gate_budget_lease_owned_enabled:
        deadline = gate_capacity.derive_deadline(
            readings,
            workers=workers,
            selfcheck=agent.config.selfcheck_timeout,
            selfcheck_max=agent.config.selfcheck_max_timeout,
        )
        cap = agent.config.selfcheck_max_timeout
    else:
        deadline = agent.config.selfcheck_timeout
        cap = 0
    return gate_environment.refresh_verdict(
        agent.config.gate_environment_path,
        iteration_key=str(iteration),
        collector=agent.evolution.collect_suite,
        readings=readings,
        workers=workers,
        deadline_seconds=deadline,
        cap_seconds=cap,
        head=agent._vcs.rev_parse("origin/main") or "",
        measured_at=datetime.now(UTC).isoformat(),
    )


async def hold_plans_on_environment(
    agent: MaestroAgent,
    verdict: gate_environment.GateRunnabilityVerdict,
    plans: list[EvolutionPlan],
) -> None:
    """Record a proposal-133 environment hold over *plans* and ask once.

    The stamp lands in one batched read-modify-write over the whole plan set
    rather than one full plan-file rewrite per plan, so a held iteration costs
    a single lock/load/save no matter how many plans the verdict covers.

    The hold itself is the stamp: each plan keeps its ``approved`` / ``failed``
    status and its ``failure_count`` — nothing here demotes or counts an attempt,
    because no attempt was spent — and carries the verdict's fingerprint so the
    hold is keyed to the fact. The ledger row and the journal note are each
    best-effort in the shape the neighbouring hold paths already use: a ledger or
    journal that cannot be written must not cost the hold, which is the thing
    that actually protects the attempt budget.

    Thomas is asked at most once per distinct
    :func:`~maestro.gate_environment.notice_key` — the ``(kind, identities)``
    fact, not the fingerprint — so an ``UNCOLLECTABLE`` head advancing on every
    merge re-stamps the held plans silently instead of re-asking about a break
    nobody has repaired. The notice is recorded *before* the send, so a failing
    transport costs one lost question rather than a loop of them.
    """
    logger.info(
        "Gate environment is %s (%s) — holding %d eligible plan(s) rather than "
        "spending an implementation attempt",
        verdict.kind, verdict.detail, len(plans),
    )
    agent.memory.update_plans_environment_hold(
        [plan.plan_id for plan in plans],
        environment_hold_fingerprint=verdict.fingerprint,
    )
    try:
        audit_checks.log_gate_activation(
            agent.gate_activations,
            gate="gate_environment_hold",
            action=verdict.kind,
            detail=verdict.detail,
        )
    except Exception:
        logger.debug("gate_environment_hold activation row failed", exc_info=True)
    model = agent.consciousness.load_self_model()
    try:
        agent.journal.record_event(
            content=(
                f"Held {len(plans)} plan(s) on the environment rather than "
                f"spending an implementation attempt: the local gate cannot "
                f"reach a verdict ({verdict.kind} — {verdict.detail}), so a red "
                f"from here would be the environment's, not the diff's."
            ),
            entry_type="evolution_note",
            triggered_by=f"environment_hold:{verdict.kind}",
        )
    except Exception:
        logger.debug("environment-hold journal note failed", exc_info=True)
    if not model.next_task_directive:
        model.next_task_directive = environment_repair_directive(verdict)
        agent.consciousness.save_self_model(model)
    key = gate_environment.notice_key(verdict)
    notice_path = agent.config.gate_environment_notice_path
    if gate_environment.read_notice(notice_path) == key:
        return
    gate_environment.record_notice(notice_path, key)
    identities = ", ".join(sorted(verdict.identities)) or "none"
    await agent.ask_thomas(
        f"The local quality gate cannot reach a verdict: {verdict.kind} — "
        f"{verdict.detail} (gate identities: {identities}). I am holding "
        f"{len(plans)} eligible plan(s) rather than spending implementation "
        f"attempts against a red no diff can clear. Could you repair the "
        f"environment? I will resume automatically once it is sound."
    )


def release_environment_hold(agent: MaestroAgent, plan: EvolutionPlan) -> None:
    """Clear *plan*'s stamp as the selector picks it up.

    Reaching selection means the environment read ``SOUND`` (or the oracle is
    off), so any stamp the plan carries records a hold that has already released.
    Clearing it here keeps a stale fingerprint from outliving its hold and being
    mistaken for a live one. A no-op for an unstamped plan.
    """
    if not plan.environment_hold_fingerprint:
        return
    agent.memory.update_plan_environment_hold(
        plan.plan_id, environment_hold_fingerprint=None,
    )
    plan.environment_hold_fingerprint = None


def release_environment_holds(
    agent: MaestroAgent, plans: list[EvolutionPlan],
) -> None:
    """Clear the stamp on every plan a released hold covered.

    :func:`release_environment_hold` sheds one selected plan's stamp; this sheds
    the whole set's, because a ``SOUND`` verdict releases every plan the hold
    covered — not only those this iteration's slice-order, inherited-hold and
    cap filters happen to let through. Without it a plan held under a non-``SOUND``
    verdict and then filtered out on the ``SOUND`` one would keep a fingerprint
    recording a hold that is already over. One batched write, and a no-op when no
    plan in *plans* carries a stamp (the steady state).
    """
    stamped = [p for p in plans if p.environment_hold_fingerprint]
    if not stamped:
        return
    agent.memory.update_plans_environment_hold(
        [p.plan_id for p in stamped], environment_hold_fingerprint=None,
    )
    for plan in stamped:
        plan.environment_hold_fingerprint = None


def release_environment_hold_state(
    agent: MaestroAgent, plans: list[EvolutionPlan],
) -> None:
    """Release everything a ``SOUND`` verdict ends, in one call.

    The selector's whole SOUND-path obligation: the ask-once notice is cleared
    (so a recurrence after a repair is a fresh fact, not a muted one) and every
    plan the hold covered sheds its stamp. Both are no-ops in the steady state.
    """
    clear_environment_notice(agent)
    release_environment_holds(agent, plans)


def clear_environment_notice(agent: MaestroAgent) -> None:
    """Clear the ask-once notice on a ``SOUND`` verdict.

    The fact the last question was about is gone, so a recurrence after a repair
    is a genuinely new fact and earns a fresh ask. The empty key matches no real
    one, which always leads with a kind.

    Read before writing: an already-empty notice is the overwhelmingly common
    steady state, and rewriting it would take the lock and rewrite the sidecar on
    every ``SOUND`` iteration to persist a value it already holds.
    """
    path = agent.config.gate_environment_notice_path
    if not gate_environment.read_notice(path):
        return
    gate_environment.record_notice(path, "")
