"""Parity/behaviour test for ``loop.task_delivery.commit_task_artifacts``.

The stage is the verbatim lift of ``run``'s ``# 5.`` block (proposal 106): the
sole writer of the run-scoped ``kept_branch``, deciding whether the task branch
survives the run. A dirty tree on a deliverable outcome is staged and committed;
an owned (``pr_branch is None``) clean-tree branch that carries commits of its
own is kept without committing; everything else returns ``None``. These tests
call the method directly with keyword args and pin each arm — ``_vcs`` is a
MagicMock, so nothing shells out.

They also close three coverage gaps the inline block carried: the commit message
format (including its 80-char goal truncation) is asserted, the failure arm is
pinned to ``None`` with a ``caplog``-verified warning, and a ``failure`` outcome
is shown to skip the block entirely.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from maestro.loop.task_delivery import commit_task_artifacts
from maestro.vcs import VCSError

_TASK_BRANCH = "maestro/task/abc123def456"
_PR_BRANCH = "maestro/evolve/ep700"
_TASK_ID = "abc123def456"


def test_dirty_tree_commits_and_keeps_branch(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Dirty-tree arm: a deliverable outcome with a dirty tree stages, commits
    with the truncated message, and returns the branch."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    goal = "A" * 90  # longer than the 80-char commit-message window

    kept = commit_task_artifacts(
        agent,
        branch_name=_TASK_BRANCH,
        pr_branch=None,
        outcome="success",
        branch_base_sha=None,
        task_id=_TASK_ID,
        goal=goal,
    )

    assert kept == _TASK_BRANCH
    agent._vcs.stage_all.assert_called_once_with()
    agent._vcs.commit.assert_called_once_with(
        f"maestro: task {_TASK_ID} — {'A' * 80}",
    )


def test_clean_tree_keeps_owned_branch_carrying_own_commits(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Clean-tree keep arm: an owned branch (``pr_branch is None``) whose own
    commits survive ``_branch_carries_own_commits`` is kept without committing."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = False
    # Both the vs-``main`` and vs-``origin/main`` probes read positive, and the
    # default ``base_sha=None`` skips the stale-cut veto → carries own commits.
    agent._vcs.non_merge_commits_ahead.return_value = 2

    kept = commit_task_artifacts(
        agent,
        branch_name=_TASK_BRANCH,
        pr_branch=None,
        outcome="partial",
        branch_base_sha=None,
        task_id=_TASK_ID,
        goal="goal",
    )

    assert kept == _TASK_BRANCH
    agent._vcs.stage_all.assert_not_called()
    agent._vcs.commit.assert_not_called()


def test_clean_tree_owned_branch_without_own_commits_returns_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Clean-tree no-work arm: an owned branch whose probe reads no own commits
    (the make_agent default) is not kept — teardown will delete it."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = False
    # make_agent default: non_merge_commits_ahead == 0 → own <= 0 → not kept.

    kept = commit_task_artifacts(
        agent,
        branch_name=_TASK_BRANCH,
        pr_branch=None,
        outcome="success",
        branch_base_sha=None,
        task_id=_TASK_ID,
        goal="goal",
    )

    assert kept is None
    agent._vcs.commit.assert_not_called()


def test_remediation_lap_short_circuits_before_probe(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Remediation-lap arm: with ``pr_branch`` set the clean-tree keep is scoped
    out — the ``elif pr_branch is None`` short-circuits before the probe runs, so
    a remediation PR branch (nearly always ahead of ``main``) is never kept."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = False
    agent._branch_carries_own_commits = MagicMock()  # type: ignore[method-assign]

    kept = commit_task_artifacts(
        agent,
        branch_name=_PR_BRANCH,
        pr_branch=_PR_BRANCH,
        outcome="success",
        branch_base_sha=None,
        task_id=_TASK_ID,
        goal="goal",
    )

    assert kept is None
    agent._branch_carries_own_commits.assert_not_called()
    agent._vcs.commit.assert_not_called()


def test_no_branch_returns_none_without_touching_vcs(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """No-branch arm: ``branch_name is None`` returns ``None`` before the guard
    ever probes the working tree."""
    agent = make_agent()

    kept = commit_task_artifacts(
        agent,
        branch_name=None,
        pr_branch=None,
        outcome="success",
        branch_base_sha=None,
        task_id=_TASK_ID,
        goal="goal",
    )

    assert kept is None
    agent._vcs.has_changes.assert_not_called()


def test_failure_outcome_skips_block_entirely(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Failure arm: a ``failure`` outcome fails the deliverable-outcome guard, so
    the block is skipped entirely — no VCS probe, no commit, ``None`` returned."""
    agent = make_agent()

    kept = commit_task_artifacts(
        agent,
        branch_name=_TASK_BRANCH,
        pr_branch=None,
        outcome="failure",
        branch_base_sha=None,
        task_id=_TASK_ID,
        goal="goal",
    )

    assert kept is None
    agent._vcs.has_changes.assert_not_called()


def test_commit_failure_logs_and_returns_none(
    make_agent,  # type: ignore[no-untyped-def]
    caplog,  # type: ignore[no-untyped-def]
) -> None:
    """Failure-swallow arm: a raising commit is logged (warning) and falls
    through, conceding the branch to teardown rather than crashing — ``None`` is
    returned even though ``stage_all`` already ran."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    agent._vcs.commit.side_effect = VCSError("remote rejected")

    with caplog.at_level(logging.WARNING, logger="maestro"):
        kept = commit_task_artifacts(
            agent,
            branch_name=_TASK_BRANCH,
            pr_branch=None,
            outcome="success",
            branch_base_sha=None,
            task_id=_TASK_ID,
            goal="goal",
        )

    assert kept is None
    agent._vcs.stage_all.assert_called_once_with()
    assert "Failed to commit task artifacts" in caplog.text
