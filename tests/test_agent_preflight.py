"""Tests for the stand-down preflight (``_preflight_merge_truth_standdown``).

The preflight is ``run()``'s literal first action on the user-request / queue
channel. It reads merge-truth through the shared ``is_pr_settled`` chokepoint
(proposal 087) — whose own fail-loud ``git fetch origin`` is the fetch-first
guarantee — and, on the *unsettled* fall-through only, calls
``_fetch_origin_and_fast_forward`` so a stale local tree can neither *miss* an
already-landed PR nor hand the SDK a *phantom conflict* to re-resolve against an
outdated base. These tests pin that contract with mocked merge-truth / git
subprocess — no real processes, per Testing Principles.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from maestro.pr_facts import MergeTruth


def _settled_truth(state: str, *, settled: bool) -> MergeTruth:
    """A :class:`MergeTruth` for the proposal-087 chokepoint preflight tests."""
    return MergeTruth(
        pr_number=2696,
        settled=settled,
        state=state,  # type: ignore[arg-type]
        branch_landed=state == "MERGED",
        source="host_api",
        observed_at=1.0,
    )


def _completed(cmd: list[str], returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """Build a benign ``CompletedProcess`` for a mocked ``git`` invocation."""
    return subprocess.CompletedProcess(
        args=cmd, returncode=returncode, stdout="", stderr="",
    )


# ──────────────────────────────────────────────────────────────────────
# No PR reference → no fetch cost
# ──────────────────────────────────────────────────────────────────────


def test_no_fetch_without_pr_reference(make_agent):  # type: ignore[no-untyped-def]
    """A goal naming no PR short-circuits before the fetch — a PR-less goal
    pays no git cost (the fetch is scoped to goals that name a PR)."""
    agent = make_agent()
    agent._fetch_origin_and_fast_forward = MagicMock()  # type: ignore[method-assign]

    result = agent._preflight_merge_truth_standdown("Improve the dashboard layout")

    assert result is None
    agent._fetch_origin_and_fast_forward.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Direct unit coverage of the fetch+fast-forward helper
# ──────────────────────────────────────────────────────────────────────


def test_fetch_origin_and_fast_forward_runs_both_git_commands(  # type: ignore[no-untyped-def]
    make_agent,
):
    """The helper issues ``git fetch origin`` then the ``--ff-only`` upstream
    merge, both bound to the agent's workspace."""
    agent = make_agent()
    seen: list[tuple[list[str], object]] = []

    def _run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append((cmd, kwargs.get("cwd")))
        return _completed(cmd)

    with patch("maestro.agent.subprocess.run", side_effect=_run):
        agent._fetch_origin_and_fast_forward()

    assert [c for c, _ in seen] == [
        ["git", "fetch", "origin"],
        ["git", "merge", "--ff-only", "@{u}"],
    ]
    assert all(cwd == agent.config.workspace for _, cwd in seen)


@pytest.mark.parametrize(
    "exc",
    [subprocess.TimeoutExpired(["git", "fetch"], 1), OSError("boom")],
)
def test_fetch_origin_fetch_failure_skips_fast_forward(  # type: ignore[no-untyped-def]
    make_agent, exc,
):
    """When the fetch itself fails, the helper returns before attempting the
    fast-forward (only one subprocess call)."""
    agent = make_agent()
    calls: list[list[str]] = []

    def _run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        raise exc

    with patch("maestro.agent.subprocess.run", side_effect=_run):
        agent._fetch_origin_and_fast_forward()

    assert calls == [["git", "fetch", "origin"]]


@pytest.mark.parametrize(
    "exc",
    [subprocess.TimeoutExpired(["git", "merge"], 1), OSError("boom")],
)
def test_fetch_origin_fast_forward_failure_swallowed(  # type: ignore[no-untyped-def]
    make_agent, exc,
):
    """An error raised by the ``--ff-only`` merge is swallowed (best-effort): the
    fetch succeeded, the fast-forward failed, and the helper returns without
    raising so the downstream assessment proceeds on the un-fast-forwarded tree."""
    agent = make_agent()
    calls: list[list[str]] = []

    def _run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        if cmd[:3] == ["git", "merge", "--ff-only"]:
            raise exc
        return _completed(cmd)

    with patch("maestro.agent.subprocess.run", side_effect=_run):
        agent._fetch_origin_and_fast_forward()

    assert calls == [
        ["git", "fetch", "origin"],
        ["git", "merge", "--ff-only", "@{u}"],
    ]


# ──────────────────────────────────────────────────────────────────────
# Proposal 087: stand-down decision through the ``is_pr_settled`` chokepoint
# ──────────────────────────────────────────────────────────────────────


def test_preflight_merged_stands_down(make_agent):  # type: ignore[no-untyped-def]
    """A MERGED ``MergeTruth`` stands the preflight down — no fall-through
    freshening, the chokepoint settles it."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_settled_truth("MERGED", settled=True),
    )
    agent._fetch_origin_and_fast_forward = MagicMock()  # type: ignore[method-assign]

    result = agent._preflight_merge_truth_standdown("Remediate PR #2696: fix")

    assert result == 2696
    agent.is_pr_settled.assert_called_once_with(2696)
    agent._fetch_origin_and_fast_forward.assert_not_called()


def test_preflight_closed_unmerged_stands_down(make_agent):  # type: ignore[no-untyped-def]
    """A CLOSED-unmerged PR is settled, so the preflight stands down."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_settled_truth("CLOSED", settled=True),
    )

    result = agent._preflight_merge_truth_standdown("Remediate PR #2696: fix")

    assert result == 2696


def test_preflight_open_falls_through_and_freshens(make_agent):  # type: ignore[no-untyped-def]
    """An OPEN PR is unsettled → returns None and still freshens the local tree
    for the downstream servicing fall-through."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_settled_truth("OPEN", settled=False),
    )
    agent._fetch_origin_and_fast_forward = MagicMock()  # type: ignore[method-assign]

    result = agent._preflight_merge_truth_standdown("Remediate PR #2696: fix")

    assert result is None
    agent._fetch_origin_and_fast_forward.assert_called_once_with()


def test_preflight_no_pr_reference_skips_chokepoint(make_agent):  # type: ignore[no-untyped-def]
    """A goal naming no PR returns None without consulting the chokepoint."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]

    result = agent._preflight_merge_truth_standdown("no pr here")

    assert result is None
    agent.is_pr_settled.assert_not_called()
