"""Tests for the host-closed dequeue gate ``_gate_pr_remediation_host_closed``.

The gate is the reality-grounding terminal the ``mergedAt``-keyed settled arms
(``_gate_pr_remediation_merged`` / ``_gate_pr_remediation_landed``) structurally
miss: both key eviction on the branch having *reached* ``main``, so a PR the
human already CLOSED without merging reads ``mergedAt`` null → not-landed →
re-admitted every iteration. This gate reads the per-iteration ``is_pr_settled``
``MergeTruth`` (no new network) and, on a host-CLOSED-unmerged terminal, evicts
at zero SDK cost and routes the backing plan to the ``rejected`` terminal.

The merge-truth observation is stubbed (no real host/git I/O), the plan lookup
and the gate-activation append are mocked (no filesystem writes), and the gate
body is exercised as an unbound :class:`MaestroAgent` method against a
duck-typed stub — matching the sibling gate tests' harness.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.config import Config
from maestro.consciousness import PendingPRRequest
from maestro.loop.pr_remediation import (
    _REPLY_COMPLETION_PROBLEM_MARKER,
    PrRemediationController,
    _DequeueContext,
)
from maestro.pr_facts import MergeTruth, PRReadiness
from maestro.pr_remediation_state import PrRemediationState


def _truth(state: str, *, branch_landed: bool, pr_number: int = 7) -> MergeTruth:
    """Build a :class:`MergeTruth` for the host-closed gate branch cases."""
    return MergeTruth(
        pr_number=pr_number,
        settled=state != "OPEN",
        state=state,  # type: ignore[arg-type]
        branch_landed=branch_landed,
        source="host_api",
        observed_at=1.0,
    )


def _plan(status: str = "implemented") -> SimpleNamespace:
    """A duck-typed backing plan carrying only the fields the gate reads."""
    return SimpleNamespace(
        plan_id="p1",
        status=status,
        pr_url="https://github.com/o/r/pull/7",
    )


def _gate_stub(
    *, truth: MergeTruth, plan: Any = None, **overrides: Any,
) -> SimpleNamespace:
    """Minimal duck-typed agent exposing only what the host-closed gate touches."""
    stub = SimpleNamespace(
        config=SimpleNamespace(memory_dir=Path("/fake/mem")),
        memory=MagicMock(),
        remediation_tracker=MagicMock(),
        # The deed-ledger backend seam the host-closed arm records through
        # (proposal 106): a mock so the assertion targets the injected backend,
        # not the module-level JSONL function.
        deed_ledger=MagicMock(),
        # The gate-activation backend seam the eviction row is appended through
        # (proposal 106). ``log_gate_activation`` is patched out in these tests,
        # but its ``self.gate_activations`` argument is still evaluated, so the
        # stub must expose the attribute — a mock, since no file is asserted.
        gate_activations=MagicMock(),
        is_pr_settled=MagicMock(return_value=truth),
        _plan_for_pr_number=MagicMock(return_value=plan),
        _notify_best_effort=AsyncMock(),
        # Session-opening seams the gate must never touch: a zero-SDK eviction
        # opens no ``run()`` and no session query. Spied so a regression that
        # minted a session here fails loudly instead of passing structurally.
        run=AsyncMock(),
        query_on_session=AsyncMock(),
    )
    for name, value in overrides.items():
        setattr(stub, name, value)
    return stub


async def _call(
    stub: SimpleNamespace,
    entry: PendingPRRequest,
    source: str = "external",
) -> bool:
    return await PrRemediationController(stub)._gate_pr_remediation_host_closed(entry, source,
    )


@pytest.mark.asyncio
async def test_host_closed_evicts_and_rejects_plan() -> None:
    """A host-CLOSED-unmerged own PR evicts and routes its plan to rejected.

    The core contract: no SDK session is minted (the gate returns ``True`` so
    the caller returns before ``run()``), the backing plan is routed to the
    ``rejected`` terminal via the human/PR-close ``update_plan_status`` path, a
    zero-cost ``partial`` ``[host-closed]`` no-op is recorded, exactly one
    activation row is appended, and the operator is notified once.
    """
    plan = _plan()
    stub = _gate_stub(
        truth=_truth("CLOSED", branch_landed=False), plan=plan,
    )
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    with patch(
        "maestro.agent.audit_checks.log_gate_activation",
    ) as log_activation:
        assert await _call(stub, entry) is True
    stub.is_pr_settled.assert_called_once_with(7)
    # Deed ledger: the honored close terminal records exactly one PR_CLOSE deed
    # at this first-fire branch, through the injected backend seam.
    from maestro.deed_ledger import DeedType

    assert stub.deed_ledger.record.call_count == 1
    assert stub.deed_ledger.record.call_args.args[0] == DeedType.PR_CLOSE
    assert stub.deed_ledger.record.call_args.args[1] == 7
    # Zero SDK cost: the eviction opens no session — neither a ``run()`` task nor
    # a direct session query. Returning ``True`` makes the caller return before
    # ``run()``; the gate body itself must invoke neither seam.
    stub.run.assert_not_called()
    stub.query_on_session.assert_not_called()
    # The backing plan is routed to the rejected terminal (human/PR-close path).
    stub.memory.update_plan_status.assert_called_once_with(
        "p1",
        "rejected",
        rejection_reason="PR closed without merging (host CLOSED-unmerged)",
        expect_status="implemented",
    )
    # A zero-cost partial no-op — never ``success`` (this lap landed nothing).
    record = stub.memory.record_task.call_args.args[0]
    assert record.outcome == "partial"
    assert record.outcome != "success"
    assert record.duration_secs == 0.0
    assert "[host-closed]" in record.result_summary
    assert "[host-closed]" in record.key_findings
    stub.remediation_tracker.record_outcome.assert_called_once_with(
        7, noop=True,
    )
    # Exactly one activation row, and exactly one escalation.
    assert log_activation.call_count == 1
    assert log_activation.call_args.kwargs["gate"] == "host_closed"
    stub._notify_best_effort.assert_called_once()


@pytest.mark.asyncio
async def test_host_closed_open_pr_passes_through() -> None:
    """An OPEN own PR is untouched — falls through to genuine remediation."""
    plan = _plan()
    stub = _gate_stub(truth=_truth("OPEN", branch_landed=False), plan=plan)
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    with patch(
        "maestro.agent.audit_checks.log_gate_activation",
    ) as log_activation:
        assert await _call(stub, entry) is False
    stub.memory.update_plan_status.assert_not_called()
    stub.memory.record_task.assert_not_called()
    stub.remediation_tracker.record_outcome.assert_not_called()
    log_activation.assert_not_called()
    stub._notify_best_effort.assert_not_called()


@pytest.mark.asyncio
async def test_host_closed_merged_pr_not_handled() -> None:
    """A CLOSED-and-merged PR landed — this arm does not evict or reject it."""
    plan = _plan()
    stub = _gate_stub(truth=_truth("MERGED", branch_landed=True), plan=plan)
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    with patch(
        "maestro.agent.audit_checks.log_gate_activation",
    ) as log_activation:
        assert await _call(stub, entry) is False
    stub.memory.update_plan_status.assert_not_called()
    log_activation.assert_not_called()
    stub._notify_best_effort.assert_not_called()


@pytest.mark.asyncio
async def test_host_closed_non_remediation_goal_skips_chokepoint() -> None:
    """A directive without a parseable header never reads merge-truth."""
    stub = _gate_stub(
        truth=_truth("CLOSED", branch_landed=False), plan=_plan(),
    )
    entry = PendingPRRequest(directive="do a thing", pr_branch="b7")
    assert await _call(stub, entry) is False
    stub.is_pr_settled.assert_not_called()
    stub.memory.update_plan_status.assert_not_called()


@pytest.mark.asyncio
async def test_host_closed_already_rejected_does_not_double_fire() -> None:
    """The escalate-at-most-once guard: an already-rejected plan evicts silently.

    A plan a prior pop already routed to the ``rejected`` terminal is still
    evicted (``True``) but appends **no** second activation row, sends **no**
    second escalation, and does not re-route the status — so the gate escalates
    at most once for a given PR number.
    """
    plan = _plan(status="rejected")
    stub = _gate_stub(
        truth=_truth("CLOSED", branch_landed=False), plan=plan,
    )
    stub.memory.update_plan_status.return_value = False
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    with patch(
        "maestro.agent.audit_checks.log_gate_activation",
    ) as log_activation:
        assert await _call(stub, entry) is True
    # Still evicted (records the no-op); the ``expect_status="implemented"``
    # write refuses, so no re-route and no re-escalation.
    stub.memory.update_plan_status.assert_called_once()
    log_activation.assert_not_called()
    stub._notify_best_effort.assert_not_called()
    stub.remediation_tracker.record_outcome.assert_called_once_with(
        7, noop=True,
    )
    # The double-fire no-op branch records no second deed.
    stub.deed_ledger.record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["obsolete", "failed", "merged"])
async def test_host_closed_obsolete_plan_is_not_rejected(status: str) -> None:
    """A plan not ``implemented`` is evicted untouched, never rejected (#4076).

    The agent's own ``obsolete`` terminal (and ``failed``/``merged``) must stay
    distinct from ``rejected``: a human closing a PR the loop already retired is
    not a rejection, so no status write, deed, activation row or notification.
    """
    plan = _plan(status=status)
    stub = _gate_stub(truth=_truth("CLOSED", branch_landed=False), plan=plan)
    # ``update_plan_status`` refuses (returns ``False``) on an ``expect_status``
    # mismatch, exactly as :meth:`Memory.update_plan_status` does.
    stub.memory.update_plan_status.side_effect = (
        lambda *_a, expect_status=None, **_k: plan.status == expect_status
    )
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    with patch(
        "maestro.agent.audit_checks.log_gate_activation",
    ) as log_activation:
        assert await _call(stub, entry) is True
    # The only write attempted is the ``implemented``-guarded one, which refused.
    stub.memory.update_plan_status.assert_called_once()
    assert (
        stub.memory.update_plan_status.call_args.kwargs["expect_status"]
        == "implemented"
    )
    assert plan.status == status
    stub.deed_ledger.record.assert_not_called()
    log_activation.assert_not_called()
    stub._notify_best_effort.assert_not_called()
    record = stub.memory.record_task.call_args.args[0]
    assert record.outcome == "partial"
    assert "[host-closed]" in record.result_summary
    stub.remediation_tracker.record_outcome.assert_called_once_with(
        7, noop=True,
    )


@pytest.mark.asyncio
async def test_host_closed_lost_status_race_evicts_silently() -> None:
    """A concurrent retirement makes ``expect_status`` refuse → silent evict."""
    stub = _gate_stub(truth=_truth("CLOSED", branch_landed=False), plan=_plan())
    stub.memory.update_plan_status.return_value = False
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    with patch(
        "maestro.agent.audit_checks.log_gate_activation",
    ) as log_activation:
        assert await _call(stub, entry) is True
    stub.memory.update_plan_status.assert_called_once()
    stub.deed_ledger.record.assert_not_called()
    log_activation.assert_not_called()
    stub._notify_best_effort.assert_not_called()


@pytest.mark.asyncio
async def test_host_closed_no_backing_plan_still_evicts() -> None:
    """A closed-unmerged PR with no locatable plan still evicts and escalates.

    Fail-safe: the terminal is honored (evict + one activation + one notify)
    even when no plan maps to the PR, but no status update is attempted.
    """
    stub = _gate_stub(truth=_truth("CLOSED", branch_landed=False), plan=None)
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    with patch(
        "maestro.agent.audit_checks.log_gate_activation",
    ) as log_activation:
        assert await _call(stub, entry) is True
    stub.memory.update_plan_status.assert_not_called()
    assert log_activation.call_count == 1
    stub._notify_best_effort.assert_called_once()
    # A closed terminal is honored even without a backing plan → one deed.
    assert stub.deed_ledger.record.call_count == 1
    # No routing happened, so no surface may claim one did.
    assert "no backing plan located" in stub.deed_ledger.record.call_args.kwargs[
        "detail"
    ]
    assert "routed to rejected" not in log_activation.call_args.kwargs["detail"]
    assert "routed" not in stub._notify_best_effort.call_args.args[0]


# --- _plan_for_pr_number lookup helper ------------------------------------


def _lookup_stub(plans: list[Any], pr_map: dict[str, int]) -> SimpleNamespace:
    """A stub agent whose plan lookup resolves ``pr_url`` via *pr_map*."""

    def _parse(url: str) -> int:
        value = pr_map.get(url)
        if value is None:
            raise ValueError(url)
        return value

    return SimpleNamespace(
        memory=SimpleNamespace(get_all_plans=MagicMock(return_value=plans)),
        evolution=SimpleNamespace(
            _pr_number_from_url=MagicMock(side_effect=_parse),
        ),
    )


def test_plan_for_pr_number_returns_matching_plan() -> None:
    """The first plan whose ``pr_url`` resolves to the number is returned.

    Exercises every branch: an empty-``pr_url`` plan is skipped, an unparseable
    ``pr_url`` (ValueError) is skipped, and the matching plan is returned.
    """
    empty = SimpleNamespace(plan_id="p-empty", pr_url="")
    unparseable = SimpleNamespace(plan_id="p-bad", pr_url="not-a-url")
    match = SimpleNamespace(
        plan_id="p-match", pr_url="https://github.com/o/r/pull/7",
    )
    stub = _lookup_stub(
        [empty, unparseable, match],
        {"https://github.com/o/r/pull/7": 7},
    )
    result = PrRemediationController(stub)._plan_for_pr_number(7)
    assert result is match


def test_plan_for_pr_number_no_match_returns_none() -> None:
    """No plan resolving to the number yields ``None``."""
    other = SimpleNamespace(
        plan_id="p-other", pr_url="https://github.com/o/r/pull/9",
    )
    stub = _lookup_stub([other], {"https://github.com/o/r/pull/9": 9})
    assert PrRemediationController(stub)._plan_for_pr_number(7) is None


# ---------------------------------------------------------------------------
# Ready-and-waiting gate under the *default* configuration.
#
# ``_gate_pr_remediation_ready`` is exercised elsewhere with the flag forced on
# (``test_agent_ready_gate.py``); those stubs hard-set
# ``observed_ready_standdown_enabled=True`` and so cannot catch a regression of
# the *default*. The two tests below drive the gate from a real ``Config()``
# with the flag left unset — proving the shipped default now evicts a
# ready-and-waiting owned PR (the proposal-endorsed treadmill fix) and that a
# not-ready PR still falls through to genuine remediation. Flipping the field
# default back to ``False`` turns the first test red.
# ---------------------------------------------------------------------------


def _ready_readiness(*, ready: bool, threads: int = 0) -> PRReadiness:
    """A :class:`PRReadiness` for the default-config ready-gate cases."""
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


def _default_config_ready_stub(readiness: PRReadiness) -> SimpleNamespace:
    """Ready-gate stub whose ``config`` is a real, unmodified ``Config()``.

    The gate reads only ``config.observed_ready_standdown_enabled``; sourcing it
    from a default ``Config()`` (rather than a hand-set namespace) makes these
    tests a tripwire on the *shipped default*.
    """
    return SimpleNamespace(
        config=Config(),
        memory=MagicMock(),
        remediation_tracker=MagicMock(),
        observe_pr_readiness=MagicMock(return_value=readiness),
    )


def test_ready_gate_evicts_under_default_config() -> None:
    """Criterion 1: unset default evicts a ready-and-waiting PR with ``[ready]``.

    ``observed_ready_standdown_enabled`` is left at its ``Config`` default, so a
    ready-and-waiting owned PR is evicted from the remediation dequeue with a
    zero-cost ``partial`` ``[ready]`` no-op rather than re-serviced.
    """
    stub = _default_config_ready_stub(_ready_readiness(ready=True))
    assert stub.config.observed_ready_standdown_enabled is True
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    result = PrRemediationController(stub)._gate_pr_remediation_ready(entry, "external")
    assert result is True
    stub.observe_pr_readiness.assert_called_once_with(7, "b7")
    record = stub.memory.record_task.call_args.args[0]
    assert record.outcome == "partial"
    assert record.outcome != "success"
    assert "[ready]" in record.result_summary
    stub.remediation_tracker.record_outcome.assert_called_once_with(
        7, noop=True,
    )


def test_ready_gate_not_ready_passes_through_under_default_config() -> None:
    """Criterion 2: a not-ready PR still falls through under the default.

    A PR carrying a self-clearable blocker (here an unresolved thread) is
    *not* ``ready_and_waiting``, so even with the gate enabled by default it
    passes through to genuine remediation with no eviction and no record.
    """
    stub = _default_config_ready_stub(
        _ready_readiness(ready=False, threads=1),
    )
    assert stub.config.observed_ready_standdown_enabled is True
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    result = PrRemediationController(stub)._gate_pr_remediation_ready(entry, "external")
    assert result is False
    stub.memory.record_task.assert_not_called()
    stub.remediation_tracker.record_outcome.assert_not_called()


@pytest.mark.asyncio
async def test_chain_admits_reply_completion_lap_on_ready_pr() -> None:
    """A reply-completion entry on a ready-and-waiting PR classifies REMEDIABLE.

    The real ready gate runs against a ``ready_and_waiting`` observation; the
    other arms are stubbed not to fire. Without the #4122 exemption the chain
    would stop at ``READY_AND_WAITING`` and the reply lap would never run.
    """
    ready_stub = _default_config_ready_stub(_ready_readiness(ready=True))
    no_fire = AsyncMock(return_value=False)
    agent = SimpleNamespace(
        _gate_pr_remediation_merged=MagicMock(return_value=False),
        _gate_pr_remediation_landed=MagicMock(return_value=False),
        _gate_pr_remediation_host_closed=no_fire,
        _gate_pr_remediation_ready=lambda entry, source: (
            PrRemediationController(ready_stub)._gate_pr_remediation_ready(entry, source)
        ),
        _gate_pr_remediation_stood_down=no_fire,
        _gate_pr_remediation_merge_block=no_fire,
        _gate_pr_remediation_branch_resolved=no_fire,
        # Non-pure-QG red-root read (#4216): a GREEN PR pays for no checkout.
        observe_pr_readiness=MagicMock(
            return_value=SimpleNamespace(ci_state="GREEN"),
        ),
    )
    goal = f"Remediate PR #7: {_REPLY_COMPLETION_PROBLEM_MARKER}"
    ctx = _DequeueContext(
        goal=goal,
        pr_branch="b7",
        source="external",
        entry=PendingPRRequest(directive=goal, pr_branch="b7"),
    )
    state = await PrRemediationController(agent).classify_dequeue(ctx)  # type: ignore[arg-type]
    assert state is PrRemediationState.REMEDIABLE
    ready_stub.observe_pr_readiness.assert_not_called()
    ready_stub.memory.record_task.assert_not_called()
