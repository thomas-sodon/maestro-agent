"""Tests for the run() verdict-fetch gate — the enforcement floor under the
fetch-before-word value (gate_remediation_verdict / record_fetch_truth).

These tests confirm the §4e verdict-fetch gate fires on the ``run()`` emission
path: a remediation verdict carrying an awaiting-tend-push narrative with no
recorded fetch-truth marker is downgraded to ``[fetch-missing]``, and the §4c
fetch action-zero records the marker BEFORE the status string is persisted.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from maestro.agent import (
    MaestroAgent,
)
from maestro.consciousness import PendingPRRequest
from maestro.escalation_bridge import has_standdown
from maestro.loop.pr_remediation import (
    STALLED_REMEDIATION_LAP_CAP,
    PrRemediationController,
    _pending_request_references_pr,
)
from maestro.memory import EvolutionPlan, TaskRecord
from maestro.pr_facts import MergeTruth, PRReadiness, Salvage, select_live_plans
from maestro.pr_remediation_state import PrRemediationState
from maestro.red_root import CheckRun
from maestro.remediation_tracker import (
    MERGE_BLOCK_AWAITING_APPROVAL,
    MERGE_BLOCK_AWAITING_PUSH,
    MERGE_BLOCK_AWAITING_REVIEW,
    MERGE_BLOCK_BEHIND_MAIN,
    MERGE_BLOCK_CI_PENDING,
    MERGE_BLOCK_CI_RUNNING,
    MERGE_BLOCK_CONFLICTING,
    MERGE_BLOCK_THREADS_UNRESOLVED,
    find_stand_down,
    get_obligation_fork,
    has_fetch_truth,
    record_fetch_truth,
    record_obligation_fork_truth,
)
from maestro.vcs import PrMergeState, VCSError
from tests.conftest import MockQueryResult
from tests.test_agent import _STALL_BASE, _remediation_lap, _stall_rev_parse


def _readiness(pr_number: int) -> PRReadiness:
    return PRReadiness(
        pr_number=pr_number,
        branch_landed=False,
        branch_behind=False,
        branch_conflicting=False,
        ci_state="GREEN",
        unresolved_threads=2,
        unaddressed_overflow=0,
        awaiting_approval=False,
        ready_and_waiting=False,
        source="host_api",
        observed_at=123.0,
    )

_PLAN = json.dumps([{"description": "Step 1"}])


async def _mock_q(*args: object, **kwargs: object) -> MockQueryResult:
    return MockQueryResult(response="Resolved on the branch.")


def _arm_awaiting_push(agent: object) -> None:
    """Configure *agent* so §4c forces an awaiting-tend-push partial.

    git-merge-truth says the SHA never reached main, so the closure stamp gate
    downgrades the would-be ``success`` to ``partial`` and frames the on-branch
    ``awaiting tend push`` note the verdict-fetch gate keys on.
    """
    agent.config.enable_session_reuse = False  # type: ignore[attr-defined]
    agent._vcs.has_changes.return_value = True  # type: ignore[attr-defined]
    agent._git_pr_merged = MagicMock(return_value=False)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_remediation_no_marker_downgrades_to_fetch_missing(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A verdict emitted without a preceding fetch-truth marker is downgraded to
    ``[fetch-missing]`` by the §4e gate on the ``run()`` path."""
    agent = make_agent()
    _arm_awaiting_push(agent)
    # Override the action-zero with a NON-recording stand-in: no marker is
    # written, so the verdict-fetch gate sees git as not having spoken.
    agent._record_remediation_fetch_truth = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    assert record.outcome == "partial"
    assert "[fetch-missing]" in record.result_summary
    # The on-branch narrative survives beneath the forced marker. The §5-push
    # succeeded (default mock), so the pre-push awaiting-tend-push marker the
    # §4e gate keyed on was rewritten to the delivered marker (proposal 091).
    assert "delivered to pr, awaiting review/merge" in record.result_summary.lower()


