"""Unit tests for ``maestro.loop.task_setup.resolve_resume_checkpoint``.

The stage is the verbatim lift of ``run``'s ``# 0.`` resume block (proposal 106):
on an owned task branch (``pr_branch is None``) it loads any surviving checkpoint
and honours it only while its recorded branch still resolves, otherwise clearing
the orphan; on a ``pr_branch`` (remediation) run it skips checkpoint loading
entirely. These tests call the function directly with keyword args and pin all four
arms; the ``memory`` and ``_vcs`` seams are mocked, so nothing shells out or
touches a real checkpoint file.

Behaviour-preservation of the *whole* block is proven by the full-``run``
regression harness (``tests/test_checkpoint.py`` for the resume/checkpoint
lifecycle), which now flows through this function unchanged; these unit tests
exercise the extracted stage in isolation — in particular the ``pr_branch``
skip arm, which nothing asserted directly before.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from maestro.loop.task_setup import resolve_resume_checkpoint
from maestro.memory import PlanStep, TaskCheckpoint

_TASK_BRANCH = "maestro/task/tid"


def test_pr_branch_skips_checkpoint_loading(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(a) remediation arm: with ``pr_branch`` set the method returns
    ``(None, None)`` and never reads a checkpoint (``load_checkpoint`` unused) —
    ``pr_branch`` runs are owned/pushed by ``_tend_single_pr`` and are out of
    scope for resume."""
    agent = make_agent()
    agent.memory.load_checkpoint = MagicMock()
    agent.memory.clear_checkpoint = MagicMock()

    result = resolve_resume_checkpoint(agent,
        pr_branch="feat/pr-branch", task_id="tid",
    )

    assert result == (None, None)
    agent.memory.load_checkpoint.assert_not_called()
    agent.memory.clear_checkpoint.assert_not_called()


def test_no_checkpoint_returns_none_and_never_clears(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(b) no-checkpoint arm: on an owned branch with ``load_checkpoint``
    returning ``None`` the method returns ``(None, None)`` and never calls
    ``clear_checkpoint`` (there is nothing to discard)."""
    agent = make_agent()
    agent.memory.load_checkpoint = MagicMock(return_value=None)
    agent.memory.clear_checkpoint = MagicMock()
    agent._vcs.rev_parse = MagicMock()

    result = resolve_resume_checkpoint(agent, pr_branch=None, task_id="tid")

    assert result == (None, None)
    agent.memory.load_checkpoint.assert_called_once_with("tid")
    agent.memory.clear_checkpoint.assert_not_called()
    # No checkpoint means the branch-resolves probe is never reached.
    agent._vcs.rev_parse.assert_not_called()


def test_surviving_checkpoint_is_resumed_not_cleared(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(c) resume arm: a checkpoint whose branch still resolves
    (``rev_parse`` non-``None``) is honoured — the method returns
    ``(loaded, loaded.branch)`` and does **not** clear it."""
    agent = make_agent()
    cp = TaskCheckpoint(
        task_id="tid",
        goal="g",
        branch=_TASK_BRANCH,
        steps=[PlanStep("s1", "restored", status="done")],
    )
    agent.memory.load_checkpoint = MagicMock(return_value=cp)
    agent.memory.clear_checkpoint = MagicMock()
    agent._vcs.rev_parse = MagicMock(return_value="deadbeef")

    resume_checkpoint, resume_branch = resolve_resume_checkpoint(agent,
        pr_branch=None, task_id="tid",
    )

    assert resume_checkpoint is cp
    assert resume_branch == _TASK_BRANCH
    agent._vcs.rev_parse.assert_called_once_with(_TASK_BRANCH)
    agent.memory.clear_checkpoint.assert_not_called()


def test_orphaned_checkpoint_is_discarded(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(d) orphan arm: a checkpoint whose branch no longer resolves
    (``rev_parse`` returns ``None``) is an orphan — the method returns
    ``(None, None)`` and clears it exactly once so the task starts fresh."""
    agent = make_agent()
    cp = TaskCheckpoint(
        task_id="tid",
        goal="g",
        branch=_TASK_BRANCH,
        steps=[PlanStep("s1", "restored", status="done")],
    )
    agent.memory.load_checkpoint = MagicMock(return_value=cp)
    agent.memory.clear_checkpoint = MagicMock()
    agent._vcs.rev_parse = MagicMock(return_value=None)

    result = resolve_resume_checkpoint(agent, pr_branch=None, task_id="tid")

    assert result == (None, None)
    agent._vcs.rev_parse.assert_called_once_with(_TASK_BRANCH)
    agent.memory.clear_checkpoint.assert_called_once_with("tid")
