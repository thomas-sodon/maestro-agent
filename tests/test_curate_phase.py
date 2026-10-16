"""Step 5 of proposal 060: the ``curate_pending_plans`` loop-phase wiring.

Covers the agent-side wiring only — registration in ``_LOOP_PHASES`` /
``_BUDGET_GATED_PHASES``, the ``_phase_will_run`` cadence gate, and the
``_curate_pending_plans`` phase body's early-return / delegation. The engine
method ``EvolutionEngine.curate_pending_plans`` (Step 3) is covered in
``tests/test_evolution.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.agent import _BUDGET_GATED_PHASES, _LOOP_PHASES
from maestro.memory import EvolutionPlan
from maestro.plan_feedback import PlanFeedback


def _pending_plan(
    plan_id: str = "plan-1",
    *,
    created_at: str = "2025-01-01T00:00:00Z",
) -> EvolutionPlan:
    """Minimal ``pending_review`` plan that ``select_curation_target`` accepts."""
    return EvolutionPlan(
        plan_id=plan_id,
        title="Stale plan",
        category="capability",
        rationale="because",
        proposed_changes=["change one"],
        files_affected=["maestro/agent.py"],
        expected_impact="none",
        status="pending_review",
        created_at=created_at,
    )


def _gate(agent, **overrides):  # type: ignore[no-untyped-def]
    """Call ``_phase_will_run`` for the curate phase with on-cadence defaults."""
    kwargs = dict(
        iteration=10,
        smoke=False,
        first_iteration_after_restart=False,
        task_every=0,
        evolve_every=0,
        dream_every=0,
        audit_self_every=0,
        curate_every=5,
        curate_enabled=True,
    )
    kwargs.update(overrides)
    return agent._phase_will_run("curate_pending_plans", **kwargs)


# ── registration ──────────────────────────────────────────────────────


def test_curate_phase_registered() -> None:
    assert "curate_pending_plans" in _LOOP_PHASES
    # SDK-spending, so the burn-rate throttle must be able to gate it.
    assert "curate_pending_plans" in _BUDGET_GATED_PHASES


def test_curate_phase_ordered_after_audit_before_dream() -> None:
    idx = _LOOP_PHASES.index("curate_pending_plans")
    assert idx > _LOOP_PHASES.index("audit_self_model")
    assert idx < _LOOP_PHASES.index("dream")


# ── cadence gate matrix ───────────────────────────────────────────────


def test_curate_gate_runs_when_enabled_and_on_cadence(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    assert _gate(agent) is True


def test_curate_gate_disabled_by_flag(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    assert _gate(agent, curate_enabled=False) is False


def test_curate_gate_disabled_by_zero_cadence(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    assert _gate(agent, curate_every=0) is False


def test_curate_gate_off_cadence(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    assert _gate(agent, iteration=11, curate_every=5) is False


def test_curate_gate_skips_under_smoke(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    assert _gate(agent, smoke=True) is False


def test_curate_gate_skips_under_budget_throttle(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._maintenance_mode_active = True
    assert agent.budget_throttle_active is True
    assert _gate(agent) is False


def test_curate_gate_dedup_same_iteration(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._last_phase_iteration["curate_pending_plans"] = 10
    # Already fired at this (possibly frozen) iteration value → skip.
    assert _gate(agent, iteration=10) is False
    # A later iteration value re-enables it.
    assert _gate(agent, iteration=15) is True


def test_curate_gate_defaults_to_disabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Omitting the curate kwargs (as every other phase's test call site does)
    leaves the phase off — the opt-in defaults keep it dark."""
    agent = make_agent()
    assert agent._phase_will_run(
        "curate_pending_plans",
        iteration=10, smoke=False, first_iteration_after_restart=False,
        task_every=0, evolve_every=0, dream_every=0, audit_self_every=0,
    ) is False