@pytest.mark.asyncio
async def test_run_remediation_marker_present_no_downgrade(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When the §4c action-zero records a fetch-truth marker, the §4e gate lets
    the awaiting-push partial stand — no ``[fetch-missing]`` stamp."""
    agent = make_agent()
    _arm_awaiting_push(agent)
    # The default conftest stand-in records the marker (no subprocess).

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    assert record.outcome == "partial"
    assert "[fetch-missing]" not in record.result_summary
    # The §5-push succeeded, so the awaiting-tend-push marker was rewritten to
    # the delivered marker (proposal 091).
    assert "delivered to pr, awaiting review/merge" in record.result_summary.lower()


@pytest.mark.asyncio
async def test_run_remediation_push_owed_scrubs_false_delivery_claim(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An agent step-output "delivered to pr, awaiting review/merge" that
    survives into the summary on a *push-owed* lap (AWAITING_PUSH blocker, both
    pushes failed) is scrubbed to the push-owed marker, so the record can no
    longer misclaim delivery a push still owes."""
    agent = make_agent()
    _arm_awaiting_push(agent)
    # Both §5-push attempts fail → push_delivered is False (push owed).
    agent._vcs.push.side_effect = Exception(  # type: ignore[attr-defined]
        "non-fast-forward",
    )

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        # The agent narrates a delivery it did not make.
        mock_exec.return_value = MockQueryResult(
            response="delivered to pr, awaiting review/merge",
        )
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    assert record.outcome == "partial"
    # The false delivery claim is gone; the push-owed marker names HEAD ahead of
    # origin, so classify_lap_delivery cannot read this lap as DELIVERING.
    assert "delivered to pr, awaiting review/merge" not in (
        record.result_summary.lower()
    )
    assert "[push-owed]" in record.result_summary
    assert "HEAD ahead of origin" in record.result_summary


@pytest.mark.asyncio
async def test_run_remediation_stamps_base_head_sha(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A ``Remediate PR #N:`` lap stamps ``base_head_sha`` from
    ``rev_parse("origin/main")`` onto the recorded ``TaskRecord``, captured
    beside the ``pr_head_sha`` watermark it reads from ``origin/<branch>``
    (proposal 099 step 1). Distinct per-ref SHAs prove the base is the
    ``origin/main`` head, not the branch head."""
    agent = make_agent()
    _arm_awaiting_push(agent)

    def _rev(ref: str) -> str:
        return "basemainsha099" if ref == "origin/main" else "branchheadsha"

    agent._vcs.rev_parse = MagicMock(side_effect=_rev)  # type: ignore[method-assign]

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    assert record.base_head_sha == "basemainsha099"
    assert record.pr_head_sha == "branchheadsha"


@pytest.mark.asyncio
async def test_run_remediation_marker_recorded_before_status_string(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The §4c fetch action-zero records the marker BEFORE the task record (the
    status string) is persisted — git speaks first, the verdict word follows."""
    agent = make_agent()
    _arm_awaiting_push(agent)

    order: list[str] = []

    def _fetch_side(task_id: str, pr_num: int, pr_branch: str) -> bool:
        order.append("fetch")
        record_fetch_truth(task_id, pr_num, False, agent.config.memory_dir)
        return False

    agent._record_remediation_fetch_truth = MagicMock(  # type: ignore[method-assign]
        side_effect=_fetch_side,
    )

    orig_record_task = agent.memory.record_task

    def _record_spy(rec: object) -> object:
        order.append("record")
        return orig_record_task(rec)

    agent.memory.record_task = _record_spy  # type: ignore[method-assign]

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    assert order, "neither hook fired"
    assert order.index("fetch") < order.index("record")
    # Marker recorded → the gate passes, so no forced downgrade marker.
    assert "[fetch-missing]" not in record.result_summary
    agent._record_remediation_fetch_truth.assert_called_once()


def test_record_remediation_fetch_truth_fetches_then_records(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The action-zero runs ``git fetch`` (loud), reads ancestry, then writes
    the per-task fetch-truth marker — git speaks before the marker is stamped."""
    agent = make_agent()
    order: list[str] = []
    agent._branch_merged_into_main = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda ref: order.append("ancestry") or True,
    )

    def _run_side(*args: object, **kwargs: object) -> MagicMock:
        order.append("fetch")
        return MagicMock(returncode=0)

    # Call the real method (the fixture mocks the instance attribute) with the
    # subprocess seam patched, so no real git runs.
    with patch("maestro.agent.subprocess.run", side_effect=_run_side) as mock_run:
        result = PrRemediationController(agent)._record_remediation_fetch_truth("task-9", 2647, "maestro/evolve/ep2647",
        )

    assert result is True
    assert order == ["fetch", "ancestry"]
    # The fetch is loud (check=True) and targets origin.
    assert mock_run.call_args.args[0][:3] == ["git", "fetch", "origin"]
    assert mock_run.call_args.kwargs["check"] is True
    agent._branch_merged_into_main.assert_called_once_with(
        "origin/maestro/evolve/ep2647",
    )
    # The ancestry verdict is persisted under the task id.
    assert has_fetch_truth("task-9", agent.config.memory_dir) is True


# ──────────────────────────────────────────────────────────────────────
# PR #3211 — obligation-fork floor wiring (obligation_fork_floor_enabled).
# The §4c indicting action-zero records the HEAD-vs-origin/<branch> marker and
# §4e passes owned_awaiting_push, so gate_remediation_verdict's fork floor
# ([fork-missing]/[push-owed]/fetch-gated pass-through) actually fires on the
# run() path instead of remaining dead code. Default off ⇒ the pre-wiring path
# is byte-identical.
# ──────────────────────────────────────────────────────────────────────


def test_record_remediation_obligation_fork_records_head_vs_origin(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The obligation-fork action-zero reads ``HEAD`` vs ``origin/<branch>`` and
    persists the marker so the §4e gate can fork an owed push from an awaited
    merge — the indicting compare, run beside the blameless fetch-truth read."""
    agent = make_agent()

    def _rev(ref: str) -> str:
        return "headsha000000" if ref == "HEAD" else "remotesha0000"

    agent._vcs.rev_parse = MagicMock(side_effect=_rev)  # type: ignore[method-assign]
    # origin/<branch> is an ancestor of HEAD → HEAD is genuinely ahead.
    agent._vcs.is_ancestor = MagicMock(return_value=True)  # type: ignore[method-assign]
    PrRemediationController(agent)._record_remediation_obligation_fork("task-fork", 2647, "maestro/evolve/ep2647",
    )
    agent._vcs.is_ancestor.assert_called_once_with(
        "origin/maestro/evolve/ep2647", "HEAD",
    )
    fork = get_obligation_fork("task-fork", agent.config.memory_dir)
    assert fork is not None
    assert fork.head_sha == "headsha000000"
    assert fork.remote_sha == "remotesha0000"
    assert fork.branch == "maestro/evolve/ep2647"
    assert fork.remote_is_ancestor is True
    # Differing SHAs AND origin an ancestor of HEAD (genuinely ahead) → the
    # indicting fork reports the push is owed.
    assert fork.head_ahead is True


@pytest.mark.asyncio
async def test_run_remediation_fork_floor_fires_when_enabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With ``obligation_fork_floor_enabled`` on, an owned AWAITING_PUSH verdict
    on a HEAD-ahead branch is coerced to ``[push-owed]`` by the §4e fork floor —
    proving the floor is reachable in production, not dead code."""
    agent = make_agent()
    agent.config.obligation_fork_floor_enabled = True
    _arm_awaiting_push(agent)

    # Record a HEAD-ahead fork marker at the action-zero (mock the helper so no
    # real rev-parse subprocess runs) so the floor forks "the push is mine".
    def _fork_side(task_id: str, pr_num: int, pr_branch: str) -> None:
        record_obligation_fork_truth(
            task_id, pr_num, "localhead0000", "originhead000", pr_branch,
            agent.config.memory_dir, remote_is_ancestor=True,
        )

    agent._record_remediation_obligation_fork = MagicMock(  # type: ignore[method-assign]
        side_effect=_fork_side,
    )

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    # The floor was reached via the live call site and fired its owned-push arm.
    agent._record_remediation_obligation_fork.assert_called_once()
    assert record.outcome == "partial"
    assert "[push-owed]" in record.result_summary


@pytest.mark.asyncio
async def test_run_remediation_fork_floor_inert_when_disabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Default off: the action-zero fork helper is never called and the §4e gate
    receives no ``owned_awaiting_push``, so no ``[push-owed]``/``[fork-missing]``
    fork stamp appears — the pre-wiring path is unchanged."""
    agent = make_agent()
    assert agent.config.obligation_fork_floor_enabled is False
    _arm_awaiting_push(agent)
    agent._record_remediation_obligation_fork = MagicMock()  # type: ignore[method-assign]

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    agent._record_remediation_obligation_fork.assert_not_called()
    assert "[push-owed]" not in record.result_summary
    assert "[fork-missing]" not in record.result_summary


# ──────────────────────────────────────────────────────────────────────
# Proposal 087 step 5 — the pre-stamp merge-truth gate on the run() path.
# A would-be ``success`` naming a ``#N`` the chokepoint cannot confirm landed
# is refused at write time (recorded ``partial`` with a provenance stamp),
# moving the catch rules 13/14 make post-hoc to pre-stamp. Flag-gated.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prestamp_gate_downgrades_unmerged_success_with_provenance(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the gate on, a ``success`` naming an unmerged ``#N`` is recorded
    ``partial`` stamped with the chokepoint observation's provenance."""
    agent = make_agent()
    _arm_awaiting_push(agent)
    agent.config.prestamp_merge_truth_gate_enabled = True
    # The chokepoint host oracle reports unmerged (conftest default
    # ``is_pr_merged`` is False), so ``is_pr_settled`` → branch not landed.

    with patch("maestro.agent.subprocess.run", MagicMock()), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    assert record.outcome == "partial"
    # The refusal carries the merge-truth provenance, not just a block reason.
    assert "not yet landed per merge-truth" in record.result_summary
    assert "source=host_api" in record.result_summary
    # Provenance is threaded onto the agent for the post-task tracker.
    assert agent._last_remediation_merge_source == "host_api"
    assert agent._last_remediation_merge_observed_at is not None


@pytest.mark.asyncio
async def test_prestamp_gate_landed_success_survives(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the gate on, a ``success`` whose ``#N`` the chokepoint confirms
    merged is left untouched — the gate refuses only what git cannot confirm."""
    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent._vcs.has_changes.return_value = True
    agent.config.prestamp_merge_truth_gate_enabled = True
    # The chokepoint confirms the PR merged → branch landed, so the gate leaves
    # the success stamp untouched.
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=2647, settled=True, state="MERGED",
            branch_landed=True, source="host_api", observed_at=0.0,
        ),
    )

    with patch("maestro.agent.subprocess.run", MagicMock()), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    assert record.outcome == "success"
    assert agent._last_remediation_merge_source is None


@pytest.mark.asyncio
async def test_prestamp_gate_off_uses_legacy_probe(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the gate off (default), the legacy host-probe stamp gate runs and no
    merge-truth provenance is stamped — today's behaviour is unchanged."""
    agent = make_agent()
    _arm_awaiting_push(agent)
    agent.config.prestamp_merge_truth_gate_enabled = False

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    assert record.outcome == "partial"
    # Legacy path: no merge-truth provenance string and no provenance thread.
    assert "per merge-truth" not in record.result_summary
    assert agent._last_remediation_merge_source is None


@pytest.mark.asyncio
async def test_run_remediation_merged_pr_coerces_phantom_to_settled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """§4f record-time merge-truth binding: when the ``is_pr_settled`` chokepoint
    reports the PR merged, a phantom not-landed / awaiting-tend-push verdict is
    overwritten with a settled ``success`` — the stale stamp cannot outvote git
    (#2809/#2810)."""
    agent = make_agent()
    _arm_awaiting_push(agent)
    # The legacy probe path frames an awaiting-tend-push not-landed note (a stale
    # stamp), but the authoritative is_pr_settled chokepoint shows #2647 merged.
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=2647, settled=True, state="MERGED",
            branch_landed=True, source="host_api", observed_at=0.0,
        ),
    )

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    # The chokepoint, not the stamp, authors the record: a settled success with
    # no phantom not-landed narrative surviving in the summary.
    assert record.outcome == "success"
    assert "awaiting tend push" not in record.result_summary.lower()
    assert "[merged]" in record.result_summary


# ──────────────────────────────────────────────────────────────────────
# Proposal 089 step 4 — the per-lap PR-readiness snapshot on the run() path.
# Stamped onto the lap's TaskRecord only when a consuming 089 flag is on, so
# the observation's git/host I/O stays off the hot path when both are off.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remediation_lap_stamps_readiness_snapshot_when_flag_on(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With a 089 flag on, the lap's TaskRecord carries the readiness snapshot."""
    agent = make_agent()
    _arm_awaiting_push(agent)
    agent.config.progress_aware_lap_budget_enabled = True
    readiness = _readiness(2647)
    agent.observe_pr_readiness = MagicMock(return_value=readiness)  # type: ignore[method-assign]

    with patch("maestro.agent.subprocess.run", MagicMock()), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    agent.observe_pr_readiness.assert_called_once_with(
        2647, "maestro/evolve/ep2647",
    )
    assert record.pr_readiness_snapshot is not None
    assert record.pr_readiness_snapshot["pr_number"] == 2647
    assert record.pr_readiness_snapshot["ci_state"] == "GREEN"
    assert record.pr_readiness_snapshot["unresolved_threads"] == 2


@pytest.mark.asyncio
async def test_remediation_lap_stamps_readiness_snapshot_when_salvage_flag_on(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Proposal 094: the salvage master flag activates the readiness snapshot via
    the ``progress_aware_lap_budget_active`` predicate, so a lap stamps its
    snapshot even with the dedicated 089 flag off."""
    agent = make_agent()
    _arm_awaiting_push(agent)
    agent.config.progress_aware_lap_budget_enabled = False
    agent.config.salvage_before_disengage_enabled = True
    readiness = _readiness(2647)
    agent.observe_pr_readiness = MagicMock(return_value=readiness)  # type: ignore[method-assign]

    with patch("maestro.agent.subprocess.run", MagicMock()), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    agent.observe_pr_readiness.assert_called_once_with(
        2647, "maestro/evolve/ep2647",
    )
    assert record.pr_readiness_snapshot is not None
    assert record.pr_readiness_snapshot["pr_number"] == 2647


@pytest.mark.asyncio
async def test_remediation_lap_no_snapshot_when_flags_off(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Both 089 flags off, plus the salvage master flag pinned off (it feeds
    ``progress_aware_lap_budget_active`` since proposal 094 flipped it on by
    default): no readiness observation, snapshot ``None``."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = False
    # observed_ready_standdown_enabled now ships default-on, so pin it off too:
    # the point of this test is that with *every* 089 consumer disabled the
    # readiness observation stays off the hot path.
    agent.config.observed_ready_standdown_enabled = False
    _arm_awaiting_push(agent)
    agent.observe_pr_readiness = MagicMock()  # type: ignore[method-assign]

    with patch("maestro.agent.subprocess.run", MagicMock()), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    agent.observe_pr_readiness.assert_not_called()
    assert record.pr_readiness_snapshot is None


# ──────────────────────────────────────────────────────────────────────
# Mechanical chosen-stand-down actuator at the remediation dequeue.
# A stalled, thread-clear, git-unlanded head ``pending_pr_requests`` entry
# (clearability governed by the salvage oracle, not blocker ownership) is
# disposed in loop code — the ``[stood-down]``
# verdict authored, a ``partial`` no-op recorded, the LLM session NEVER opened.
# ``run()`` is the remediation session: the assertions below turn on whether it
# was awaited.
# ──────────────────────────────────────────────────────────────────────

_STANDDOWN_SHA = "a" * 40


def _awaiting_push_lap(task_id: str, pr_num: int, sha: str) -> TaskRecord:
    """A ``partial`` ``Remediate PR #N`` lap stamped an AWAITING_PUSH blocker."""
    return TaskRecord(
        task_id=task_id,
        goal=f"Remediate PR #{pr_num}: address comments",
        plan=[],
        outcome="partial",
        result_summary=(
            "Resolved on branch, awaiting tend push — "
            f"PR #{pr_num} not yet landed (merge blocker: AWAITING_PUSH)."
        ),
        errors=[],
        duration_secs=1.0,
        timestamp="2026-06-26T00:00:00+00:00",
        source="external",
        pr_head_sha=sha,
    )


def _open_merge_truth(pr_num: int) -> MergeTruth:
    """The merge-truth chokepoint's reading of a still-OPEN, unlanded PR."""
    return MergeTruth(
        pr_number=pr_num, settled=False, state="OPEN",
        branch_landed=False, source="host_api", observed_at=0.0,
    )


def _arm_mechanical_standdown(agent: object, pr_num: int, sha: str) -> None:
    """Seed *agent* with a stalled, unlanded #pr_num lineage (AWAITING_PUSH laps).

    Records ``STALLED_REMEDIATION_LAP_CAP`` same-SHA AWAITING_PUSH ``partial``
    laps, points the VCS head at *sha*, and pins merge truth to OPEN so
    every mechanical-stand-down condition holds.
    """
    agent._vcs.rev_parse.return_value = sha  # type: ignore[attr-defined]
    agent.is_pr_settled = _open_merge_truth  # type: ignore[attr-defined,method-assign]
    for i in range(STALLED_REMEDIATION_LAP_CAP):
        agent.memory.record_task(  # type: ignore[attr-defined]
            _awaiting_push_lap(f"lap{i}", pr_num, sha),
        )


def _conflicting_lap(task_id: str, pr_num: int, sha: str) -> TaskRecord:
    """A ``partial`` ``Remediate PR #N`` lap stamped an owned CONFLICTING blocker."""
    return TaskRecord(
        task_id=task_id,
        goal=f"Remediate PR #{pr_num}: address comments",
        plan=[],
        outcome="partial",
        result_summary=(
            f"Conflicts remain — PR #{pr_num} not yet landed "
            "(merge blocker: CONFLICTING)."
        ),
        errors=[],
        duration_secs=1.0,
        timestamp="2026-06-26T00:00:00+00:00",
        source="external",
        pr_head_sha=sha,
    )


def _arm_conflicting_standdown(agent: object, pr_num: int, sha: str) -> None:
    """Seed *agent* with a stalled, unlanded #pr_num lineage stamped CONFLICTING.

    The owned-blocker complement of :func:`_arm_mechanical_standdown`: the laps
    name a blocker the loop could clear itself, which no longer disqualifies
    the candidate verdict — clearability is the salvage oracle's question at
    the ``_dispose_mechanical_standdown`` seam. The live thread observation is
    pinned thread-clear and conflicting so the #2907 fold stays deterministic.
    """
    agent._vcs.rev_parse.return_value = sha  # type: ignore[attr-defined]
    agent.is_pr_settled = _open_merge_truth  # type: ignore[attr-defined,method-assign]
    agent.observe_pr_readiness = MagicMock(  # type: ignore[attr-defined]
        return_value=_blocker_readiness(pr_num, branch_conflicting=True),
    )
    for i in range(STALLED_REMEDIATION_LAP_CAP):
        agent.memory.record_task(  # type: ignore[attr-defined]
            _conflicting_lap(f"lap{i}", pr_num, sha),
        )


def _non_pr_lap(task_id: str) -> TaskRecord:
    """A non-remediation lap that the PR-lineage scan must skip over."""
    return TaskRecord(
        task_id=task_id,
        goal="Build a sensor",
        plan=[],
        outcome="success",
        result_summary="done",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-06-26T00:00:00+00:00",
        source="self_directed",
    )


def _no_marker_lap(task_id: str, pr_num: int, sha: str) -> TaskRecord:
    """A ``partial`` ``Remediate PR #N`` lap whose summary names NO merge blocker.

    The marker-absent lap the observation-backed fallback (#2865) must classify
    live: ``result_summary`` carries no ``(merge blocker: <CODE>)``, and the
    ``address comments`` directive is thread-clear.
    """
    return TaskRecord(
        task_id=task_id,
        goal=f"Remediate PR #{pr_num}: address comments",
        plan=[],
        outcome="partial",
        result_summary="Resolved on branch; PR not yet landed.",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-06-26T00:00:00+00:00",
        source="external",
        pr_head_sha=sha,
    )


def _blocker_readiness(
    pr_number: int,
    *,
    branch_behind: bool = False,
    branch_conflicting: bool = False,
    awaiting_approval: bool = False,
    unresolved_threads: int = 0,
    settled: bool = False,
    branch_landed: bool = False,
) -> PRReadiness:
    """A :class:`PRReadiness` carrying at most one merge-block field set.

    The live observation the mechanical actuator reads when no lap named its
    blocker: with every field left clear it confirms *no* blocker code — an
    unobservable merge state, the suppression-safe refusal direction — and
    setting exactly one models a
    CONFLICTING / BEHIND_MAIN / AWAITING_APPROVAL merge state. *unresolved_threads*
    models a live review thread still awaiting an answer — owned work the
    completion predicate folds into thread-clarity (#2907), blocking the
    stand-down whatever the merge-block state reads.
    """
    return PRReadiness(
        pr_number=pr_number,
        branch_landed=branch_landed,
        branch_behind=branch_behind,
        branch_conflicting=branch_conflicting,
        ci_state="GREEN",
        unresolved_threads=unresolved_threads,
        unaddressed_overflow=0,
        awaiting_approval=awaiting_approval,
        ready_and_waiting=False,
        source="host_api",
        observed_at=123.0,
        settled=settled,
    )


def _arm_no_marker_lineage(
    agent: object,
    pr_num: int,
    sha: str,
    *,
    extra_older_sha: str | None = None,
    lap_count: int = STALLED_REMEDIATION_LAP_CAP,
) -> None:
    """Seed *agent* with same-SHA no-marker ``partial`` laps, head at *sha*, unlanded.

    Points the VCS head at *sha* and pins merge truth to OPEN so every
    cheap condition holds. Records *lap_count* same-SHA laps (defaulting to the
    stall threshold, so the streak qualifies); pass a smaller *lap_count* to seed
    a below-threshold run. When *extra_older_sha* is given, an older
    differing-SHA ``partial`` lap is recorded first (oldest) so the actuator's
    trailing-run scan exercises its streak ``break`` above a still-qualifying
    leading run.
    """
    agent._vcs.rev_parse.return_value = sha  # type: ignore[attr-defined]
    agent.is_pr_settled = _open_merge_truth  # type: ignore[attr-defined,method-assign]
    if extra_older_sha is not None:
        agent.memory.record_task(  # type: ignore[attr-defined]
            _no_marker_lap("older", pr_num, extra_older_sha),
        )
    for i in range(lap_count):
        agent.memory.record_task(  # type: ignore[attr-defined]
            _no_marker_lap(f"lap{i}", pr_num, sha),
        )


def test_mechanical_standdown_verdict_none_when_no_branch(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    entry = PendingPRRequest(
        directive="Remediate PR #2819: address comments", pr_branch=None,
    )
    assert agent._mechanical_standdown_verdict(entry) is None


def test_mechanical_standdown_verdict_none_when_not_remediation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    entry = PendingPRRequest(
        directive="Address review feedback", pr_branch="maestro/evolve/ep1",
    )
    assert agent._mechanical_standdown_verdict(entry) is None


def test_mechanical_standdown_verdict_none_when_no_lap_history(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    entry = PendingPRRequest(
        directive="Remediate PR #2819: address comments",
        pr_branch="maestro/evolve/ep2819",
    )
    # No recorded laps for the PR — never reaches the git head resolution.
    assert agent._mechanical_standdown_verdict(entry) is None


def test_mechanical_standdown_verdict_none_when_head_unresolvable(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    _arm_mechanical_standdown(agent, 2819, _STANDDOWN_SHA)
    agent._vcs.rev_parse.return_value = ""  # unresolvable head
    entry = PendingPRRequest(
        directive="Remediate PR #2819: address comments",
        pr_branch="maestro/evolve/ep2819",
    )
    assert agent._mechanical_standdown_verdict(entry) is None


def test_mechanical_standdown_verdict_none_when_blocker_unreadable(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent.is_pr_settled = _open_merge_truth  # type: ignore[attr-defined,method-assign]
    # Partial laps with no parseable ``merge blocker:`` marker AND a live
    # observation confirming no blocker code (all readiness fields clear) —
    # the merge state is unobservable on either signal, so the actuator
    # never stands down (the suppression-safe direction, #2865).
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2819),
    )
    for i in range(STALLED_REMEDIATION_LAP_CAP):
        agent.memory.record_task(
            TaskRecord(
                task_id=f"lap{i}",
                goal="Remediate PR #2819: address comments",
                plan=[],
                outcome="partial",
                result_summary="resolved on branch",
                errors=[],
                duration_secs=1.0,
                timestamp="2026-06-26T00:00:00+00:00",
                source="external",
                pr_head_sha=_STANDDOWN_SHA,
            ),
        )
    entry = PendingPRRequest(
        directive="Remediate PR #2819: address comments",
        pr_branch="maestro/evolve/ep2819",
    )
    assert agent._mechanical_standdown_verdict(entry) is None


def test_mechanical_standdown_verdict_none_on_unserviced_thread(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    _arm_mechanical_standdown(agent, 2819, _STANDDOWN_SHA)
    # Directive carries a comment/review-problem marker (LLM-only thread work),
    # so ``has_unserviced_threads`` is True and the predicate declines.
    entry = PendingPRRequest(
        directive=(
            "Remediate PR #2819: unresolved AI bug review thread needs a reply"
        ),
        pr_branch="maestro/evolve/ep2819",
    )
    assert agent._mechanical_standdown_verdict(entry) is None


def test_mechanical_standdown_verdict_fires_skipping_non_pr_laps(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent.is_pr_settled = _open_merge_truth  # type: ignore[attr-defined,method-assign]
    # A lap-named blocker now also reaches the live thread observation (#2907);
    # a thread-clear reading keeps the stand-down firing exactly as before.
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2819),
    )
    # Interleave a non-PR lap between two same-SHA AWAITING_PUSH partials — the
    # scan must skip it (not break the run).
    agent.memory.record_task(_awaiting_push_lap("lap0", 2819, _STANDDOWN_SHA))
    agent.memory.record_task(_non_pr_lap("between"))
    agent.memory.record_task(_awaiting_push_lap("lap1", 2819, _STANDDOWN_SHA))
    entry = PendingPRRequest(
        directive="Remediate PR #2819: address comments",
        pr_branch="maestro/evolve/ep2819",
    )
    verdict = agent._mechanical_standdown_verdict(entry)
    assert verdict is not None
    assert verdict.pr == 2819
    assert verdict.head_sha == _STANDDOWN_SHA


def test_mechanical_standdown_verdict_scan_breaks_on_landed_same_pr_lap(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The trailing-partial-lap scan stops at the first non-``partial`` same-PR
    lap. An older landed (``success``) lap for the same PR terminates the walk
    — a non-partial lap means the head advanced and real work landed — while the
    stalled same-SHA partial run ahead of it still fires the stand-down."""
    agent = make_agent()
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2819),
    )
    # Oldest lap: a landed (``success``) lap for the same PR. Walking
    # most-recent-first, the scan counts the trailing partial run then breaks
    # here rather than counting past a lap where the head advanced.
    landed = _awaiting_push_lap("landed", 2819, _STANDDOWN_SHA)
    landed.outcome = "success"
    agent.memory.record_task(landed)
    _arm_mechanical_standdown(agent, 2819, _STANDDOWN_SHA)
    entry = PendingPRRequest(
        directive="Remediate PR #2819: address comments",
        pr_branch="maestro/evolve/ep2819",
    )
    verdict = agent._mechanical_standdown_verdict(entry)
    assert verdict is not None
    assert verdict.pr == 2819
    assert verdict.head_sha == _STANDDOWN_SHA


def test_mechanical_standdown_verdict_owned_conflicting_qualifies(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An owned CONFLICTING blocker no longer disqualifies the candidate
    verdict: a stalled, thread-clear, git-unlanded lineage whose laps name
    CONFLICTING now yields a :class:`StandDownVerdict` from the wrapper — the
    clearability decision has moved out of the predicate, into the salvage
    oracle consulted at the ``_dispose_mechanical_standdown`` seam."""
    agent = make_agent()
    _arm_conflicting_standdown(agent, 2946, _STANDDOWN_SHA)
    entry = PendingPRRequest(
        directive="Remediate PR #2946: address comments",
        pr_branch="maestro/evolve/ep2946",
    )
    verdict = agent._mechanical_standdown_verdict(entry)
    assert verdict is not None
    assert verdict.pr == 2946
    assert verdict.head_sha == _STANDDOWN_SHA


@pytest.mark.asyncio
async def test_mechanical_standdown_verdict_counts_stamped_merge_block_laps(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``[merge-block]`` skip laps now stamp ``pr_head_sha`` (#4218), so two of
    them at an unmoved head extend the same-SHA streak instead of breaking it.
    Their summaries name no ``(merge blocker: <CODE>)``, so the blocker is
    derived from the live readiness observation; the verdict is only a
    candidate — disposal stays governed by the salvage oracle."""
    agent = make_agent()
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
        return_value=(False, MERGE_BLOCK_BEHIND_MAIN),
    )
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent.is_pr_settled = _open_merge_truth  # type: ignore[attr-defined,method-assign]
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2819, branch_behind=True),
    )
    for _ in range(STALLED_REMEDIATION_LAP_CAP):
        assert await agent._gate_pr_remediation_merge_block(
            "Remediate PR #2819: address comments",
            "maestro/evolve/ep2819",
            "external",
        )
    entry = PendingPRRequest(
        directive="Remediate PR #2819: address comments",
        pr_branch="maestro/evolve/ep2819",
    )
    verdict = agent._mechanical_standdown_verdict(entry)
    assert verdict is not None
    assert verdict.head_sha == _STANDDOWN_SHA
    agent.observe_pr_readiness.assert_called_once_with(
        2819, "maestro/evolve/ep2819",
    )


@pytest.mark.asyncio
async def test_mechanical_standdown_fires_skips_llm_session(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When the predicate fires, the remediation session is NEVER invoked, the
    verdict row is written, and a ``partial`` ``[stood-down]`` no-op is recorded."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = False
    _arm_mechanical_standdown(agent, 2819, _STANDDOWN_SHA)
    # A lap-named blocker now also reaches the live thread observation (#2907);
    # a thread-clear reading keeps the stand-down firing exactly as before.
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2819),
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2819: address comments",
        pr_branch="maestro/evolve/ep2819",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]
    # Guard: the dequeue gates must never run — the mechanical actuator returns
    # before the normal remediation route is even considered.
    agent._pr_remediation.dequeue_gates = AsyncMock(  # type: ignore[method-assign]
        return_value=False,
    )

    await agent._run_task_phase()

    # No SDK remediation session, no dequeue-gate walk.
    agent.run.assert_not_awaited()
    agent._pr_remediation.dequeue_gates.assert_not_awaited()
    # The durable (pr, head_sha) verdict was written to the keyed ledger.
    verdict = find_stand_down(2819, _STANDDOWN_SHA, agent.stand_down_verdicts)
    assert verdict is not None
    assert verdict.pr == 2819
    assert verdict.head_sha == _STANDDOWN_SHA
    # A partial [stood-down] no-op lap was recorded for the PR.
    latest = agent.memory.get_recent_tasks(1)[0]
    assert latest.outcome == "partial"
    assert "[stood-down]" in latest.result_summary
    assert latest.pr_head_sha == _STANDDOWN_SHA
    # The entry was consumed (popped) by the disposal.
    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == []


@pytest.mark.parametrize(
    ("branch_landed", "lap_named_blocker"),
    [(True, False), (False, False), (True, True)],
    ids=["merged-readiness-blocker", "closed-readiness-blocker", "merged-lap-blocker"],
)
def test_mechanical_standdown_declines_settled_pr(
    make_agent,  # type: ignore[no-untyped-def]
    branch_landed: bool,
    lap_named_blocker: bool,
) -> None:
    """A settled PR (merged, or human-closed unmerged) never stands down (#4102).

    The host-blind git-subject oracle is pinned *unmerged* — a rebase merge or
    edited-subject squash — yet the readiness chokepoint reads the PR settled,
    so the actuator declines and leaves the entry queued for the dequeue
    MERGED / HOST_CLOSED gates, whether the blocker came from a lap marker or
    the readiness fallback.
    """
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = False
    pr_num = 4102
    if lap_named_blocker:
        _arm_mechanical_standdown(agent, pr_num, _STANDDOWN_SHA)
    else:
        _arm_no_marker_lineage(agent, pr_num, _STANDDOWN_SHA)
    agent._git_pr_merged = MagicMock(return_value=False)  # type: ignore[method-assign]
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(
            pr_num, branch_behind=True, settled=True, branch_landed=branch_landed,
        ),
    )
    entry = PendingPRRequest(
        directive=f"Remediate PR #{pr_num}: address comments",
        pr_branch=f"maestro/evolve/ep{pr_num}",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)
    tasks_before = len(agent.memory.get_recent_tasks(30))

    assert agent._dispose_mechanical_standdown(model) is False

    assert find_stand_down(pr_num, _STANDDOWN_SHA, agent.stand_down_verdicts) is None
    assert len(agent.memory.get_recent_tasks(30)) == tasks_before
    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == [entry]


@pytest.mark.asyncio
async def test_mechanical_standdown_none_runs_llm_session(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When the predicate returns None (here: a below-threshold same-SHA
    streak), the existing LLM remediation path runs unchanged — the session IS
    invoked."""
    agent = make_agent()
    sha = _STANDDOWN_SHA
    agent._vcs.rev_parse.return_value = sha
    agent.is_pr_settled = _open_merge_truth  # type: ignore[attr-defined,method-assign]
    # A single ``partial`` lap — below ``STALLED_REMEDIATION_LAP_CAP`` — so the
    # stalled-streak condition fails, the predicate returns None, and
    # remediation proceeds via the normal route (run()).
    agent.memory.record_task(
        TaskRecord(
            task_id="lap0",
            goal="Remediate PR #2820: resolve conflict",
            plan=[],
            outcome="partial",
            result_summary=(
                "PR #2820 not yet landed (merge blocker: CONFLICTING)."
            ),
            errors=[],
            duration_secs=1.0,
            timestamp="2026-06-26T00:00:00+00:00",
            source="external",
            pr_head_sha=sha,
        ),
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2820: resolve conflict",
        pr_branch="maestro/evolve/ep2820",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)
    agent._pr_remediation.dequeue_gates = AsyncMock(  # type: ignore[method-assign]
        return_value=False,
    )
    agent.run = AsyncMock(return_value=MagicMock(task_id=None))  # type: ignore[method-assign]

    await agent._run_task_phase()

    # The LLM remediation session IS invoked, exactly as today.
    agent.run.assert_awaited_once()
    args, _ = agent.run.await_args
    assert args[0] == "Remediate PR #2820: resolve conflict"
    # No stand-down verdict was authored for the owned lineage.
    assert find_stand_down(2820, sha, agent.stand_down_verdicts) is None


@pytest.mark.asyncio
async def test_mechanical_standdown_new_head_sha_auto_releases(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A new head SHA drops the stalled run below threshold, so the predicate
    returns None and the lineage re-admits to normal remediation (auto-release)."""
    agent = make_agent()
    # Laps were recorded at the old SHA; the branch head has since advanced.
    _arm_mechanical_standdown(agent, 2819, _STANDDOWN_SHA)
    agent._vcs.rev_parse.return_value = "c" * 40  # advanced head
    entry = PendingPRRequest(
        directive="Remediate PR #2819: address comments",
        pr_branch="maestro/evolve/ep2819",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)
    agent._pr_remediation.dequeue_gates = AsyncMock(  # type: ignore[method-assign]
        return_value=False,
    )
    agent.run = AsyncMock(return_value=MagicMock(task_id=None))  # type: ignore[method-assign]

    await agent._run_task_phase()

    # Advanced head => not stalled, no mechanical stand-down: the entry routes
    # to remediation and the session is invoked.
    agent.run.assert_awaited_once()
    args, _ = agent.run.await_args
    assert args[0] == "Remediate PR #2819: address comments"
    # No stand-down verdict authored at the advanced head.
    assert find_stand_down(2819, "c" * 40, agent.stand_down_verdicts) is None
    # Entry consumed on the normal route.
    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == []


# ──────────────────────────────────────────────────────────────────────
# Observation-backed blocker fallback (#2865) — when no lap named its
# ``(merge blocker: <CODE>)`` the actuator observes the live merge-block state
# through ``observe_pr_readiness`` instead of disqualifying the lineage, so the
# eviction gate is fed candidates it was previously starved of.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "readiness_kwargs",
    [
        {"awaiting_approval": True},
        {"branch_behind": True},
        {"branch_conflicting": True},
    ],
)
@pytest.mark.asyncio
async def test_mechanical_standdown_fires_without_lap_marker(
    make_agent,  # type: ignore[no-untyped-def]
    readiness_kwargs: dict[str, bool],
) -> None:
    """A stalled, git-unlanded, thread-clear lineage whose laps named NO blocker
    stands down when the live observation confirms the merge-block state —
    whatever its ownership (clearability is the salvage oracle's question, and
    the salvage flag is pinned off here, so the probe never runs). The
    verdict lands, no SDK session opens, and the read-side gate evicts a later
    pop of the same head."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = False
    # An older differing-SHA partial lap sits under the qualifying same-SHA run
    # so the trailing-run scan exercises its streak break.
    _arm_no_marker_lineage(agent, 2819, _STANDDOWN_SHA, extra_older_sha="d" * 40)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2819, **readiness_kwargs),
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2819: address comments",
        pr_branch="maestro/evolve/ep2819",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)

    disposed = agent._dispose_mechanical_standdown(model)

    assert disposed is True
    # The entry was popped and exactly one verdict landed on the keyed ledger.
    assert agent.consciousness.load_self_model().pending_pr_requests == []
    verdict = find_stand_down(2819, _STANDDOWN_SHA, agent.stand_down_verdicts)
    assert verdict is not None
    ledger = agent.config.memory_dir / "stand_down_verdicts.jsonl"
    with open(ledger, encoding="utf-8") as f:
        rows = [line for line in f if line.strip()]
    assert len(rows) == 1
    # A zero-cost partial [stood-down] no-op was recorded (no SDK session).
    latest = agent.memory.get_recent_tasks(1)[0]
    assert latest.outcome == "partial"
    assert "[stood-down]" in latest.result_summary
    # A later pop of the same (pr, head_sha) is evicted by the read-side gate,
    # which re-verifies git shows the head unlanded and Tend's to clear. Probed
    # with BEHIND_MAIN — the sole Tend-clearable blocker (AWAITING_PUSH is owned,
    # proposal 098, and no longer evicts here — pinned by the sibling below).
    agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
        return_value=(False, MERGE_BLOCK_BEHIND_MAIN),
    )
    evicted = await agent._gate_pr_remediation_stood_down(
        entry.directive, entry.pr_branch, "external",
    )
    assert evicted is True


@pytest.mark.asyncio
async def test_mechanical_standdown_write_survives_read_release(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The write/read asymmetry the owned-push partition introduces (098).

    The mechanical actuator's *write* side is ownership-blind — its clearability
    question belongs to the salvage oracle, not the ownership partition — so the
    verdict is still authored and the entry still popped. The *read* side is not:
    an unpushed on-branch resolution is owned, so ``AWAITING_PUSH`` is not in
    ``_tend_clearable_blockers`` and a later pop probing that blocker is no longer
    evicted by the stand-down gate — the brake releases and the lap runs. The
    BEHIND_MAIN eviction case above pins the complementary honored path.
    """
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = False
    _arm_no_marker_lineage(agent, 2822, _STANDDOWN_SHA)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2822, awaiting_approval=True),
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2822: address comments",
        pr_branch="maestro/evolve/ep2822",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)

    disposed = agent._dispose_mechanical_standdown(model)

    # Write side unchanged by the flag: the verdict landed and the entry popped.
    assert disposed is True
    assert agent.consciousness.load_self_model().pending_pr_requests == []
    assert find_stand_down(2822, _STANDDOWN_SHA, agent.stand_down_verdicts) is not None

    # Read side released: the same verdict no longer evicts an AWAITING_PUSH pop.
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
        return_value=(False, MERGE_BLOCK_AWAITING_PUSH),
    )
    evicted = await agent._gate_pr_remediation_stood_down(
        entry.directive, entry.pr_branch, "external",
    )
    assert evicted is False


def test_mechanical_standdown_no_marker_owned_observation_salvage_vetoes(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A stalled no-marker lineage whose live observation reads CONFLICTING
    still does not stand down — but the refusal now comes from the salvage
    oracle, not blocker ownership: the observed merge state qualifies the
    candidate verdict, and the real ``_assess_pr_salvage`` path (a
    code-clearable conflict on work whose diff probe reads substantive)
    returns SALVAGE_AND_LAND, vetoing the write. No verdict, the entry stays
    queued for the normal remediation route."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    _arm_no_marker_lineage(agent, 2820, _STANDDOWN_SHA)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2820, branch_conflicting=True),
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2820: address comments",
        pr_branch="maestro/evolve/ep2820",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)

    disposed = agent._dispose_mechanical_standdown(model)

    assert disposed is False
    assert model.pending_pr_requests == [entry]
    assert find_stand_down(2820, _STANDDOWN_SHA, agent.stand_down_verdicts) is None


def test_mechanical_standdown_no_marker_unknown_observation_refuses(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A stalled no-marker lineage whose live observation confirms NO blocker
    code (a CI-pending / already-clear / unclassifiable state sets none of the
    three fields) never stands down — the suppression-safe direction."""
    agent = make_agent()
    _arm_no_marker_lineage(agent, 2821, _STANDDOWN_SHA)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2821),
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2821: address comments",
        pr_branch="maestro/evolve/ep2821",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)

    disposed = agent._dispose_mechanical_standdown(model)

    assert disposed is False
    assert model.pending_pr_requests == [entry]
    assert find_stand_down(2821, _STANDDOWN_SHA, agent.stand_down_verdicts) is None


def test_mechanical_standdown_verdict_none_on_live_unresolved_thread(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#2907: a live unresolved review thread blocks the mechanical stand-down
    even when the sole merge blocker is external and the directive carries no
    thread marker.

    The lineage is stalled, git-unlanded, and marker-absent — its generic
    ``address comments`` directive trips none of
    ``_COMMENT_REVIEW_PROBLEM_MARKERS``, so the directive-marker proxy reads
    thread-clear — yet the live observation reports ``unresolved_threads=1``
    while ``awaiting_approval``. That thread is owned work to answer or refute,
    not an external gate a merge-blocker status word can excuse, so the actuator
    must decline: no ``[stood-down]`` verdict is authored and the entry stays
    queued for the normal remediation route. Before the fix the marker proxy
    read thread-clear and the external blocker fired a mechanical stand-down,
    abandoning the thread.
    """
    agent = make_agent()
    _arm_no_marker_lineage(agent, 2824, _STANDDOWN_SHA)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(
            2824, awaiting_approval=True, unresolved_threads=1,
        ),
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2824: address comments",
        pr_branch="maestro/evolve/ep2824",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)

    disposed = agent._dispose_mechanical_standdown(model)

    assert disposed is False
    assert model.pending_pr_requests == [entry]
    assert find_stand_down(2824, _STANDDOWN_SHA, agent.stand_down_verdicts) is None


def test_mechanical_standdown_no_marker_below_cap_skips_observation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Oracle-last: a no-marker lineage whose same-SHA run is below the stall
    threshold never pays the live readiness read — the observation is consulted
    only after the cheap stall condition holds (mirroring the git oracle the pure
    predicate consults last)."""
    agent = make_agent()
    _arm_no_marker_lineage(
        agent, 2822, _STANDDOWN_SHA, lap_count=STALLED_REMEDIATION_LAP_CAP - 1,
    )
    agent.observe_pr_readiness = MagicMock()  # type: ignore[method-assign]
    entry = PendingPRRequest(
        directive="Remediate PR #2822: address comments",
        pr_branch="maestro/evolve/ep2822",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)

    disposed = agent._dispose_mechanical_standdown(model)

    assert disposed is False
    assert model.pending_pr_requests == [entry]
    agent.observe_pr_readiness.assert_not_called()


def test_mechanical_standdown_no_marker_unserviced_thread_skips_observation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Oracle-last: a stalled no-marker lineage whose directive still carries an
    unserviced review thread never pays the live readiness read — answering
    threads is LLM-only work a stand-down must not skip, so the cheap thread-clear
    condition gates the observation just like the streak does."""
    agent = make_agent()
    _arm_no_marker_lineage(agent, 2823, _STANDDOWN_SHA)
    agent.observe_pr_readiness = MagicMock()  # type: ignore[method-assign]
    entry = PendingPRRequest(
        directive="Remediate PR #2823: unresolved AI bug review threads remain",
        pr_branch="maestro/evolve/ep2823",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)

    disposed = agent._dispose_mechanical_standdown(model)

    assert disposed is False
    assert model.pending_pr_requests == [entry]
    agent.observe_pr_readiness.assert_not_called()


def test_mechanical_standdown_none_on_reply_completion_directive(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#2893 cross-cut: a reply-completion directive carrying
    ``_REPLY_COMPLETION_PROBLEM_MARKER`` — now folded into
    ``_COMMENT_REVIEW_PROBLEM_MARKERS`` — flips ``has_unserviced_threads`` True,
    so the mechanical stand-down never fires. Posting a terminal reply to an
    outdated thread is LLM-only work a stand-down must not skip. The lineage is
    otherwise fully armed (stalled, AWAITING_PUSH-stamped, git-unlanded), so the
    reply-completion marker is the sole reason both the pure predicate and the
    actuator decline — the entry stays queued and no verdict is written."""
    from maestro.loop.pr_remediation import _REPLY_COMPLETION_PROBLEM_MARKER

    agent = make_agent()
    _arm_mechanical_standdown(agent, 2893, _STANDDOWN_SHA)
    entry = PendingPRRequest(
        directive=(
            f"Remediate PR #2893: PR #2893 has 1 {_REPLY_COMPLETION_PROBLEM_MARKER}"
            ": stale finding (a.py, comment 1)."
        ),
        pr_branch="maestro/evolve/ep2893",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)

    assert agent._mechanical_standdown_verdict(entry) is None
    assert agent._dispose_mechanical_standdown(model) is False
    assert model.pending_pr_requests == [entry]
    assert find_stand_down(2893, _STANDDOWN_SHA, agent.stand_down_verdicts) is None


# ──────────────────────────────────────────────────────────────────────
# Chosen (LLM-narrated ``[stood-down]``) stand-down write path: two vetoes
# gate the durable verdict at the sole chosen write site in
# ``_record_remediation_outcome`` — the salvage oracle (proposal 094; an open
# AI review thread keeps the PR from ready-and-waiting, subsuming the retired
# threads-only veto of #2907) and the human-demand veto (proposal 091).
# ──────────────────────────────────────────────────────────────────────


def _stood_down_record(pr_num: int) -> TaskRecord:
    """A chosen ``[stood-down]`` remediation lap for PR #pr_num (no commit)."""
    return TaskRecord(
        task_id=f"sd{pr_num}",
        goal=f"Remediate PR #{pr_num}: address comments",
        plan=[],
        outcome="partial",
        result_summary=(
            f"[stood-down] Git-confirmed #{pr_num} head unlanded + Tend sole "
            "pusher; chose silence at the fork rather than re-cure."
        ),
        errors=[],
        duration_secs=0.0,
        timestamp="2026-06-26T00:00:00+00:00",
        source="external",
        key_findings="[stood-down] chose silence",
    )


@pytest.mark.parametrize(
    ("readiness_kwargs", "expected_blocker"),
    [
        # Approval wait + open thread → promoted to THREADS_UNRESOLVED.
        ({"awaiting_approval": True}, MERGE_BLOCK_THREADS_UNRESOLVED),
        # A conflict names its own still-blocking gate — not promoted.
        ({"branch_conflicting": True}, MERGE_BLOCK_CONFLICTING),
        # Behind-main names its own gate — not promoted.
        ({"branch_behind": True}, MERGE_BLOCK_BEHIND_MAIN),
        # No merge blocker at all → base NONE → promoted to THREADS_UNRESOLVED.
        ({}, MERGE_BLOCK_THREADS_UNRESOLVED),
    ],
)
@pytest.mark.asyncio
async def test_chosen_standdown_refused_while_thread_open(
    make_agent,  # type: ignore[no-untyped-def]
    readiness_kwargs: dict[str, bool],
    expected_blocker: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``[stood-down]`` lap must NOT persist its verdict while an AI review
    thread is still unresolved: the open thread keeps the PR from
    ready-and-waiting, so the salvage oracle — the flag-on gate that subsumed
    the retired threads-only veto (#2907, proposal 094) — reads
    ``SALVAGE_AND_LAND`` on non-disposable work and refuses the chosen write.
    No row lands in ``stand_down_verdicts.jsonl`` and the PR stays eligible
    for the next tend lap. Parametrised over every merge-block shape the
    log-naming derivation reads. The state is still classified STOOD_DOWN, so
    the refusal (not a misclassification) is what voids it. A single loud
    WARNING must name the owned blocker and thread count (fail-loud, not a
    silent pass)."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(
            2907, unresolved_threads=1, **readiness_kwargs,
        ),
    )
    # Pin the oracle's disposability half (non-disposable work); the open
    # thread keeps the readiness from ready-and-waiting, so the *real* oracle
    # returns SALVAGE_AND_LAND from the mocked observation.
    agent._pr_disposability = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    with caplog.at_level(logging.WARNING, logger="maestro"):
        await agent._record_remediation_outcome(
            goal="Remediate PR #2907: address comments",
            pr_branch="maestro/evolve/ep2907",
            record=_stood_down_record(2907),
            head_before=_STANDDOWN_SHA,  # no commit → genuine stand-down
        )

    assert agent._last_remediation_state is PrRemediationState.STOOD_DOWN
    assert find_stand_down(2907, _STANDDOWN_SHA, agent.stand_down_verdicts) is None
    agent.observe_pr_readiness.assert_called_once_with(
        2907, "maestro/evolve/ep2907",
    )
    # Fail-loud: exactly one WARNING naming the owned blocker + the PR.
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Chosen stand-down" in r.getMessage()
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert expected_blocker in message
    assert "#2907" in message
    assert "unresolved AI review thread(s)" in message
    assert "salvageable" in message


@pytest.mark.asyncio
async def test_chosen_standdown_salvage_veto_conflict_omits_thread_clause(
    make_agent,  # type: ignore[no-untyped-def]
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A conflict blocker with zero open threads logs the blocker name alone.

    The salvage veto fires on a CONFLICTING branch that is not
    ready-and-waiting and non-disposable, even with ``unresolved_threads == 0``.
    The WARNING must name the blocker alone — never the misleading
    "CONFLICTING: 0 unresolved AI review thread(s)" the unconditional thread
    clause produced (ledger r1). The verdict is still refused."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2945, branch_conflicting=True),
    )
    # Non-disposable + not ready-and-waiting (a live conflict, no thread) → the
    # real oracle reads SALVAGE_AND_LAND with zero threads open.
    agent._pr_disposability = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    with caplog.at_level(logging.WARNING, logger="maestro"):
        await agent._record_remediation_outcome(
            goal="Remediate PR #2945: resolve conflict",
            pr_branch="maestro/evolve/ep2945",
            record=_stood_down_record(2945),
            head_before=_STANDDOWN_SHA,
        )

    assert find_stand_down(2945, _STANDDOWN_SHA, agent.stand_down_verdicts) is None
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Chosen stand-down" in r.getMessage()
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert MERGE_BLOCK_CONFLICTING in message
    assert "salvageable" in message
    # The bug: an unconditional thread clause would read
    # "CONFLICTING: 0 unresolved AI review thread(s)".
    assert "unresolved AI review thread(s)" not in message
    assert "0 unresolved" not in message


@pytest.mark.asyncio
async def test_chosen_standdown_persists_when_threads_clear(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With every code-clearable blocker clear (ready-and-waiting: threads
    resolved, sole gate an external approval), the *real* salvage oracle reads
    ``DISENGAGE_PERMITTED`` even on non-disposable work and the ``[stood-down]``
    lap persists the durable ``(pr, head_sha)`` verdict — a genuinely external
    quiet."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    # Branch not ahead of origin (conftest default count 0 → EXTERNAL); mock the
    # ahead-of-origin guard's fetch-first seam so no subprocess runs.
    agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[method-assign]
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_ready_and_waiting(2908),
    )
    # Non-disposable work: disengagement is earned by ready-and-waiting alone.
    agent._pr_disposability = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    await agent._record_remediation_outcome(
        goal="Remediate PR #2908: address comments",
        pr_branch="maestro/evolve/ep2908",
        record=_stood_down_record(2908),
        head_before=_STANDDOWN_SHA,
    )

    verdict = find_stand_down(2908, _STANDDOWN_SHA, agent.stand_down_verdicts)
    assert verdict is not None
    assert verdict.pr == 2908
    assert verdict.head_sha == _STANDDOWN_SHA


@pytest.mark.asyncio
async def test_chosen_standdown_flag_off_writes_without_observing(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With ``salvage_before_disengage_enabled`` off (pinned; default is on),
    the verdict persists and the readiness observation is never made — even
    though a thread is open. This flag-off shape is deliberate (proposal 094):
    the retired threads-only veto does not come back with the flag — the
    chosen write keeps only the human-demand veto, and the rollback path for
    the retirement is ``git revert``, not the flag toggle."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = False
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[method-assign]
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(
            2909, awaiting_approval=True, unresolved_threads=1,
        ),
    )

    await agent._record_remediation_outcome(
        goal="Remediate PR #2909: address comments",
        pr_branch="maestro/evolve/ep2909",
        record=_stood_down_record(2909),
        head_before=_STANDDOWN_SHA,
    )

    verdict = find_stand_down(2909, _STANDDOWN_SHA, agent.stand_down_verdicts)
    assert verdict is not None
    assert verdict.pr == 2909
    agent.observe_pr_readiness.assert_not_called()


def test_pending_request_references_pr_matches_and_ignores() -> None:
    """The PR-reference predicate matches ``#N`` and ``PR N`` forms, ignores
    non-matches, and handles several PRs named in one message (proposal 091)."""
    assert _pending_request_references_pr(["PR #123 still broken"], 123) is True
    assert _pending_request_references_pr(["please look at PR 123"], 123) is True
    # Several PRs in one message veto each of them.
    assert (
        _pending_request_references_pr(["#123 and #456 both stalled"], 456)
        is True
    )
    # A match in any queued entry counts.
    assert _pending_request_references_pr(["nothing here", "#123"], 123) is True
    # A different PR number does not match.
    assert _pending_request_references_pr(["PR #124 is the one"], 123) is False
    # No reference at all — a bare number without ``#``/``PR`` is not a match.
    assert _pending_request_references_pr(["issue 123 noted"], 123) is False
    assert _pending_request_references_pr([], 123) is False


@pytest.mark.asyncio
async def test_chosen_standdown_refused_while_user_request_names_pr(
    make_agent,  # type: ignore[no-untyped-def]
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``[stood-down]`` lap must NOT persist its verdict while an unconsumed
    ``pending_user_requests`` message names the PR — even when the salvage
    oracle permits disengagement. A human demand naming the PR is an absolute
    veto (proposal 091), not subsumed by the oracle: no row lands in
    ``stand_down_verdicts.jsonl`` and one loud WARNING names the PR and the
    queued-message condition."""
    agent = make_agent()
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2910, awaiting_approval=True),
    )
    # The oracle affirmatively permits disengagement — the SOLE veto is the
    # queued human message.
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.DISENGAGE_PERMITTED,
    )
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["PR #2910 is still broken — please fix"]
    agent.consciousness.save_self_model(model)

    with caplog.at_level(logging.WARNING, logger="maestro"):
        await agent._record_remediation_outcome(
            goal="Remediate PR #2910: address comments",
            pr_branch="maestro/evolve/ep2910",
            record=_stood_down_record(2910),
            head_before=_STANDDOWN_SHA,
        )

    assert find_stand_down(2910, _STANDDOWN_SHA, agent.stand_down_verdicts) is None
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Chosen stand-down" in r.getMessage()
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "#2910" in message
    assert "queued human message" in message


@pytest.mark.asyncio
async def test_chosen_standdown_persists_when_user_request_names_other_pr(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A queued message naming a *different* PR does not veto: with the salvage
    flag off the verdict persists exactly as when the queue is empty."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = False
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[method-assign]
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["PR #9999 is unrelated"]
    agent.consciousness.save_self_model(model)

    await agent._record_remediation_outcome(
        goal="Remediate PR #2911: address comments",
        pr_branch="maestro/evolve/ep2911",
        record=_stood_down_record(2911),
        head_before=_STANDDOWN_SHA,
    )

    verdict = find_stand_down(2911, _STANDDOWN_SHA, agent.stand_down_verdicts)
    assert verdict is not None
    assert verdict.pr == 2911


@pytest.mark.asyncio
async def test_chosen_standdown_user_request_flag_off_writes(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With ``user_request_blocks_standdown_enabled`` off, a queued message
    naming the PR is ignored: with the salvage flag off too, the verdict
    persists as today."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = False
    agent.config.user_request_blocks_standdown_enabled = False
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[method-assign]
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["PR #2912 is still broken"]
    agent.consciousness.save_self_model(model)

    await agent._record_remediation_outcome(
        goal="Remediate PR #2912: address comments",
        pr_branch="maestro/evolve/ep2912",
        record=_stood_down_record(2912),
        head_before=_STANDDOWN_SHA,
    )

    verdict = find_stand_down(2912, _STANDDOWN_SHA, agent.stand_down_verdicts)
    assert verdict is not None
    assert verdict.pr == 2912


@pytest.mark.asyncio
async def test_chosen_standdown_refused_with_both_salvage_and_user_request(
    make_agent,  # type: ignore[no-untyped-def]
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When BOTH vetoes fire — a salvageable PR (open AI thread on
    non-disposable work) AND a queued human message naming the PR — the single
    WARNING must join both reasons with `` and `` (proposals 091, 094). No
    verdict lands, and the compound narration names the owned thread blocker,
    the thread-count phrasing, and the queued-message condition so neither
    reason is silently dropped from the log."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    # A live open thread keeps the PR from ready-and-waiting → salvage veto via
    # the real oracle (disposability pinned); the merge blocker is left clear so
    # the owned blocker promotes to THREADS_UNRESOLVED.
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2913, unresolved_threads=1),
    )
    agent._pr_disposability = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["PR #2913 is still broken — please fix"]
    agent.consciousness.save_self_model(model)

    with caplog.at_level(logging.WARNING, logger="maestro"):
        await agent._record_remediation_outcome(
            goal="Remediate PR #2913: address comments",
            pr_branch="maestro/evolve/ep2913",
            record=_stood_down_record(2913),
            head_before=_STANDDOWN_SHA,
        )

    assert find_stand_down(2913, _STANDDOWN_SHA, agent.stand_down_verdicts) is None
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Chosen stand-down" in r.getMessage()
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "#2913" in message
    # Both reasons present, joined by " and " — neither dropped.
    assert MERGE_BLOCK_THREADS_UNRESOLVED in message
    assert "unresolved AI review thread(s)" in message
    assert "queued human message" in message
    assert " and " in message


def test_mechanical_standdown_vetoed_by_user_request(
    make_agent,  # type: ignore[no-untyped-def]
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unconsumed ``pending_user_requests`` message naming the PR vetoes the
    mechanical stand-down too (proposal 091): ``_dispose_mechanical_standdown``
    returns False without popping, the entry stays queued for the normal
    remediation route, and no verdict is written."""
    agent = make_agent()
    _arm_mechanical_standdown(agent, 2819, _STANDDOWN_SHA)
    # A thread-clear reading keeps the verdict non-None, so the human-demand
    # veto is the sole reason the disposal declines.
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2819),
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2819: address comments",
        pr_branch="maestro/evolve/ep2819",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    model.pending_user_requests = ["PR #2819 is still broken"]

    with caplog.at_level(logging.WARNING, logger="maestro"):
        disposed = agent._dispose_mechanical_standdown(model)

    assert disposed is False
    assert model.pending_pr_requests == [entry]
    assert find_stand_down(2819, _STANDDOWN_SHA, agent.stand_down_verdicts) is None
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "mechanical stand-down on PR #2819 vetoed" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_mechanical_standdown_user_request_flag_off_fires(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag off, a queued message naming the PR is ignored: the
    mechanical stand-down fires as today — the entry is popped and the durable
    verdict written."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = False
    agent.config.user_request_blocks_standdown_enabled = False
    _arm_mechanical_standdown(agent, 2819, _STANDDOWN_SHA)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2819),
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2819: address comments",
        pr_branch="maestro/evolve/ep2819",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    model.pending_user_requests = ["PR #2819 is still broken"]
    agent.consciousness.save_self_model(model)

    disposed = agent._dispose_mechanical_standdown(model)

    assert disposed is True
    assert agent.consciousness.load_self_model().pending_pr_requests == []
    verdict = find_stand_down(2819, _STANDDOWN_SHA, agent.stand_down_verdicts)
    assert verdict is not None
    assert verdict.pr == 2819


# ──────────────────────────────────────────────────────────────────────
# Salvage-before-disengage (proposal 094): both stand-down writers first
# consult the pure ``assess_pr_salvage`` oracle behind
# ``salvage_before_disengage_enabled`` (default-on). A ``SALVAGE_AND_LAND``
# verdict — a code-clearable blocker remains on non-disposable work — vetoes
# the give-up; ``DISENGAGE_PERMITTED`` leaves each writer's behavior intact.
# ──────────────────────────────────────────────────────────────────────


def _impl_plan(pr_num: int, *, title: str, rationale: str) -> EvolutionPlan:
    """An ``implemented`` plan whose ``pr_url`` names PR #pr_num."""
    return EvolutionPlan(
        plan_id=f"ep{pr_num}",
        title=title,
        category="capability",
        rationale=rationale,
        proposed_changes=["change one"],
        files_affected=["maestro/agent.py"],
        expected_impact="steadier",
        status="implemented",
        pr_url=f"https://github.com/o/r/pull/{pr_num}",
    )


def _ready_and_waiting(pr_number: int) -> PRReadiness:
    """A :class:`PRReadiness` whose every code-clearable blocker is clear."""
    return PRReadiness(
        pr_number=pr_number,
        branch_landed=False,
        branch_behind=False,
        branch_conflicting=False,
        ci_state="GREEN",
        unresolved_threads=0,
        unaddressed_overflow=0,
        awaiting_approval=True,
        ready_and_waiting=True,
        source="host_api",
        observed_at=123.0,
    )


@pytest.mark.asyncio
async def test_chosen_standdown_refused_while_salvageable(
    make_agent,  # type: ignore[no-untyped-def]
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Flag on: a SALVAGE_AND_LAND PR vetoes the chosen stand-down even with
    threads clear and no queued human message — the salvage oracle is the sole
    veto. No verdict lands and one loud WARNING names the salvage reason. The
    already-fetched readiness is reused (passed through as the ``readiness``
    kwarg)."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    # Threads clear (an external conflict blocker, no open thread) so threads_veto
    # is False — salvageability is the only thing that can void the write.
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2940, branch_conflicting=True),
    )
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.SALVAGE_AND_LAND,
    )

    with caplog.at_level(logging.WARNING, logger="maestro"):
        await agent._record_remediation_outcome(
            goal="Remediate PR #2940: address comments",
            pr_branch="maestro/evolve/ep2940",
            record=_stood_down_record(2940),
            head_before=_STANDDOWN_SHA,
        )

    assert find_stand_down(2940, _STANDDOWN_SHA, agent.stand_down_verdicts) is None
    agent._assess_pr_salvage.assert_called_once_with(
        2940, "maestro/evolve/ep2940",
        readiness=agent.observe_pr_readiness.return_value,
        unpersisted=ANY,
    )
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Chosen stand-down" in r.getMessage()
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "#2940" in message
    assert "salvageable" in message


@pytest.mark.asyncio
async def test_chosen_standdown_persists_when_disengage_permitted(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on: a DISENGAGE_PERMITTED PR leaves the chosen write untouched — with
    threads clear the durable verdict persists exactly as today."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[method-assign]
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2941, awaiting_approval=True),
    )
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.DISENGAGE_PERMITTED,
    )

    await agent._record_remediation_outcome(
        goal="Remediate PR #2941: address comments",
        pr_branch="maestro/evolve/ep2941",
        record=_stood_down_record(2941),
        head_before=_STANDDOWN_SHA,
    )

    verdict = find_stand_down(2941, _STANDDOWN_SHA, agent.stand_down_verdicts)
    assert verdict is not None
    assert verdict.pr == 2941
    agent._assess_pr_salvage.assert_called_once()


@pytest.mark.asyncio
async def test_chosen_standdown_persists_with_open_threads_when_disengage_permitted(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The corner the retired threads-only veto over-blocked (proposal 094):
    flag on, an open AI review thread, but the oracle judges the work
    disposable → ``DISENGAGE_PERMITTED`` — the durable verdict now persists,
    so worthless work with an open thread is no longer kept alive."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[method-assign]
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2944, unresolved_threads=1),
    )
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.DISENGAGE_PERMITTED,
    )

    await agent._record_remediation_outcome(
        goal="Remediate PR #2944: address comments",
        pr_branch="maestro/evolve/ep2944",
        record=_stood_down_record(2944),
        head_before=_STANDDOWN_SHA,
    )

    verdict = find_stand_down(2944, _STANDDOWN_SHA, agent.stand_down_verdicts)
    assert verdict is not None
    assert verdict.pr == 2944
    agent._assess_pr_salvage.assert_called_once_with(
        2944, "maestro/evolve/ep2944",
        readiness=agent.observe_pr_readiness.return_value,
        unpersisted=ANY,
    )


@pytest.mark.asyncio
async def test_chosen_standdown_salvage_flag_off_skips_probe(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off (pinned; default is now on): the salvage oracle is never
    consulted — no readiness observation is made — and the verdict persists."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = False
    assert agent.config.salvage_before_disengage_enabled is False
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[method-assign]
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.SALVAGE_AND_LAND,
    )

    await agent._record_remediation_outcome(
        goal="Remediate PR #2942: address comments",
        pr_branch="maestro/evolve/ep2942",
        record=_stood_down_record(2942),
        head_before=_STANDDOWN_SHA,
    )

    verdict = find_stand_down(2942, _STANDDOWN_SHA, agent.stand_down_verdicts)
    assert verdict is not None
    agent._assess_pr_salvage.assert_not_called()


def test_mechanical_standdown_refused_while_salvageable(
    make_agent,  # type: ignore[no-untyped-def]
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Flag on: a SALVAGE_AND_LAND PR vetoes the mechanical stand-down — the
    actuator returns False without popping, no verdict lands, and one loud
    WARNING names the salvage reason. The entry stays queued for the normal
    remediation route."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    _arm_mechanical_standdown(agent, 2943, _STANDDOWN_SHA)
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.SALVAGE_AND_LAND,
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2943: address comments",
        pr_branch="maestro/evolve/ep2943",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)

    with caplog.at_level(logging.WARNING, logger="maestro"):
        disposed = agent._dispose_mechanical_standdown(model)

    assert disposed is False
    assert model.pending_pr_requests == [entry]
    assert find_stand_down(2943, _STANDDOWN_SHA, agent.stand_down_verdicts) is None
    agent._assess_pr_salvage.assert_called_once_with(2943, "maestro/evolve/ep2943")
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "mechanical stand-down on PR #2943 vetoed" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "salvageable" in warnings[0].getMessage()


def test_mechanical_standdown_fires_when_disengage_permitted(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on: a DISENGAGE_PERMITTED PR leaves the mechanical actuator's behavior
    intact — the verdict is authored, the entry popped, disposal returns True."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    _arm_mechanical_standdown(agent, 2944, _STANDDOWN_SHA)
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.DISENGAGE_PERMITTED,
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2944: address comments",
        pr_branch="maestro/evolve/ep2944",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)

    disposed = agent._dispose_mechanical_standdown(model)

    assert disposed is True
    assert agent.consciousness.load_self_model().pending_pr_requests == []
    verdict = find_stand_down(2944, _STANDDOWN_SHA, agent.stand_down_verdicts)
    assert verdict is not None
    assert verdict.pr == 2944
    agent._assess_pr_salvage.assert_called_once_with(2944, "maestro/evolve/ep2944")


def test_mechanical_standdown_salvage_flag_off_skips_probe(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off (pinned; default is now on): the salvage oracle is never consulted and the
    mechanical actuator behaves exactly as today — the verdict fires."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = False
    assert agent.config.salvage_before_disengage_enabled is False
    _arm_mechanical_standdown(agent, 2945, _STANDDOWN_SHA)
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.SALVAGE_AND_LAND,
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2945: address comments",
        pr_branch="maestro/evolve/ep2945",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)

    disposed = agent._dispose_mechanical_standdown(model)

    assert disposed is True
    assert agent.consciousness.load_self_model().pending_pr_requests == []
    agent._assess_pr_salvage.assert_not_called()


def test_mechanical_standdown_owned_conflicting_salvage_veto(
    make_agent,  # type: ignore[no-untyped-def]
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Flag on: an owned CONFLICTING lineage qualifies at the wrapper, and the
    salvage oracle is what vetoes the give-up on non-disposable work
    (SALVAGE_AND_LAND): no verdict lands, the entry stays queued for the
    normal remediation route, and one loud WARNING names the salvage reason."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    _arm_conflicting_standdown(agent, 2947, _STANDDOWN_SHA)
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.SALVAGE_AND_LAND,
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2947: address comments",
        pr_branch="maestro/evolve/ep2947",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)

    with caplog.at_level(logging.WARNING, logger="maestro"):
        disposed = agent._dispose_mechanical_standdown(model)

    assert disposed is False
    assert model.pending_pr_requests == [entry]
    assert find_stand_down(2947, _STANDDOWN_SHA, agent.stand_down_verdicts) is None
    agent._assess_pr_salvage.assert_called_once_with(2947, "maestro/evolve/ep2947")
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "mechanical stand-down on PR #2947 vetoed" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "salvageable" in warnings[0].getMessage()


def test_mechanical_standdown_owned_conflicting_disposable_stands_down(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on: the same owned CONFLICTING lineage IS stood down when the work
    is disposable (DISENGAGE_PERMITTED) — the behavioral delta this increment
    delivers: a conflicting duplicate or empty-diff PR is released at zero
    cost for the first time, instead of being re-routed into a full
    remediation session lap after lap."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    _arm_conflicting_standdown(agent, 2948, _STANDDOWN_SHA)
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.DISENGAGE_PERMITTED,
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2948: address comments",
        pr_branch="maestro/evolve/ep2948",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)

    disposed = agent._dispose_mechanical_standdown(model)

    assert disposed is True
    assert agent.consciousness.load_self_model().pending_pr_requests == []
    verdict = find_stand_down(2948, _STANDDOWN_SHA, agent.stand_down_verdicts)
    assert verdict is not None
    assert verdict.pr == 2948
    agent._assess_pr_salvage.assert_called_once_with(2948, "maestro/evolve/ep2948")


# ──────────────────────────────────────────────────────────────────────
# Orphaned-cure retirement salvage veto (proposal 094) — the last autonomous
# PR-backing-work retirement routes through the same ``assess_pr_salvage``
# oracle at its ``_audit_self_model`` caller: behind
# ``salvage_before_disengage_enabled``, a plan backing a SALVAGE_AND_LAND open
# own PR is left ``implemented`` instead of retired.
# ──────────────────────────────────────────────────────────────────────


def _open_pr_state(number: int, head_ref: str) -> PrMergeState:
    """A minimal open ``PrMergeState`` for the orphaned-cure snapshot."""
    return PrMergeState(
        number=number, head_ref=head_ref, title="t", body="b", state="OPEN",
        mergeable="CONFLICTING", merge_state_status="DIRTY",
        review_decision="", created_at="", updated_at="", url="",
    )


def _seed_orphaned_cure(  # type: ignore[no-untyped-def]
    agent, mem_fs, *, pr_url="https://github.com/o/r/pull/1",
) -> None:
    """Seed an aged, conflict-evidenced ``implemented`` cure backing PR #1.

    Mirrors the ``test_agent_lifecycle`` orphaned-cure seed: an ``implemented``
    plan (whose ``pr_url`` names PR #1 by default) with tend-confirmed conflict
    evidence and an age past the retire ceiling, plus the message store and
    PR-comment poster the disposition delivery needs so retirement can fire when
    salvage permits. Pass ``pr_url=None`` for a cure with no PR URL, or an
    unparseable string, to exercise the salvage predicate's permissive guards.
    """
    from maestro.messaging import MessageStore

    agent.memory.save_evolution_plan(
        EvolutionPlan(
            plan_id="ep_cure",
            title="Add the cure",
            category="capability",
            rationale="r",
            proposed_changes=["x"],
            files_affected=["maestro/x.py"],
            expected_impact="none",
            status="implemented",
        ),
    )
    if pr_url is None:
        agent.memory.update_plan_status(
            "ep_cure",
            "implemented",
            last_conflict_resolution_attempt_at="2026-01-01T00:00:00+00:00",
        )
    else:
        agent.memory.update_plan_status(
            "ep_cure",
            "implemented",
            pr_url=pr_url,
            last_conflict_resolution_attempt_at="2026-01-01T00:00:00+00:00",
        )
    mem_fs[str(agent.config.memory_dir / "orphaned_cure_first_seen.json")] = (
        json.dumps({"ep_cure": 0})
    )
    agent.message_store = MessageStore(agent.config.memory_dir)
    agent.evolution._host.comment_issue = MagicMock(return_value=True)


@pytest.mark.asyncio
async def test_audit_orphaned_cure_salvage_flag_off_retires(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off (pinned; default is now on): the injected predicate always permits and the salvage
    probe never runs — an aged conflict-evidenced cure retires exactly as
    today."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = False
    assert agent.config.salvage_before_disengage_enabled is False
    _seed_orphaned_cure(agent, mem_fs)
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.SALVAGE_AND_LAND,
    )

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=40)

    reloaded = agent.memory.get_plan("ep_cure")
    assert reloaded is not None
    assert reloaded.status == "obsolete"
    # Parity: with the flag off the oracle is never consulted.
    agent._assess_pr_salvage.assert_not_called()


