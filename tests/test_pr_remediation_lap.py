"""PR-remediation lap: dequeue gates, stalled-head yield, merge-truth stamps,
comment completeness and trusted reply delivery
(``maestro/loop/pr_remediation.py``); split out of ``tests/test_agent.py``.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import evolution_quality
from maestro.agent import (
    MaestroAgent,
)
from maestro.consciousness import PendingPRRequest
from maestro.evolution import QualityGateResult
from maestro.loop.pr_remediation import (
    STALLED_REMEDIATION_LAP_CAP,
    _is_stalled_remediation_lineage,
)
from maestro.pr_facts import MergeTruth
from maestro.pr_remediation_state import Marker
from maestro.sdk import ErrorCategory, SDKError
from tests.conftest import MockQueryResult
from tests.test_agent import (
    _STALL_BASE,
    _STALL_HEAD,
    _make_task_record,
    _remediation_lap,
    _stall_rev_parse,
)


def _seed_approved_plan(agent: MaestroAgent, plan_id: str, title: str) -> None:
    from maestro.memory import EvolutionPlan

    plan = EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)


def _clean_observation():  # type: ignore[no-untyped-def]
    """A green ``GateRunObservation`` — every gate CLEAN (proposal 124)."""
    from maestro.gate_observation import GateObservation, GateRunObservation

    return GateRunObservation(
        gates=(
            GateObservation(tool="ruff", kind="CLEAN", failures=(), output=""),
            GateObservation(tool="mypy", kind="CLEAN", failures=(), output=""),
            GateObservation(tool="tests", kind="CLEAN", failures=(), output=""),
        ),
    )


# --- Dequeue-face merged-PR exclusion (_gate_pr_remediation_merged) ---------
#
# At the ``pending_pr_requests`` dequeue, a PR merge-truth shows already merged
# into ``origin/main`` has nothing left to remediate. The exclusion evicts it
# (the pop already removed it) and returns before routing a ``Remediate PR #N``
# task or recording any stamp — starving the false-success-recurrence counter
# at its source. It runs *before* the merge-block gate and preflight, and reads
# merge-truth through the shared ``is_pr_settled`` chokepoint (proposal 087).


def _dequeue_merge_truth(*, branch_landed: bool) -> MergeTruth:
    """A MERGED/landed or OPEN/unsettled :class:`MergeTruth` for the dequeue
    tests (the chokepoint the merged-exclusion gate now reads through)."""
    return MergeTruth(
        pr_number=0,
        settled=branch_landed,
        state="MERGED" if branch_landed else "OPEN",
        branch_landed=branch_landed,
        source="host_api",
        observed_at=0.0,
    )


@pytest.mark.asyncio
async def test_run_task_phase_merged_pr_evicted_no_remediation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A dequeued PR that merge-truth shows merged is evicted with no
    remediation task and no stamp."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #77: fix it",
            pr_branch="maestro/evolve/ep77",
        ),
    ]
    agent.consciousness.save_self_model(model)
    # merge-truth: PR #77 has landed on origin/main.
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_dequeue_merge_truth(branch_landed=True),
    )
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    # No remediation task was routed — run() never invoked.
    agent.run.assert_not_awaited()
    # The entry was evicted from the queue (the pop is the eviction).
    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == []
    # No TaskRecord stamp was produced for the merged PR.
    assert agent.memory.get_recent_tasks(10) == []
    assert agent.remediation_tracker._data == {}


@pytest.mark.asyncio
async def test_run_task_phase_unmerged_pr_routes_to_remediation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An unmerged dequeued PR routes to remediation exactly as before — no
    live-path regression from the merged-PR exclusion."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #88: fix it",
            pr_branch="maestro/evolve/ep88",
        ),
    ]
    agent.consciousness.save_self_model(model)
    # merge-truth: PR #88 has not landed — remediation should proceed.
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_dequeue_merge_truth(branch_landed=False),
    )
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            task_id="t88",
            outcome="success",
            result_summary="Committed a real fix.",
            key_findings="Patched the bug.",
        ),
    )

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0] == "Remediate PR #88: fix it"
    assert kwargs["pr_branch"] == "maestro/evolve/ep88"
    # The exclusion consulted the chokepoint and let the PR through; the
    # post-run outcome recorder's items-noop fallback also reads the same
    # (per-iteration memoized) observation, so #88 is asked through the
    # chokepoint at least once.
    agent.is_pr_settled.assert_any_call(88)


@pytest.mark.asyncio
async def test_run_task_phase_merged_check_consulted_at_most_once(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The merged check is consulted at most once per dequeued item.

    The pre-pop CI-pending gate reads CI state first (GREEN by the fixture
    default) and short-circuits before any ``observe_pr_readiness`` /
    ``is_pr_settled`` consultation, so the merged-exclusion gate remains the sole
    caller of the chokepoint for this item.
    """
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #99: fix it",
            pr_branch="maestro/evolve/ep99",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_dequeue_merge_truth(branch_landed=True),
    )
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.is_pr_settled.assert_called_once_with(99)


def test_is_stalled_remediation_lineage_at_cap_same_sha() -> None:
    """``>= cap`` consecutive same-SHA partial laps for the PR => stalled."""
    laps = [
        _remediation_lap(task_id="t1", pr_num=2727),
        _remediation_lap(task_id="t2", pr_num=2727),
    ]
    assert (
        _is_stalled_remediation_lineage(
            "Remediate PR #2727: fix the threads", _STALL_HEAD, laps,
        )
        is True
    )


def test_is_stalled_remediation_lineage_below_cap() -> None:
    """A single same-SHA partial lap is below the cap => not stalled."""
    laps = [_remediation_lap(task_id="t1", pr_num=2727)]
    assert (
        _is_stalled_remediation_lineage(
            "Remediate PR #2727: fix the threads", _STALL_HEAD, laps,
        )
        is False
    )


def test_is_stalled_remediation_lineage_changed_sha_breaks_streak() -> None:
    """A lap whose head SHA differs from the current head breaks the streak."""
    laps = [
        _remediation_lap(task_id="t1", pr_num=2727, pr_head_sha="b" * 40),
        _remediation_lap(task_id="t2", pr_num=2727),
    ]
    # Most recent (t2) matches, but the next-most-recent (t1) is on a different
    # SHA, so the consecutive run is only 1 < cap.
    assert (
        _is_stalled_remediation_lineage(
            "Remediate PR #2727: fix the threads", _STALL_HEAD, laps,
        )
        is False
    )


def test_is_stalled_remediation_lineage_success_breaks_streak() -> None:
    """A non-partial lap ends the consecutive run (else-branch break)."""
    laps = [
        _remediation_lap(task_id="t1", pr_num=2727, outcome="success"),
        _remediation_lap(task_id="t2", pr_num=2727),
    ]
    assert (
        _is_stalled_remediation_lineage(
            "Remediate PR #2727: fix the threads", _STALL_HEAD, laps,
        )
        is False
    )


def test_is_stalled_remediation_lineage_skips_interleaved_tasks() -> None:
    """Non-matching interleaved tasks are ignored, not treated as breaks."""
    laps = [
        _remediation_lap(task_id="t1", pr_num=2727),
        _make_task_record(task_id="construction", outcome="success"),
        _remediation_lap(task_id="t2", pr_num=2727),
    ]
    assert (
        _is_stalled_remediation_lineage(
            "Remediate PR #2727: fix the threads", _STALL_HEAD, laps,
        )
        is True
    )


def test_is_stalled_remediation_lineage_non_remediation_goal() -> None:
    """A goal that is not a ``Remediate PR #N:`` directive is never stalled."""
    laps = [
        _remediation_lap(task_id="t1", pr_num=2727),
        _remediation_lap(task_id="t2", pr_num=2727),
    ]
    assert (
        _is_stalled_remediation_lineage("Build a new sensor", _STALL_HEAD, laps)
        is False
    )


def test_is_stalled_remediation_lineage_missing_head_routes_normally() -> None:
    """An unresolved current head SHA returns False (route normally)."""
    laps = [
        _remediation_lap(task_id="t1", pr_num=2727),
        _remediation_lap(task_id="t2", pr_num=2727),
    ]
    assert (
        _is_stalled_remediation_lineage(
            "Remediate PR #2727: fix the threads", None, laps,
        )
        is False
    )


def test_is_stalled_remediation_lineage_non_positive_cap() -> None:
    """A non-positive cap disables the brake (always route normally)."""
    laps = [
        _remediation_lap(task_id="t1", pr_num=2727),
        _remediation_lap(task_id="t2", pr_num=2727),
    ]
    assert (
        _is_stalled_remediation_lineage(
            "Remediate PR #2727: fix the threads", _STALL_HEAD, laps, cap=0,
        )
        is False
    )


def test_pending_pr_head_stalled_no_branch_routes_normally(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An entry with no ``pr_branch`` cannot resolve a head => not stalled."""
    agent = make_agent()
    entry = PendingPRRequest(
        directive="Remediate PR #2727: fix it", pr_branch=None,
    )
    assert agent._pending_pr_head_stalled(entry) is False


def test_pending_pr_head_stalled_non_remediation_directive(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A non-``Remediate PR #N:`` directive is never stalled, never hits git."""
    agent = make_agent()
    agent._vcs.rev_parse = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError("rev_parse must not be called"),
    )
    entry = PendingPRRequest(
        directive="Address review feedback", pr_branch="maestro/evolve/ep1",
    )
    assert agent._pending_pr_head_stalled(entry) is False


def test_pending_pr_head_stalled_no_lap_history_skips_git(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A PR with no prior laps in history skips the ``git rev-parse`` entirely."""
    agent = make_agent()
    agent._vcs.rev_parse = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError("rev_parse must not be called"),
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2727: fix it",
        pr_branch="maestro/evolve/ep2727",
    )
    assert agent._pending_pr_head_stalled(entry) is False


@pytest.mark.asyncio
async def test_run_task_phase_stalled_lineage_skips_and_falls_through(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A >= cap same-SHA partial lineage is skipped (left queued) and the chain
    falls through to the empty self-directed queue with zero SDK cost."""
    agent = make_agent()
    agent._vcs.rev_parse.side_effect = _stall_rev_parse
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #2727: fix it",
            pr_branch="maestro/evolve/ep2727",
        ),
    ]
    agent.consciousness.save_self_model(model)
    for i in range(STALLED_REMEDIATION_LAP_CAP):
        agent.memory.record_task(
            _remediation_lap(
                task_id=f"lap{i}", pr_num=2727, base_head_sha=_STALL_BASE,
            ),
        )
    # Guard: the dequeue gates must never run on the skip branch — proving the
    # entry was never consumed.
    agent._pr_remediation.dequeue_gates = AsyncMock(  # type: ignore[method-assign]
        return_value=False,
    )
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    # Skip branch: no SDK task routed, no dequeue gate consulted.
    agent.run.assert_not_awaited()
    agent._pr_remediation.dequeue_gates.assert_not_awaited()
    # Entry preserved at the head of the queue for a future iteration.
    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == model.pending_pr_requests


