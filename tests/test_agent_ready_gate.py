"""Tests for the observed ready-and-waiting dequeue gate ``_gate_pr_remediation_ready``.

The gate is the observed-readiness complement to the keyed stand-down gate:
where ``_gate_pr_remediation_stood_down`` honors a stale ``(pr, head_sha)``-keyed
verdict, this gate consults a live :class:`~maestro.pr_facts.PRReadiness`
recomputed each pop — so a blocker reappearing on the same head re-admits the PR.

The readiness observation is stubbed (no real host/git I/O); the gate body is
exercised as an unbound :class:`MaestroAgent` method against a duck-typed stub.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from maestro.consciousness import PendingPRRequest
from maestro.loop.pr_remediation import (
    _COMMENT_REVIEW_PROBLEM_MARKERS,
    _REPLY_COMPLETION_PROBLEM_MARKER,
    PrRemediationController,
    _format_reply_completion_problem,
)
from maestro.pr_facts import PRReadiness


def _readiness(*, ready: bool, threads: int = 0) -> PRReadiness:
    return PRReadiness(
        pr_number=7,
        branch_landed=False,
        branch_behind=False,
        branch_conflicting=False,
        ci_state="GREEN",
        unresolved_threads=threads,
        unaddressed_overflow=0,
        awaiting_approval=True,
        ready_and_waiting=ready,
        source="host_api",
        observed_at=100.0,
    )


def _gate_stub(*, enabled: bool = True, **overrides: Any) -> SimpleNamespace:
    """Minimal duck-typed agent exposing only what the ready gate touches."""
    stub = SimpleNamespace(
        config=SimpleNamespace(observed_ready_standdown_enabled=enabled),
        memory=MagicMock(),
        remediation_tracker=MagicMock(),
        observe_pr_readiness=MagicMock(return_value=_readiness(ready=True)),
    )
    for name, value in overrides.items():
        setattr(stub, name, value)
    return stub


def _call_ready(
    stub: SimpleNamespace,
    pr_request: PendingPRRequest,
    source: str = "external",
) -> bool:
    return PrRemediationController(stub)._gate_pr_remediation_ready(pr_request, source)


def test_ready_gate_flag_off_passes_through() -> None:
    """With the flag off the gate is a no-op — no observation, no record."""
    stub = _gate_stub(enabled=False)
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    assert _call_ready(stub, entry) is False
    stub.observe_pr_readiness.assert_not_called()
    stub.memory.record_task.assert_not_called()
    stub.remediation_tracker.record_outcome.assert_not_called()


def test_ready_gate_ready_pr_evicts_with_partial_noop() -> None:
    """A ready-and-waiting PR evicts with a zero-cost ``[ready]`` partial no-op."""
    stub = _gate_stub()
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    assert _call_ready(stub, entry) is True
    stub.observe_pr_readiness.assert_called_once_with(7, "b7")
    record = stub.memory.record_task.call_args.args[0]
    assert record.outcome == "partial"
    assert record.outcome != "success"
    assert record.duration_secs == 0.0
    assert "[ready]" in record.result_summary
    assert "[ready]" in record.key_findings
    # The readiness snapshot rides the record for the no-progress sensor.
    assert record.pr_readiness_snapshot is not None
    assert record.pr_readiness_snapshot["ready_and_waiting"] is True
    stub.remediation_tracker.record_outcome.assert_called_once_with(
        7, noop=True,
    )


def test_ready_gate_not_ready_passes_through() -> None:
    """A PR with a code-clearable blocker falls through to genuine remediation."""
    stub = _gate_stub(
        observe_pr_readiness=MagicMock(return_value=_readiness(ready=False)),
    )
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    assert _call_ready(stub, entry) is False
    stub.memory.record_task.assert_not_called()
    stub.remediation_tracker.record_outcome.assert_not_called()


def test_ready_gate_non_remediation_goal_skips_observation() -> None:
    """A directive without a parseable header never observes readiness."""
    stub = _gate_stub()
    entry = PendingPRRequest(directive="do a thing", pr_branch="b7")
    assert _call_ready(stub, entry) is False
    stub.observe_pr_readiness.assert_not_called()


def test_ready_gate_branchless_request_skips_observation() -> None:
    """A branchless request cannot compute branch state — falls through."""
    stub = _gate_stub()
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch=None)
    assert _call_ready(stub, entry) is False
    stub.observe_pr_readiness.assert_not_called()


def test_ready_gate_same_head_new_thread_readmits() -> None:
    """The live re-arm: a fresh blocker on the same head re-admits the PR.

    First observation (clean) evicts with ``[ready]``; a second observation of
    the *same head* carrying a newly-arrived unresolved thread reports
    not-ready, so the gate falls through and the PR re-admits as remediable —
    the re-arm the ``(pr, head_sha)``-keyed stand-down verdict cannot do.
    """
    observe = MagicMock(
        side_effect=[
            _readiness(ready=True),
            _readiness(ready=False, threads=1),
        ],
    )
    stub = _gate_stub(observe_pr_readiness=observe)
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    assert _call_ready(stub, entry) is True
    assert _call_ready(stub, entry) is False
    assert observe.call_count == 2
    # Only the first lap recorded a stand-down; the second re-admits.
    assert stub.memory.record_task.call_count == 1


def test_ready_gate_spares_reply_completion_directive() -> None:
    """A reply-completion goal is never evicted by the ready gate (#4122).

    Readiness counts only non-outdated threads, so a force-admitted reply lap
    answering an outdated thread reads ``ready_and_waiting`` — evicting it would
    re-admit it every Tend pass. The exemption precedes the observation.
    """
    stub = _gate_stub()
    entry = PendingPRRequest(
        directive=f"Remediate PR #2884: {_REPLY_COMPLETION_PROBLEM_MARKER}",
        pr_branch="b7",
    )
    assert _call_ready(stub, entry) is False
    stub.observe_pr_readiness.assert_not_called()
    stub.memory.record_task.assert_not_called()
    stub.remediation_tracker.record_outcome.assert_not_called()


def test_ready_gate_spares_rendered_reply_completion_directive() -> None:
    """A directive rendered by ``_format_reply_completion_problem`` survives."""
    stub = _gate_stub()
    problem = _format_reply_completion_problem(
        2884, [(1, "maestro/x.py", "stale finding", "bug")],
    )
    entry = PendingPRRequest(
        directive=f"Remediate PR #2884: {problem}", pr_branch="b7",
    )
    assert _call_ready(stub, entry) is False
    stub.observe_pr_readiness.assert_not_called()
    stub.memory.record_task.assert_not_called()


@pytest.mark.parametrize(
    "marker",
    [m for m in _COMMENT_REVIEW_PROBLEM_MARKERS
     if m != _REPLY_COMPLETION_PROBLEM_MARKER],
)
def test_ready_gate_still_observes_readiness_visible_markers(marker: str) -> None:
    """Markers whose threads readiness counts keep the zero-cost eviction."""
    stub = _gate_stub()
    entry = PendingPRRequest(
        directive=f"Remediate PR #2884: {marker}", pr_branch="b7",
    )
    _call_ready(stub, entry)
    stub.observe_pr_readiness.assert_called_once()
