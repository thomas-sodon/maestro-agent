"""Committed-but-unpushed arm of the ahead-of-origin push actuator.

The uncommitted-edit arm rescues a dirty working tree; the OWNED_AHEAD
external-blocker arm re-labels a false external quiet. Neither fires the one
deed a *committed* owned head ahead of ``origin/<branch>`` under an owned
AWAITING_PUSH blocker owes: ``git push``. Left to an LLM lap it is narrated
"delivered, awaiting review/merge" and never pushed — the exact
delivered-on-branch-is-not-a-terminal costume. These tests pin the arm that
fires it mechanically at record time: a green committed head is pushed via the
oracle-gated ``_push_owned_branch`` (delivery bound to a same-turn
``verify_push_advanced_remote_head`` read, never the push return), while a RED
gate rewinds the PR branch to origin by name (``reset_branch_to``) so a red head never
reaches origin. Every external is mocked (``mem_fs``, ``_vcs`` subprocess, the
gate runner, ``is_pr_settled``), so nothing shells out.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.consciousness import PendingPRRequest
from maestro.evolution import QualityGateResult
from maestro.gate_observation import (
    GateFailure,
    GateObservation,
    GateRunObservation,
)
from maestro.loop.pr_remediation import _OwnedPushResult
from maestro.memory import TaskRecord
from maestro.pr_facts import MergeTruth
from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import (
    MERGE_BLOCK_AWAITING_PUSH,
    measure_lap_delivery,
)
from maestro.task_queue import QueuedTask
from maestro.vcs import PrMergeState
from tests.conftest import MockQueryResult


def _gate_result(passed: bool) -> QualityGateResult:
    """A structured quality-gate outcome — all sub-gates agree with *passed*."""
    return QualityGateResult(
        passed=passed,
        ruff_ok=passed,
        mypy_ok=passed,
        tests_ok=passed,
        raw_output="",
    )


def _red_observation() -> GateRunObservation:
    """A one-failure red pytest observation whose failure sits in a base file."""
    failure = GateFailure(
        tool="tests",
        kind="FAILED",
        location="maestro/base_module.py",
        identifier="tests/test_base.py::test_x",
        message="assert 1 == 2",
    )
    return GateRunObservation(
        gates=(
            GateObservation(
                tool="tests",
                kind="FAILED",
                failures=(failure,),
                output="FAILED tests/test_base.py::test_x",
            ),
        ),
    )


def _red_result_with_obs() -> QualityGateResult:
    """A red gate result carrying the observation the proposal-124 attributor reads."""
    return QualityGateResult(
        passed=False,
        ruff_ok=True,
        mypy_ok=True,
        tests_ok=False,
        raw_output="",
        observation=_red_observation(),
    )


def _arm_attribution(
    agent: object,
    *,
    changed_paths: tuple[str, ...],
    baseline: GateRunObservation | None,
) -> None:
    """Turn on gate observation and wire the VCS/baseline seams the reader uses."""
    agent.config.gate_observation_enabled = True  # type: ignore[attr-defined]
    agent._vcs.get_head_hash.return_value = "headsha"  # type: ignore[attr-defined]
    agent._vcs.merge_base.return_value = "basesha"  # type: ignore[attr-defined]
    agent._vcs.changed_paths.return_value = changed_paths  # type: ignore[attr-defined]
    agent.evolution._observe_baseline = MagicMock(  # type: ignore[attr-defined]
        return_value=baseline,
    )


def _merge_truth(pr_num: int, *, settled: bool) -> MergeTruth:
    """A provenance-stamped merge-state fact for the re-derivation seam."""
    return MergeTruth(
        pr_number=pr_num,
        settled=settled,
        state="MERGED" if settled else "OPEN",
        branch_landed=settled,
        source="host_api",
        observed_at=0.0,
    )


def _remediation_record(pr_num: int, summary: str) -> TaskRecord:
    """A remediation lap whose LLM summary narrates *summary* (no [stood-down])."""
    return TaskRecord(
        task_id=f"cu{pr_num}",
        goal=f"Remediate PR #{pr_num}: resolve conflict",
        plan=[],
        outcome="partial",
        result_summary=summary,
        errors=[],
        duration_secs=1.0,
        timestamp="2026-07-30T00:00:00+00:00",
        source="external",
        key_findings=summary,
    )


def _own_branch(agent: object, pr_num: int, branch: str) -> None:
    """Register *branch* as an owned-writable evolution head (host + ledger)."""
    agent.evolution._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        branch: object(),
    }
    agent.pr_ledger.observe_head(pr_num, branch, "deadbeef")  # type: ignore[attr-defined]


def _arm_committed_unpushed(
    agent: object, pr_num: int, branch: str, *, ahead: int = 2,
    set_flag: bool = True,
) -> None:
    """Pin the committed-unpushed AWAITING_PUSH shape: owned, clean, ahead, flag on.

    ``set_flag=False`` leaves ``owned_committed_push_actuator_enabled`` untouched so
    the caller can assert the *production default* (promoted to ``True``) routes the
    arm without an explicit per-test enable — the guard the flag flip earns.
    """
    if set_flag:
        agent.config.owned_committed_push_actuator_enabled = True  # type: ignore[attr-defined]
    # Disable the STOOD_DOWN vetoes so nothing else touches the branch.
    agent.config.user_request_blocks_standdown_enabled = False  # type: ignore[attr-defined]
    agent.config.salvage_before_disengage_enabled = False  # type: ignore[attr-defined]
    _own_branch(agent, pr_num, branch)
    # Clean working tree (committed, not an uncommitted edit).
    agent._vcs.has_changes.return_value = False  # type: ignore[attr-defined]
    # Committed ahead of origin, and freshen-remote is a no-op in the memoised
    # count seam.
    agent._branch_ahead_of_origin_count = MagicMock(  # type: ignore[attr-defined]
        return_value=ahead,
    )
    # AWAITING_PUSH blocker recorded on this lap.
    agent._last_remediation_merge_block = MERGE_BLOCK_AWAITING_PUSH  # type: ignore[attr-defined]
    _track_checkout(agent)


def _track_checkout(agent: object, current: str = "main") -> list[str]:
    """Make ``checkout`` move ``get_current_branch``; start on *current*.

    Models the post-``run()`` workspace (teardown left it on ``main``) and
    returns the live list of checked-out branches, in order.
    """
    vcs = agent._vcs  # type: ignore[attr-defined]
    history = [current]

    def _checkout(branch: str) -> None:
        history.append(branch)
        vcs.get_current_branch.return_value = branch

    vcs.get_current_branch.return_value = current
    vcs.get_current_branch.side_effect = None
    vcs.checkout.side_effect = _checkout
    return history


# ---------------------------------------------------------------------------
# Detector: _branch_is_owned_writable
# ---------------------------------------------------------------------------


def test_owned_writable_true_for_registered_head(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An oracle-confirmed evolution head is owned-writable."""
    agent = make_agent()
    branch = "maestro/evolve/ep800"
    _own_branch(agent, 800, branch)

    assert agent._branch_is_owned_writable(branch) is True