@pytest.mark.asyncio
async def test_run_task_phase_stalled_lineage_falls_through_to_queue(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When stalled, the chain falls through to the self-directed queue head —
    the queued construction task is routed, not the stalled remediation."""
    from maestro.task_queue import QueuedTask

    agent = make_agent()
    agent._vcs.rev_parse.side_effect = _stall_rev_parse
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #2727: fix it",
            pr_branch="maestro/evolve/ep2727",
        ),
    ]
    agent.consciousness.save_self_model(model)
    for i in range(STALLED_REMEDIATION_LAP_CAP):
        agent.memory.record_task(
            _remediation_lap(
                task_id=f"lap{i}", pr_num=2727, base_head_sha=_STALL_BASE,
            ),
        )
    agent.task_queue.save([QueuedTask(goal="Build a landable construction task")])
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(task_id=None),
    )

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0] == "Build a landable construction task"
    assert kwargs["source"] == "self_directed"
    # Stalled remediation entry stays queued.
    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == model.pending_pr_requests


@pytest.mark.asyncio
async def test_run_task_phase_changed_head_routes_to_remediation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A lineage whose head SHA advanced past the recorded laps is not stalled —
    it routes to remediation as before."""
    agent = make_agent()
    # Current head differs from the laps' recorded SHA -> brake released.
    agent._vcs.rev_parse.return_value = "c" * 40
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #2727: fix it",
            pr_branch="maestro/evolve/ep2727",
        ),
    ]
    agent.consciousness.save_self_model(model)
    for i in range(STALLED_REMEDIATION_LAP_CAP):
        agent.memory.record_task(
            _remediation_lap(task_id=f"lap{i}", pr_num=2727),
        )
    agent._pr_remediation.dequeue_gates = AsyncMock(  # type: ignore[method-assign]
        return_value=False,
    )
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(task_id=None),
    )

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0] == "Remediate PR #2727: fix it"
    assert kwargs["pr_branch"] == "maestro/evolve/ep2727"
    # Entry consumed (popped) on the normal route.
    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == []


@pytest.mark.asyncio
async def test_run_task_phase_below_cap_routes_to_remediation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A first / below-cap remediation request routes normally."""
    agent = make_agent()
    agent._vcs.rev_parse.return_value = _STALL_HEAD
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #2727: fix it",
            pr_branch="maestro/evolve/ep2727",
        ),
    ]
    agent.consciousness.save_self_model(model)
    # Only one same-SHA partial lap recorded -> below the cap.
    agent.memory.record_task(_remediation_lap(task_id="lap0", pr_num=2727))
    agent._pr_remediation.dequeue_gates = AsyncMock(  # type: ignore[method-assign]
        return_value=False,
    )
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(task_id=None),
    )

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, _ = agent.run.await_args
    assert args[0] == "Remediate PR #2727: fix it"
    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == []


@pytest.mark.asyncio
async def test_run_task_phase_stalled_head_selects_healthy_entry_behind_it(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A stalled head no longer starves a healthy entry queued behind it (#2837).

    ``pending_pr_requests`` is consumed head-first, but the old guard inspected
    only index 0: a stalled head wedged the whole FIFO, falling the chain
    through to the (here empty) self-directed queue and starving every healthy
    entry behind it. The first-non-stalled scan instead selects the healthy
    entry while leaving the stuck head queued.
    """
    agent = make_agent()
    agent._vcs.rev_parse.side_effect = _stall_rev_parse
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        # index 0: stalled lineage for #2727 (>= cap same-SHA partial laps).
        PendingPRRequest(
            directive="Remediate PR #2727: fix it",
            pr_branch="maestro/evolve/ep2727",
        ),
        # index 1: a healthy entry for a *different* PR, no lap history, so
        # ``_pending_pr_head_stalled`` returns False for it with no git probe.
        PendingPRRequest(
            directive="Remediate PR #2728: fix the other thing",
            pr_branch="maestro/evolve/ep2728",
        ),
    ]
    agent.consciousness.save_self_model(model)
    for i in range(STALLED_REMEDIATION_LAP_CAP):
        agent.memory.record_task(
            _remediation_lap(
                task_id=f"lap{i}", pr_num=2727, base_head_sha=_STALL_BASE,
            ),
        )
    # Empty self-directed queue: under the #2837 bug the stalled head would skip
    # the whole branch and the chain would fall through to nothing.
    agent._pr_remediation.dequeue_gates = AsyncMock(  # type: ignore[method-assign]
        return_value=False,
    )
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(task_id=None),
    )

    await agent._run_task_phase()

    # The healthy entry behind the stalled head is serviced — not the stuck
    # head, and not a self_directed fall-through (``source="external"`` proves
    # the chain did not drop to the empty queue).
    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0] == "Remediate PR #2728: fix the other thing"
    assert kwargs["pr_branch"] == "maestro/evolve/ep2728"
    assert kwargs["source"] == "external"
    # The dequeue gates ran for the *selected* (healthy) entry.
    agent._pr_remediation.dequeue_gates.assert_awaited_once()
    # Only the healthy entry was popped; the stalled head stays queued at
    # index 0 for a future iteration (auto-released on a new SHA or merge/close).
    after = agent.consciousness.load_self_model()
    assert len(after.pending_pr_requests) == 1
    assert (
        after.pending_pr_requests[0].directive == "Remediate PR #2727: fix it"
    )


def test_first_non_stalled_pr_index_shares_one_history_read(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The scan reads ``get_recent_tasks`` once, not once per leading entry.

    Two leading stalled entries followed by a healthy one: the per-entry
    ``_pending_pr_head_stalled`` probe used to re-read ``get_recent_tasks(30)``
    itself, so K leading stalled entries cost K identical tail reads. The scan
    now hoists a single shared read and passes it into each probe, so the count
    stays one regardless of how many stalled entries it walks past.
    """
    agent = make_agent()
    agent._vcs.rev_parse.side_effect = _stall_rev_parse
    # Two stalled lineages (#2727, #2728) then a healthy entry (#2729, no laps).
    for pr_num in (2727, 2728):
        for i in range(STALLED_REMEDIATION_LAP_CAP):
            agent.memory.record_task(
                _remediation_lap(
                    task_id=f"lap{pr_num}_{i}",
                    pr_num=pr_num,
                    base_head_sha=_STALL_BASE,
                ),
            )
    entries = [
        PendingPRRequest(
            directive="Remediate PR #2727: fix it",
            pr_branch="maestro/evolve/ep2727",
        ),
        PendingPRRequest(
            directive="Remediate PR #2728: fix it",
            pr_branch="maestro/evolve/ep2728",
        ),
        PendingPRRequest(
            directive="Remediate PR #2729: fix it",
            pr_branch="maestro/evolve/ep2729",
        ),
    ]
    spy = MagicMock(side_effect=agent.memory.get_recent_tasks)
    agent.memory.get_recent_tasks = spy  # type: ignore[method-assign]

    index = agent._first_non_stalled_pr_index(entries)

    # The healthy entry behind the two stalled lineages is selected.
    assert index == 2
    # One shared tail read for the whole scan — not one per stalled entry.
    assert spy.call_count == 1


# --- Owned-act preemption of the yielded stalled-remediation slot -------------
#
# ``owned_act_preempt_enabled``: when a stalled ``pending_pr_requests`` head
# would yield its slot to the next non-stalled remediation entry (#2837) *and*
# the rule-18 owned-act drought is armed, the yielded slot is redirected to the
# zero-SDK owned prune (``sweep_clustered/restatement_pending_plans``) instead of
# re-lapping an externally-addressed PR. Self-extinguishes on one owned act.


def _stalled_head_and_healthy_entry_model(
    agent,  # type: ignore[no-untyped-def]
):  # type: ignore[no-untyped-def]
    """Seed a stalled #2727 head with a healthy #2728 entry queued behind it.

    Mirrors the #2837 regression fixture: index 0 is a stalled lineage (>= cap
    same-SHA ``partial`` laps), index 1 is a fresh PR with no lap history, so the
    first-non-stalled scan yields the slot from the head to index 1.
    """
    agent._vcs.rev_parse.side_effect = _stall_rev_parse
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #2727: fix it",
            pr_branch="maestro/evolve/ep2727",
        ),
        PendingPRRequest(
            directive="Remediate PR #2728: fix the other thing",
            pr_branch="maestro/evolve/ep2728",
        ),
    ]
    agent.consciousness.save_self_model(model)
    for i in range(STALLED_REMEDIATION_LAP_CAP):
        agent.memory.record_task(
            _remediation_lap(
                task_id=f"lap{i}", pr_num=2727, base_head_sha=_STALL_BASE,
            ),
        )
    agent._pr_remediation.dequeue_gates = AsyncMock(  # type: ignore[method-assign]
        return_value=False,
    )
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(task_id=None),
    )


@pytest.mark.asyncio
async def test_owned_act_preempt_fills_yielded_slot_with_owned_prune(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on + drought active + stalled-head yield → the owned prune takes the
    slot, not the healthy remediation entry behind the stuck head (criterion 1).
    """
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    agent.config.owned_act_preempt_enabled = True
    _stalled_head_and_healthy_entry_model(agent)
    # Two near-identical ``pending_review`` plans form a prunable cluster, so the
    # owned prune actually retires the older one — proving the yielded slot moved
    # net ``pending_review`` rather than counting a fire.
    for i in range(2):
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=f"dup{i}",
            title="Duplicate cluster plan about telemetry flush cadence",
            category="capability",
            rationale=(
                "Improve telemetry flush cadence for iteration usage rows exactly"
            ),
            proposed_changes=["change telemetry flush"],
            files_affected=["telemetry.py"],
            expected_impact="better",
            status="pending_review",
            created_at=f"2026-06-0{i + 1}T00:00:00+00:00",
        ))
    # Drought armed (predicate stubbed so the scenario is the subject, not the
    # rule-18 measurement, which has its own exhaustive audit-check tests).
    agent._owned_act_drought_active = MagicMock(  # type: ignore[method-assign]
        return_value=True,
    )

    await agent._run_task_phase()

    # The owned prune ran and obsoleted the older cluster member — the slot was
    # filled by the owned-prune path.
    obsolete = [p for p in agent.memory.get_all_plans() if p.status == "obsolete"]
    assert [p.plan_id for p in obsolete] == ["dup0"]
    # No remediation session ran; the healthy entry was NOT serviced.
    agent.run.assert_not_awaited()
    agent._pr_remediation.dequeue_gates.assert_not_awaited()
    # Both pending PR entries stay queued — neither the stuck head nor the
    # healthy entry was popped; the slot went to the owned prune instead.
    after = agent.consciousness.load_self_model()
    assert len(after.pending_pr_requests) == 2


@pytest.mark.asyncio
async def test_owned_act_preempt_inactive_drought_yields_to_next_remediation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Drought inactive → the stalled-head yield routes to the next non-stalled
    remediation entry exactly as before (criterion 2, regression preserved)."""
    agent = make_agent()
    agent.config.owned_act_preempt_enabled = True
    _stalled_head_and_healthy_entry_model(agent)
    swept = MagicMock(side_effect=agent.memory.sweep_clustered_pending_plans)
    agent.memory.sweep_clustered_pending_plans = swept  # type: ignore[method-assign]
    # Drought NOT armed.
    agent._owned_act_drought_active = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    await agent._run_task_phase()

    # The healthy entry behind the stalled head is serviced — unchanged #2837
    # behavior — and the owned prune is never invoked.
    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0] == "Remediate PR #2728: fix the other thing"
    assert kwargs["pr_branch"] == "maestro/evolve/ep2728"
    assert kwargs["source"] == "external"
    swept.assert_not_called()
    after = agent.consciousness.load_self_model()
    assert len(after.pending_pr_requests) == 1
    assert (
        after.pending_pr_requests[0].directive == "Remediate PR #2727: fix it"
    )


@pytest.mark.asyncio
async def test_owned_act_preempt_flag_off_is_strict_noop(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off → strict no-op regardless of drought: the yield routes to the
    next remediation entry and the drought predicate is never even consulted
    (criterion 3)."""
    agent = make_agent()
    agent.config.owned_act_preempt_enabled = False
    _stalled_head_and_healthy_entry_model(agent)
    swept = MagicMock(side_effect=agent.memory.sweep_clustered_pending_plans)
    agent.memory.sweep_clustered_pending_plans = swept  # type: ignore[method-assign]
    # Even with the drought "armed", the flag-off short-circuit must never
    # evaluate it — the selection path is byte-identical to today.
    drought = MagicMock(return_value=True)
    agent._owned_act_drought_active = drought  # type: ignore[method-assign]

    await agent._run_task_phase()

    # Identical to the drought-inactive case: healthy entry serviced.
    agent.run.assert_awaited_once()
    args, _ = agent.run.await_args
    assert args[0] == "Remediate PR #2728: fix the other thing"
    swept.assert_not_called()
    # Strict no-op: the flag-off conjunction short-circuits before the predicate.
    drought.assert_not_called()


