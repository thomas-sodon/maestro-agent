"""Unit tests for ``maestro.loop.lifecycle.resolve_preflight_standdown``.

The stage is the verbatim lift of ``run``'s top-of-body merge-truth-first
stand-down preflight (proposal 106): on the user-request / queue channel
(``pr_branch is None``) it reads merge-truth for a PR the goal references and,
when that PR is already settled (merged/closed), emits the ``[stood-down]``
chosen-quiet marker and returns a no-op ``TaskRecord`` rather than servicing a
settled fork; otherwise it returns ``None`` and ``run`` falls through to normal
servicing. These tests call the function directly with the agent positional
plus keyword args and pin all four arms; the ``_preflight_merge_truth_standdown``
and ``memory`` seams are mocked, so nothing reads git/host merge-truth or
touches a real state file.

Behaviour-preservation of the *whole* block is proven by the full-``run``
regression harness (the stand-down tests in ``tests/test_pr_remediation_preflight.py``:
``test_run_stands_down_on_merged_pr`` and siblings), which now flows through
this function unchanged; these unit tests exercise the extracted stage in
isolation — in
particular the ``pr_branch`` skip arm and the ``source`` / ``linked_question``
threading, neither asserted directly before.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from maestro.agent import _STAND_DOWN_MARKER
from maestro.loop.lifecycle import resolve_preflight_standdown


def test_pr_branch_set_skips_merge_truth_and_records_nothing(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(a) skip arm: with ``pr_branch`` set the method returns ``None`` and never
    consults merge-truth nor persists a record — the remediation channel is
    covered by the dequeue-face guards / stamp gate, out of scope here."""
    agent = make_agent()
    agent._preflight_merge_truth_standdown = MagicMock()
    agent.memory.record_task = MagicMock()

    result = resolve_preflight_standdown(
        agent,
        goal="Remediate PR #2579: fix CI",
        pr_branch="maestro/evolve/ep2579",
        source="external",
        linked_question=None,
    )

    assert result is None
    agent._preflight_merge_truth_standdown.assert_not_called()
    agent.memory.record_task.assert_not_called()


def test_settled_pr_records_standdown_marker(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(b) record arm: an already-settled referenced PR yields a no-op record
    carrying the ``[stood-down]`` marker (``outcome != "success"``, empty plan,
    no branch), persisted exactly once via ``memory.record_task``."""
    agent = make_agent()
    agent._preflight_merge_truth_standdown = MagicMock(return_value=2579)
    agent.memory.record_task = MagicMock()

    record = resolve_preflight_standdown(
        agent,
        goal="Please check on PR #2579 and remediate it",
        pr_branch=None,
        source="external",
        linked_question=None,
    )

    assert record is not None
    assert record.result_summary == _STAND_DOWN_MARKER
    assert record.outcome != "success"
    assert record.plan == []
    assert record.branch is None
    agent._preflight_merge_truth_standdown.assert_called_once_with(
        "Please check on PR #2579 and remediate it",
    )
    agent.memory.record_task.assert_called_once_with(record)


def test_source_and_linked_question_thread_onto_record(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(c) propagation arm: a non-default ``source`` and a ``linked_question``
    both land on the returned stand-down record unchanged."""
    agent = make_agent()
    agent._preflight_merge_truth_standdown = MagicMock(return_value=2611)
    agent.memory.record_task = MagicMock()

    record = resolve_preflight_standdown(
        agent,
        goal="Resolve PR 2611 for me",
        pr_branch=None,
        source="self_directed",
        linked_question="Why do settled PRs keep getting serviced?",
    )

    assert record is not None
    assert record.source == "self_directed"
    assert record.linked_question == "Why do settled PRs keep getting serviced?"


def test_unsettled_pr_returns_none_and_records_nothing(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(d) fall-through arm: when ``_preflight_merge_truth_standdown`` returns
    ``None`` (no PR reference, or the PR is still open) the method returns
    ``None`` and persists nothing — ``run`` proceeds to normal servicing."""
    agent = make_agent()
    agent._preflight_merge_truth_standdown = MagicMock(return_value=None)
    agent.memory.record_task = MagicMock()

    result = resolve_preflight_standdown(
        agent,
        goal="Improve the dashboard layout",
        pr_branch=None,
        source="external",
        linked_question=None,
    )

    assert result is None
    agent._preflight_merge_truth_standdown.assert_called_once_with(
        "Improve the dashboard layout",
    )
    agent.memory.record_task.assert_not_called()