@pytest.mark.asyncio
async def test_audit_orphaned_cure_salvage_and_land_skips_retire(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on: a plan backing a SALVAGE_AND_LAND open own PR is not retired — it
    stays ``implemented`` and no disposition is delivered (the salvage veto is
    ordered before delivery)."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    _seed_orphaned_cure(agent, mem_fs)
    agent.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "maestro/evolve/ep_cure": _open_pr_state(1, "maestro/evolve/ep_cure"),
        },
    )
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.SALVAGE_AND_LAND,
    )

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=40)

    reloaded = agent.memory.get_plan("ep_cure")
    assert reloaded is not None
    assert reloaded.status == "implemented"
    agent._assess_pr_salvage.assert_called_once_with(1, "maestro/evolve/ep_cure")
    # Salvage veto ordered before delivery: no disposition comment posted.
    agent.evolution._host.comment_issue.assert_not_called()


@pytest.mark.asyncio
async def test_audit_orphaned_cure_disengage_permitted_retires(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on: a plan backing a DISENGAGE_PERMITTED open own PR retires as today
    — the salvage oracle permits and the disposition is delivered."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    _seed_orphaned_cure(agent, mem_fs)
    agent.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "maestro/evolve/ep_cure": _open_pr_state(1, "maestro/evolve/ep_cure"),
        },
    )
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.DISENGAGE_PERMITTED,
    )

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=40)

    reloaded = agent.memory.get_plan("ep_cure")
    assert reloaded is not None
    assert reloaded.status == "obsolete"
    agent._assess_pr_salvage.assert_called_once_with(1, "maestro/evolve/ep_cure")