@pytest.mark.asyncio
async def test_owned_act_preempt_self_extinguishes_after_owned_act(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """One owned terminal act recorded in-window releases the preemption on the
    next selection (criterion 4, self-extinguish) — exercised through the real
    rule-18 predicate, not a stub."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    agent.config.owned_act_preempt_enabled = True
    # Drop the salvage closure so the drought is pure obsolete/stand-down
    # counting — no code-host interaction in this unit test.
    agent.config.salvage_before_disengage_enabled = False
    _stalled_head_and_healthy_entry_model(agent)
    # Three *content-distinct* pending plans: a prunable backlog above the floor
    # that the mechanical sweep leaves untouched, so the only thing that can move
    # the drought count is the owned act recorded below.
    distinct = (
        ("Refactor telemetry flush cadence for iteration usage rows", "telemetry"),
        ("Improve dashboard websocket reconnection backoff handling", "dashboard"),
        ("Add caching to the serena retrieval bridge lookups", "serena"),
    )
    for i, (rationale, topic) in enumerate(distinct):
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=f"pp{i}",
            title=f"Plan {topic}",
            category="capability",
            rationale=rationale,
            proposed_changes=[f"change {topic}"],
            files_affected=[f"{topic}.py"],
            expected_impact="better",
            status="pending_review",
            created_at="2026-06-01T00:00:00+00:00",
        ))

    # Zero owned acts + a backlog of three → the drought is armed and the first
    # selection preempts (no remediation session; both entries stay queued).
    assert agent._owned_act_drought_active() is True
    await agent._run_task_phase()
    agent.run.assert_not_awaited()
    assert len(agent.consciousness.load_self_model().pending_pr_requests) == 2

    # Record one owned terminal act — an ``obsolete`` plan-status event — while
    # two plans remain pending (still above the min-backlog floor), so the
    # release is by the owned-act count, not by the backlog dropping.
    assert agent.memory.mark_plan_obsolete("pp0", "owned prune under drought")
    assert agent._owned_act_drought_active() is False

    # The next selection no longer preempts: the healthy remediation entry is
    # serviced exactly as the unchanged #2837 path would.
    await agent._run_task_phase()
    agent.run.assert_awaited_once()
    args, _ = agent.run.await_args
    assert args[0] == "Remediate PR #2728: fix the other thing"


def test_gate_pr_remediation_merged_ignores_non_remediation_goal(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A directive without a parseable ``Remediate PR #<n>:`` header is not a
    remediation dequeue — the merged check never fires and the gate passes."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_dequeue_merge_truth(branch_landed=True),
    )

    assert agent._gate_pr_remediation_merged("do something else", None) is False
    agent.is_pr_settled.assert_not_called()


# --- One merge-truth oracle: host API first, git fallback (proposal 080 ph.1) ---
#
# ``_host_or_git_pr_merged`` is the single seam the unlanded-remediation sensor,
# the escalation bridge, and the dequeue merged-exclusion gate now share. The
# authoritative code-host API wins; the #2523 git-log oracle is consulted only
# when the host call raises — closing the #2579 name-drop false-positive where
# an unrelated merge commit mentioning ``#2579`` made the git oracle report it
# merged and erased the PR from the bridge's own input.


def test_host_or_git_pr_merged_host_not_merged_wins_over_git_namedrop(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A host "not merged" verdict wins over a git subject name-dropping the PR;
    the git fallback is never consulted (the #2579 reproduction)."""
    agent = make_agent()
    agent.evolution.code_host.is_pr_merged.return_value = False  # type: ignore[attr-defined]
    with patch(
        "maestro.agent._git_pr_merged", return_value=True,
    ) as git_oracle:
        result = agent._host_or_git_pr_merged(2579)

    assert result is False
    # The *fail-loud* read is the one this seam wires (#2954); the conftest
    # shim routes it to the best-effort stub's verdict above.
    agent.evolution.code_host.is_pr_merged_strict.assert_called_once_with(  # type: ignore[attr-defined]
        2579,
    )
    git_oracle.assert_not_called()


def test_host_or_git_pr_merged_returns_host_merged_true(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A host "merged" verdict is returned directly; the git oracle is unused."""
    agent = make_agent()
    agent.evolution.code_host.is_pr_merged.return_value = True  # type: ignore[attr-defined]
    with patch(
        "maestro.agent._git_pr_merged", return_value=False,
    ) as git_oracle:
        result = agent._host_or_git_pr_merged(42)

    assert result is True
    agent.evolution.code_host.is_pr_merged_strict.assert_called_once_with(  # type: ignore[attr-defined]
        42,
    )
    git_oracle.assert_not_called()


def test_host_or_git_pr_merged_falls_back_to_git_only_when_host_raises(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The git oracle is consulted *only* when the host call raises, and the
    fallback threads the PR number and the agent's workspace as the ``git log``
    cwd (#2523)."""
    agent = make_agent()
    agent.evolution.code_host.is_pr_merged_strict.side_effect = (  # type: ignore[attr-defined]
        RuntimeError("gh unavailable")
    )
    with patch(
        "maestro.agent._git_pr_merged", return_value=True,
    ) as git_oracle:
        result = agent._host_or_git_pr_merged(123)

    assert result is True
    git_oracle.assert_called_once_with(123, cwd=agent.config.workspace)


def test_host_or_git_pr_merged_falls_back_when_real_gh_fails(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A *real* failing ``gh`` — not an injected raising mock — reaches the git
    fallback here too.

    The #2954 regression at this seam: the three tests above drive a
    ``MagicMock`` host and so pass against either merge read, which is why
    wiring the best-effort ``is_pr_merged`` (whose swallowed ``VCSError``
    returns a confident-looking ``False``) went unnoticed. Driving the only
    production ``CodeHost`` proves the seam reads the fail-loud variant: with
    ``is_pr_merged`` wired, this would return ``False`` and never consult git.
    """
    from maestro.vcs import SubprocessCodeHost, VCSError

    agent = make_agent()
    host = SubprocessCodeHost("owner/repo", workspace=agent.config.workspace)
    host._run = MagicMock(  # type: ignore[method-assign]
        side_effect=VCSError("gh: API rate limit exceeded"),
    )
    agent.evolution._host = host

    with patch(
        "maestro.agent._git_pr_merged", return_value=True,
    ) as git_oracle:
        result = agent._host_or_git_pr_merged(2954)

    assert result is True
    git_oracle.assert_called_once_with(2954, cwd=agent.config.workspace)


def test_unlanded_sensor_counts_pr_when_host_says_not_merged(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Acceptance (proposal 080 Phase 1): with the host API reporting #N not
    merged and a planted ``origin/main`` subject that name-drops ``(#N)``,
    ``measure_unlanded_remediations`` driven by the agent's host-first oracle
    still counts #N as unlanded — the host verdict wins and the git oracle is
    never consulted."""
    from maestro.remediation_tracker import measure_unlanded_remediations

    agent = make_agent()
    agent.evolution.code_host.is_pr_merged.return_value = False  # type: ignore[attr-defined]
    record = {
        "task_id": "rem-2579",
        "goal": "Remediate PR #2579: land the cure",
        "outcome": "partial",
    }
    history = agent.config.memory_dir / "task_history.jsonl"
    mem_fs[str(history)] = json.dumps(record) + "\n"

    with patch(
        "maestro.agent._git_pr_merged", return_value=True,
    ) as git_oracle:
        unlanded = measure_unlanded_remediations(
            agent.config.memory_dir, 50,
            is_pr_merged=agent._host_or_git_pr_merged,
        )

    assert unlanded == {2579: 1}
    git_oracle.assert_not_called()


@pytest.mark.asyncio
async def test_contemplate_measures_unlanded_sensor_once_via_host_oracle(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """contemplate() measures the per-PR unlanded-lap dict exactly once per
    iteration, fed the agent's host-first merge-truth oracle: the
    UNLANDED-REMEDIATION line and the PR-LAPS line (proposal 080 step 4) both
    derive from that single ``measure`` pass, so the host (network) merge-truth
    oracle is not re-run a second time over the same window."""
    agent = make_agent()

    with patch(
        "maestro.loop.contemplation.measure_unlanded_remediations", return_value={},
    ) as mock_measure, patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_q:
        mock_q.side_effect = [
            MockQueryResult(response="reflection"),
            MockQueryResult(response=json.dumps({
                "identity_statements": ["I am"],
                "values": ["growth"],
            })),
        ]
        await agent.contemplate()

    mock_measure.assert_called_once()
    assert (
        mock_measure.call_args.kwargs["is_pr_merged"]
        == agent._host_or_git_pr_merged
    )


def test_module_git_pr_merged_forwards_cwd_to_default() -> None:
    """The module-level ``maestro.agent._git_pr_merged`` seam is a thin wrapper
    over :func:`audit_checks_phantom._git_pr_merged_default`, forwarding both the PR
    number and the ``cwd`` so the dequeue-face exclusion's git-merge-truth query
    targets the agent's repo regardless of process cwd (#2523)."""
    import maestro.agent as agent_module

    with patch(
        "maestro.agent._git_pr_merged_default", return_value=True,
    ) as mock_default:
        result = agent_module._git_pr_merged(123, cwd="/work/repo")

    assert result is True
    mock_default.assert_called_once_with(123, cwd="/work/repo")


# --- Remediation closure stamp must gate on git-merge-truth (_git_pr_merged) ---


@pytest.mark.asyncio
async def test_run_remediation_partial_when_git_pr_merged_false(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A resolved-on-branch ``Remediate PR #N`` task must be stamped ``partial``,
    not ``success``, when git-merge-truth says the PR never reached ``main``.

    The make_agent defaults produce a clean host state (``is_pr_merged=False``,
    ``get_pr_check_conclusion='success'``, even commits, a resolvable
    ``origin/<branch>`` ref), so ``_probe_pr_merge_block`` returns
    ``landed_ok=True``. The authoritative git-merge-truth oracle
    ``_git_pr_merged`` returns ``False`` (the PR's SHA is absent from
    ``origin/main``), so the closure gate must force ``partial`` — the
    resolution exists on the branch but never landed.
    """
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture
    # Resolved-on-branch: the remediation commits a real fix, so run() keeps the
    # branch and the success path reaches the closure stamp gate.
    agent._vcs.has_changes.return_value = True

    # The authoritative oracle: git-merge-truth reports PR #91 never reached
    # main, so the closure gate must downgrade the stamp to ``partial``.
    agent._git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args: object, **kwargs: object) -> MockQueryResult:
        return MockQueryResult(response="Resolved the failure on the branch.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #91: fix CI", source="external",
            pr_branch="maestro/evolve/ep91",
        )

    # Honest stamp: the PR never landed, so the remediation is partial.
    assert record.outcome == "partial"
    # The closure gate must consult git-merge-truth for the correct PR exactly
    # once before keeping a stamp.
    agent._git_pr_merged.assert_called_once_with(91)


def _landed_settlement(pr_number: int) -> MergeTruth:
    """A settled/landed :class:`MergeTruth` for the merge-truth chokepoint.

    Arms ``is_pr_settled`` so the §5d mechanical-outcome authority (proposal
    105, now default-on) reads ``branch_landed=True`` and keeps a
    genuinely-merged remediation's verdict ``success`` — the tests below stage
    a merged PR only via the host/``_git_pr_merged`` seams, so the chokepoint
    must agree.
    """
    return MergeTruth(
        pr_number=pr_number,
        settled=True,
        state="MERGED",
        branch_landed=True,
        source="host_api",
        observed_at=0.0,
    )


@pytest.mark.asyncio
async def test_run_remediation_keeps_success_when_git_pr_merged_true(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When the host reports no explicit merge but git-merge-truth confirms the
    PR's SHA reached ``main`` (``_git_pr_merged`` returns ``True``), a
    resolved-on-branch remediation keeps its ``success`` stamp and the
    host-verdict attribute stays ``None`` (#2457)."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture
    agent._vcs.has_changes.return_value = True
    # Host does not report MERGED (make_agent default), but git confirms the
    # merge — so the success stamp survives.
    agent._git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=True,
    )
    # Merge-truth chokepoint agrees the branch landed so the default-on §5d
    # mechanical-outcome authority keeps the verdict ``success`` (proposal 105).
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_landed_settlement(94),
    )

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args: object, **kwargs: object) -> MockQueryResult:
        return MockQueryResult(response="Resolved the failure on the branch.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #94: fix CI", source="external",
            pr_branch="maestro/evolve/ep94",
        )

    assert record.outcome == "success"
    assert agent._last_remediation_merge_block is None
    agent._git_pr_merged.assert_called_once_with(94)


@pytest.mark.asyncio
async def test_run_remediation_host_merged_keeps_success_without_git(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When the code host reports the PR already MERGED, the closure gate keeps
    ``success`` and never consults git-merge-truth — the host's explicit MERGED
    state is authoritative, so the ``not is_pr_merged`` guard short-circuits
    before ``_git_pr_merged`` runs (#2457)."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture
    agent._vcs.has_changes.return_value = True
    # Host reports the PR merged → probe early-returns landed_ok=True and the
    # gate's host guard short-circuits.
    agent.evolution.code_host.is_pr_merged.return_value = True  # type: ignore[attr-defined]
    # Spy: the git oracle must NOT run when the host already confirms the merge.
    agent._git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )
    # Merge-truth chokepoint agrees the branch landed so the default-on §5d
    # mechanical-outcome authority keeps the verdict ``success`` (proposal 105).
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_landed_settlement(95),
    )

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args: object, **kwargs: object) -> MockQueryResult:
        return MockQueryResult(response="Resolved the failure on the branch.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #95: fix CI", source="external",
            pr_branch="maestro/evolve/ep95",
        )

    assert record.outcome == "success"
    assert agent._last_remediation_merge_block is None
    agent._git_pr_merged.assert_not_called()


# --- Comment-completeness gate (step 4d): one pass must address every item ---


def _bug_thread_node(comment_id: int, path: str = "a.py") -> dict[str, object]:
    """One raw GraphQL review-thread node for an unresolved 🐛 bug finding."""
    return {
        "id": f"PRRT_{comment_id}",
        "isResolved": False,
        "isOutdated": False,
        "comments": {
            "nodes": [
                {
                    "databaseId": comment_id,
                    "path": path,
                    "body": "🐛 **Off-by-one in loop**\n\nDetail.",
                },
            ],
        },
    }


@pytest.mark.asyncio
async def test_run_remediation_partial_when_review_threads_remain(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A remediation that would otherwise stamp ``success`` (git-merge-truth
    confirms the SHA landed) but left an eligible review thread un-replied is
    downgraded to ``partial`` by the completeness gate, with a count-bearing
    note and the remaining count recorded for the repetition gate to reuse."""
    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent._vcs.has_changes.return_value = True
    # 4c keeps ``success``: git confirms the merge landed.
    agent._git_pr_merged = MagicMock(return_value=True)  # type: ignore[method-assign]
    # One eligible bug thread still open → 4d must downgrade to ``partial``.
    agent.evolution.code_host.get_pr_review_threads = MagicMock(  # type: ignore[attr-defined]
        return_value=[_bug_thread_node(100)],
    )

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args: object, **kwargs: object) -> MockQueryResult:
        return MockQueryResult(response="Addressed one of the threads.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #91: address comments", source="external",
            pr_branch="maestro/evolve/ep91",
        )

    assert record.outcome == "partial"
    assert agent._last_remediation_items_remaining == 1
    assert "still lack a terminal reply marker" in record.result_summary


# ──────────────────────────────────────────────────────────────────────
# §5-push total delivery-truth reconciliation (proposal 098)
# ──────────────────────────────────────────────────────────────────────


async def _run_remediation_lap(agent: object, pr: int = 97) -> object:
    """Drive ``run()`` through §5-push on a downgraded remediation lap.

    The shared four-patch SDK quartet the sibling reconciliation tests inline.
    Callers arm the delivery-truth arm they want (``push.side_effect``, a
    broken checkout, ``commits_not_on_remote``) before calling.
    """
    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args: object, **kwargs: object) -> MockQueryResult:
        return MockQueryResult(response="Resolved on branch.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        return await agent.run(  # type: ignore[attr-defined]
            f"Remediate PR #{pr}: address comments", source="external",
            pr_branch=f"maestro/evolve/ep{pr}",
        )


def _arm_downgraded_remediation(agent: object) -> None:
    """Make §4c stamp the awaiting-tend-push note §5-push then reconciles."""
    agent.config.enable_session_reuse = False  # type: ignore[attr-defined]
    agent._vcs.has_changes.return_value = True  # type: ignore[attr-defined]
    agent._git_pr_merged = MagicMock(return_value=False)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_remediation_failed_push_records_owned_marker_when_enabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag on, a failed push records the OWNED marker, not the
    external-deferral costume (proposal 098).

    Pushing the resolution is *my* act — ``classify_blocker_ownership``
    classifies AWAITING_PUSH owned — so keeping ``awaiting tend push`` here
    would have the loop write, in its own hand, the deferral verdict its own
    remediation preamble forbids. The PUSH FAILED suffix still rides along so
    the error reaches the framed summary.
    """
    from maestro.vcs import VCSError

    agent = make_agent()
    _arm_downgraded_remediation(agent)
    agent._vcs.push.side_effect = VCSError("remote rejected")

    record = await _run_remediation_lap(agent)

    assert record.outcome == "partial"
    assert record.push_delivered is False
    assert Marker.OWNED_PUSH_AVAILABLE in record.result_summary
    assert Marker.AWAITING_TEND_PUSH not in record.result_summary
    assert "PUSH FAILED" in record.result_summary
    assert "remote rejected" in record.result_summary


@pytest.mark.asyncio
async def test_run_remediation_failed_push_routes_fire_or_close_directive(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The owned-push arm routes the next task at the act it owns."""
    from maestro.vcs import VCSError

    agent = make_agent()
    _arm_downgraded_remediation(agent)
    agent._vcs.push.side_effect = VCSError("remote rejected")

    await _run_remediation_lap(agent)

    directive = agent.consciousness.load_self_model().next_task_directive
    assert "#97" in directive
    assert "gh pr close" in directive


@pytest.mark.asyncio
async def test_run_remediation_owned_push_defers_to_occupied_directive_slot(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A directive a higher-precedence bridge already wrote is never clobbered.

    Mirrors ``_refuse_owned_push_standdown``'s empty-slot deference: the
    fire-or-close directive is an offer into an *empty* slot, not a seizure.
    """
    from maestro.vcs import VCSError

    agent = make_agent()
    _arm_downgraded_remediation(agent)
    agent._vcs.push.side_effect = VCSError("remote rejected")
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "Pre-set by a higher-precedence bridge"
    agent.consciousness.save_self_model(model)

    await _run_remediation_lap(agent)

    directive = agent.consciousness.load_self_model().next_task_directive
    assert directive == "Pre-set by a higher-precedence bridge"


@pytest.mark.asyncio
async def test_run_remediation_never_attempted_but_ahead_records_owned_marker(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A never-attempted push on an OWNED_AHEAD branch is an owned act.

    The checkout fell back so no push ran, but git proves a commit no origin
    ref carries — the deferral costume would be a false quiet. Grounded in the
    fetch-first ``_branch_ahead_of_origin_count`` probe, not assumed.
    """
    from maestro.vcs import VCSError

    agent = make_agent()
    _arm_downgraded_remediation(agent)
    agent._vcs.checkout.side_effect = VCSError("not local")
    agent._vcs.checkout_new_branch.side_effect = VCSError("no git")
    agent._vcs.commits_not_on_remote.return_value = 2
    agent._fetch_origin_for_merge_truth = MagicMock()

    record = await _run_remediation_lap(agent)

    assert record.push_delivered is None
    agent._vcs.push.assert_not_called()
    assert Marker.OWNED_PUSH_AVAILABLE in record.result_summary
    assert Marker.AWAITING_TEND_PUSH not in record.result_summary
    # Never attempted, so no push error to append.
    assert "PUSH FAILED" not in record.result_summary


@pytest.mark.asyncio
async def test_run_remediation_never_attempted_not_ahead_keeps_legacy_marker(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Nothing local to push → deferring to Tend is honest, so the legacy
    marker stays. The 098 swap is narrow by design."""
    from maestro.vcs import VCSError

    agent = make_agent()
    _arm_downgraded_remediation(agent)
    agent._vcs.checkout.side_effect = VCSError("not local")
    agent._vcs.checkout_new_branch.side_effect = VCSError("no git")
    agent._vcs.commits_not_on_remote.return_value = 0
    agent._fetch_origin_for_merge_truth = MagicMock()

    record = await _run_remediation_lap(agent)

    assert record.push_delivered is None
    assert Marker.AWAITING_TEND_PUSH in record.result_summary
    assert Marker.OWNED_PUSH_AVAILABLE not in record.result_summary
    # An honest external deferral routes no fire-or-close directive.
    assert not agent.consciousness.load_self_model().next_task_directive


@pytest.mark.asyncio
async def test_run_remediation_delivered_push_still_records_delivered_marker(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The success arm is unchanged by the resolver: a delivered push still
    stamps the delivered marker and routes no fire-or-close directive — while a
    §4d completeness detail composed on the same lap survives the marker swap
    (augment, not clobber)."""
    agent = make_agent()
    _arm_downgraded_remediation(agent)
    # An eligible thread stays open, so §4d augments the downgraded note; the
    # delivered-push swap must rewrite the marker without clobbering that detail.
    agent.evolution.code_host.get_pr_review_threads = MagicMock(  # type: ignore[attr-defined]
        return_value=[_bug_thread_node(100)],
    )

    record = await _run_remediation_lap(agent)

    assert record.outcome == "partial"
    assert record.push_delivered is True
    assert Marker.DELIVERED_AWAITING_MERGE in record.result_summary
    assert Marker.AWAITING_TEND_PUSH not in record.result_summary
    assert Marker.OWNED_PUSH_AVAILABLE not in record.result_summary
    # The §4d completeness detail survives the delivered-marker swap.
    assert "still lack a terminal reply marker" in record.result_summary
    assert not agent.consciousness.load_self_model().next_task_directive


@pytest.mark.asyncio
async def test_run_remediation_owned_push_appends_marker_to_completeness_note(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A note §4c never wrote still records the owned marker (§4d-only lap).

    §4c is not this note's only author. When git-merge-truth confirms the SHA
    landed, §4c stays silent and §4d's completeness gate composes the whole
    note itself — a note carrying no ``AWAITING_TEND_PUSH`` text, so the marker
    *swap* matches nothing. Without the append the fire-or-close directive would
    fire and the warning log loudly on a record no owned-push sensor could see.
    """
    from maestro.vcs import VCSError

    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent._vcs.has_changes.return_value = True
    # 4c stays silent: git confirms the SHA landed, so it writes no note.
    agent._git_pr_merged = MagicMock(return_value=True)  # type: ignore[method-assign]
    # 4d authors the entire note instead, downgrading to ``partial``.
    agent.evolution.code_host.get_pr_review_threads = MagicMock(  # type: ignore[attr-defined]
        return_value=[_bug_thread_node(100)],
    )
    agent._vcs.push.side_effect = VCSError("remote rejected")

    record = await _run_remediation_lap(agent, pr=99)

    assert record.outcome == "partial"
    assert record.push_delivered is False
    # The §4d completeness detail survives, and the owned marker joins it.
    assert "still lack a terminal reply marker" in record.result_summary
    assert Marker.OWNED_PUSH_AVAILABLE in record.result_summary
    assert "PUSH FAILED" in record.result_summary
    directive = agent.consciousness.load_self_model().next_task_directive
    assert "#99" in directive
    assert "gh pr close" in directive


# ──────────────────────────────────────────────────────────────────────
# §5d partial delivery provenance — [delivering] vs [null-lap] (proposal 116)
# ──────────────────────────────────────────────────────────────────────


def _unsettled_pr(pr: int) -> MergeTruth:
    """``is_pr_settled`` verdict for a PR git shows unlanded (→ ``partial``)."""
    return MergeTruth(
        pr_number=pr, settled=False, state="OPEN", branch_landed=False,
        source="host_api", observed_at=0.0,
    )


def _settled_pr(pr: int) -> MergeTruth:
    """``is_pr_settled`` verdict for a PR git shows landed (→ ``success``)."""
    return MergeTruth(
        pr_number=pr, settled=True, state="MERGED", branch_landed=True,
        source="host_api", observed_at=0.0,
    )


@pytest.mark.asyncio
async def test_run_remediation_delivered_partial_appends_delivering_note(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """§5d annotates a delivered-but-gated ``partial`` with ``[delivering]``: the
    lap delivered its owed head (the summary carries the delivered marker) and
    the PR has not landed (proposal 116)."""
    agent = make_agent()
    _arm_downgraded_remediation(agent)
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_unsettled_pr(97),
    )
    # A thread stays open so §4d augments the note on the delivered path, giving
    # the lap a delivered marker in its summary (the sibling delivered test).
    agent.evolution.code_host.get_pr_review_threads = MagicMock(  # type: ignore[attr-defined]
        return_value=[_bug_thread_node(100)],
    )

    record = await _run_remediation_lap(agent)

    assert record.outcome == "partial"
    assert record.push_delivered is True
    assert Marker.DELIVERED_AWAITING_MERGE in record.result_summary
    assert record.key_findings is not None
    assert "[delivering]" in record.key_findings
    assert "[null-lap]" not in record.key_findings


@pytest.mark.asyncio
async def test_run_remediation_null_partial_appends_null_lap_note(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """§5d annotates a make-work ``partial`` with ``[null-lap]``: the lap
    advanced no owed head (a failed push → owned-push-available, not the
    delivered marker) and the PR has not landed (proposal 116)."""
    from maestro.vcs import VCSError

    agent = make_agent()
    _arm_downgraded_remediation(agent)
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_unsettled_pr(97),
    )
    agent._vcs.push.side_effect = VCSError("remote rejected")

    record = await _run_remediation_lap(agent)

    assert record.outcome == "partial"
    assert record.push_delivered is False
    assert Marker.OWNED_PUSH_AVAILABLE in record.result_summary
    assert record.key_findings is not None
    assert "[null-lap]" in record.key_findings
    assert "[delivering]" not in record.key_findings


@pytest.mark.asyncio
async def test_run_remediation_success_appends_no_delivery_note(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A ``success`` landing verdict (PR settled/landed) carries no delivery
    provenance — the note is only for a ``partial`` (proposal 116)."""
    agent = make_agent()
    _arm_downgraded_remediation(agent)
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_settled_pr(97),
    )

    record = await _run_remediation_lap(agent)

    assert record.outcome == "success"
    assert "[delivering]" not in (record.key_findings or "")
    assert "[null-lap]" not in (record.key_findings or "")


@pytest.mark.asyncio
async def test_run_non_remediation_task_appends_no_delivery_note(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A non-remediation task never reaches §5d's landing authority
    (``remediation_pr_num`` is ``None``), so no delivery note is appended
    (proposal 116)."""
    plan_response = json.dumps([{"description": "Step 1"}])

    agent = make_agent()
    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        mock_distill.return_value = MockQueryResult(response="A clean finding")

        record = await agent.run("Build a new capability")

    assert record.outcome == "success"
    assert record.key_findings == "A clean finding"
    assert "[delivering]" not in (record.key_findings or "")
    assert "[null-lap]" not in (record.key_findings or "")


@pytest.mark.asyncio
async def test_run_remediation_bare_delivery_note_when_distillation_degrades(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When distillation degrades to ``None`` (the §5b SDKError catch) on a
    ``partial`` remediation lap that earned no remediation note (mixed step
    outcomes, so §4c's success-only stamp gate never fires and §4d has no open
    threads), the §5d delivery note becomes the record's *entire*
    ``key_findings`` — the ``else delivery_note`` arm (proposal 116)."""
    from maestro.outcome_authority import _NULL_LAP_NOTE

    agent = make_agent()
    agent.config.loop_max_replans = 0
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_unsettled_pr(97),
    )

    plan_response = json.dumps(
        [{"description": "Step OK"}, {"description": "Step fail"}],
    )

    exec_calls = 0

    async def alternating_exec(**kwargs: object) -> MockQueryResult:
        nonlocal exec_calls
        exec_calls += 1
        if exec_calls == 1:
            return MockQueryResult(response="Done")
        raise RuntimeError("Step 2 failed")

    async def selective_query(
        *args: object, **kwargs: object,
    ) -> MockQueryResult:
        options = kwargs.get("options")
        if getattr(options, "caller", None) == "distill_findings":
            raise SDKError(
                "distillation unavailable", category=ErrorCategory.UNKNOWN,
            )
        return MockQueryResult(response="Resolved on branch.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", side_effect=alternating_exec), \
         patch("maestro.agent.query", side_effect=selective_query), \
         patch("maestro.agent.query_on_session", side_effect=selective_query):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        record = await agent.run(
            "Remediate PR #97: address comments", source="external",
            pr_branch="maestro/evolve/ep97",
        )

    assert record.outcome == "partial"
    assert record.key_findings == _NULL_LAP_NOTE


@pytest.mark.asyncio
async def test_run_remediation_owned_push_appends_marker_only_once(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The append is guarded on absence, so a §4c-stamped note is not doubled.

    The swap already put the owned marker in this note; appending a second copy
    would double-count it for every sensor matching on substring presence.
    """
    from maestro.vcs import VCSError

    agent = make_agent()
    _arm_downgraded_remediation(agent)
    agent._vcs.push.side_effect = VCSError("remote rejected")

    record = await _run_remediation_lap(agent)

    assert record.result_summary.count(Marker.OWNED_PUSH_AVAILABLE) == 1


@pytest.mark.asyncio
async def test_run_remediation_records_zero_threads_remaining(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When the single pass addressed every routed item (no eligible threads
    remain) the completeness gate records a zero remaining count, so
    ``_record_remediation_outcome`` reuses it without re-fetching. The PR is
    still OPEN, so the git-grounded authority records the landing verdict
    ``partial`` (proposal 105)."""
    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent._vcs.has_changes.return_value = True
    agent._git_pr_merged = MagicMock(return_value=True)  # type: ignore[method-assign]
    # This test isolates the §4d completeness gate, which runs only for an
    # OPEN PR (``is_pr_settled(...).settled`` False, ``branch_landed`` False —
    # the conftest default); a landed chokepoint would skip the gate and leave
    # the count unset. On that open PR the §5d authority records the landing
    # verdict ``partial`` (covered in test_agent_remediation, proposal 105).
    # Default ``get_pr_review_threads`` returns ``[]`` (conftest) → nothing left.

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args: object, **kwargs: object) -> MockQueryResult:
        return MockQueryResult(response="Addressed every thread on the PR.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #92: address comments", source="external",
            pr_branch="maestro/evolve/ep92",
        )

    assert record.outcome == "partial"
    assert agent._last_remediation_items_remaining == 0


@pytest.mark.asyncio
async def test_run_remediation_completeness_gate_skipped_for_merged_pr(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The completeness gate never probes threads on a merged PR — a stand-down
    there is legitimate — so the remaining count stays ``None`` and the thread
    fetch is not called."""
    agent = make_agent()
    agent.config.enable_session_reuse = False
    # Pin the salvage master flag off (default flipped on in proposal 094): this
    # test asserts the *completeness gate* never probes threads on a merged PR;
    # the separately-activated salvage observation would otherwise fetch once.
    agent.config.salvage_before_disengage_enabled = False
    agent._vcs.has_changes.return_value = True
    # Host reports the PR merged → 4c keeps success; the ``is_pr_settled``
    # chokepoint reports it settled so 4d's open-PR guard skips (proposal 087).
    agent.evolution.code_host.is_pr_merged.return_value = True  # type: ignore[attr-defined]
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=93, settled=True, state="MERGED",
            branch_landed=True, source="host_api", observed_at=0.0,
        ),
    )
    threads_spy = MagicMock(return_value=[_bug_thread_node(100)])
    agent.evolution.code_host.get_pr_review_threads = threads_spy  # type: ignore[attr-defined]

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args: object, **kwargs: object) -> MockQueryResult:
        return MockQueryResult(response="Resolved on the branch.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #93: address comments", source="external",
            pr_branch="maestro/evolve/ep93",
        )

    assert record.outcome == "success"
    assert agent._last_remediation_items_remaining is None
    threads_spy.assert_not_called()


# --- Trusted loop-side review-thread reply delivery (§4c-ii, #2817) ---


def _bug_thread_node_with_reply(
    comment_id: int, reply_body: str, path: str = "a.py",
) -> dict[str, object]:
    """A 🐛 bug thread node carrying a terminal-marker reply (as if delivered)."""
    return {
        "id": f"PRRT_{comment_id}",
        "isResolved": False,
        "isOutdated": False,
        "comments": {
            "nodes": [
                {
                    "databaseId": comment_id,
                    "path": path,
                    "body": "🐛 **Off-by-one in loop**\n\nDetail.",
                },
                {
                    "databaseId": comment_id + 1,
                    "path": path,
                    "body": reply_body,
                },
            ],
        },
    }


def _bug_thread_node_unanswered(
    comment_id: int, path: str = "a.py",
) -> dict[str, object]:
    """A 🐛 bug thread with no terminal-marker reply yet — still eligible."""
    return {
        "id": f"PRRT_{comment_id}",
        "isResolved": False,
        "isOutdated": False,
        "comments": {
            "nodes": [
                {
                    "databaseId": comment_id,
                    "path": path,
                    "body": "🐛 **Off-by-one in loop**\n\nDetail.",
                },
            ],
        },
    }


@pytest.mark.asyncio
async def test_run_remediation_delivers_recorded_thread_replies(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A remediation lap on a ``pr_branch`` delivers each terminal reply its
    ``thread-replies.jsonl`` manifest records via a trusted
    ``reply_to_review_comment`` host call — the same shape as the §5-push commit
    delivery, which the Bash action-gate never sees (#2817)."""
    from unittest.mock import call

    from maestro.loop.pr_remediation import _REPLY_MANIFEST_FILENAME

    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent._vcs.has_changes.return_value = True
    agent._git_pr_merged = MagicMock(return_value=True)  # type: ignore[method-assign]
    agent.evolution.code_host.reply_to_review_comment = MagicMock()  # type: ignore[attr-defined]

    # Pin the task id so the manifest can be planted at the artifacts path run()
    # computes (``artifacts_dir / task_id``). uuid4().hex[:12] -> "deadbeefcafe".
    fixed_uuid = MagicMock()
    fixed_uuid.hex = "deadbeefcafe" + "0" * 20
    task_id = "deadbeefcafe"
    manifest = agent.config.artifacts_dir / task_id / _REPLY_MANIFEST_FILENAME
    mem_fs[str(manifest)] = (
        json.dumps({
            "comment_id": 100,
            "body": "Fixed locally with implementation-plan context. — patched",
        }) + "\n"
        + json.dumps({"comment_id": 200, "body": "Tracked in #5"}) + "\n"
    )

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args: object, **kwargs: object) -> MockQueryResult:
        return MockQueryResult(response="Addressed every thread on the PR.")

    with patch("maestro.agent.uuid.uuid4", return_value=fixed_uuid), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #91: address comments", source="external",
            pr_branch="maestro/evolve/ep91",
        )

    assert record.task_id == task_id
    assert agent.evolution.code_host.reply_to_review_comment.call_args_list == [
        call(91, 100, "Fixed locally with implementation-plan context. — patched"),
        call(91, 200, "Tracked in #5"),
    ]


@pytest.mark.asyncio
async def test_run_remediation_reply_delivery_host_error_does_not_crash_lap(
    make_agent, caplog,  # type: ignore[no-untyped-def]
) -> None:
    """A host error while delivering a recorded reply is best-effort: the lap
    logs a warning and completes unchanged, leaving re-delivery to the next
    remediation lap (the outcome is not affected by the delivery failure)."""
    from maestro.vcs import VCSError

    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent._vcs.has_changes.return_value = True
    agent._git_pr_merged = MagicMock(return_value=True)  # type: ignore[method-assign]
    # Merge-truth chokepoint agrees the branch landed so the default-on §5d
    # mechanical-outcome authority keeps the verdict ``success`` (proposal 105).
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_landed_settlement(91),
    )
    agent.evolution.code_host.reply_to_review_comment = MagicMock(  # type: ignore[attr-defined]
        side_effect=VCSError("host down"),
    )

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args: object, **kwargs: object) -> MockQueryResult:
        return MockQueryResult(response="Addressed every thread on the PR.")

    with caplog.at_level(logging.WARNING, logger="maestro"), \
         patch(
             "maestro.loop.remediation_stamps.read_thread_reply_manifest",
             return_value=[(100, "Fixed locally with implementation-plan context.")],
         ), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #91: address comments", source="external",
            pr_branch="maestro/evolve/ep91",
        )

    # The delivery was attempted and raised, but the lap completed normally.
    assert record.outcome == "success"
    agent.evolution.code_host.reply_to_review_comment.assert_called_once_with(
        91, 100, "Fixed locally with implementation-plan context.",
    )
    assert any(
        "Failed to deliver review-thread reply" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_run_remediation_stamps_only_delivered_ids_serviced(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """§4c-ii marks a thread serviced only after its reply is confirmed
    delivered (proposal 091): a manifest with two delivering and one raising
    ``reply_to_review_comment`` stamps exactly the two delivered ids under the
    PR's standdown — the raising id stays unserviced for the next tend lap so a
    failed delivery is retried, not buried (the #2884 shape)."""
    from maestro.loop.pr_remediation import _REPLY_MANIFEST_FILENAME
    from maestro.vcs import VCSError

    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent._vcs.has_changes.return_value = True
    agent._git_pr_merged = MagicMock(return_value=True)  # type: ignore[method-assign]

    def _reply(_pr: int, comment_id: int, _body: str) -> None:
        if comment_id == 300:
            raise VCSError("host down")

    agent.evolution.code_host.reply_to_review_comment = MagicMock(  # type: ignore[attr-defined]
        side_effect=_reply,
    )

    # A live standdown for PR 91 so ``mark_comment_ids_serviced`` records rather
    # than no-ops; its serviced set starts empty.
    ledger_path = str(agent.config.memory_dir / "escalation_handoffs.json")
    mem_fs[ledger_path] = json.dumps({
        "91": {
            "head_sha": "h",
            "delivered_at": "2026-01-01T00:00:00+00:00",
            "serviced_comment_ids": [],
        },
    })

    fixed_uuid = MagicMock()
    fixed_uuid.hex = "deadbeefcafe" + "0" * 20
    task_id = "deadbeefcafe"
    manifest = agent.config.artifacts_dir / task_id / _REPLY_MANIFEST_FILENAME
    mem_fs[str(manifest)] = "".join(
        json.dumps({"comment_id": cid, "body": f"reply {cid}"}) + "\n"
        for cid in (100, 200, 300)
    )

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args: object, **kwargs: object) -> MockQueryResult:
        return MockQueryResult(response="Addressed every thread on the PR.")

    with patch("maestro.agent.uuid.uuid4", return_value=fixed_uuid), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        await agent.run(
            "Remediate PR #91: address comments", source="external",
            pr_branch="maestro/evolve/ep91",
        )

    # Exactly the two delivered ids are serviced; the raising id (300) is not,
    # so it re-enters the unserviced set and earns a reply-completion lap next
    # tend pass.
    serviced = json.loads(mem_fs[ledger_path])["91"]["serviced_comment_ids"]
    assert serviced == [100, 200]


@pytest.mark.asyncio
async def test_run_remediation_no_standdown_stamps_nothing(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With no standdown for the PR, the §4c-ii serviced stamp is a no-op: the
    recorded reply still posts, but no standdown ledger is written (proposal
    091 — ``mark_comment_ids_serviced`` no-ops without a standdown)."""
    from maestro.loop.pr_remediation import _REPLY_MANIFEST_FILENAME

    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent._vcs.has_changes.return_value = True
    agent._git_pr_merged = MagicMock(return_value=True)  # type: ignore[method-assign]
    agent.evolution.code_host.reply_to_review_comment = MagicMock()  # type: ignore[attr-defined]

    fixed_uuid = MagicMock()
    fixed_uuid.hex = "deadbeefcafe" + "0" * 20
    task_id = "deadbeefcafe"
    manifest = agent.config.artifacts_dir / task_id / _REPLY_MANIFEST_FILENAME
    mem_fs[str(manifest)] = (
        json.dumps({"comment_id": 100, "body": "reply 100"}) + "\n"
    )

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args: object, **kwargs: object) -> MockQueryResult:
        return MockQueryResult(response="Addressed every thread on the PR.")

    with patch("maestro.agent.uuid.uuid4", return_value=fixed_uuid), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        await agent.run(
            "Remediate PR #91: address comments", source="external",
            pr_branch="maestro/evolve/ep91",
        )

    # The reply was delivered, but with no standdown nothing is stamped: the
    # ledger stays absent/empty.
    agent.evolution.code_host.reply_to_review_comment.assert_called_once_with(
        91, 100, "reply 100",
    )
    ledger_path = str(agent.config.memory_dir / "escalation_handoffs.json")
    raw = mem_fs.get(ledger_path, "")
    assert (json.loads(raw) if raw else {}) == {}


@pytest.mark.asyncio
async def test_run_remediation_completeness_clears_after_reply_delivery(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the reply delivered BEFORE the §4d completeness gate re-reads the
    PR's threads (the before-gate ordering, #2817), a lap that answered every
    thread sees its own terminal marker and records zero items remaining — no
    perpetual "still lack a terminal reply marker" count. The PR is still OPEN,
    so the git-grounded authority records the landing verdict ``partial``
    (proposal 105)."""
    marker = "Fixed locally with implementation-plan context. — done"

    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent._vcs.has_changes.return_value = True
    agent._git_pr_merged = MagicMock(return_value=True)  # type: ignore[method-assign]
    # This test isolates the §4d completeness gate, which runs only for an
    # OPEN PR (``is_pr_settled(...).settled`` False, ``branch_landed`` False —
    # the conftest default); a landed chokepoint would skip the gate and
    # short-circuit the clear-after-delivery behaviour under test. On that open
    # PR the §5d authority records the landing verdict ``partial`` (covered in
    # test_agent_remediation, proposal 105).
    # The host reflects delivery: ``get_pr_review_threads`` returns the thread
    # WITHOUT a terminal marker (still eligible) until the reply is actually
    # delivered, then the marker-bearing thread. This pins the ordering the
    # #2817 fix depends on — §4c-ii delivers BEFORE §4d re-reads. Were delivery
    # moved after the gate (or dropped), §4d would re-read the still-eligible
    # thread and force ``partial`` with ``items_remaining == 1``, failing the
    # assertions below.
    delivered: set[object] = set()

    def _record_reply(*args: object) -> None:
        delivered.add(args[1])

    def _threads(*args: object) -> list[dict[str, object]]:
        if 100 in delivered:
            return [_bug_thread_node_with_reply(100, marker)]
        return [_bug_thread_node_unanswered(100)]

    agent.evolution.code_host.reply_to_review_comment = MagicMock(  # type: ignore[attr-defined]
        side_effect=_record_reply,
    )
    agent.evolution.code_host.get_pr_review_threads = MagicMock(  # type: ignore[attr-defined]
        side_effect=_threads,
    )

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args: object, **kwargs: object) -> MockQueryResult:
        return MockQueryResult(response="Addressed every thread on the PR.")

    with patch(
             "maestro.loop.remediation_stamps.read_thread_reply_manifest",
             return_value=[(100, marker)],
         ), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #91: address comments", source="external",
            pr_branch="maestro/evolve/ep91",
        )

    assert record.outcome == "partial"
    assert agent._last_remediation_items_remaining == 0
    agent.evolution.code_host.reply_to_review_comment.assert_called_once_with(
        91, 100, marker,
    )


_RESOLVE_MARKER = "Fixed locally with implementation-plan context. — patched"


def _resolution_agent(make_agent):  # type: ignore[no-untyped-def]
    """A remediation-lap agent primed for the §5b thread-resolution block.

    Reply delivery, thread re-fetch, and resolution are all mocked recorders;
    ``_git_pr_merged`` True keeps the lap ``success`` and skips the §4d gate.
    """
    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent._vcs.has_changes.return_value = True
    agent._git_pr_merged = MagicMock(return_value=True)  # type: ignore[method-assign]
    # The lap runs on PR #91; arm the merge-truth chokepoint landed so the
    # §5d mechanical-outcome authority keeps ``success`` (proposal 105) — these
    # tests stage the merge only via ``_git_pr_merged``.
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_landed_settlement(91),
    )
    agent.evolution.code_host.reply_to_review_comment = MagicMock()  # type: ignore[attr-defined]
    agent.evolution.code_host.resolve_review_thread = MagicMock()  # type: ignore[attr-defined]
    agent.evolution.code_host.get_pr_review_threads = MagicMock(  # type: ignore[attr-defined]
        return_value=[_bug_thread_node_with_reply(100, _RESOLVE_MARKER)],
    )
    return agent


