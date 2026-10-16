"""Unit tests for ``loop.task_outcome`` watermark captures.

``capture_lap_start_head_watermark`` reads the ``origin/<pr_branch>`` head at
lap start (before step execution, so before the agent's own in-session push or
the ``# 4b-ii.`` actuator) — the ``TaskRecord.pr_head_sha`` watermark consumed
by the costume-signature gate and the stalled-lineage brake (#4567).

``capture_prepush_watermarks`` is the verbatim lift of ``run``'s ``# 4b-i.``
block (proposal 106): it reads the post-session ``origin/<pr_branch>`` head
(feeding only the mergeable-flip actuator key) and the ``origin/main`` base
stamp, defaulting both to ``None`` for a non-remediation lap.

These tests call the stages directly on a bare ``make_agent`` agent, driving
``agent._vcs.rev_parse`` with a ``MagicMock``, pinning every guard arm. The
run-level ordering regressions live in ``tests/test_session_landing.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

from maestro.loop.task_outcome import (
    capture_lap_start_head_watermark,
    capture_prepush_watermarks,
)

_PR_BRANCH = "maestro/evolve/p7"
_GOAL = "Remediate PR #7: address the review threads"


def test_remediation_lap_captures_both_watermarks(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(a) A ``Remediate PR #N:`` goal with a real ``pr_branch`` rev-parses the
    PR-branch head then ``origin/main``, returning both stamps in that order."""
    agent = make_agent()
    rev_parse = MagicMock(side_effect=["pr-head-sha", "base-head-sha"])
    agent._vcs.rev_parse = rev_parse

    pr_head, base_head = capture_prepush_watermarks(
        agent,
        pr_branch=_PR_BRANCH, goal=_GOAL,
    )

    assert pr_head == "pr-head-sha"
    assert base_head == "base-head-sha"
    assert rev_parse.call_args_list == [
        call(f"origin/{_PR_BRANCH}"),
        call("origin/main"),
    ]


def test_non_remediation_goal_returns_none_without_rev_parse(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(b) A goal that does not begin with the ``Remediate PR #`` prefix
    short-circuits — both stamps stay ``None`` and ``rev_parse`` never runs."""
    agent = make_agent()
    rev_parse = MagicMock()
    agent._vcs.rev_parse = rev_parse

    pr_head, base_head = capture_prepush_watermarks(
        agent,
        pr_branch=_PR_BRANCH, goal="Improve the dashboard status layer",
    )

    assert pr_head is None
    assert base_head is None
    rev_parse.assert_not_called()


def test_none_pr_branch_returns_none_without_rev_parse(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(c) ``pr_branch is None`` (not a PR remediation) short-circuits the same
    way even on a ``Remediate PR #N:`` goal — both stamps stay ``None``."""
    agent = make_agent()
    rev_parse = MagicMock()
    agent._vcs.rev_parse = rev_parse

    pr_head, base_head = capture_prepush_watermarks(
        agent,
        pr_branch=None, goal=_GOAL,
    )

    assert pr_head is None
    assert base_head is None
    rev_parse.assert_not_called()


def test_unresolvable_refs_pass_none_through(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(d) A ``rev_parse`` that cannot resolve a ref returns ``None`` (a
    best-effort read); the capture passes that ``None`` straight onto both
    stamps rather than raising, still reading both refs in order."""
    agent = make_agent()
    rev_parse = MagicMock(return_value=None)
    agent._vcs.rev_parse = rev_parse

    pr_head, base_head = capture_prepush_watermarks(
        agent,
        pr_branch=_PR_BRANCH, goal=_GOAL,
    )

    assert pr_head is None
    assert base_head is None
    assert rev_parse.call_args_list == [
        call(f"origin/{_PR_BRANCH}"),
        call("origin/main"),
    ]


def test_lap_start_reads_only_pr_branch_head(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A remediation goal rev-parses only ``origin/<pr_branch>``."""
    agent = make_agent()
    rev_parse = MagicMock(return_value="lap-start-sha")
    agent._vcs.rev_parse = rev_parse

    head = capture_lap_start_head_watermark(
        agent, pr_branch=_PR_BRANCH, goal=_GOAL,
    )

    assert head == "lap-start-sha"
    assert rev_parse.call_args_list == [call(f"origin/{_PR_BRANCH}")]


def test_lap_start_non_remediation_goal_returns_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A non-remediation goal returns ``None`` without reading git."""
    agent = make_agent()
    rev_parse = MagicMock()
    agent._vcs.rev_parse = rev_parse

    head = capture_lap_start_head_watermark(
        agent, pr_branch=_PR_BRANCH, goal="Improve the dashboard",
    )

    assert head is None
    rev_parse.assert_not_called()


def test_lap_start_none_pr_branch_returns_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``pr_branch is None`` returns ``None`` without reading git."""
    agent = make_agent()
    rev_parse = MagicMock()
    agent._vcs.rev_parse = rev_parse

    head = capture_lap_start_head_watermark(agent, pr_branch=None, goal=_GOAL)

    assert head is None
    rev_parse.assert_not_called()


def test_lap_start_unresolvable_ref_passes_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An unresolvable ref's ``None`` passes straight through."""
    agent = make_agent()
    rev_parse = MagicMock(return_value=None)
    agent._vcs.rev_parse = rev_parse

    head = capture_lap_start_head_watermark(
        agent, pr_branch=_PR_BRANCH, goal=_GOAL,
    )

    assert head is None
    rev_parse.assert_called_once_with(f"origin/{_PR_BRANCH}")