# ── phase body ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_curate_body_early_returns_when_no_pending_plan(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.memory.get_all_plans = MagicMock(return_value=[])  # type: ignore[method-assign]
    agent.evolution.curate_pending_plans = AsyncMock()  # type: ignore[method-assign]
    agent._get_or_create_session = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await agent._curate_pending_plans(session=None)

    assert result is None
    # No session opened and no SDK call when the queue is empty.
    agent._get_or_create_session.assert_not_called()
    agent.evolution.curate_pending_plans.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_body_early_returns_when_only_pending_plan_too_young(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A positive ``curate_min_plan_age_hours`` makes the phase-entry guard
    drop a too-young pending plan, so the body no-ops without opening a
    session or making an SDK call — the guard and the selector agree."""
    agent = make_agent()
    agent.config.loop_curate_min_plan_age_hours = 24.0
    # The only pending plan was created an hour ago — below the 24h floor.
    young = _pending_plan(
        created_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat()
    )
    agent.memory.get_all_plans = MagicMock(return_value=[young])  # type: ignore[method-assign]
    agent.evolution.curate_pending_plans = AsyncMock()  # type: ignore[method-assign]
    agent._get_or_create_session = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await agent._curate_pending_plans(session=None)

    assert result is None
    agent._get_or_create_session.assert_not_called()
    agent.evolution.curate_pending_plans.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_body_no_sdk_when_all_serviced_under_flag(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Criterion 7: with loop_curate_skip_serviced_enabled, a backlog of only
    already-serviced plans makes select_curation_target return None, so the
    body no-ops without opening a session or making an SDK call."""
    agent = make_agent()
    agent.config.loop_curate_skip_serviced_enabled = True
    kept = _pending_plan()
    kept.last_curated_at = "2025-06-01T00:00:00Z"
    agent.memory.get_all_plans = MagicMock(return_value=[kept])  # type: ignore[method-assign]
    agent.evolution.curate_pending_plans = AsyncMock()  # type: ignore[method-assign]
    agent._get_or_create_session = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await agent._curate_pending_plans(session=None)

    assert result is None
    agent._get_or_create_session.assert_not_called()
    agent.evolution.curate_pending_plans.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_body_delegates_when_pending_plan_exists(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=[_pending_plan()],
    )
    agent.evolution.curate_pending_plans = AsyncMock(  # type: ignore[method-assign]
        return_value=True,
    )
    sentinel = object()
    agent._get_or_create_session = AsyncMock(  # type: ignore[method-assign]
        return_value=sentinel,
    )

    result = await agent._curate_pending_plans(session=None)

    agent._get_or_create_session.assert_awaited_once()
    # The freshly created slot-B session is threaded into the engine call.
    agent.evolution.curate_pending_plans.assert_awaited_once_with(session=sentinel)
    # …and returned so the loop keeps its per-iteration session cursor.
    assert result is sentinel


@pytest.mark.asyncio
async def test_curate_body_single_use_flag_opens_no_session(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 139 step 4: under the single-use flag the body opens no slot-B
    session, hands the engine ``session=None``, and returns the cursor."""
    agent = make_agent()
    agent.config.sdk_single_use_evolution_calls_enabled = True
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=[_pending_plan()],
    )
    agent.evolution.curate_pending_plans = AsyncMock()  # type: ignore[method-assign]
    agent._get_or_create_session = AsyncMock()  # type: ignore[method-assign]
    cursor = object()

    result = await agent._curate_pending_plans(session=cursor)  # type: ignore[arg-type]

    agent._get_or_create_session.assert_not_called()
    agent.evolution.curate_pending_plans.assert_awaited_once_with(
        session=None, tree_reads=False,
    )
    assert result is cursor


@pytest.mark.asyncio
async def test_curate_body_grounded_caller_passes_tree_reads(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 143: with the flag on and curation listed, the body routes
    ``tree_reads=True`` through the engine's one caller predicate."""
    agent = make_agent()
    agent.config.sdk_single_use_evolution_calls_enabled = True
    agent.config.grounded_planning_enabled = True
    agent.config.grounded_planning_callers = ["curate_pending_plans"]
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=[_pending_plan()],
    )
    agent.evolution.curate_pending_plans = AsyncMock()  # type: ignore[method-assign]

    await agent._curate_pending_plans(session=None)

    agent.evolution.curate_pending_plans.assert_awaited_once_with(
        session=None, tree_reads=True,
    )


@pytest.mark.asyncio
async def test_curate_body_single_use_flag_empty_queue_early_returns(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.sdk_single_use_evolution_calls_enabled = True
    agent.memory.get_all_plans = MagicMock(return_value=[])  # type: ignore[method-assign]
    agent.evolution.curate_pending_plans = AsyncMock()  # type: ignore[method-assign]
    agent._get_or_create_session = AsyncMock()  # type: ignore[method-assign]

    result = await agent._curate_pending_plans(session=None)

    assert result is None
    agent._get_or_create_session.assert_not_called()
    agent.evolution.curate_pending_plans.assert_not_awaited()


# ── loop dispatch ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_loop_dispatches_curate_phase_when_enabled(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """A single ``run_loop`` iteration fires the curate phase when it is
    enabled and on-cadence (``curate_every=1`` ⇒ fires on iteration 1)."""
    from maestro.evolution import MergeCheckResult
    from maestro.evolution_tend import TendPRsResult

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.ensure_main_branch = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="")  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent._process_rejected_plans = AsyncMock()  # type: ignore[method-assign]
    agent._respond_to_messages = AsyncMock()  # type: ignore[method-assign]
    agent._audit_self_model = AsyncMock()  # type: ignore[method-assign]
    agent._curate_pending_plans = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent.evolution.tend_evolution_prs = AsyncMock(  # type: ignore[method-assign]
        return_value=TendPRsResult(),
    )
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
        curate_every=1, curate_enabled=True,
    )

    agent._curate_pending_plans.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_loop_skips_curate_phase_when_disabled(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """With the default opt-in flags off, the curate phase never dispatches."""
    from maestro.evolution import MergeCheckResult
    from maestro.evolution_tend import TendPRsResult

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.ensure_main_branch = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="")  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent._process_rejected_plans = AsyncMock()  # type: ignore[method-assign]
    agent._respond_to_messages = AsyncMock()  # type: ignore[method-assign]
    agent._audit_self_model = AsyncMock()  # type: ignore[method-assign]
    agent._curate_pending_plans = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent.evolution.tend_evolution_prs = AsyncMock(  # type: ignore[method-assign]
        return_value=TendPRsResult(),
    )
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    agent._curate_pending_plans.assert_not_awaited()


# ── off-cadence feedback trigger (proposal 145 step 5) ────────────────


def _feedback_plan(*, addressed: bool = False) -> EvolutionPlan:
    plan = _pending_plan()
    plan.reviewer_feedback = [
        PlanFeedback(
            feedback_id="fb-1",
            text="narrow the scope",
            addressed_at="2025-01-02T00:00:00Z" if addressed else None,
        ),
    ]
    return plan


def _feedback_agent(make_agent, plans):  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.loop_plan_feedback_enabled = True
    agent.memory.get_pending_plans = MagicMock(return_value=plans)
    return agent


def _off(agent, **overrides):  # type: ignore[no-untyped-def]
    kwargs = {"iteration": 11, "curate_every": 5, **overrides}
    return _gate(agent, **kwargs)


def test_feedback_trigger_fires_off_cadence(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = _feedback_agent(make_agent, [_feedback_plan()])
    assert _off(agent) is True


def test_feedback_trigger_respects_curate_enabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = _feedback_agent(make_agent, [_feedback_plan()])
    assert _off(agent, curate_enabled=False) is False
    agent.memory.get_pending_plans.assert_not_called()


def test_feedback_trigger_respects_budget_throttle(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = _feedback_agent(make_agent, [_feedback_plan()])
    agent._maintenance_mode_active = True
    assert _off(agent) is False
    agent.memory.get_pending_plans.assert_not_called()


def test_feedback_trigger_respects_focus_suppression(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = _feedback_agent(make_agent, [_feedback_plan()])
    agent.config.work_focus_profile = "external_delivery"
    assert _off(agent) is False
    agent.memory.get_pending_plans.assert_not_called()


def test_feedback_trigger_respects_smoke(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = _feedback_agent(make_agent, [_feedback_plan()])
    assert _off(agent, smoke=True) is False
    agent.memory.get_pending_plans.assert_not_called()


def test_feedback_trigger_ignores_curate_every_zero(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 145 Step 5: the off-cadence arm fires regardless of curate_every."""
    agent = _feedback_agent(make_agent, [_feedback_plan()])
    assert _off(agent, curate_every=0) is True


def test_feedback_trigger_curate_every_zero_without_feedback_skips(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = _feedback_agent(make_agent, [])
    assert _off(agent, curate_every=0) is False


def test_feedback_only_on_approved_plan_skips_off_cadence(make_agent) -> None:  # type: ignore[no-untyped-def]
    plan = _feedback_plan()
    plan.status = "approved"
    agent = _feedback_agent(make_agent, [plan])
    assert _off(agent) is False


def test_feedback_trigger_dedups_same_iteration(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = _feedback_agent(make_agent, [_feedback_plan()])
    agent._last_phase_iteration["curate_pending_plans"] = 11
    assert _off(agent) is False


def test_feedback_trigger_flag_off_never_reads(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = _feedback_agent(make_agent, [_feedback_plan()])
    agent.config.loop_plan_feedback_enabled = False
    assert _off(agent) is False
    assert _gate(agent) is True
    agent.memory.get_pending_plans.assert_not_called()


def test_feedback_bit_cached_between_curate_passes(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = _feedback_agent(make_agent, [])
    assert _off(agent, iteration=11) is False
    assert _off(agent, iteration=12) is False
    assert agent.memory.get_pending_plans.call_count == 1


def test_feedback_bit_rereads_after_curate_pass(make_agent) -> None:  # type: ignore[no-untyped-def]
    plan = _feedback_plan()
    agent = _feedback_agent(make_agent, [plan])
    assert _off(agent, iteration=11) is True
    # The pass ran and addressed the feedback; run_loop records its marker.
    agent._last_phase_iteration["curate_pending_plans"] = 11
    plan.reviewer_feedback[0].addressed_at = "2025-01-03T00:00:00Z"
    assert _off(agent, iteration=12) is False
    assert _off(agent, iteration=13) is False
    assert agent.memory.get_pending_plans.call_count == 2


def test_invalidate_feedback_pending_forces_reread(make_agent) -> None:  # type: ignore[no-untyped-def]
    plans: list[EvolutionPlan] = []
    agent = _feedback_agent(make_agent, plans)
    assert _off(agent, iteration=11) is False
    plans.append(_feedback_plan())
    assert _off(agent, iteration=12) is False
    agent._lifecycle.invalidate_feedback_pending()
    assert _off(agent, iteration=13) is True
    assert agent.memory.get_pending_plans.call_count == 2


def test_feedback_bit_not_consulted_on_cadence(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = _feedback_agent(make_agent, [_feedback_plan()])
    assert _gate(agent) is True
    agent.memory.get_pending_plans.assert_not_called()