@pytest.mark.asyncio
async def test_audit_orphaned_cure_pr_absent_from_snapshot_retires(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on: a plan whose PR is absent from the open-owned snapshot (closed or
    merged) is genuinely dead — the predicate permits without consulting the
    oracle, and the cure retires."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    _seed_orphaned_cure(agent, mem_fs)
    agent.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        return_value={},
    )
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.SALVAGE_AND_LAND,
    )

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=40)

    reloaded = agent.memory.get_plan("ep_cure")
    assert reloaded is not None
    assert reloaded.status == "obsolete"
    # A PR absent from the snapshot is closed/merged and never reaches the oracle.
    agent._assess_pr_salvage.assert_not_called()


@pytest.mark.asyncio
async def test_audit_orphaned_cure_listing_failure_vetoes_retire(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """#4107: a failed open-PR listing confirms nothing — the cure behind the
    PR is not retired, no disposition is delivered, and no stand-down is
    recorded, even when a best-effort consumer ran first and degraded to
    ``{}`` (pre-fix, that ``{}`` was memoized and read as "PR closed")."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    _seed_orphaned_cure(agent, mem_fs)

    def _listing(prefix: str, *, strict: bool = False) -> dict[str, object]:
        if strict:
            raise VCSError("HTTP 502")
        return {}

    agent.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        side_effect=_listing,
    )
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.SALVAGE_AND_LAND,
    )

    agent._current_task_focus()
    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=40)

    reloaded = agent.memory.get_plan("ep_cure")
    assert reloaded is not None
    assert reloaded.status == "implemented"
    agent.evolution._host.comment_issue.assert_not_called()
    assert has_standdown(agent.escalation_standdowns, 1) is False
    assert agent._open_owned_pr_states_memo is None
    agent._assess_pr_salvage.assert_not_called()


def test_failed_listing_not_memoized_as_empty_snapshot(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#4107: a failed listing is never memoized as a trusted ``{}``; the
    failure is remembered for the iteration (best-effort → ``{}``, strict →
    re-raise) so an outage costs one ``gh pr list``. Once the iteration reset
    clears it, a successful listing is memoized and served without re-listing.
    """
    agent = make_agent()
    listing = MagicMock(side_effect=VCSError("HTTP 502"))
    agent.evolution.code_host.get_open_evolution_pr_states = listing

    agent._current_task_focus()
    assert agent._open_owned_pr_states_memo is None
    with pytest.raises(VCSError):
        agent._open_owned_pr_states(strict=True)
    assert agent._open_owned_pr_states(strict=False) == {}
    assert listing.call_count == 1
    listing.assert_called_with("maestro/evolve/", strict=True)

    agent._open_owned_pr_states_error = None
    state = _open_pr_state(1, "maestro/evolve/ep1")
    listing.side_effect = None
    listing.return_value = {"maestro/evolve/ep1": state}
    assert agent._open_owned_pr_states(strict=False) == {
        "maestro/evolve/ep1": state,
    }
    assert agent._open_owned_pr_states(strict=True) == {
        "maestro/evolve/ep1": state,
    }
    assert listing.call_count == 2


@pytest.mark.asyncio
async def test_audit_orphaned_cure_salvage_flag_on_no_pr_url_permits(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on: a cure with no PR URL has nothing to protect — the salvage
    predicate permits without consulting the oracle (the delivery guard then
    leaves the plan implemented for its own reason)."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    _seed_orphaned_cure(agent, mem_fs, pr_url=None)
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.SALVAGE_AND_LAND,
    )

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=40)

    reloaded = agent.memory.get_plan("ep_cure")
    assert reloaded is not None
    assert reloaded.status == "implemented"
    # An absent PR URL short-circuits the predicate before the oracle.
    agent._assess_pr_salvage.assert_not_called()


