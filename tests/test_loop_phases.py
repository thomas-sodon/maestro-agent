"""Tests for the loop phase-runner registry (proposal 106).

Covers three surfaces:

* the order-pin guard on :data:`REGISTERED_PHASE_NAMES` (mirroring
  ``test_dequeue_gate_table_order_is_pinned``) plus the
  :func:`validate_registered_phase_order` validator's reject paths,
* one direct-call unit test per migrated ``_phase_*`` runner (Style A) —
  each asserts the runner drives its collaborator, and
* dispatch tests (Style B) proving ``run_loop`` routes a migrated phase
  through the ``self._phase_runners`` table rather than an inline branch.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.agent import _LOOP_PHASES
from maestro.evolution import MergeCheckResult
from maestro.evolution_tend import TendPRsResult
from maestro.loop.phases import (
    REGISTERED_PHASE_NAMES,
    validate_registered_phase_order,
)
from maestro.sdk import QueryOptions
from maestro.vcs import VCSError
from tests.support.gap_fuel import stub_evolve_phase

# ---------------------------------------------------------------------------
# Registry order pin + validator
# ---------------------------------------------------------------------------


def test_registered_phase_names_is_pinned() -> None:
    """The migrated-runner set and its order are load-bearing; a reorder or a
    membership change must fail this guard (proposal 106)."""
    assert REGISTERED_PHASE_NAMES == (
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


def test_registered_names_are_order_preserving_subsequence() -> None:
    """Every registered name appears in ``_LOOP_PHASES`` exactly once and in the
    same relative order, so the table lookup can never reorder a phase."""
    positions = [_LOOP_PHASES.index(name) for name in REGISTERED_PHASE_NAMES]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)
    # The validator agrees with the live ``_LOOP_PHASES`` and does not raise.
    validate_registered_phase_order(_LOOP_PHASES)


def test_validate_rejects_missing_member() -> None:
    """A registered name absent from the supplied ``loop_phases`` fails loud."""
    truncated = tuple(name for name in _LOOP_PHASES if name != "run_task")
    with pytest.raises(ValueError, match="not a member of _LOOP_PHASES"):
        validate_registered_phase_order(truncated)


def test_validate_rejects_out_of_order() -> None:
    """A ``loop_phases`` whose order disagrees with the registry fails loud."""
    reversed_phases = tuple(reversed(_LOOP_PHASES))
    with pytest.raises(ValueError, match="order-preserving subsequence"):
        validate_registered_phase_order(reversed_phases)


def test_validate_rejects_unregistered_loop_phase() -> None:
    """A ``loop_phases`` member with no registered runner fails loud at
    construction rather than surfacing only as a mid-loop ``KeyError`` at the
    hard ``self._phase_runners[phase]`` dispatch lookup: the subsequence check
    alone tolerates an extra phase, so the validator must also enforce the
    reverse cover."""
    # Append a phase the registry does not know about — order is preserved and
    # every registered name is still present, so only the cover check can catch
    # it.
    with_extra = (*_LOOP_PHASES, "phantom_unregistered_phase")
    with pytest.raises(ValueError, match="mid-loop KeyError"):
        validate_registered_phase_order(with_extra)


# ---------------------------------------------------------------------------
# Per-instance runner map
# ---------------------------------------------------------------------------


def test_phase_runners_keys_equal_registered_names(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    assert set(agent._phase_runners) == set(REGISTERED_PHASE_NAMES)


def test_every_registered_name_resolves_to_coroutine_runner(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    for name in REGISTERED_PHASE_NAMES:
        runner = agent._phase_runners[name]
        assert asyncio.iscoroutinefunction(runner), name


# ---------------------------------------------------------------------------
# Style A — direct-call unit tests, one per migrated runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_ensure_main_branch_drives_collaborator(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.evolution.ensure_main_branch = MagicMock()
    await agent._phase_ensure_main_branch(iteration=1)
    agent.evolution.ensure_main_branch.assert_called_once_with()


@pytest.mark.asyncio
async def test_phase_check_contributions_drives_collaborator(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.contributions.check_for_contributions = MagicMock()
    await agent._phase_check_contributions(iteration=1)
    agent.contributions.check_for_contributions.assert_called_once()
    call = agent.contributions.check_for_contributions.call_args
    assert call.args[0] is agent.journal


@pytest.mark.asyncio
async def test_phase_process_messages_sets_iter_context(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._process_messages = AsyncMock(return_value="ctx-xyz")
    await agent._phase_process_messages(iteration=1)
    agent._process_messages.assert_awaited_once()
    assert agent._iter_message_context == "ctx-xyz"


@pytest.mark.asyncio
async def test_phase_tend_evolution_prs_updates_count_and_routes(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    result = TendPRsResult(enumerated=True, tended=3)
    agent.evolution.tend_evolution_prs = AsyncMock(return_value=result)
    agent._route_pr_feedback_to_pending_requests = MagicMock()
    agent._reconcile_settled_tend_prs = MagicMock()
    agent._open_evolution_pr_count = 0
    await agent._phase_tend_evolution_prs(iteration=5)
    agent.evolution.tend_evolution_prs.assert_awaited_once_with(
        settled_oracle=agent.is_pr_settled,
    )
    assert agent._open_evolution_pr_count == 3
    agent._route_pr_feedback_to_pending_requests.assert_called_once_with(result)
    agent._reconcile_settled_tend_prs.assert_called_once_with(result, 5)


@pytest.mark.asyncio
async def test_phase_tend_evolution_prs_preserves_count_when_not_enumerated(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    agent = make_agent()
    result = TendPRsResult(enumerated=False, tended=9)
    agent.evolution.tend_evolution_prs = AsyncMock(return_value=result)
    agent._route_pr_feedback_to_pending_requests = MagicMock()
    agent._reconcile_settled_tend_prs = MagicMock()
    agent._open_evolution_pr_count = 2
    await agent._phase_tend_evolution_prs(iteration=1)
    # ``enumerated`` False leaves the loop-held count untouched.
    assert agent._open_evolution_pr_count == 2


@pytest.mark.asyncio
async def test_phase_attend_records_one_episode(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The observe-only ``attend`` runner (proposal 130) appends exactly one
    conscious episode per pass and stamps the recorded moment id as the active
    id — a direct call bypasses the flag gate, so this covers the body itself."""
    agent = make_agent()
    await agent._phase_attend(iteration=1)
    episodes = agent.conscious_episodes.read_all()
    assert len(episodes) == 1
    assert agent._active_conscious_moment_id == episodes[0].moment.moment_id
    # First pass with an empty ledger is a genesis moment.
    assert episodes[0].moment.continuity == "genesis"