def test_owned_writable_false_for_foreign_branch(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A branch the oracle does not confirm is not owned-writable."""
    agent = make_agent()
    agent.evolution._host.get_open_evolution_pr_states.return_value = {}

    assert agent._branch_is_owned_writable("maestro/evolve/foreign") is False


# ---------------------------------------------------------------------------
# Actuator: _push_committed_unpushed_owned_head
# ---------------------------------------------------------------------------


def test_actuator_green_pushes_and_reobserves(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """GREEN → push via ``_push_owned_branch``, marker re-derived fetch-first."""
    agent = make_agent()
    branch = "maestro/evolve/ep810"
    _arm_committed_unpushed(agent, 810, branch)
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=True),
    )
    agent._merge_truth_memo[810] = _merge_truth(810, settled=True)
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_merge_truth(810, settled=False),
    )
    record = _remediation_record(810, "resolved and pushed")

    handled = agent._push_committed_unpushed_owned_head(810, branch, record)

    assert handled is True
    agent._push_owned_branch.assert_called_once_with(branch)
    # The stale per-iteration memo was dropped so the read re-fetched.
    assert 810 not in agent._merge_truth_memo
    agent.is_pr_settled.assert_any_call(810)
    assert Marker.DELIVERED_AWAITING_MERGE in record.result_summary
    assert record.outcome == "partial"


def test_actuator_green_settled_stamps_landed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A pushed head git shows settled coerces to the ``[landed]`` marker."""
    agent = make_agent()
    branch = "maestro/evolve/ep811"
    _arm_committed_unpushed(agent, 811, branch)
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=True),
    )
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_merge_truth(811, settled=True),
    )
    record = _remediation_record(811, "resolved and pushed")

    agent._push_committed_unpushed_owned_head(811, branch, record)

    assert Marker.LANDED in record.result_summary
    assert Marker.DELIVERED_AWAITING_MERGE not in record.result_summary


def test_actuator_delivery_bound_to_verified_advance_not_push_return(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """AC2: a push that exited 0 but did NOT advance the remote head (unconfirmed)
    never stamps ``delivered`` — the [push-unconfirmed] partial is recorded instead.

    ``_push_owned_branch`` binds ``pushed`` to a same-turn
    ``verify_push_advanced_remote_head`` read, so an ``unconfirmed`` result is
    exactly the case where the subprocess returned but the remote head did not
    move. Delivery is unproven; the arm must not narrate it delivered.
    """
    agent = make_agent()
    branch = "maestro/evolve/ep812"
    _arm_committed_unpushed(agent, 812, branch)
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=False, unconfirmed=True),
    )
    record = _remediation_record(812, "resolved and pushed")

    agent._push_committed_unpushed_owned_head(812, branch, record)

    assert Marker.PUSH_UNCONFIRMED in record.result_summary
    assert Marker.DELIVERED_AWAITING_MERGE not in record.result_summary
    assert Marker.LANDED not in record.result_summary
    assert record.outcome == "partial"