@pytest.mark.asyncio
async def test_audit_orphaned_cure_salvage_flag_on_unparseable_pr_url_permits(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on: a cure whose PR URL cannot be parsed has no number to protect —
    the salvage predicate catches the ValueError and permits without consulting
    the oracle."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    _seed_orphaned_cure(
        agent, mem_fs, pr_url="https://github.com/o/r/pull/not-a-number",
    )
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.SALVAGE_AND_LAND,
    )

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=40)

    reloaded = agent.memory.get_plan("ep_cure")
    assert reloaded is not None
    assert reloaded.status == "implemented"
    # An unparseable PR URL short-circuits the predicate before the oracle.
    agent._assess_pr_salvage.assert_not_called()


@pytest.mark.asyncio
async def test_audit_owned_act_drought_salvage_flag_off_passes_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off (pinned; default is now on): the drought dispatch passes ``salvage_disqualifies``
    as ``None`` — the obsolete half is counted by timestamp alone, byte-for-byte
    today's behavior (proposal 094 step 4)."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = False
    assert agent.config.salvage_before_disengage_enabled is False

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks.check_owned_act_drought", return_value=[],
        ) as mock_check,
    ):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=40)

    mock_check.assert_called_once()
    assert mock_check.call_args.kwargs["salvage_disqualifies"] is None