@pytest.mark.asyncio
async def test_phase_respond_to_messages_drives_collaborator(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._respond_to_messages = AsyncMock()
    agent._iter_message_context = "msg-ctx"
    agent._iter_reply_context = "reply-ctx"
    agent._iter_reflection = "reflection-text"
    await agent._phase_respond_to_messages(iteration=1)
    agent._respond_to_messages.assert_awaited_once_with(
        "reply-ctx", "reflection-text",
    )


@pytest.mark.asyncio
async def test_phase_execute_approved_publications_ticks_publisher(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    agent = make_agent()
    publisher_instance = MagicMock()
    publisher_instance.tick = AsyncMock()
    with patch(
        "maestro.publication.Publisher",
        return_value=publisher_instance,
    ) as publisher_cls:
        await agent._phase_execute_approved_publications(iteration=1)
    publisher_cls.assert_called_once()
    publisher_instance.tick.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase_run_task_restores_running_flag(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._run_task_phase = AsyncMock()
    agent._running = False
    await agent._phase_run_task(iteration=1)
    agent._run_task_phase.assert_awaited_once()
    assert agent._running is True


@pytest.mark.asyncio
async def test_phase_run_task_restores_running_flag_on_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._run_task_phase = AsyncMock(side_effect=RuntimeError("boom"))
    agent._running = False
    with pytest.raises(RuntimeError, match="boom"):
        await agent._phase_run_task(iteration=1)
    # The ``finally`` restores the loop-level running flag even on failure.
    assert agent._running is True


@pytest.mark.asyncio
async def test_phase_audit_self_model_delegates(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._audit_self_model = AsyncMock()
    await agent._phase_audit_self_model(iteration=7)
    agent._audit_self_model.assert_awaited_once_with(iteration=7)


@pytest.mark.asyncio
async def test_phase_check_merged_plans_drives_check_and_reconciler(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """The runner offloads the merge check and threads ``iteration`` into the
    settlement-reconciler tail — the one migrated runner that consumes it."""
    agent = make_agent()
    result = MergeCheckResult(merged=[], rejected=[])
    agent.evolution.check_merged_plans = MagicMock(return_value=result)
    agent._reconcile_settled_merge_check = MagicMock()
    await agent._phase_check_merged_plans(iteration=4)
    agent.evolution.check_merged_plans.assert_called_once_with(
        settled_oracle=agent.is_pr_settled,
    )
    agent._reconcile_settled_merge_check.assert_called_once_with(result, 4)


@pytest.mark.asyncio
async def test_phase_review_artifacts_backfills_and_selects(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The runner constructs an ``ArtifactReviewer``, always backfills, and
    selects candidates; with none selected it skips the LLM ``review`` call."""
    agent = make_agent()
    reviewer = MagicMock()
    reviewer.backfill = AsyncMock()
    reviewer.select_candidates = MagicMock(return_value=[])
    reviewer.review = AsyncMock()
    with patch(
        "maestro.artifact_review.ArtifactReviewer",
        return_value=reviewer,
    ) as reviewer_cls:
        await agent._phase_review_artifacts(iteration=1)
    reviewer_cls.assert_called_once()
    reviewer.backfill.assert_awaited_once()
    reviewer.select_candidates.assert_called_once_with(
        agent.config.artifact_review_batch_size,
    )
    reviewer.review.assert_not_awaited()


@pytest.mark.asyncio
async def test_phase_process_rejected_plans_drives_delegate(make_agent) -> None:  # type: ignore[no-untyped-def]
    """With no pending rejection and session reuse off (the ``make_agent``
    default), the runner drives ``_process_rejected_plans`` with no session."""
    agent = make_agent()
    agent._process_rejected_plans = AsyncMock()
    await agent._phase_process_rejected_plans(iteration=1)
    agent._process_rejected_plans.assert_awaited_once_with(session=None, self_model_session=None)


@pytest.mark.asyncio
async def test_phase_generate_task_drives_delegate(make_agent) -> None:  # type: ignore[no-untyped-def]
    """With session reuse off, the runner drives ``generate_task`` with no
    Session D opened."""
    agent = make_agent()
    agent.generate_task = AsyncMock()
    await agent._phase_generate_task(iteration=1)
    agent.generate_task.assert_awaited_once_with(session=None)


# ---------------------------------------------------------------------------
# Style A — the five runners migrated this increment (proposal 106): the
# persistent-session phases (contemplate / implement_approved_plans / evolve /
# curate_pending_plans / dream).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_contemplate_threads_session_a_and_restores_running(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """The runner get-or-creates the slot-A session into ``self._session_a``,
    threads it (plus the iteration index) into ``contemplate``, stores the
    reflection, and restores the loop-level running flag."""
    agent = make_agent()
    sess_a = MagicMock()
    agent._get_or_create_session = AsyncMock(return_value=sess_a)
    agent.contemplate = AsyncMock(return_value="reflection-text")
    agent._iter_message_context = "msg-ctx"
    agent._running = False
    await agent._phase_contemplate(iteration=3)
    assert agent._get_or_create_session.await_args.kwargs["slot"] == "a"
    assert agent._session_a is sess_a
    agent.contemplate.assert_awaited_once()
    assert agent.contemplate.await_args.kwargs["session"] is sess_a
    assert agent.contemplate.await_args.kwargs["iteration"] == 3
    assert agent.contemplate.await_args.kwargs["context"] == "msg-ctx"
    assert agent._iter_reflection == "reflection-text"
    assert agent._running is True


@pytest.mark.asyncio
async def test_phase_contemplate_passes_built_options_into_session(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """The runner threads ``_build_contemplation_session_options()``'s result into
    ``_get_or_create_session`` rather than an inline literal (proposal 132)."""
    agent = make_agent()
    sentinel = QueryOptions(
        caller="contemplation_session", tools=[], max_turns=99,
    )
    agent._build_contemplation_session_options = MagicMock(return_value=sentinel)
    agent._get_or_create_session = AsyncMock(return_value=MagicMock())
    agent.contemplate = AsyncMock(return_value="r")

    await agent._phase_contemplate(iteration=1)

    agent._build_contemplation_session_options.assert_called_once_with()
    assert agent._get_or_create_session.await_args.kwargs["options"] is sentinel


@pytest.mark.asyncio
async def test_phase_contemplate_restores_running_on_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A failing ``contemplate`` still restores the loop-level running flag via
    the runner's ``finally``."""
    agent = make_agent()
    agent._get_or_create_session = AsyncMock(return_value=None)
    agent.contemplate = AsyncMock(side_effect=RuntimeError("boom"))
    agent._running = False
    with pytest.raises(RuntimeError, match="boom"):
        await agent._phase_contemplate(iteration=1)
    assert agent._running is True


@pytest.mark.asyncio
async def test_phase_implement_approved_plans_disconnects_session_a_first(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """When a slot-A session is live, the runner disconnects and nulls it
    before delegating to ``_implement_approved_plans``."""
    agent = make_agent()
    sess_a = MagicMock()
    agent._session_a = sess_a
    order: list[str] = []

    async def _disc(sess) -> None:  # type: ignore[no-untyped-def]
        order.append(f"disconnect:{sess is sess_a}")

    async def _impl(**_kwargs: object) -> None:
        order.append("implement")

    agent._implement_approved_plans = _impl  # type: ignore[method-assign]
    with patch("maestro.agent.disconnect_session", side_effect=_disc):
        await agent._phase_implement_approved_plans(iteration=1)
    assert order == ["disconnect:True", "implement"]
    assert agent._session_a is None


@pytest.mark.asyncio
async def test_phase_implement_approved_plans_no_session_just_delegates(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """With no slot-A session, the runner skips the disconnect and delegates."""
    agent = make_agent()
    agent._session_a = None
    agent._implement_approved_plans = AsyncMock()
    with patch(
        "maestro.agent.disconnect_session", new_callable=AsyncMock,
    ) as mock_disc:
        await agent._phase_implement_approved_plans(iteration=1)
    mock_disc.assert_not_awaited()
    agent._implement_approved_plans.assert_awaited_once_with(iteration=1)


@pytest.mark.asyncio
async def test_phase_evolve_threads_session_b_and_restores_running(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """The runner get-or-creates the slot-B session into ``self._session_b``,
    threads it into the fuelled evolve body, and restores the running flag."""
    agent = make_agent()
    sess_b = MagicMock()
    agent._get_or_create_session = AsyncMock(return_value=sess_b)
    body = stub_evolve_phase(agent, AsyncMock())
    agent._running = False
    await agent._phase_evolve(iteration=2)
    assert agent._get_or_create_session.await_args.kwargs["slot"] == "b"
    assert agent._session_b is sess_b
    body.assert_awaited_once_with(agent._admitted_evolution_fuel.return_value, sess_b)
    assert agent._running is True


@pytest.mark.asyncio
async def test_phase_evolve_restores_running_on_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A failing ``evolve`` still restores the loop-level running flag."""
    agent = make_agent()
    agent._get_or_create_session = AsyncMock(return_value=None)
    stub_evolve_phase(agent, AsyncMock(side_effect=RuntimeError("nope")))
    agent._running = False
    with pytest.raises(RuntimeError, match="nope"):
        await agent._phase_evolve(iteration=1)
    assert agent._running is True


@pytest.mark.asyncio
async def test_phase_curate_pending_plans_assigns_session_b(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The runner assigns the helper's session-returning result back to
    ``self._session_b`` so the slot survives into ``dream``."""
    agent = make_agent()
    returned_session = MagicMock()
    agent._curate_pending_plans = AsyncMock(return_value=returned_session)
    agent._session_b = None
    await agent._phase_curate_pending_plans(iteration=1)
    agent._curate_pending_plans.assert_awaited_once_with(session=None)
    assert agent._session_b is returned_session


@pytest.mark.asyncio
async def test_phase_dream_refreshes_report_and_archives(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The runner refreshes the phase-cost report, then archives best-effort
    with the configured threshold (proposal 149 Step 5: no priced digest)."""
    agent = make_agent()
    agent.config.archive_threshold_days = 30
    agent._session_b = MagicMock()
    agent.memory.archive = MagicMock()  # type: ignore[method-assign]
    agent._open_owned_pr_states = MagicMock(  # type: ignore[method-assign]
        return_value={"maestro/evolve/abc123": MagicMock(), "other/x": MagicMock()},
    )
    with patch("maestro.agent.refresh_phase_cost_report") as refresh:
        await agent._phase_dream(iteration=6)
    refresh.assert_called_once_with(
        agent.cost_samples, agent.config.artifacts_dir / "phase_cost_report.md")
    agent._open_owned_pr_states.assert_called_once_with(strict=True)
    agent.memory.archive.assert_called_once_with(
        agent.config.archive_threshold_days,
        retry_budget=agent.config.max_implementation_attempts,
        open_pr_plan_ids={"abc123"},
    )


@pytest.mark.asyncio
async def test_phase_dream_unknown_pr_state_passes_none(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A failed open-PR listing reads as unknown: ``None`` holds PR-backed rows."""
    agent = make_agent()
    agent.config.archive_threshold_days = 30
    agent._session_b = None
    agent.memory.archive = MagicMock()  # type: ignore[method-assign]
    agent._open_owned_pr_states = MagicMock(  # type: ignore[method-assign]
        side_effect=VCSError("gh down"),
    )
    await agent._phase_dream(iteration=1)
    assert agent.memory.archive.call_args.kwargs["open_pr_plan_ids"] is None


@pytest.mark.asyncio
async def test_phase_dream_disabled_archive_skips_pr_listing(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A 0 threshold disables archival without paying for a ``gh pr list``."""
    agent = make_agent()
    agent.config.archive_threshold_days = 0
    agent._session_b = None
    agent.memory.archive = MagicMock()  # type: ignore[method-assign]
    agent._open_owned_pr_states = MagicMock()  # type: ignore[method-assign]
    await agent._phase_dream(iteration=1)
    agent._open_owned_pr_states.assert_not_called()
    agent.memory.archive.assert_not_called()



@pytest.mark.asyncio
async def test_phase_dream_swallows_archive_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An archive I/O failure is logged and swallowed — the runner must not
    propagate it (the log-and-fall-through contract moved with the code)."""
    agent = make_agent()
    agent.config.archive_threshold_days = 30
    agent._session_b = None
    agent.memory.archive = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("disk full"),
    )
    agent._open_owned_pr_states = MagicMock(  # type: ignore[method-assign]
        return_value={},
    )
    # Must not raise.
    await agent._phase_dream(iteration=1)
    agent.memory.archive.assert_called_once()


# ---------------------------------------------------------------------------
# Style B — dispatch routing through the registry table
# ---------------------------------------------------------------------------


def _neutralize_loop(agent) -> None:  # type: ignore[no-untyped-def]
    """Apply the known-good ``run_loop`` mock set (mirrors the heartbeat tests)
    plus the migrated work-methods, so a single iteration is deterministic."""
    agent._install_signal_handlers = MagicMock()
    agent._write_pid_file = MagicMock()
    agent.contemplate = AsyncMock(return_value="reflection")
    agent._implement_approved_plans = AsyncMock()
    agent.evolve = AsyncMock()
    agent.shutdown = AsyncMock()
    agent._process_messages = AsyncMock(return_value="")
    agent._respond_to_messages = AsyncMock()
    agent._run_task_phase = AsyncMock()
    agent._audit_self_model = AsyncMock()
    agent.contributions.check_for_contributions = MagicMock()


@pytest.mark.asyncio
async def test_run_loop_dispatches_migrated_phases_via_registry(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A full iteration drives the always-scheduled migrated phases'
    collaborators. Their only dispatch path is now the ``_phase_runners`` table
    (the inline ``elif`` branches were deleted), so their execution proves the
    registry routing works end-to-end."""
    agent = make_agent()
    _neutralize_loop(agent)
    # ``ensure_main_branch`` is a neutralised MagicMock via the make_agent
    # fixture; ``check_contributions`` / ``process_messages`` are stubbed above.
    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )
    agent.evolution.ensure_main_branch.assert_called()
    agent.contributions.check_for_contributions.assert_called()
    agent._process_messages.assert_awaited()


@pytest.mark.asyncio
async def test_run_loop_honours_phase_runners_table_entry(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """Swapping a table entry redirects dispatch: the swapped runner is awaited
    with the iteration index and the phase's real collaborator never runs —
    proving the dispatch reads ``self._phase_runners``, not an inline branch."""
    agent = make_agent()
    _neutralize_loop(agent)
    spy = AsyncMock()
    agent._phase_runners["ensure_main_branch"] = spy
    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )
    spy.assert_awaited_once()
    assert isinstance(spy.await_args.kwargs["iteration"], int)
    # The table entry replaced the dispatch — the real collaborator is bypassed.
    agent.evolution.ensure_main_branch.assert_not_called()


@pytest.mark.asyncio
async def test_run_loop_routes_newly_migrated_phase_via_registry(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A phase migrated onto the registry this increment (proposal 106) —
    ``process_rejected_plans``, which runs every non-smoke iteration — now
    dispatches only through ``self._phase_runners``. Swapping its table entry
    with a spy runs the spy with the iteration index while the real
    collaborator is bypassed, proving its inline ``elif`` branch is gone."""
    agent = make_agent()
    _neutralize_loop(agent)
    agent._process_rejected_plans = AsyncMock()
    spy = AsyncMock()
    agent._phase_runners["process_rejected_plans"] = spy
    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )
    spy.assert_awaited_once()
    assert isinstance(spy.await_args.kwargs["iteration"], int)
    agent._process_rejected_plans.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_loop_routes_contemplate_via_registry(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A persistent-session phase migrated onto the registry this increment
    (proposal 106) — ``contemplate``, which runs every iteration
    (``contemplate_every=1``) — now dispatches only through
    ``self._phase_runners`` (its inline ``elif`` branch was deleted). Swapping
    its table entry with a spy runs the spy with the iteration index while the
    real ``contemplate`` collaborator is bypassed."""
    agent = make_agent()
    _neutralize_loop(agent)
    spy = AsyncMock()
    agent._phase_runners["contemplate"] = spy
    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )
    spy.assert_awaited_once()
    assert isinstance(spy.await_args.kwargs["iteration"], int)
    # The table entry replaced the dispatch — the real collaborator is bypassed.
    agent.contemplate.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_loop_routes_attend_via_registry_when_enabled(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """The observe-only ``attend`` phase (proposal 130) dispatches only through
    ``self._phase_runners`` when its flag is on. Swapping its table entry
    with a spy runs the spy with the iteration index; with the flag off the same
    swap never fires, proving the gate keeps the phase dark when disabled."""
    agent = make_agent()
    agent.config.conscious_workspace_observe_enabled = False
    _neutralize_loop(agent)
    spy = AsyncMock()
    agent._phase_runners["attend"] = spy
    # Flag off (the rollback): the gated phase never dispatches.
    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )
    spy.assert_not_awaited()
    # Flag on: the phase dispatches through the registry table.
    agent.config.conscious_workspace_observe_enabled = True
    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )
    spy.assert_awaited_once()
    assert isinstance(spy.await_args.kwargs["iteration"], int)