async def _run_resolution_lap(agent, manifest=None):  # type: ignore[no-untyped-def]
    """Drive one remediation lap on PR #91 with a stubbed reply manifest."""
    reply_manifest = manifest if manifest is not None else [(100, _RESOLVE_MARKER)]
    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args: object, **kwargs: object) -> MockQueryResult:
        return MockQueryResult(response="Addressed every thread on the PR.")

    with patch(
             "maestro.loop.remediation_stamps.read_thread_reply_manifest",
             return_value=reply_manifest,
         ), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        return await agent.run(
            "Remediate PR #91: address comments", source="external",
            pr_branch="maestro/evolve/ep91",
        )


@pytest.mark.asyncio
async def test_run_remediation_resolves_addressed_thread_after_push(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """After a lap confirms both deliveries (reply posted + push landed), the
    addressed AI-finding thread is resolved on GitHub (proposal 091 step 5)."""
    agent = _resolution_agent(make_agent)

    record = await _run_resolution_lap(agent)

    assert record.outcome == "success"
    assert record.push_delivered is True
    agent.evolution.code_host.resolve_review_thread.assert_called_once_with(
        "PRRT_100",
    )


@pytest.mark.asyncio
async def test_run_remediation_no_resolve_when_flag_off(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With ``resolve_addressed_threads_enabled`` off, no thread is resolved
    even after a confirmed delivery — the rollback flag gates the whole block."""
    agent = _resolution_agent(make_agent)
    agent.config.resolve_addressed_threads_enabled = False

    record = await _run_resolution_lap(agent)

    assert record.outcome == "success"
    agent.evolution.code_host.resolve_review_thread.assert_not_called()


@pytest.mark.asyncio
async def test_run_remediation_no_resolve_when_push_failed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A failed push (``push_delivered`` False) blocks resolution: the fix never
    reached ``origin``, so the thread stays open for the next lap."""
    from maestro.vcs import VCSError

    agent = _resolution_agent(make_agent)
    agent._vcs.push.side_effect = VCSError("remote rejected")

    record = await _run_resolution_lap(agent)

    assert record.push_delivered is False
    agent.evolution.code_host.resolve_review_thread.assert_not_called()


@pytest.mark.asyncio
async def test_run_remediation_no_resolve_when_reply_not_delivered(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When the reply POST raised, the id is not in ``delivered_ids``, so the
    empty-``delivered_ids`` guard short-circuits and nothing is resolved."""
    from maestro.vcs import VCSError

    agent = _resolution_agent(make_agent)
    agent.evolution.code_host.reply_to_review_comment = MagicMock(  # type: ignore[attr-defined]
        side_effect=VCSError("host down"),
    )

    record = await _run_resolution_lap(agent)

    assert record.outcome == "success"
    agent.evolution.code_host.resolve_review_thread.assert_not_called()


@pytest.mark.asyncio
async def test_run_remediation_no_resolve_when_thread_human_touched(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A delivered AI thread carrying a marker-less (human) reply is never
    auto-resolved — ``_resolvable_thread_ids`` refuses it, so no resolve fires."""
    agent = _resolution_agent(make_agent)
    human_touched = _bug_thread_node_with_reply(100, _RESOLVE_MARKER)
    human_touched["comments"]["nodes"].append({  # type: ignore[attr-defined]
        "databaseId": 102, "path": "a.py", "body": "Thanks, but what about X?",
    })
    agent.evolution.code_host.get_pr_review_threads = MagicMock(  # type: ignore[attr-defined]
        return_value=[human_touched],
    )

    record = await _run_resolution_lap(agent)

    assert record.outcome == "success"
    agent.evolution.code_host.resolve_review_thread.assert_not_called()


@pytest.mark.asyncio
async def test_run_remediation_resolve_host_error_does_not_crash_lap(
    make_agent, caplog,  # type: ignore[no-untyped-def]
) -> None:
    """A raising ``resolve_review_thread`` is best-effort: the lap logs a warning
    and completes unchanged (proposal 091 step 5)."""
    from maestro.vcs import VCSError

    agent = _resolution_agent(make_agent)
    agent.evolution.code_host.resolve_review_thread = MagicMock(  # type: ignore[attr-defined]
        side_effect=VCSError("resolve failed"),
    )

    with caplog.at_level(logging.WARNING, logger="maestro"):
        record = await _run_resolution_lap(agent)

    assert record.outcome == "success"
    agent.evolution.code_host.resolve_review_thread.assert_called_once_with(
        "PRRT_100",
    )
    assert any(
        "Failed to resolve review thread PRRT_100" in r.message
        for r in caplog.records
    )


def _overflow_review_body(*lines: str) -> str:
    """An AI review body carrying a sentinel-wrapped overflow block."""
    from maestro.evolution_tend import _OVERFLOW_SENTINEL_CLOSE, _OVERFLOW_SENTINEL_OPEN

    block = "\n".join(lines)
    return (
        "Summary.\n\n"
        f"{_OVERFLOW_SENTINEL_OPEN}\n{block}\n{_OVERFLOW_SENTINEL_CLOSE}\n"
    )


@pytest.mark.asyncio
async def test_run_remediation_partial_when_overflow_findings_remain(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A remediation that resolved every inline thread (none remain) but left a
    body-overflow finding un-acknowledged is downgraded to ``partial`` by the
    completeness gate — overflow is in the gate's reach now, scanned with the
    same selector the tend router re-routes on."""
    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent._vcs.has_changes.return_value = True
    # 4c keeps ``success``: git confirms the merge landed; no threads remain.
    agent._git_pr_merged = MagicMock(return_value=True)  # type: ignore[method-assign]
    # One unaddressed overflow finding (no acknowledgement comment) → downgrade.
    agent.evolution.code_host.get_pr_review_bodies = MagicMock(  # type: ignore[attr-defined]
        return_value=[
            _overflow_review_body(
                "- of-aaaaaaaa **a.py:0** [bug] — Null deref: boom",
            ),
        ],
    )

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args: object, **kwargs: object) -> MockQueryResult:
        return MockQueryResult(response="Replied to the threads only.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #94: address comments", source="external",
            pr_branch="maestro/evolve/ep94",
        )

    assert record.outcome == "partial"
    assert agent._last_remediation_items_remaining == 1
    assert "overflow finding(s) still lack an acknowledgement marker" in (
        record.result_summary
    )


@pytest.mark.asyncio
async def test_run_remediation_partial_combines_threads_and_overflow(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When both an inline thread and an overflow finding remain, the gate sums
    them into the remaining count and the note names each kind."""
    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent._vcs.has_changes.return_value = True
    agent._git_pr_merged = MagicMock(return_value=True)  # type: ignore[method-assign]
    agent.evolution.code_host.get_pr_review_threads = MagicMock(  # type: ignore[attr-defined]
        return_value=[_bug_thread_node(100)],
    )
    agent.evolution.code_host.get_pr_review_bodies = MagicMock(  # type: ignore[attr-defined]
        return_value=[
            _overflow_review_body(
                "- of-aaaaaaaa **a.py:0** [bug] — Null deref: boom",
            ),
        ],
    )

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args: object, **kwargs: object) -> MockQueryResult:
        return MockQueryResult(response="Did some of the work.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #95: address comments", source="external",
            pr_branch="maestro/evolve/ep95",
        )

    assert record.outcome == "partial"
    assert agent._last_remediation_items_remaining == 2
    assert "inline review thread(s) still lack a terminal reply marker" in (
        record.result_summary
    )
    assert "overflow finding(s) still lack an acknowledgement marker" in (
        record.result_summary
    )


def test_git_pr_merged_delegates_to_default(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``_git_pr_merged`` is a thin wrapper over the audit-checks
    git-merge-truth oracle, forwarding the PR number and returning its verdict
    unchanged so the closure gate and the PR-keyed audit rules share one oracle."""
    agent = make_agent()
    with patch(
        "maestro.agent._git_pr_merged_default", return_value=True,
    ) as mock_default:
        result = agent._git_pr_merged(123)

    assert result is True
    mock_default.assert_called_once_with(123)


# --- Merge-block zero-cost skip stamps `partial` from git-truth (#2543) ---
#
# git-merge-truth shows a BEHIND_MAIN / AWAITING_PUSH PR unlanded, so the
# `[merge-block]` zero-cost skip must record `partial`, never a false
# `success` (Trust-git-over-stamps). A false `success` is exactly what
# ``measure_false_success_recurrence`` counts — paying the eviction edge at
# the truth of the stamp drains that sensor without re-servicing the PR.


@pytest.mark.asyncio
async def test_merge_block_skip_records_partial_not_success(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(a) A BEHIND_MAIN merge-block skip on an unlanded PR records ``partial``
    — git-merge-truth shows the PR never reached ``main``, so the zero-cost
    skip must not fabricate a ``success`` label, and (b) the no-op repetition
    count and the named escalation still fire."""
    from maestro.remediation_tracker import MERGE_BLOCK_BEHIND_MAIN

    agent = make_agent()
    # Branch trails origin/main by four commits → classify → BEHIND_MAIN.
    agent._vcs.commits_ahead_behind.return_value = (0, 4)
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    gated = await agent._gate_pr_remediation_merge_block(
        "Remediate PR #91: fix CI", "maestro/evolve/ep91", "external",
    )

    # The gate fired: the LLM remediation path is short-circuited.
    assert gated is True
    tasks = agent.memory.get_recent_tasks(5)
    assert len(tasks) == 1
    # (a) Honest stamp: the PR is unlanded, so the skip is `partial`.
    assert tasks[0].outcome == "partial"
    assert tasks[0].duration_secs == 0.0
    assert "[merge-block]" in (tasks[0].key_findings or "")
    assert MERGE_BLOCK_BEHIND_MAIN in (tasks[0].result_summary or "")
    # (b) The skip is counted in the separate merge-block class — NOT the
    # ``should_reroute`` outcome list, so it never exiles the PR's
    # still-unattempted comment/overflow work (#2796) — and the named
    # escalation still fires.
    assert agent.remediation_tracker.merge_block_skip_count(91) == 1
    assert "91" not in agent.remediation_tracker._data
    agent._notify_best_effort.assert_awaited_once()
    message = agent._notify_best_effort.await_args.args[0]
    assert "PR #91" in message
    assert MERGE_BLOCK_BEHIND_MAIN in message
    # The gate never injects a directive itself.
    refreshed = agent.consciousness.load_self_model()
    assert refreshed.next_task_directive is None


@pytest.mark.asyncio
async def test_merge_block_skip_excluded_from_false_success_recurrence(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(c) Two merge-block skips for the same unlanded PR are excluded from
    ``measure_false_success_recurrence`` because they record ``partial``, not
    ``success`` — the sensor that the skip used to inflate now reads zero for
    this PR."""
    from dataclasses import asdict

    from maestro.audit_checks import measure_false_success_recurrence

    agent = make_agent()
    agent._vcs.commits_ahead_behind.return_value = (0, 4)
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

    # Two laps of the same PR through the merge-block gate.
    for _ in range(2):
        gated = await agent._gate_pr_remediation_merge_block(
            "Remediate PR #91: fix CI", "maestro/evolve/ep91", "external",
        )
        assert gated is True

    history = [asdict(t) for t in agent.memory.get_recent_tasks(10)]
    # Sanity: both skips are remediation tasks for PR #91 stamped partial.
    assert len(history) == 2
    assert all(t["outcome"] == "partial" for t in history)
    # The false-success sensor counts only `success`/`completed` stamps, so a
    # PR whose only stamps are `partial` is not a recurrence.
    result = measure_false_success_recurrence(history, n=0)
    assert result == {"count": 0, "by_pr": {}}


@pytest.mark.asyncio
async def test_merge_block_skip_silences_partial_construction_bridge(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(d) The partial-construction directive bridge stays silent on the
    zero-duration merge-block skip: a no-op skip did no work, so there is
    nothing to construct, and firing the bridge would re-feed the very
    remediation the gate just declined to run (the wheel)."""
    from dataclasses import asdict

    from maestro.audit_checks import check_partial_construction_gap

    agent = make_agent()
    agent._vcs.commits_ahead_behind.return_value = (0, 4)
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

    gated = await agent._gate_pr_remediation_merge_block(
        "Remediate PR #91: fix CI", "maestro/evolve/ep91", "external",
    )
    assert gated is True

    tasks = agent.memory.get_recent_tasks(5)
    skip_record = asdict(tasks[-1])
    # The skip is partial with non-empty findings, but zero-duration — the
    # bridge must return None rather than minting a "complete this" directive.
    assert skip_record["outcome"] == "partial"
    assert skip_record["duration_secs"] == 0.0
    assert (skip_record["key_findings"] or "").strip()
    assert check_partial_construction_gap([skip_record]) is None


# --- Golden gate-activation equivalence battery (proposal 069) ---
# Each scenario pins the exact (gate, action, detail) records — and the
# resulting next_task_directive state — that the pre-registry inline
# blocks produced for a fixed task history. The GATE_CHAIN port must
# reproduce them one-for-one.


def _activation_tuples(
    mem_fs: dict[str, str], agent: MaestroAgent,
) -> list[tuple[str, str, str]]:
    """Read (gate, action, detail) tuples from gate_activations.jsonl."""
    raw = mem_fs.get(
        str(agent.config.memory_dir / "gate_activations.jsonl"), "",
    )
    return [
        (rec["gate"], rec["action"], rec["detail"])
        for rec in (json.loads(line) for line in raw.splitlines() if line.strip())
    ]


@pytest.mark.asyncio
async def test_implement_plan_verify_grace_keeps_green_branch(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Flag on: a verify-only first review round against a green gate is not demoted.

    Proposal 124 — the end-to-end regression. ``implement_plan`` drives a
    self-review whose first round neither approves nor commits a fix; under the
    old tree-dirtiness proxy that demoted the plan (branch cleanup + demote to
    ``failed``). With the gate read at the review terminal and the branch green,
    that round buys a further review round, the next round approves, and the plan
    proceeds to a PR — no ``cleanup_branch``, no ``_demote_plan_to_failed`` — and
    exactly one ``self_review_terminal`` row is recorded at the terminal exit.
    """
    from maestro.gate_convergence import ConvergenceResult

    agent = make_agent()
    agent.config.self_review_max_rounds = 2
    agent.config.gate_observation_enabled = True
    _seed_approved_plan(agent, "p-verify-grace", "Verify Grace")

    review_clean = QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True, raw_output="",
        observation=_clean_observation(),
    )
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "head999"
    vcs.get_diff.return_value = "diff --git a/file.py b/file.py\n+line"
    vcs.has_changes.return_value = False
    agent.evolution._vcs = vcs

    demote_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base000")),
        patch.object(
            agent.evolution, "run_quality_gates", return_value=review_clean,
        ) as rqg_mock,
        patch.object(
            agent.evolution, "converge_to_green",
            AsyncMock(return_value=ConvergenceResult(
                outcome="green", review=review_clean, rounds_used=0,
            )),
        ),
        patch.object(
            agent.evolution, "create_pr",
            AsyncMock(return_value="https://github.com/repo/pull/1"),
        ),
        patch.object(agent.evolution, "cleanup_branch") as cleanup_mock,
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
        patch.object(agent.contributions, "get_current_head", return_value="head999"),
        patch.object(agent.contributions, "get_new_commits", return_value=[]),
        patch.object(agent.contributions, "record_own_commit", MagicMock()),
        patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q,
    ):
        mock_q.side_effect = [
            MockQueryResult(response="Reviewed — nothing to change here"),
            MockQueryResult(response="LGTM"),
        ]
        await agent.implement_plan("p-verify-grace")

        # A SECOND review round ran — the grace bought a continuation.
        assert mock_q.call_count == 2
        # One extra gate run total: the single verify-only terminal read.
        assert rqg_mock.call_count == 1

    # The green branch was neither cleaned up nor demoted on the verify round.
    cleanup_mock.assert_not_called()
    demote_mock.assert_not_called()
    # Exactly one self_review_terminal row, at the (green) terminal exit.
    terminal_rows = [
        row for row in _activation_tuples(mem_fs, agent)
        if row[0] == "self_review_terminal"
    ]
    assert len(terminal_rows) == 1
    assert terminal_rows[0][1] == "green"


_EMPTY_CURATION = json.dumps({"prune": [], "edit": [], "enqueue": []})


@pytest.mark.asyncio
async def test_golden_quiet_skip_block_record(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """Quiet-skip emits exactly the historical blocked record, no SDK call."""
    from maestro.task_queue import QueuedTask

    agent = make_agent()
    agent.task_queue.save([QueuedTask(task_id="q1", goal="queued work")])

    with patch(
        "maestro.loop.task_generation.query",
        AsyncMock(side_effect=AssertionError("SDK must not be called")),
    ):
        await agent.generate_task()

    assert _activation_tuples(mem_fs, agent) == [
        ("quiet_skip", "blocked", "queue_size=1"),
    ]


@pytest.mark.asyncio
async def test_golden_audit_stall_block_record(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """Audit-stall emits the historical blocked record with worst-key detail."""
    agent = make_agent()
    agent.config.audit_stall_block_enabled = True
    agent.config.audit_stall_threshold = 2

    with (
        patch(
            "maestro.audit_checks.compute_audit_stall_counts",
            return_value={"[audit] impact_review overdue": 2, "[audit] other": 1},
        ),
        patch(
            "maestro.loop.task_generation.query",
            AsyncMock(side_effect=AssertionError("SDK must not be called")),
        ),
    ):
        await agent.generate_task()

    assert _activation_tuples(mem_fs, agent) == [
        ("audit_stall", "blocked", "[audit] impact_review overdue fired 2 cycles"),
    ]


@pytest.mark.asyncio
async def test_golden_cost_risk_block_record_and_journal(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """Cost-risk emits the historical blocked record and gate event."""
    agent = make_agent()
    verdict = "2 consecutive expensive failures totalling $4.10"

    with (
        patch(
            "maestro.task_gates.task_cost_gate.check_task_cost_risk",
            return_value=verdict,
        ),
        patch(
            "maestro.loop.task_generation.query",
            AsyncMock(side_effect=AssertionError("SDK must not be called")),
        ),
    ):
        await agent.generate_task()

    assert _activation_tuples(mem_fs, agent) == [
        ("cost_risk", "blocked", verdict),
    ]
    events_raw = mem_fs.get(
        str(agent.config.memory_dir / "events.jsonl"), "",
    )
    event_rows = [
        json.loads(line) for line in events_raw.splitlines() if line.strip()
    ]
    cost_events = [
        e for e in event_rows if e.get("triggered_by") == "cost_gate"
    ]
    assert len(cost_events) == 1
    assert cost_events[0]["content"] == (
        f"Cost gate blocked task generation: {verdict}"
    )
    assert cost_events[0]["entry_type"] == "reflection"


@pytest.mark.asyncio
async def test_golden_integration_cadence_directive_record(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """The real integration-cadence predicate fires with the historical
    directive text, and exactly one directive_set record lands — the
    bridges below it in the chain never fire once the slot is full."""
    from maestro.memory import TaskRecord

    agent = make_agent()
    agent.memory.record_task(TaskRecord(
        task_id="t-measure", goal="Measure response sizes across phases",
        plan=[], outcome="success", result_summary="measured",
        errors=[], duration_secs=10.0,
        timestamp="2026-01-01T00:00:00",
    ))

    with patch(
        "maestro.loop.task_generation.query",
        AsyncMock(return_value=MockQueryResult(response=_EMPTY_CURATION)),
    ):
        await agent.generate_task()

    expected_directive = (
        "No recent task performed integration work. "
        "Triage unmerged successful branches and promote one "
        "via a source_task_id follow-up plan."
    )
    assert _activation_tuples(mem_fs, agent) == [
        ("integration_cadence", "directive_set", expected_directive),
    ]
    # One-shot contract: the parseable SDK response cleared the directive.
    assert agent.consciousness.load_self_model().next_task_directive is None


@pytest.mark.asyncio
async def test_golden_preset_directive_produces_no_records(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """A directive set before generate_task suppresses every bridge — zero
    gate-activation records — and is still cleared after a parseable
    response, exactly as the inline guards behaved."""
    from maestro.memory import TaskRecord

    agent = make_agent()
    # History that WOULD fire integration-cadence were the slot empty.
    agent.memory.record_task(TaskRecord(
        task_id="t-measure", goal="Measure response sizes across phases",
        plan=[], outcome="success", result_summary="measured",
        errors=[], duration_secs=10.0,
        timestamp="2026-01-01T00:00:00",
    ))
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "Pre-set by contemplation"
    agent.consciousness.save_self_model(model)

    with patch(
        "maestro.loop.task_generation.query",
        AsyncMock(return_value=MockQueryResult(response=_EMPTY_CURATION)),
    ):
        await agent.generate_task()

    assert _activation_tuples(mem_fs, agent) == []
    assert agent.consciousness.load_self_model().next_task_directive is None


@pytest.mark.asyncio
async def test_golden_first_bridge_wins_lower_bridge_not_called(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """Precedence: once self-directed-success fills the slot, the
    forced-read bridge is never consulted — one record, first bridge's."""
    from maestro.memory import TaskRecord

    agent = make_agent()
    # Isolate: satisfy integration-cadence and keep rate gates quiet.
    agent.config.investigation_rate_enabled = False
    agent.memory.record_task(TaskRecord(
        task_id="t-triage", goal="Triage unmerged branches and promote one",
        plan=[], outcome="success", result_summary="triaged",
        errors=[], duration_secs=10.0,
        timestamp="2026-01-01T00:00:00",
    ))

    forced_read = MagicMock(return_value="steer B")
    with (
        patch(
            "maestro.task_gates.check_self_directed_success_rate",
            return_value="steer A",
        ),
        patch("maestro.task_gates.check_forced_read_directive", forced_read),
        patch(
            "maestro.loop.task_generation.query",
            AsyncMock(return_value=MockQueryResult(response=_EMPTY_CURATION)),
        ),
    ):
        await agent.generate_task()

    assert _activation_tuples(mem_fs, agent) == [
        ("self_directed_success", "directive_set", "steer A"),
    ]
    forced_read.assert_not_called()


def test_generate_task_source_has_no_directive_guards() -> None:
    """Step-2 acceptance (proposal 069): the inline ``if not
    model.next_task_directive:`` guard pattern is gone from the
    task-generation body — precedence lives in GATE_CHAIN order."""
    import inspect

    from maestro.loop import task_generation

    source = inspect.getsource(task_generation)
    assert "if not model.next_task_directive" not in source