@pytest.mark.asyncio
async def test_audit_owned_act_drought_salvage_flag_on_supplies_closure(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on: the dispatch supplies a salvage closure that disqualifies an
    obsolete event only when its ``pr_url`` names an open own PR the loop could
    still land (``SALVAGE_AND_LAND``). An empty/unparseable URL, a PR absent from
    the open-owned snapshot, and a ``DISENGAGE_PERMITTED`` PR all keep crediting
    the event — the inverse polarity of ``_salvage_permits_retire`` (step 4)."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = True
    agent.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "maestro/evolve/ep1": _open_pr_state(1, "maestro/evolve/ep1"),
        },
    )
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.SALVAGE_AND_LAND,
    )

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks.check_owned_act_drought", return_value=[],
        ) as mock_check,
    ):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=40)

    predicate = mock_check.call_args.kwargs["salvage_disqualifies"]
    assert predicate is not None

    # Reset the per-iteration snapshot so the first call lazily fills it (covers
    # the ``states is None`` fill branch), then exercise every polarity.
    agent._open_owned_pr_states_memo = None
    # Open, salvageable PR → disqualify (the obsolete event is NOT credited).
    assert predicate("https://github.com/o/r/pull/1") is True
    agent._assess_pr_salvage.assert_called_once_with(1, "maestro/evolve/ep1")
    # A PR absent from the (now-filled) snapshot is closed/merged → credit it.
    assert predicate("https://github.com/o/r/pull/2") is False
    # An empty URL protects nothing → credit the event.
    assert predicate("") is False
    # An unparseable URL raises ValueError → credit the event.
    assert predicate("https://github.com/o/r/pull/not-a-number") is False
    # A DISENGAGE_PERMITTED open PR is give-up-permitted → credit it.
    agent._assess_pr_salvage = MagicMock(  # type: ignore[method-assign]
        return_value=Salvage.DISENGAGE_PERMITTED,
    )
    assert predicate("https://github.com/o/r/pull/1") is False
    # A failed listing confirms nothing → disqualify, never memoized (#4107).
    agent._open_owned_pr_states_memo = None
    agent.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        side_effect=VCSError("HTTP 502"),
    )
    assert predicate("https://github.com/o/r/pull/1") is True
    assert agent._open_owned_pr_states_memo is None


