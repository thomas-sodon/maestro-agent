"""Unit test for ``loop.remediation_stamps.apply_host_state_stamp_gate``.

The stage is the verbatim lift of ``run``'s ``# 4c.`` host-state stamp gate
(proposal 106): on a ``Remediate PR #N:`` goal it records the action-zero
fetch-truth marker (and, flag-gated, the obligation-fork marker) the later
verdict-fetch gate depends on, then — when the outcome would otherwise stamp
``success`` — probes merge truth and forces ``partial`` with a deterministic
merge-block note when the resolution has not landed. It first-declares
``remediation_note`` / ``remediation_pr_num`` and returns them for the
downstream ``# 4c-ii.`` / ``# 4d.`` stages to read back.

These tests call the stage directly, agent-first-positional, on a bare
``make_agent`` agent (no ``agent.run()``, no SDK patching) and pin every arm so
it holds 100 % coverage on its own; the full-``run`` regression harness
(``tests/test_pr_remediation_lap.py`` "merge_block_skip" tests) exercises the same block
end-to-end and must pass unchanged. ``is_pr_settled``,
``_probe_pr_merge_block``, ``_git_pr_merged``, ``code_host.is_pr_merged``,
``_record_remediation_fetch_truth``, ``_record_remediation_obligation_fork``
and ``_refine_ci_review_blocker`` are mocked, so nothing shells out.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.loop.remediation_stamps import apply_host_state_stamp_gate
from maestro.pr_facts import MergeState, MergeTruth

_PR = 4242
_PR_BRANCH = "maestro/evolve/ep1"
_GOAL = f"Remediate PR #{_PR}: address the review threads"
_TASK_ID = "task-abc"


def _merge_truth(*, branch_landed: bool) -> MergeTruth:
    """Build a ``MergeTruth`` whose ``.branch_landed`` drives the prestamp arm."""
    state: MergeState = "OPEN"
    return MergeTruth(
        pr_number=_PR,
        settled=False,
        state=state,
        branch_landed=branch_landed,
        source="host_api",
        observed_at=0.0,
    )


def _wire_no_shell(agent) -> None:  # type: ignore[no-untyped-def]
    """Replace every host/git seam the gate touches with a non-shelling mock."""
    agent._record_remediation_fetch_truth = MagicMock()  # type: ignore[method-assign]
    agent._record_remediation_obligation_fork = (  # type: ignore[method-assign]
        MagicMock()
    )
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]
    agent._probe_pr_merge_block = AsyncMock()  # type: ignore[method-assign]
    agent._git_pr_merged = MagicMock(return_value=False)  # type: ignore[method-assign]
    agent._refine_ci_review_blocker = MagicMock(  # type: ignore[method-assign]
        return_value="CI_RED_REFINED",
    )
    agent.evolution.code_host.is_pr_merged = MagicMock(  # type: ignore[attr-defined]
        return_value=False,
    )


@pytest.mark.asyncio
async def test_non_remediation_goal_returns_pair_unchanged(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A non-remediation goal (``pr_branch is None``) never parses a PR number,
    records no markers, skips the gate, and returns the pair unchanged with a
    ``None`` PR number."""
    agent = make_agent()
    _wire_no_shell(agent)

    outcome, note, pr_num = await apply_host_state_stamp_gate(
        agent,
        outcome="success",
        pr_branch=None,
        goal="Improve dashboard latency",
        task_id=_TASK_ID,
    )

    assert outcome == "success"
    assert note is None
    assert pr_num is None
    agent._record_remediation_fetch_truth.assert_not_called()
    agent._record_remediation_obligation_fork.assert_not_called()
    agent.is_pr_settled.assert_not_called()
    agent._probe_pr_merge_block.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_remediation_goal_skips_parse_and_markers(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A goal that carries the remediation prefix but no well-formed ``#N:``
    marker matches the prefix guard yet fails the number regex, so no PR number
    is parsed, no marker is recorded, and the success gate is skipped."""
    agent = make_agent()
    _wire_no_shell(agent)

    outcome, note, pr_num = await apply_host_state_stamp_gate(
        agent,
        outcome="success",
        pr_branch=_PR_BRANCH,
        goal="Remediate PR #x: no numeric id",
        task_id=_TASK_ID,
    )

    assert outcome == "success"
    assert note is None
    assert pr_num is None
    agent._record_remediation_fetch_truth.assert_not_called()
    agent.is_pr_settled.assert_not_called()
    agent._probe_pr_merge_block.assert_not_called()


@pytest.mark.asyncio
async def test_remediation_goal_records_fetch_truth_obligation_off(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A well-formed ``Remediate PR #N:`` goal parses the number and records the
    action-zero fetch-truth marker; with the obligation-fork flag off, the
    obligation-fork marker is never recorded. A non-``success`` outcome skips
    the stamp gate, so the pair returns unchanged bar the parsed number."""
    agent = make_agent()
    _wire_no_shell(agent)
    agent.config.obligation_fork_floor_enabled = False

    outcome, note, pr_num = await apply_host_state_stamp_gate(
        agent,
        outcome="partial",
        pr_branch=_PR_BRANCH,
        goal=_GOAL,
        task_id=_TASK_ID,
    )

    assert outcome == "partial"
    assert note is None
    assert pr_num == _PR
    agent._record_remediation_fetch_truth.assert_called_once_with(
        _TASK_ID, _PR, _PR_BRANCH,
    )
    agent._record_remediation_obligation_fork.assert_not_called()
    agent.is_pr_settled.assert_not_called()
    agent._probe_pr_merge_block.assert_not_called()


@pytest.mark.asyncio
async def test_remediation_goal_records_obligation_fork_when_flag_on(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With ``obligation_fork_floor_enabled`` on, the obligation-fork marker is
    recorded beside the fetch-truth marker."""
    agent = make_agent()
    _wire_no_shell(agent)
    agent.config.obligation_fork_floor_enabled = True

    outcome, note, pr_num = await apply_host_state_stamp_gate(
        agent,
        outcome="partial",
        pr_branch=_PR_BRANCH,
        goal=_GOAL,
        task_id=_TASK_ID,
    )

    assert outcome == "partial"
    assert note is None
    assert pr_num == _PR
    agent._record_remediation_fetch_truth.assert_called_once_with(
        _TASK_ID, _PR, _PR_BRANCH,
    )
    agent._record_remediation_obligation_fork.assert_called_once_with(
        _TASK_ID, _PR, _PR_BRANCH,
    )


@pytest.mark.asyncio
async def test_prestamp_gate_unlanded_forces_partial(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Prestamp arm: a ``success`` remediation whose ``is_pr_settled`` reports
    the branch not landed is downgraded to ``partial`` with the awaiting-push
    merge-truth note, the provenance side effects are stamped, and the host
    probe is never consulted."""
    agent = make_agent()
    _wire_no_shell(agent)
    agent.config.prestamp_merge_truth_gate_enabled = True
    agent.is_pr_settled.return_value = _merge_truth(branch_landed=False)

    outcome, note, pr_num = await apply_host_state_stamp_gate(
        agent,
        outcome="success",
        pr_branch=_PR_BRANCH,
        goal=_GOAL,
        task_id=_TASK_ID,
    )

    assert outcome == "partial"
    assert pr_num == _PR
    assert note is not None
    assert "awaiting tend push" in note
    assert "not yet landed per merge-truth" in note
    assert "source=host_api" in note
    assert "observed_at=0)" in note
    assert agent._last_remediation_merge_block == "AWAITING_PUSH"
    assert agent._last_remediation_merge_source == "host_api"
    assert agent._last_remediation_merge_observed_at == 0.0
    agent._probe_pr_merge_block.assert_not_called()


@pytest.mark.asyncio
async def test_prestamp_gate_landed_leaves_success(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Prestamp arm: a ``success`` remediation whose ``is_pr_settled`` reports
    the branch landed survives ``success`` untouched, with no note, no side
    effects, and no host probe."""
    agent = make_agent()
    _wire_no_shell(agent)
    agent.config.prestamp_merge_truth_gate_enabled = True
    agent.is_pr_settled.return_value = _merge_truth(branch_landed=True)

    outcome, note, pr_num = await apply_host_state_stamp_gate(
        agent,
        outcome="success",
        pr_branch=_PR_BRANCH,
        goal=_GOAL,
        task_id=_TASK_ID,
    )

    assert outcome == "success"
    assert note is None
    assert pr_num == _PR
    assert agent._last_remediation_merge_block is None
    agent._probe_pr_merge_block.assert_not_called()


@pytest.mark.asyncio
async def test_host_probe_clean_but_unmerged_forces_partial(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Host-probe arm: a branch the probe reads as cleanly mergeable but that
    neither the host nor git confirms merged is forced to ``partial`` with the
    refined awaiting-push blocker note."""
    agent = make_agent()
    _wire_no_shell(agent)
    agent.config.prestamp_merge_truth_gate_enabled = False
    agent._probe_pr_merge_block.return_value = (True, "NONE")
    agent.evolution.code_host.is_pr_merged.return_value = False  # type: ignore[attr-defined]
    agent._git_pr_merged.return_value = False

    outcome, note, pr_num = await apply_host_state_stamp_gate(
        agent,
        outcome="success",
        pr_branch=_PR_BRANCH,
        goal=_GOAL,
        task_id=_TASK_ID,
    )

    assert outcome == "partial"
    assert pr_num == _PR
    # The clean-but-unmerged read flips ``landed_ok`` off with an
    # ``AWAITING_PUSH`` block reason, which the blocker refiner then rewrites.
    agent._refine_ci_review_blocker.assert_called_once_with(
        _PR, _PR_BRANCH, "AWAITING_PUSH",
    )
    assert agent._last_remediation_merge_block == "CI_RED_REFINED"
    assert note is not None
    assert "merge blocker: CI_RED_REFINED" in note
    assert "awaiting tend push" in note


@pytest.mark.asyncio
async def test_host_probe_host_merged_survives_success(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Host-probe arm: an explicit host ``is_pr_merged`` read short-circuits the
    git-merge-truth confirmation, so a cleanly-mergeable, host-merged PR keeps
    its ``success`` stamp with no downgrade and no refiner call."""
    agent = make_agent()
    _wire_no_shell(agent)
    agent.config.prestamp_merge_truth_gate_enabled = False
    agent._probe_pr_merge_block.return_value = (True, "NONE")
    agent.evolution.code_host.is_pr_merged.return_value = True  # type: ignore[attr-defined]

    outcome, note, pr_num = await apply_host_state_stamp_gate(
        agent,
        outcome="success",
        pr_branch=_PR_BRANCH,
        goal=_GOAL,
        task_id=_TASK_ID,
    )

    assert outcome == "success"
    assert note is None
    assert pr_num == _PR
    agent._refine_ci_review_blocker.assert_not_called()
    assert agent._last_remediation_merge_block is None


@pytest.mark.asyncio
async def test_host_probe_git_merged_survives_success(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Host-probe arm: a cleanly-mergeable PR the host does not report merged but
    that ``_git_pr_merged`` confirms landed on ``origin/main`` keeps its
    ``success`` stamp — the git-merge-truth confirmation's second disjunct
    short-circuits the downgrade, so there is no refiner call and no note."""
    agent = make_agent()
    _wire_no_shell(agent)
    agent.config.prestamp_merge_truth_gate_enabled = False
    agent._probe_pr_merge_block.return_value = (True, "NONE")
    agent.evolution.code_host.is_pr_merged.return_value = False  # type: ignore[attr-defined]
    agent._git_pr_merged.return_value = True

    outcome, note, pr_num = await apply_host_state_stamp_gate(
        agent,
        outcome="success",
        pr_branch=_PR_BRANCH,
        goal=_GOAL,
        task_id=_TASK_ID,
    )

    assert outcome == "success"
    assert note is None
    assert pr_num == _PR
    agent._refine_ci_review_blocker.assert_not_called()
    assert agent._last_remediation_merge_block is None


@pytest.mark.asyncio
async def test_host_probe_not_landed_forces_partial_with_refined_blocker(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Host-probe arm: a probe that itself reports the branch not landed is
    forced to ``partial`` with the refined blocker note, without the
    git-merge-truth confirmation ever flipping ``landed_ok``."""
    agent = make_agent()
    _wire_no_shell(agent)
    agent.config.prestamp_merge_truth_gate_enabled = False
    agent._probe_pr_merge_block.return_value = (False, "CONFLICTING")

    outcome, note, pr_num = await apply_host_state_stamp_gate(
        agent,
        outcome="success",
        pr_branch=_PR_BRANCH,
        goal=_GOAL,
        task_id=_TASK_ID,
    )

    assert outcome == "partial"
    assert pr_num == _PR
    agent._refine_ci_review_blocker.assert_called_once_with(
        _PR, _PR_BRANCH, "CONFLICTING",
    )
    assert agent._last_remediation_merge_block == "CI_RED_REFINED"
    assert note is not None
    assert "merge blocker: CI_RED_REFINED" in note