def test_actuator_refused_push_falls_back_to_owned_push_partial(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A refused/failed push → the loud OWNED_PUSH_AVAILABLE partial, not delivered."""
    agent = make_agent()
    branch = "maestro/evolve/ep813"
    _arm_committed_unpushed(agent, 813, branch)
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=False, error="remote rejected"),
    )
    record = _remediation_record(813, "resolved and pushed")

    agent._push_committed_unpushed_owned_head(813, branch, record)

    assert Marker.OWNED_PUSH_AVAILABLE in record.result_summary
    assert Marker.DELIVERED_AWAITING_MERGE not in record.result_summary


def test_actuator_red_gate_rewinds_to_origin_and_never_pushes(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """AC3: RED gate → ``reset_branch_to(branch, origin/<branch>)`` rewind, no push, [committed-red]."""
    agent = make_agent()
    branch = "maestro/evolve/ep814"
    _arm_committed_unpushed(agent, 814, branch)
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(False),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]
    record = _remediation_record(814, "resolved and pushed")

    handled = agent._push_committed_unpushed_owned_head(814, branch, record)

    assert handled is True
    agent._vcs.reset_branch_to.assert_called_once_with(
        branch, f"origin/{branch}",
    )
    agent._push_owned_branch.assert_not_called()
    assert record.outcome == "partial"
    assert Marker.COMMITTED_RED in record.result_summary
    assert Marker.COMMITTED_RED in (record.key_findings or "")


def test_actuator_inherited_red_holds_head_and_never_pushes(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """INHERITED red → hold the head: no reset_branch_to, no push, returns True, [inherited-red].

    The identical failure identity is present in a baseline of the branch's own
    base, so the committed head's red belongs to origin/main, not this diff. The
    arm holds the head (no rewind of the branch's own committed work) and records
    a partial naming the inherited base failures — still returning ``True`` so the
    arm is counted as having handled the branch.
    """
    agent = make_agent()
    branch = "maestro/evolve/ep830"
    _arm_committed_unpushed(agent, 830, branch)
    _arm_attribution(
        agent,
        changed_paths=("maestro/other.py",),  # the failing file is NOT in the diff
        baseline=_red_observation(),  # the base carries the identical identity
    )
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_red_result_with_obs(),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]
    record = _remediation_record(830, "resolved and pushed")

    handled = agent._push_committed_unpushed_owned_head(830, branch, record)

    assert handled is True
    agent._vcs.reset_branch_to.assert_not_called()
    agent._push_owned_branch.assert_not_called()
    assert record.outcome == "partial"
    assert Marker.INHERITED_RED in record.result_summary
    assert Marker.INHERITED_RED in (record.key_findings or "")
    assert Marker.COMMITTED_RED not in record.result_summary
    assert "tests:tests/test_base.py::test_x" in record.result_summary


def test_actuator_attributed_red_still_rewinds(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """ATTRIBUTED red → reset_branch_to(branch, origin/<branch>) rewind, [committed-red].

    The failing file *is* in the diff, so the red is the branch's own committed
    work and it is discarded exactly as before.
    """
    agent = make_agent()
    branch = "maestro/evolve/ep831"
    _arm_committed_unpushed(agent, 831, branch)
    _arm_attribution(
        agent,
        changed_paths=("maestro/base_module.py",),  # the failing file IS in the diff
        baseline=_red_observation(),
    )
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_red_result_with_obs(),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]
    record = _remediation_record(831, "resolved and pushed")

    handled = agent._push_committed_unpushed_owned_head(831, branch, record)

    assert handled is True
    agent._vcs.reset_branch_to.assert_called_once_with(
        branch, f"origin/{branch}",
    )
    agent._push_owned_branch.assert_not_called()
    assert Marker.COMMITTED_RED in record.result_summary
    assert Marker.INHERITED_RED not in record.result_summary


def test_actuator_flag_off_red_rewinds_despite_observation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off → the observation is ignored and the rewind is untouched."""
    agent = make_agent()
    branch = "maestro/evolve/ep832"
    _arm_committed_unpushed(agent, 832, branch)
    # Flag stays off (the ``make_agent`` default); wire the seams anyway.
    agent._vcs.get_head_hash.return_value = "headsha"
    agent._vcs.merge_base.return_value = "basesha"
    agent._vcs.changed_paths.return_value = ("maestro/other.py",)
    agent.evolution._observe_baseline = MagicMock(  # type: ignore[method-assign]
        return_value=_red_observation(),
    )
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_red_result_with_obs(),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]
    record = _remediation_record(832, "resolved and pushed")

    handled = agent._push_committed_unpushed_owned_head(832, branch, record)

    assert handled is True
    agent._vcs.reset_branch_to.assert_called_once_with(
        branch, f"origin/{branch}",
    )
    assert Marker.COMMITTED_RED in record.result_summary
    assert Marker.INHERITED_RED not in record.result_summary
    # Flag-off short-circuits before the baseline is ever read.
    agent.evolution._observe_baseline.assert_not_called()


def test_actuator_noop_when_tree_dirty(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A dirty tree is the uncommitted arm's domain — this arm is a strict no-op."""
    agent = make_agent()
    branch = "maestro/evolve/ep815"
    _arm_committed_unpushed(agent, 815, branch)
    agent._vcs.has_changes.return_value = True
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]

    handled = agent._push_committed_unpushed_owned_head(815, branch, None)

    assert handled is False
    # The expensive gate run never fired — the cheap dirty-tree check short-circuits.
    agent.evolution.run_quality_gates.assert_not_called()
    agent._push_owned_branch.assert_not_called()
    agent._vcs.reset_branch_to.assert_not_called()


def test_actuator_noop_when_remote_caught_up(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """AC4: a caught-up remote (ahead == 0) makes the arm self-extinguish."""
    agent = make_agent()
    branch = "maestro/evolve/ep816"
    _arm_committed_unpushed(agent, 816, branch, ahead=0)
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]

    handled = agent._push_committed_unpushed_owned_head(816, branch, None)

    assert handled is False
    agent.evolution.run_quality_gates.assert_not_called()
    agent._push_owned_branch.assert_not_called()


def test_actuator_noop_for_external_blocker(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A non-AWAITING_PUSH (external) blocker is not this arm's push to fire."""
    agent = make_agent()
    branch = "maestro/evolve/ep817"
    _arm_committed_unpushed(agent, 817, branch)
    agent._last_remediation_merge_block = "AWAITING_APPROVAL"
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]

    handled = agent._push_committed_unpushed_owned_head(817, branch, None)

    assert handled is False
    agent.evolution.run_quality_gates.assert_not_called()
    agent._push_owned_branch.assert_not_called()


def test_actuator_noop_for_foreign_branch(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A branch the oracle refuses is never reset or pushed by this arm."""
    agent = make_agent()
    branch = "maestro/evolve/ep818"
    _arm_committed_unpushed(agent, 818, branch)
    # Un-register the branch so the oracle refuses it.
    agent.evolution._host.get_open_evolution_pr_states.return_value = {}
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(False),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]

    handled = agent._push_committed_unpushed_owned_head(818, branch, None)

    assert handled is False
    agent.evolution.run_quality_gates.assert_not_called()
    agent._vcs.reset_branch_to.assert_not_called()
    agent._push_owned_branch.assert_not_called()


# ---------------------------------------------------------------------------
# Integration through _record_remediation_outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_outcome_fires_committed_push_zero_sdk_no_lap(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """AC1: a gate-clean committed-unpushed AWAITING_PUSH head is pushed at record
    time via ``_push_owned_branch`` — even when the LLM narrated 'delivered' — with
    no intervening LLM lap and no stand-down verdict minted."""
    agent = make_agent()
    branch = "maestro/evolve/ep900"
    _arm_committed_unpushed(agent, 900, branch)
    agent._fetch_origin_for_merge_truth = MagicMock()
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=True),
    )
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_merge_truth(900, settled=False),
    )
    # The LLM narrated a delivered/awaiting-merge success — no [stood-down].
    record = _remediation_record(
        900, "Delivered the fix to the PR branch, awaiting review/merge.",
    )

    await agent._record_remediation_outcome(
        goal="Remediate PR #900: resolve conflict",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    agent._push_owned_branch.assert_called_once_with(branch)
    assert Marker.DELIVERED_AWAITING_MERGE in record.result_summary
    # No stand-down verdict minted — the branch was delivered, not deferred.
    assert str(agent.config.memory_dir / "stand_down_verdicts.jsonl") not in mem_fs


@pytest.mark.asyncio
async def test_record_outcome_stood_down_narration_still_pushes_no_standdown(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A [stood-down] narration on a committed-unpushed AWAITING_PUSH head is
    overridden: the arm pushes and the STOOD_DOWN stand-down minting is skipped —
    the deed replaces the external-quiet verdict."""
    agent = make_agent()
    branch = "maestro/evolve/ep903"
    _arm_committed_unpushed(agent, 903, branch)
    agent._fetch_origin_for_merge_truth = MagicMock()
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=True),
    )
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_merge_truth(903, settled=False),
    )
    # The LLM chose a stand-down — but the head is a committed owned push it owns.
    record = _remediation_record(
        903, "[stood-down] awaiting external approval on PR #903.",
    )

    await agent._record_remediation_outcome(
        goal="Remediate PR #903: resolve conflict",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    agent._push_owned_branch.assert_called_once_with(branch)
    # No stand-down verdict minted despite the [stood-down] narration.
    assert str(agent.config.memory_dir / "stand_down_verdicts.jsonl") not in mem_fs


@pytest.mark.asyncio
async def test_record_outcome_committed_red_rewinds(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """AC3 through the record path: a RED committed head is reset to origin and
    never pushed, recording a [committed-red] partial."""
    agent = make_agent()
    branch = "maestro/evolve/ep901"
    _arm_committed_unpushed(agent, 901, branch)
    agent._fetch_origin_for_merge_truth = MagicMock()
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(False),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]
    record = _remediation_record(901, "Delivered, awaiting review.")

    await agent._record_remediation_outcome(
        goal="Remediate PR #901: resolve conflict",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    agent._vcs.reset_branch_to.assert_called_once_with(
        branch, f"origin/{branch}",
    )
    agent._push_owned_branch.assert_not_called()
    assert Marker.COMMITTED_RED in record.result_summary


@pytest.mark.asyncio
async def test_record_outcome_flag_off_arm_inert(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """AC4: with the flag off the arm never fires — no gate run, no push, no reset,
    even on the exact committed-unpushed AWAITING_PUSH shape."""
    agent = make_agent()
    branch = "maestro/evolve/ep902"
    _arm_committed_unpushed(agent, 902, branch)
    agent.config.owned_committed_push_actuator_enabled = False
    agent._fetch_origin_for_merge_truth = MagicMock()
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]
    agent._push_committed_unpushed_owned_head = MagicMock(  # type: ignore[method-assign]
    )
    record = _remediation_record(902, "Delivered, awaiting review.")

    await agent._record_remediation_outcome(
        goal="Remediate PR #902: resolve conflict",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    # The flag short-circuits before the actuator is ever consulted.
    agent._push_committed_unpushed_owned_head.assert_not_called()
    agent._push_owned_branch.assert_not_called()
    agent._vcs.reset_branch_to.assert_not_called()


# ---------------------------------------------------------------------------
# Promotion guard: the flag ships default-on (was default-off)
# ---------------------------------------------------------------------------


def test_config_default_enables_committed_push_actuator(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """AC1: a fresh agent's Config defaults the actuator flag ON.

    Guards the promotion at the source: flipping the default back to ``False``
    (or a fixture pinning it off) reds this. No per-test enable — the fixture
    builds a real ``Config`` whose production default this asserts.
    """
    agent = make_agent()
    assert agent.config.owned_committed_push_actuator_enabled is True


@pytest.mark.asyncio
async def test_record_outcome_routes_via_default_flag_not_explicit_enable(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """AC2: with the flag left at its *default* (not set per-test),
    ``_record_remediation_outcome`` still routes a gate-clean committed-unpushed
    AWAITING_PUSH owned head through ``_push_committed_unpushed_owned_head`` — the
    exact wiring the default-on promotion earns. A spy on the actuator confirms it
    is consulted and returns handled, and the owned push fires once."""
    agent = make_agent()
    branch = "maestro/evolve/ep910"
    # set_flag=False: rely on the production default, not an explicit enable.
    _arm_committed_unpushed(agent, 910, branch, set_flag=False)
    assert agent.config.owned_committed_push_actuator_enabled is True
    agent._fetch_origin_for_merge_truth = MagicMock()
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=True),
    )
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_merge_truth(910, settled=False),
    )
    actuator = agent._push_committed_unpushed_owned_head
    spy = MagicMock(side_effect=actuator)
    agent._push_committed_unpushed_owned_head = spy  # type: ignore[method-assign]
    record = _remediation_record(
        910, "Delivered the fix to the PR branch, awaiting review/merge.",
    )

    await agent._record_remediation_outcome(
        goal="Remediate PR #910: resolve conflict",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    spy.assert_called_once_with(910, branch, record)
    agent._push_owned_branch.assert_called_once_with(branch)
    assert Marker.DELIVERED_AWAITING_MERGE in record.result_summary
    # No stand-down verdict minted — the deed replaced the external-quiet verdict.
    assert str(agent.config.memory_dir / "stand_down_verdicts.jsonl") not in mem_fs


@pytest.mark.asyncio
async def test_record_outcome_red_head_rewound_never_pushed_via_default(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """AC2: relying on the *default* flag, a gate-RED committed head is rewound to
    ``origin/<branch>`` and never pushed — a red head never reaches origin."""
    agent = make_agent()
    branch = "maestro/evolve/ep911"
    _arm_committed_unpushed(agent, 911, branch, set_flag=False)
    assert agent.config.owned_committed_push_actuator_enabled is True
    agent._fetch_origin_for_merge_truth = MagicMock()
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(False),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]
    record = _remediation_record(911, "Delivered, awaiting review.")

    await agent._record_remediation_outcome(
        goal="Remediate PR #911: resolve conflict",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    agent._vcs.reset_branch_to.assert_called_once_with(
        branch, f"origin/{branch}",
    )
    agent._push_owned_branch.assert_not_called()
    assert Marker.COMMITTED_RED in record.result_summary
    assert record.outcome == "partial"


@pytest.mark.asyncio
async def test_delivery_truth_stamps_once_on_advanced_head_no_phantom_success(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """AC3: on a real advanced remote head the delivery-truth accounting stamps the
    delivered marker exactly once (single ``_record_owned_push_delivered`` call, a
    single post-push ``is_pr_settled`` re-read) and records ``partial`` — never a
    phantom ``success``, since this hand merged nothing into main."""
    agent = make_agent()
    branch = "maestro/evolve/ep912"
    _arm_committed_unpushed(agent, 912, branch, set_flag=False)
    agent._fetch_origin_for_merge_truth = MagicMock()
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    # pushed=True == verify_push_advanced_remote_head confirmed the remote advanced.
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=True),
    )
    settle_read = MagicMock(return_value=_merge_truth(912, settled=False))
    agent.is_pr_settled = settle_read  # type: ignore[method-assign]
    deliver_spy = MagicMock(side_effect=agent._record_owned_push_delivered)
    agent._record_owned_push_delivered = deliver_spy  # type: ignore[method-assign]
    record = _remediation_record(912, "Delivered, awaiting review.")

    await agent._record_remediation_outcome(
        goal="Remediate PR #912: resolve conflict",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    # Delivery stamped exactly once against the observed advanced head. (The
    # arm re-reads is_pr_settled fetch-first before stamping; downstream no-op
    # accounting may read it again, so the exactly-once guard is on the stamp.)
    deliver_spy.assert_called_once()
    settle_read.assert_any_call(912)
    assert Marker.DELIVERED_AWAITING_MERGE in record.result_summary
    # Never a phantom success — this lap merged nothing into main by its own hand.
    assert record.outcome == "partial"


@pytest.mark.asyncio
async def test_no_success_when_remote_head_did_not_advance(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """AC3: a push that exited 0 but did NOT advance the remote head (unconfirmed)
    stamps no delivered marker and records ``partial`` — no success absent an
    actually advanced remote head."""
    agent = make_agent()
    branch = "maestro/evolve/ep913"
    _arm_committed_unpushed(agent, 913, branch, set_flag=False)
    agent._fetch_origin_for_merge_truth = MagicMock()
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=False, unconfirmed=True),
    )
    deliver_spy = MagicMock(side_effect=agent._record_owned_push_delivered)
    agent._record_owned_push_delivered = deliver_spy  # type: ignore[method-assign]
    record = _remediation_record(913, "Delivered, awaiting review.")

    await agent._record_remediation_outcome(
        goal="Remediate PR #913: resolve conflict",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    # No advanced head → no delivery stamp, no success.
    deliver_spy.assert_not_called()
    assert Marker.PUSH_UNCONFIRMED in record.result_summary
    assert Marker.DELIVERED_AWAITING_MERGE not in record.result_summary
    assert record.outcome == "partial"


# ---------------------------------------------------------------------------
# #4043 — the arm measures and rewinds the PR head, never main
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_outcome_committed_arm_never_gates_or_resets_main(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Post-teardown the workspace sits on ``main``: the gates must run on the PR
    branch, the red rewind must name the PR branch, and ``main`` is restored."""
    agent = make_agent()
    branch = "maestro/evolve/ep4043"
    _arm_committed_unpushed(agent, 4043, branch)
    history = _track_checkout(agent, "main")
    agent._fetch_origin_for_merge_truth = MagicMock()
    gate_branches: list[str] = []
    vcs = agent._vcs

    def _gates() -> QualityGateResult:
        gate_branches.append(vcs.get_current_branch.return_value)
        return _gate_result(False)

    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        side_effect=_gates,
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]

    await agent._record_remediation_outcome(
        goal="Remediate PR #4043: resolve conflict",
        pr_branch=branch,
        record=_remediation_record(4043, "Delivered, awaiting review."),
        head_before="abc",
    )

    assert gate_branches == [branch]
    vcs.reset_hard.assert_not_called()
    vcs.reset_branch_to.assert_called_once_with(branch, f"origin/{branch}")
    assert history == ["main", branch, "main"]
    assert vcs.get_current_branch.return_value == "main"


def test_committed_arm_already_on_pr_branch_skips_checkout(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Already on the PR branch → no checkout round-trip, gates still run."""
    agent = make_agent()
    branch = "maestro/evolve/ep4044"
    _arm_committed_unpushed(agent, 4044, branch)
    history = _track_checkout(agent, branch)
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(False),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]

    handled = agent._push_committed_unpushed_owned_head(4044, branch, None)

    assert handled is True
    assert history == [branch]
    agent._vcs.reset_branch_to.assert_called_once_with(
        branch, f"origin/{branch}",
    )


def test_committed_arm_noop_when_checkout_fails(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A failed checkout of the PR branch is a no-op, never a gate run on main."""
    from maestro.vcs import VCSError

    agent = make_agent()
    branch = "maestro/evolve/ep4045"
    _arm_committed_unpushed(agent, 4045, branch)
    agent._vcs.checkout.side_effect = VCSError("checkout failed")
    agent.evolution.run_quality_gates = MagicMock()  # type: ignore[method-assign]
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]

    handled = agent._push_committed_unpushed_owned_head(4045, branch, None)

    assert handled is False
    agent.evolution.run_quality_gates.assert_not_called()
    agent._vcs.reset_branch_to.assert_not_called()
    agent._push_owned_branch.assert_not_called()


def test_committed_arm_noop_when_checkout_does_not_move_head(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A checkout that silently leaves main checked out → no gate, main restored."""
    agent = make_agent()
    branch = "maestro/evolve/ep4046"
    _arm_committed_unpushed(agent, 4046, branch)
    agent._vcs.checkout.side_effect = None
    agent._vcs.get_current_branch.return_value = "main"
    agent.evolution.run_quality_gates = MagicMock()  # type: ignore[method-assign]

    handled = agent._push_committed_unpushed_owned_head(4046, branch, None)

    assert handled is False
    agent.evolution.run_quality_gates.assert_not_called()
    agent._vcs.checkout.assert_called_with("main")


def test_committed_arm_noop_when_current_branch_unreadable(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An unreadable current branch (detached HEAD) is a no-op."""
    from maestro.vcs import VCSError

    agent = make_agent()
    branch = "maestro/evolve/ep4047"
    _arm_committed_unpushed(agent, 4047, branch)
    agent._vcs.get_current_branch.side_effect = VCSError("detached")
    agent.evolution.run_quality_gates = MagicMock()  # type: ignore[method-assign]

    handled = agent._push_committed_unpushed_owned_head(4047, branch, None)

    assert handled is False
    agent.evolution.run_quality_gates.assert_not_called()
    agent._vcs.checkout.assert_not_called()


def test_committed_arm_restore_failure_is_logged_not_raised(
    make_agent, caplog,  # type: ignore[no-untyped-def]
) -> None:
    """A failed checkout back to main is logged at ERROR; the arm's verdict stands."""
    from maestro.vcs import VCSError

    agent = make_agent()
    branch = "maestro/evolve/ep4048"
    _arm_committed_unpushed(agent, 4048, branch)
    vcs = agent._vcs

    def _checkout(target: str) -> None:
        if target == "main":
            raise VCSError("cannot return")
        vcs.get_current_branch.return_value = target

    vcs.checkout.side_effect = _checkout
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(False),
    )

    with caplog.at_level("ERROR"):
        handled = agent._push_committed_unpushed_owned_head(4048, branch, None)

    assert handled is True
    assert "restoring the workspace" in caplog.text


# ---------------------------------------------------------------------------
# Persistence: the record-time re-stamps reach task history (#4062)
# ---------------------------------------------------------------------------


def _queue_remediation(agent: object, pr_num: int, branch: str) -> str:
    """Queue one ``Remediate PR #N:`` request on *branch*; return its goal.

    The owned head is re-registered as a full open ``PrMergeState`` so the
    phase's dequeue gates read a real (unsettled, blocked) PR shape.
    """
    agent.evolution._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        branch: PrMergeState(
            number=pr_num,
            head_ref=branch,
            title=f"PR {pr_num}",
            body="body",
            state="OPEN",
            mergeable="MERGEABLE",
            merge_state_status="BLOCKED",
            review_decision="",
            created_at="2026-07-30T00:00:00Z",
            updated_at="2026-07-30T00:00:00Z",
            url=f"https://github.com/o/r/pull/{pr_num}",
        ),
    }
    goal = f"Remediate PR #{pr_num}: resolve conflict"
    model = agent.consciousness.load_self_model()  # type: ignore[attr-defined]
    model.pending_pr_requests = [
        PendingPRRequest(directive=goal, pr_branch=branch),
    ]
    agent.consciousness.save_self_model(model)  # type: ignore[attr-defined]
    return goal


def _persisted_rows(agent: object, task_id: str) -> list[dict[str, object]]:
    """Every persisted task-history row for *task_id*, in file order."""
    return [
        asdict(t)
        for t in agent.memory.get_recent_tasks(50)  # type: ignore[attr-defined]
        if t.task_id == task_id
    ]


@pytest.mark.asyncio
async def test_persisted_row_carries_committed_red_restamp(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A remediation lap's committed-red re-stamp lands in the one persisted row:
    ``run()`` is told to defer, and the phase appends after the arms ran."""
    agent = make_agent()
    branch = "maestro/evolve/ep920"
    _arm_committed_unpushed(agent, 920, branch)
    _queue_remediation(agent, 920, branch)
    agent._fetch_origin_for_merge_truth = MagicMock()
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(False),
    )
    agent._push_owned_branch = MagicMock()  # type: ignore[method-assign]
    record = _remediation_record(920, "Delivered, awaiting review.")
    agent.run = AsyncMock(return_value=record)  # type: ignore[method-assign]

    await agent._run_task_phase()

    assert agent.run.await_args.kwargs["defer_persist"] is True
    rows = _persisted_rows(agent, "cu920")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "partial"
    assert Marker.COMMITTED_RED in str(rows[0]["result_summary"])


@pytest.mark.asyncio
async def test_persisted_row_carries_delivered_restamp(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A confirmed record-time push persists ``DELIVERED_AWAITING_MERGE``, so the
    lap-delivery sensor over task history reads the lap as DELIVERING."""
    agent = make_agent()
    branch = "maestro/evolve/ep921"
    _arm_committed_unpushed(agent, 921, branch)
    _queue_remediation(agent, 921, branch)
    agent._fetch_origin_for_merge_truth = MagicMock()
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_gate_result(True),
    )
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=True),
    )
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_merge_truth(921, settled=False),
    )
    record = _remediation_record(921, "Delivered, awaiting review.")
    agent.run = AsyncMock(return_value=record)  # type: ignore[method-assign]

    await agent._run_task_phase()

    rows = _persisted_rows(agent, "cu921")
    assert len(rows) == 1
    assert Marker.DELIVERED_AWAITING_MERGE in str(rows[0]["result_summary"])
    assert measure_lap_delivery(rows).delivering == 1


@pytest.mark.asyncio
async def test_persisted_row_survives_arm_crash(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An arm that raises still leaves exactly one persisted row before the
    exception propagates — never swallowed, never lost."""
    agent = make_agent()
    branch = "maestro/evolve/ep922"
    _queue_remediation(agent, 922, branch)
    record = _remediation_record(922, "Delivered, awaiting review.")
    agent.run = AsyncMock(return_value=record)  # type: ignore[method-assign]
    agent._record_remediation_outcome = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("arm boom"),
    )

    with pytest.raises(RuntimeError, match="arm boom"):
        await agent._run_task_phase()

    assert len(_persisted_rows(agent, "cu922")) == 1


@pytest.mark.asyncio
async def test_non_remediation_task_does_not_defer_persist(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A plain queue-head task leaves persistence to ``run()``: no deferral, and
    the phase appends nothing of its own."""
    agent = make_agent()
    agent.task_queue.save([QueuedTask(task_id="q1", goal="queue goal")])
    record = _remediation_record(923, "done")
    record.goal = "queue goal"
    agent.run = AsyncMock(return_value=record)  # type: ignore[method-assign]

    await agent._run_task_phase()

    assert agent.run.await_args.kwargs["defer_persist"] is False
    assert _persisted_rows(agent, "cu923") == []


def _real_run_patches():  # type: ignore[no-untyped-def]
    """Patch the SDK seams so a real ``run()`` completes one successful step."""
    plan = MockQueryResult(response=json.dumps([{"description": "Step 1"}]))
    step = MockQueryResult(response="Step completed")
    reply = AsyncMock(return_value=MockQueryResult(response="Key findings"))
    return (
        patch("maestro.planner.query", new=AsyncMock(return_value=plan)),
        patch("maestro.executor.query", new=AsyncMock(return_value=step)),
        patch("maestro.agent.query", new=reply),
        patch("maestro.agent.query_on_session", new=reply),
    )


@pytest.mark.asyncio
async def test_run_with_defer_persist_writes_no_row(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``run(defer_persist=True)`` returns the record but appends no row."""
    agent = make_agent()
    p1, p2, p3, p4 = _real_run_patches()
    with p1, p2, p3, p4:
        record = await agent.run("Test goal", defer_persist=True)

    assert _persisted_rows(agent, record.task_id) == []


@pytest.mark.asyncio
async def test_run_with_defer_persist_persists_when_tail_raises(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """If the post-record tail raises under deferral, ``run()`` persists the row
    itself before propagating — the caller never reaches its append."""
    agent = make_agent()
    p1, p2, p3, p4 = _real_run_patches()
    with p1, p2, p3, p4, patch(
        "maestro.agent.run_post_task_reflection",
        new=AsyncMock(side_effect=RuntimeError("tail boom")),
    ), pytest.raises(RuntimeError, match="tail boom"):
        await agent.run("Test goal", defer_persist=True)

    assert len(agent.memory.get_recent_tasks(5)) == 1


@pytest.mark.asyncio
async def test_run_without_defer_persist_tail_raise_single_row(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Default mode already appended at 5f; a raising tail adds no second row."""
    agent = make_agent()
    p1, p2, p3, p4 = _real_run_patches()
    with p1, p2, p3, p4, patch(
        "maestro.agent.run_post_task_reflection",
        new=AsyncMock(side_effect=RuntimeError("tail boom")),
    ), pytest.raises(RuntimeError, match="tail boom"):
        await agent.run("Test goal")

    assert len(agent.memory.get_recent_tasks(5)) == 1


def test_salvage_streak_counts_unpersisted_current_lap(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The current lap's deferred record joins the same-base partial streak,
    so disengage is not delayed a lap by the deferred append (#4062)."""
    agent = make_agent()
    agent.observe_pr_readiness = MagicMock()  # type: ignore[method-assign]
    agent._pr_disposability = MagicMock(return_value=True)  # type: ignore[method-assign]
    agent._vcs.rev_parse.side_effect = (  # type: ignore[attr-defined]
        lambda ref: "base1" if ref == "origin/main" else "head1"
    )
    lap = _remediation_record(930, "partial")
    lap.pr_head_sha = "head1"
    lap.base_head_sha = "base1"
    agent.evolution._observe_pr_staleness = MagicMock()  # type: ignore[method-assign]
    with patch("maestro.agent.assess_pr_salvage") as oracle:
        agent._assess_pr_salvage(930, "maestro/evolve/ep930", unpersisted=lap)
        agent._assess_pr_salvage(930, "maestro/evolve/ep930")

    first, second = oracle.call_args_list
    assert first.kwargs["same_base_failures"] == 1
    assert second.kwargs["same_base_failures"] == 0