def test_assess_pr_salvage_observes_and_returns_salvage_and_land(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """No readiness passed: ``_assess_pr_salvage`` observes it via the chokepoint,
    reads a not-ready fact plus a non-disposable predicate, and — with the branch
    within the reconcile budget — returns SALVAGE_AND_LAND."""
    agent = make_agent()
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2950, branch_conflicting=True),
    )
    agent._pr_disposability = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )
    # A conflicting branch only three commits behind is within the reconcile
    # budget → the budget-aware disposal never fires, so salvage stands.
    agent.evolution._observe_pr_staleness = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(commits_behind=3),
    )

    verdict = agent._assess_pr_salvage(2950, "maestro/evolve/ep2950")

    assert verdict is Salvage.SALVAGE_AND_LAND
    agent.observe_pr_readiness.assert_called_once_with(
        2950, "maestro/evolve/ep2950",
    )
    agent._pr_disposability.assert_called_once_with(2950, "maestro/evolve/ep2950")
    agent.evolution._observe_pr_staleness.assert_called_once_with(
        "maestro/evolve/ep2950",
    )


def test_assess_pr_salvage_past_budget_conflicting_permits_disengage(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A CONFLICTING own PR measured 77 commits behind resolves to
    DISENGAGE_PERMITTED — the adapter threads the observed behind-count into the
    oracle, lifting the ``gh pr close`` veto on a branch that re-conflicts before
    it can land (proposal: budget-aware salvage oracle).

    Under the graduated (default-on) base-advance re-entry keying the behind-budget
    arm also requires a same-``(head, base)`` failure streak at the cap, so the
    lineage is seeded with ``cap`` such laps and ``origin/main`` is pinned to the
    laps' recorded base — distance past the budget on a base that stood still."""
    agent = make_agent()
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2952, branch_conflicting=True),
    )
    agent._pr_disposability = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )
    agent.evolution._observe_pr_staleness = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(commits_behind=77),
    )
    agent._vcs.rev_parse.side_effect = _stall_rev_parse
    for i in range(STALLED_REMEDIATION_LAP_CAP):
        agent.memory.record_task(
            _remediation_lap(
                task_id=f"lap{i}", pr_num=2952, base_head_sha=_STALL_BASE,
            ),
        )

    verdict = agent._assess_pr_salvage(2952, "maestro/evolve/ep2952")

    assert verdict is Salvage.DISENGAGE_PERMITTED
    agent.evolution._observe_pr_staleness.assert_called_once_with(
        "maestro/evolve/ep2952",
    )


def test_assess_pr_salvage_branchless_pr_skips_staleness_measurement(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A branchless PR (no head to measure) leaves the behind-count ``None``, so
    the staleness observer is never called and salvage stands (fail-safe)."""
    agent = make_agent()
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2953, branch_conflicting=True),
    )
    agent._pr_disposability = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )
    agent.evolution._observe_pr_staleness = MagicMock(  # type: ignore[method-assign]
    )

    verdict = agent._assess_pr_salvage(2953, None)

    assert verdict is Salvage.SALVAGE_AND_LAND
    agent.evolution._observe_pr_staleness.assert_not_called()


def test_assess_pr_salvage_reuses_provided_readiness(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A readiness passed by the caller is reused — the chokepoint is not
    re-observed — and a ready-and-waiting fact yields DISENGAGE_PERMITTED."""
    agent = make_agent()
    agent.observe_pr_readiness = MagicMock()  # type: ignore[method-assign]
    agent._pr_disposability = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )
    agent.evolution._observe_pr_staleness = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(commits_behind=0),
    )

    verdict = agent._assess_pr_salvage(
        2951, "maestro/evolve/ep2951", readiness=_ready_and_waiting(2951),
    )

    assert verdict is Salvage.DISENGAGE_PERMITTED
    agent.observe_pr_readiness.assert_not_called()


