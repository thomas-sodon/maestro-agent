"""Unit tests for ``maestro.loop.task_setup.prepare_task_branch``.

The stage is the verbatim lift of ``run``'s ``# 2.`` block (proposal 106): the
entry-time working-tree reset (discard changes, return to ``main``, warn if the
tree is still dirty) followed by the three-way working-branch selection — check
out an existing PR head (remediation lap), check out a surviving checkpoint
branch (resume), or cut a fresh ``maestro/task/<id>`` branch and stamp its
creation-head SHA (normal path). These tests call the function directly with
keyword args and pin each arm; the VCS seam is the fixture's ``MagicMock``, so
nothing shells out.

Behaviour-preservation of the *whole* block is proven by the full-``run``
regression harness (``tests/test_agent.py`` for the branch-creation, entry-reset
ordering, PR-branch origin fallback, and base-SHA watermark suites;
``tests/test_checkpoint.py`` for the resume lifecycle), which now flows through
this method unchanged; these unit tests exercise the extracted stage in
isolation.
"""

from __future__ import annotations

import logging
from unittest.mock import call

from maestro.loop.task_setup import prepare_task_branch
from maestro.vcs import VCSError

_PR_BRANCH = "maestro/evolve/ep1"
_RESUME_BRANCH = "maestro/task/resume123"


def _vcs_call_names(agent) -> list[str]:  # type: ignore[no-untyped-def]
    """The ordered method names invoked on the ``_vcs`` seam."""
    return [c[0] for c in agent._vcs.mock_calls]


def test_fresh_path_returns_task_branch_and_stamps_base_sha(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Normal path (no PR, no resume): a fresh ``maestro/task/<id>`` branch is
    cut, its creation head is stamped from ``rev_parse``, and the tuple carries
    both."""
    agent = make_agent()
    agent._vcs.rev_parse.return_value = "basesha123"

    branch_name, branch_base_sha, watermark = prepare_task_branch(agent,
        pr_branch=None, resume_branch=None, task_id="tid",
    )

    assert branch_name == "maestro/task/tid"
    assert branch_base_sha == "basesha123"
    assert watermark is None
    agent._vcs.checkout_new_branch.assert_called_once_with("maestro/task/tid")
    agent._vcs.rev_parse.assert_called_once_with("maestro/task/tid")


def test_fresh_creation_failure_clears_both_to_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Fresh-path ``except`` arm: ``checkout_new_branch`` raising ``VCSError``
    falls back to the current branch — ``(None, None)`` and no base-SHA
    probe."""
    agent = make_agent()
    agent._vcs.checkout_new_branch.side_effect = VCSError("cannot create")

    branch_name, branch_base_sha, watermark = prepare_task_branch(agent,
        pr_branch=None, resume_branch=None, task_id="tid",
    )

    assert branch_name is None
    assert watermark is None
    assert branch_base_sha is None
    agent._vcs.rev_parse.assert_not_called()


def test_pr_path_returns_pr_branch_no_base_sha(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """PR-remediation path: the PR head is checked out and returned; no fresh
    branch is cut and no base SHA is stamped."""
    agent = make_agent()

    branch_name, branch_base_sha, watermark = prepare_task_branch(agent,
        pr_branch=_PR_BRANCH, resume_branch=None, task_id="tid",
    )

    assert branch_name == _PR_BRANCH
    assert branch_base_sha is None
    assert watermark == ("0" * 40,)
    assert agent._vcs.rev_parse.call_args_list == [
        call(_PR_BRANCH), call(f"origin/{_PR_BRANCH}"),
    ]
    agent._vcs.checkout.assert_any_call(_PR_BRANCH)
    agent._vcs.checkout_new_branch.assert_not_called()


def test_pr_path_local_miss_falls_back_to_origin(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """PR-path inner fallback: a local checkout miss checks out
    ``origin/<branch>`` then re-creates the local branch, still returning the
    PR head."""
    agent = make_agent()

    def checkout(ref: str) -> None:
        if ref == _PR_BRANCH:
            raise VCSError("no local branch")

    agent._vcs.checkout.side_effect = checkout

    branch_name, branch_base_sha, watermark = prepare_task_branch(agent,
        pr_branch=_PR_BRANCH, resume_branch=None, task_id="tid",
    )

    assert branch_name == _PR_BRANCH
    assert branch_base_sha is None
    assert watermark == ("0" * 40,)
    assert agent._vcs.rev_parse.call_args_list == [
        call(_PR_BRANCH), call(f"origin/{_PR_BRANCH}"),
    ]
    agent._vcs.checkout.assert_any_call(f"origin/{_PR_BRANCH}")
    agent._vcs.checkout_new_branch.assert_called_once_with(_PR_BRANCH)


def test_pr_path_double_failure_clears_to_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """PR-path inner ``except`` arm: both the local checkout and the
    ``origin/<branch>`` fallback raising ``VCSError`` clears the branch to
    ``None``."""
    agent = make_agent()
    agent._vcs.checkout.side_effect = VCSError("both fail")

    branch_name, branch_base_sha, watermark = prepare_task_branch(agent,
        pr_branch=_PR_BRANCH, resume_branch=None, task_id="tid",
    )

    assert branch_name is None
    assert branch_base_sha is None
    assert watermark is None
    agent._vcs.rev_parse.assert_not_called()


def test_resume_path_returns_checkpoint_branch(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Resume path: the checkpoint's branch is checked out and returned with no
    base SHA."""
    agent = make_agent()

    branch_name, branch_base_sha, watermark = prepare_task_branch(agent,
        pr_branch=None, resume_branch=_RESUME_BRANCH, task_id="tid",
    )

    assert branch_name == _RESUME_BRANCH
    assert branch_base_sha is None
    assert watermark is None
    agent._vcs.checkout.assert_any_call(_RESUME_BRANCH)
    agent._vcs.checkout_new_branch.assert_not_called()


def test_resume_path_checkout_failure_clears_to_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Resume-path ``except`` arm: a checkout failure on the checkpoint branch
    falls back to the current branch (``None``)."""
    agent = make_agent()

    def checkout(ref: str) -> None:
        if ref == _RESUME_BRANCH:
            raise VCSError("branch gone")

    agent._vcs.checkout.side_effect = checkout

    branch_name, branch_base_sha, watermark = prepare_task_branch(agent,
        pr_branch=None, resume_branch=_RESUME_BRANCH, task_id="tid",
    )

    assert branch_name is None
    assert watermark is None
    assert branch_base_sha is None


def test_entry_reset_runs_before_branch_selection(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The entry-time reset (``discard_changes`` → ``checkout('main')`` →
    ``has_changes``) is ordered before branch selection so a prior run's diff
    cannot migrate onto the fresh task branch."""
    agent = make_agent()

    branch_name, _, _ = prepare_task_branch(agent,
        pr_branch=None, resume_branch=None, task_id="tid",
    )
    names = _vcs_call_names(agent)

    assert branch_name == "maestro/task/tid"
    assert names.index("discard_changes") < names.index("checkout_new_branch")
    assert names.index("checkout") < names.index("checkout_new_branch")
    assert names.index("has_changes") < names.index("checkout_new_branch")


def test_entry_reset_dirty_tree_warns(
    make_agent,  # type: ignore[no-untyped-def]
    caplog,  # type: ignore[no-untyped-def]
) -> None:
    """``has_changes`` returning ``True`` after the reset logs the dirty-tree
    warning (the ``if`` true arm)."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = True

    with caplog.at_level(logging.WARNING, logger="maestro"):
        prepare_task_branch(agent,
            pr_branch=None, resume_branch=None, task_id="tid",
        )

    assert "Tree still dirty after entry-time reset" in caplog.text


def test_entry_reset_has_changes_raise_is_swallowed(
    make_agent,  # type: ignore[no-untyped-def]
    caplog,  # type: ignore[no-untyped-def]
) -> None:
    """A raising ``has_changes`` is swallowed (the ``except`` arm) — the reset
    logs and branch selection still proceeds."""
    agent = make_agent()
    agent._vcs.has_changes.side_effect = RuntimeError("git blew up")

    with caplog.at_level(logging.WARNING, logger="maestro"):
        branch_name, _, _ = prepare_task_branch(agent,
            pr_branch=None, resume_branch=None, task_id="tid",
        )

    assert branch_name == "maestro/task/tid"
    assert "has_changes failed during entry-time reset" in caplog.text


def test_entry_reset_discard_and_checkout_failures_are_suppressed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The best-effort ``discard_changes`` and ``checkout('main')`` are wrapped
    in ``contextlib.suppress`` — raising there does not abort the stage, which
    still selects the fresh branch."""
    agent = make_agent()
    agent._vcs.discard_changes.side_effect = RuntimeError("no repo")
    agent._vcs.checkout.side_effect = VCSError("cannot reach main")

    branch_name, _, _ = prepare_task_branch(agent,
        pr_branch=None, resume_branch=None, task_id="tid",
    )

    assert branch_name == "maestro/task/tid"
    agent._vcs.checkout_new_branch.assert_called_once_with("maestro/task/tid")


def test_pr_path_watermark_keeps_only_resolved_heads(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An unresolvable ``origin/`` head drops out; none resolving → ``None``."""
    agent = make_agent()
    agent._vcs.rev_parse.side_effect = lambda ref: (
        None if ref.startswith("origin/") else "local1"
    )
    _, _, watermark = prepare_task_branch(agent,
        pr_branch=_PR_BRANCH, resume_branch=None, task_id="tid",
    )
    assert watermark == ("local1",)

    agent = make_agent()
    agent._vcs.rev_parse.return_value = None
    _, _, watermark = prepare_task_branch(agent,
        pr_branch=_PR_BRANCH, resume_branch=None, task_id="tid",
    )
    assert watermark is None