def test_pr_disposability_restatement_assembles_backing_and_siblings(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The backing plan (matched by ``pr_number``) restating a sibling makes the
    PR disposable. Exercises the full live-corpus assembly: an implemented+pr_url
    backing plan, a sibling with identical text, a ``pending_review`` plan and an
    implemented plan whose ``pr_url`` is unparseable — both now live *siblings*
    (their ``pr_number`` never matches *pr_num*), no longer skipped."""
    agent = make_agent()
    shared = "Add a salvage oracle gating the two stand-down writers on readiness"
    bad_url = EvolutionPlan(
        plan_id="ep-bad",
        title="Salvage oracle",
        category="capability",
        rationale=shared,
        proposed_changes=["change one"],
        files_affected=["maestro/agent.py"],
        expected_impact="steadier",
        status="implemented",
        pr_url="not-a-valid-pr-url",
    )
    pending = EvolutionPlan(
        plan_id="ep-pending",
        title="unrelated pending",
        category="capability",
        rationale="totally different subject about telemetry rollup deltas",
        proposed_changes=["x"],
        files_affected=["maestro/x.py"],
        expected_impact="n/a",
        status="pending_review",
        pr_url="https://github.com/o/r/pull/8888",
    )
    agent.memory.get_live_plans = MagicMock(  # type: ignore[method-assign]
        return_value=select_live_plans([
            _impl_plan(2960, title="Salvage oracle", rationale=shared),
            _impl_plan(7777, title="Salvage oracle", rationale=shared),
            pending,
            bad_url,
        ]),
    )
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent._vcs.get_diff_stat.return_value = " maestro/agent.py | 5 +++++"

    assert agent._pr_disposability(2960, "maestro/evolve/ep2960") is True


def test_pr_disposability_approved_sibling_without_pr_is_visible(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An ``approved`` plan that has not yet opened a PR is now a live *sibling*.

    The live-work oracle surfaces ``approved`` plans, so the backing plan
    restating one drives a ``True`` disposability verdict — precisely the state
    the old ``implemented``-and-``pr_url`` filter dropped, since such a plan is
    neither ``implemented`` nor carries a ``pr_url`` (``pr_number`` is ``None``,
    so it never matches *pr_num* and always lands in the sibling set)."""
    agent = make_agent()
    shared = "Add a salvage oracle gating the two stand-down writers on readiness"
    approved_no_pr = EvolutionPlan(
        plan_id="ep-approved",
        title="Salvage oracle",
        category="capability",
        rationale=shared,
        proposed_changes=["change one"],
        files_affected=["maestro/agent.py"],
        expected_impact="steadier",
        status="approved",
        pr_url=None,
    )
    agent.memory.get_live_plans = MagicMock(  # type: ignore[method-assign]
        return_value=select_live_plans([
            _impl_plan(2965, title="Salvage oracle", rationale=shared),
            approved_no_pr,
        ]),
    )
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent._vcs.get_diff_stat.return_value = " maestro/agent.py | 5 +++++"

    assert agent._pr_disposability(2965, "maestro/evolve/ep2965") is True


def test_pr_disposability_empty_diff_is_disposable(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """No restatement, both refs resolve, but the branch diff stat is empty → no
    substantive change to land → disposable."""
    agent = make_agent()
    agent.memory.get_live_plans = MagicMock(  # type: ignore[method-assign]
        return_value=select_live_plans([]),
    )
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent._vcs.get_diff_stat.return_value = "   "

    assert agent._pr_disposability(2961, "maestro/evolve/ep2961") is True


def test_pr_disposability_unresolved_ref_not_disposable(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An unresolved ref leaves ``has_substantive_diff`` unknown (None), so the
    predicate fails toward salvage: the PR is not disposable and no diff is
    computed."""
    agent = make_agent()
    agent.memory.get_live_plans = MagicMock(  # type: ignore[method-assign]
        return_value=select_live_plans([]),
    )
    agent._vcs.rev_parse.return_value = None

    assert agent._pr_disposability(2962, "maestro/evolve/ep2962") is False
    agent._vcs.get_diff_stat.assert_not_called()


def test_pr_disposability_no_branch_is_unknown_diff(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With no PR branch, the branch head is unknown without probing git, so
    ``has_substantive_diff`` stays unknown and the PR is not disposable."""
    agent = make_agent()
    agent.memory.get_live_plans = MagicMock(  # type: ignore[method-assign]
        return_value=select_live_plans([]),
    )
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA

    assert agent._pr_disposability(2963, None) is False


def test_pr_disposability_novel_pr_with_real_diff_not_disposable(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The common salvageable path: both refs resolve, the branch carries a
    substantive diff, and the backing plan restates no sibling → the PR is *not*
    disposable. Exercises the full assembly with a real backing plan plus an
    unrelated sibling that does not restate it, so the restatement branch runs and
    finds no match — the counterpart to the empty-diff / unresolved-ref cases that
    all fail toward salvage through ``has_substantive_diff`` being unknown."""
    agent = make_agent()
    agent.memory.get_live_plans = MagicMock(  # type: ignore[method-assign]
        return_value=select_live_plans([
            _impl_plan(
                2964,
                title="Salvage oracle gating stand-down writers on readiness",
                rationale="Route both give-up actuators through assess_pr_salvage",
            ),
            _impl_plan(
                7778,
                title="Telemetry rollup deltas surfaced in contemplation",
                rationale="Aggregate usage rows into per-cycle burn-rate summaries",
            ),
        ]),
    )
    agent._vcs.rev_parse.return_value = _STANDDOWN_SHA
    agent._vcs.get_diff_stat.return_value = " maestro/agent.py | 12 ++++++++++++"

    assert agent._pr_disposability(2964, "maestro/evolve/ep2964") is False


# ──────────────────────────────────────────────────────────────────────
# Proposal 105 — mechanical outcome authority authoring (unconditional). For
# every remediation-PR record, run() computes a git-grounded landing verdict
# (``outcome_authority.decide_outcome``) alongside the settled narration path,
# logs every divergence to the ``gate_activations.jsonl`` ledger, and then ADOPTS
# the git-truth verdict as the recorded outcome. These tests assert the authored
# outcome plus the ledger row (whose ``narr=`` field is the pre-adoption
# narration verdict). A non-remediation record is never landing-relevant, so no
# row runs.
# ──────────────────────────────────────────────────────────────────────


def _mechanical_rows(
    mem_fs: dict[str, str], agent: MaestroAgent,
) -> list[dict[str, object]]:
    """The ``mechanical_outcome_authority`` activation rows written by *agent*."""
    raw = mem_fs.get(
        str(agent.config.memory_dir / "gate_activations.jsonl"), "",
    )
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return [r for r in rows if r["gate"] == "mechanical_outcome_authority"]


async def _mock_q_neutral(*args: object, **kwargs: object) -> MockQueryResult:
    """A remediation summary carrying NO resolution keyword, so no prose-keyed
    scrub touches it and a would-be ``success`` reaches the §5d git-grounded
    authority with its narration intact for the authority to override."""
    return MockQueryResult(response="Reviewed the PR branch and pushed an update.")


def _arm_phantom_success(agent: MaestroAgent) -> None:
    """Drive the narration path to ``success`` while the merge-truth chokepoint
    reports the branch NOT landed — the phantom the mechanical authority flags.

    The closure probe reports landed and the host reports the PR merged, so the
    §4c gate leaves the ``success`` stamp untouched; ``is_pr_settled`` stays the
    conftest default (``branch_landed=False``), so ``decide_outcome`` reads
    ``partial``. Narration and the mechanical authority therefore disagree.
    """
    agent.config.enable_session_reuse = False
    agent._vcs.has_changes.return_value = True
    agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
        return_value=(True, None),
    )
    agent.evolution._host.is_pr_merged = MagicMock(  # type: ignore[attr-defined]
        return_value=True,
    )


@pytest.mark.asyncio
async def test_authority_divergence_records_diverged_row(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """When git-truth disagrees with the narration: the authority AUTHORS the
    git-truth verdict, so ``record.outcome`` is the mechanical ``partial`` (not the
    narration ``success``), and a ``diverged`` row is written whose ``narr=``
    field is the pre-adoption narration verdict and ``mech=`` the git-truth one,
    naming both plus the PR."""
    agent = make_agent()
    _arm_phantom_success(agent)

    with patch("maestro.agent.subprocess.run", MagicMock()), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q_neutral), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q_neutral):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    # The authority now authors the recorded verdict from git-truth: the
    # phantom-``success`` narration is overwritten with the mechanical ``partial``.
    assert record.outcome == "partial"
    rows = _mechanical_rows(mem_fs, agent)
    assert len(rows) == 1
    assert rows[0]["action"] == "diverged"
    # The ledger still names both verdicts — ``narr=`` is the pre-adoption prose.
    assert "narr=success" in rows[0]["detail"]
    assert "mech=partial" in rows[0]["detail"]
    assert "#2647" in rows[0]["detail"]


@pytest.mark.asyncio
async def test_authority_agreement_records_agree_row(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """When git-truth agrees with the narration: the authored verdict equals the
    narration one, so ``record.outcome`` is unchanged and an ``agree`` row is
    written. The awaiting-push narration forces ``partial`` and the chokepoint
    (default not-landed) reads ``partial`` too — authoring is a no-op when they
    coincide."""
    agent = make_agent()
    _arm_awaiting_push(agent)

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    assert record.outcome == "partial"
    rows = _mechanical_rows(mem_fs, agent)
    assert len(rows) == 1
    assert rows[0]["action"] == "agree"
    assert "narr=partial" in rows[0]["detail"]
    assert "mech=partial" in rows[0]["detail"]


@pytest.mark.asyncio
async def test_authority_non_remediation_writes_no_row(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """For a task with no PR-landing dimension: the authority observes only
    landing-relevant records, so no ``is_pr_settled`` call is made and NO
    activation row is written. A non-remediation task can never diverge
    (``decide_outcome`` returns the candidate unchanged when not
    landing-relevant), so its guaranteed-``agree`` row would only dilute the
    divergence rate the ledger exists to measure."""
    agent = make_agent()
    agent.config.enable_session_reuse = False

    with patch("maestro.agent.subprocess.run", MagicMock()), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q_neutral), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q_neutral):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Investigate loop cadence patterns", source="self_directed",
        )

    assert record.outcome == "success"
    assert _mechanical_rows(mem_fs, agent) == []


@pytest.mark.asyncio
async def test_authority_authors_success_when_chokepoint_confirms_landed(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """When the merge-truth chokepoint confirms the branch landed: the
    authority authors ``success`` from git-truth, even though the narration path
    was heading toward an awaiting-push ``partial``. Mirrors
    ``test_prestamp_gate_landed_success_survives`` in the landing direction."""
    agent = make_agent()
    _arm_awaiting_push(agent)
    # The chokepoint confirms #2647 merged → branch landed, so the authority
    # reads ``landing_confirmed=True`` and decides ``success``.
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=2647, settled=True, state="MERGED",
            branch_landed=True, source="host_api", observed_at=0.0,
        ),
    )

    with patch("maestro.agent.subprocess.run", MagicMock()), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    # git-truth (landed) is the recorded verdict.
    assert record.outcome == "success"
    # The authority computed ``success`` from the chokepoint's landing fact.
    rows = _mechanical_rows(mem_fs, agent)
    assert len(rows) == 1
    assert "mech=success" in rows[0]["detail"]


@pytest.mark.asyncio
async def test_authority_authors_partial_when_prose_claims_unlanded_landing(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """When the narration reaches ``success`` on a PR the chokepoint reports
    NOT landed: the authority authors ``partial`` from git-truth, overriding the
    prose. The ``diverged`` row proves the narration verdict was ``success`` while
    the authored one is ``partial`` — the recorded outcome is independent of the
    prose."""
    agent = make_agent()
    _arm_phantom_success(agent)
    # The chokepoint reports #2647 NOT landed, so the authority reads
    # ``landing_confirmed=False`` and decides ``partial``.
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=2647, settled=False, state="OPEN",
            branch_landed=False, source="host_api", observed_at=0.0,
        ),
    )

    with patch("maestro.agent.subprocess.run", MagicMock()), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q_neutral), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q_neutral):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    # git-truth (not landed) is the recorded verdict, not the phantom ``success``.
    assert record.outcome == "partial"
    rows = _mechanical_rows(mem_fs, agent)
    assert len(rows) == 1
    assert rows[0]["action"] == "diverged"
    assert "narr=success" in rows[0]["detail"]
    assert "mech=partial" in rows[0]["detail"]


async def _remediation_outcome(agent: MaestroAgent) -> str:
    """Run a #2647 remediation through ``agent.run`` and return the recorded
    outcome. The narration is the resolved-on-branch default (``_mock_q``)."""
    with patch("maestro.agent.subprocess.run", MagicMock()), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )
    return record.outcome


@pytest.mark.asyncio
async def test_authority_records_honest_verdict_on_honest_scenarios(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """On scenarios where prose and git-truth agree, the git-grounded authority
    records the honest verdict unconditionally: an awaiting-push agree case
    records ``partial`` (the narration ``partial`` matches the not-landed
    chokepoint) and a chokepoint-confirmed landed case records ``success``."""

    def _arm_landed(agent: MaestroAgent) -> None:
        agent.config.enable_session_reuse = False
        agent._vcs.has_changes.return_value = True
        agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
            return_value=MergeTruth(
                pr_number=2647, settled=True, state="MERGED",
                branch_landed=True, source="host_api", observed_at=0.0,
            ),
        )

    # Agree case: the awaiting-push narration ``partial`` matches the not-landed
    # chokepoint, so the authority records ``partial``.
    agree_agent = make_agent()
    _arm_awaiting_push(agree_agent)
    assert await _remediation_outcome(agree_agent) == "partial"

    # Landed case: the chokepoint confirms the branch landed, so the authority
    # authors ``success`` from git-truth.
    landed_agent = make_agent()
    _arm_landed(landed_agent)
    assert await _remediation_outcome(landed_agent) == "success"


# ---------------------------------------------------------------------------
# _refine_ci_review_blocker — the not-landed verdict's CI/review label split.
# The composition point run() records: it reads the memoized readiness fact and
# the per-check verdict, then hands both to classify_merge_blocker_label so the
# recorded ``merge blocker:`` names the real gate rather than a flat CI_PENDING.
# ---------------------------------------------------------------------------


def _ci_review_readiness(
    pr_number: int, *, ci_state: str, awaiting_approval: bool,
) -> PRReadiness:
    """A minimal readiness fact steering the CI/review label split."""
    return PRReadiness(
        pr_number=pr_number,
        branch_landed=False,
        branch_behind=False,
        branch_conflicting=False,
        ci_state=ci_state,  # type: ignore[arg-type]
        unresolved_threads=0,
        unaddressed_overflow=0,
        awaiting_approval=awaiting_approval,
        ready_and_waiting=False,
        source="host_api",
        observed_at=123.0,
    )


def test_refine_green_unapproved_records_awaiting_review(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A green-checks, unapproved PR records AWAITING_REVIEW, never CI_PENDING.

    ``_probe_pr_merge_block`` reads a green-but-unapproved PR as
    ``AWAITING_APPROVAL``; the split re-reads the readiness fact (GREEN CI, review
    still outstanding) and records the review-grounded ``AWAITING_REVIEW`` — the
    honest hand-to-Thomas gate, distinct from a CI wait.
    """
    agent = make_agent()
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_ci_review_readiness(
            4100, ci_state="GREEN", awaiting_approval=True,
        ),
    )
    agent._observe_pr_check_runs = MagicMock(  # type: ignore[method-assign]
        return_value=(
            CheckRun(name="tests", conclusion="success"),
        ),
    )

    label = agent._refine_ci_review_blocker(
        4100, "maestro/evolve/ep4100", MERGE_BLOCK_AWAITING_APPROVAL,
    )

    assert label == MERGE_BLOCK_AWAITING_REVIEW
    assert label != MERGE_BLOCK_CI_PENDING


def test_refine_in_progress_checks_records_ci_running(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A PR with in-progress checks records CI_RUNNING, not the flat CI_PENDING."""
    agent = make_agent()
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_ci_review_readiness(
            4101, ci_state="PENDING", awaiting_approval=False,
        ),
    )
    agent._observe_pr_check_runs = MagicMock(  # type: ignore[method-assign]
        return_value=(),  # no named failing check → RUNNING
    )

    label = agent._refine_ci_review_blocker(
        4101, "maestro/evolve/ep4101", MERGE_BLOCK_CI_PENDING,
    )

    assert label == MERGE_BLOCK_CI_RUNNING


def test_refine_discards_none_verdict_keeps_probe_code(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A timing-skew NONE verdict is discarded so a not-landed lap never says NONE.

    If the checks flip green-and-approved between the probe and the readiness
    re-read, ``classify_merge_blocker_label`` returns NONE — but the lap is
    not-landed, so recording "blocker: NONE" would contradict itself. The split
    keeps the original probe code instead.
    """
    agent = make_agent()
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_ci_review_readiness(
            4103, ci_state="GREEN", awaiting_approval=False,
        ),
    )
    agent._observe_pr_check_runs = MagicMock(  # type: ignore[method-assign]
        return_value=(CheckRun(name="tests", conclusion="success"),),
    )

    label = agent._refine_ci_review_blocker(
        4103, "maestro/evolve/ep4103", MERGE_BLOCK_AWAITING_APPROVAL,
    )

    assert label == MERGE_BLOCK_AWAITING_APPROVAL


def test_refine_leaves_git_side_blocker_untouched(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A git-side blocker (CONFLICTING) names a different gate and passes through.

    The readiness/CI reads are never consulted for a non-CI/review blocker, so a
    CONFLICTING probe verdict is recorded verbatim.
    """
    agent = make_agent()
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError("readiness must not be read for a git blocker"),
    )

    label = agent._refine_ci_review_blocker(
        4102, "maestro/evolve/ep4102", MERGE_BLOCK_CONFLICTING,
    )

    assert label == MERGE_BLOCK_CONFLICTING


@pytest.mark.asyncio
async def test_mechanical_standdown_dequeue_preserves_concurrent_append(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#4403: disposal removes only the head from the *live* queue, so an entry
    appended after the caller's snapshot survives; a vanished head authors no
    verdict."""
    agent = make_agent()
    agent.config.salvage_before_disengage_enabled = False
    _arm_no_marker_lineage(agent, 2819, _STANDDOWN_SHA)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_blocker_readiness(2819, branch_behind=True),
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2819: address comments",
        pr_branch="maestro/evolve/ep2819",
    )
    late = PendingPRRequest(directive="late", pr_branch="maestro/evolve/late")
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)
    live = agent.consciousness.load_self_model()
    live.pending_pr_requests.append(late)
    agent.consciousness.save_self_model(live)

    assert agent._dispose_mechanical_standdown(model) is True
    assert agent.consciousness.load_self_model().pending_pr_requests == [late]

    # Head already gone from the live queue: no verdict, no disposal.
    gone = agent.consciousness.load_self_model()
    gone.pending_pr_requests = []
    agent.consciousness.save_self_model(gone)
    model.pending_pr_requests = [entry]
    tasks_before = len(agent.memory.get_recent_tasks(50))
    assert agent._dispose_mechanical_standdown(model) is False
    assert len(agent.memory.get_recent_tasks(50)) == tasks_before
