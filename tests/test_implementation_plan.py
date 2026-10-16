"""implement_plan scope gate, scope replan, scope-reject slicing and slice-rollout
sequencing (``maestro/loop/implementation.py``); split out of
``tests/test_agent.py``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.agent import (
    MaestroAgent,
)
from maestro.pr_facts import MergeTruth
from maestro.sdk import ErrorCategory, SDKError
from maestro.types import EvolutionProposalStatus, PlanStatus
from tests.conftest import MockQueryResult
from tests.test_agent import (
    _OK_REVIEW,
    _pending_review_plan,
)

# --- implement_plan scope gate tests ---


@pytest.mark.asyncio
async def test_implement_plan_scope_gate_rejects_uncovered_multi_file(make_agent) -> None:  # type: ignore[no-untyped-def]
    """implement_plan() rejects multi-file plans missing per-file verification.

    The first rejection now triggers ``_replan_for_scope``; we mock it to
    return ``None`` here so the test exercises the unchanged failure path.
    """
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-scope-gate",
        title="Scope Gate Test",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Add function to maestro/planner.py",
            "Wire into maestro/agent.py",
        ],
        files_affected=["maestro/planner.py", "maestro/agent.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    # Should NOT call implement — should return early
    implement_mock = AsyncMock()
    with (
        patch.object(agent, "_replan_for_scope", AsyncMock(return_value=None)),
        patch.object(agent.evolution, "implement", implement_mock),
    ):
        await agent.implement_plan("p-scope-gate")

    implement_mock.assert_not_called()

    stored = agent.memory.get_plan("p-scope-gate")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1
    assert "scope gate" in (stored.last_failure_reason or "").lower()


@pytest.mark.asyncio
async def test_implement_plan_scope_gate_writes_journal(make_agent) -> None:  # type: ignore[no-untyped-def]
    """implement_plan() writes a journal entry when the scope gate rejects."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-scope-journal",
        title="Scope Journal Test",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Modify maestro/foo.py",
            "Modify maestro/bar.py",
        ],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    write_mock = MagicMock()
    with (
        patch.object(agent, "_replan_for_scope", AsyncMock(return_value=None)),
        patch.object(agent.journal, "record_event", write_mock),
    ):
        await agent.implement_plan("p-scope-journal")

    write_mock.assert_called_once()
    _, kwargs = write_mock.call_args
    assert "scope gate" in kwargs["content"].lower()
    assert kwargs["entry_type"] == "evolution_note"
    assert kwargs["triggered_by"].startswith("scope_gate:")


@pytest.mark.asyncio
async def test_implement_plan_scope_gate_journal_failure_tolerated(make_agent) -> None:  # type: ignore[no-untyped-def]
    """implement_plan() tolerates journal write failure in scope gate path."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-scope-journal-fail",
        title="Scope Journal Fail Test",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Modify maestro/foo.py",
            "Modify maestro/bar.py",
        ],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    with (
        patch.object(agent, "_replan_for_scope", AsyncMock(return_value=None)),
        patch.object(agent.journal, "record_event", side_effect=OSError("disk full")),
    ):
        await agent.implement_plan("p-scope-journal-fail")

    # Plan should still be marked as failed despite journal write error
    stored = agent.memory.get_plan("p-scope-journal-fail")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1


@pytest.mark.asyncio
async def test_implement_plan_scope_gate_passes_single_file(make_agent) -> None:  # type: ignore[no-untyped-def]
    """implement_plan() proceeds normally for single-file plans (scope gate passes)."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-scope-pass",
        title="Single File Plan",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Add function to maestro/planner.py",
            "Run the test suite",
        ],
        files_affected=["maestro/planner.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    # When the scope gate passes, ``implement`` must actually be reached.
    # Bind the mocks so the gate-pass path can be asserted end to end.
    implement_mock = AsyncMock(return_value="base000")
    create_pr_mock = AsyncMock(return_value="https://github.com/repo/pull/99")
    with (
        patch.object(agent.evolution, "implement", implement_mock),
        patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW),
        patch.object(agent.contributions, "get_current_head", return_value="abc123"),
        patch.object(agent.contributions, "get_new_commits", return_value=[]),
        patch.object(agent.evolution, "create_pr", create_pr_mock),
        patch.object(agent.contributions, "record_own_commit", MagicMock()),
        patch.object(agent.consciousness, "increment_evolution", MagicMock()),
    ):
        await agent.implement_plan("p-scope-pass")

    # Load-bearing regression guard: a scope gate that wrongly blocked this
    # single-file plan would return before reaching ``implement``. Asserting
    # the call fails loudly on that regression (the prior no-op assertion did
    # not). ``create_pr`` awaited once further proves the gate-pass path ran
    # end to end.
    implement_mock.assert_called_once()
    create_pr_mock.assert_awaited_once()

    # Read plan state from the mem_fs-backed store (no ``get_plan`` patch), so a
    # silent scope-gate demotion is caught. The status stays ``approved``
    # because ``create_pr`` — which flips it to ``implemented`` mid-flight — is
    # mocked; a scope-gate block instead routes through
    # ``update_plan_status(..., "failed", ...)``, so a non-``failed`` status
    # proves the gate passed.
    stored = agent.memory.get_plan("p-scope-pass")
    assert stored is not None
    assert stored.status == "approved"


@pytest.mark.asyncio
async def test_implement_plan_replans_once_when_scope_rejects(make_agent) -> None:  # type: ignore[no-untyped-def]
    """First scope-gate rejection invokes ``_replan_for_scope``; if the
    replanned changes pass the gate, the plan persists with the new prose,
    ``failure_count`` is NOT incremented, and implementation proceeds."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-replan-success",
        title="Replan success",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Add helper to maestro/foo.py",
            "Wire helper into maestro/bar.py",
        ],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    rewritten = [
        "Add helper to maestro/foo.py; tests added in tests/test_foo.py verify the helper",
        "Wire helper into maestro/bar.py; verify integration in tests/test_bar.py",
    ]
    update_spy = MagicMock(wraps=agent.memory.update_plan_proposed_changes)

    # Stub the rest of implement_plan so the test focuses on the gate.
    with (
        patch.object(agent, "_replan_for_scope", AsyncMock(return_value=rewritten)),
        patch.object(agent.memory, "update_plan_proposed_changes", update_spy),
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base000")),
        patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW),
        patch.object(agent.contributions, "get_current_head", return_value="abc123"),
        patch.object(agent.contributions, "get_new_commits", return_value=[]),
        patch.object(agent.evolution, "create_pr", AsyncMock(return_value="https://github.com/repo/pull/1")),
        patch.object(agent.contributions, "record_own_commit", MagicMock()),
        patch.object(agent.consciousness, "increment_evolution", MagicMock()),
    ):
        await agent.implement_plan("p-replan-success")

    update_spy.assert_called_once_with("p-replan-success", rewritten)

    stored = agent.memory.get_plan("p-replan-success")
    assert stored is not None
    assert stored.proposed_changes == rewritten
    # failure_count must NOT have been bumped — the plan made forward progress.
    assert stored.failure_count == 0
    # status not "failed" because the gate passed after replan.
    assert stored.status != "failed"


@pytest.mark.asyncio
async def test_implement_plan_aborts_when_replan_persist_returns_false(make_agent) -> None:  # type: ignore[no-untyped-def]
    """If the scope-gate replan rewrite passes the gate but
    ``update_plan_proposed_changes`` returns ``False`` (the plan vanished
    from disk between ``get_plan`` and the write), implementation aborts
    loudly rather than running against unpersisted prose."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-replan-vanished",
        title="Replan vanished",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Add helper to maestro/foo.py",
            "Wire helper into maestro/bar.py",
        ],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    rewritten = [
        "Add helper to maestro/foo.py; tests added in tests/test_foo.py verify the helper",
        "Wire helper into maestro/bar.py; verify integration in tests/test_bar.py",
    ]
    # The plan passes the recheck, but the persist write reports the plan
    # is no longer on disk.
    update_stub = MagicMock(return_value=False)
    implement_mock = AsyncMock()

    with (
        patch.object(agent, "_replan_for_scope", AsyncMock(return_value=rewritten)),
        patch.object(agent.memory, "update_plan_proposed_changes", update_stub),
        patch.object(agent.evolution, "implement", implement_mock),
    ):
        await agent.implement_plan("p-replan-vanished")

    # The persist was attempted but returned False, so the run aborted before
    # touching the evolution implementation.
    update_stub.assert_called_once_with("p-replan-vanished", rewritten)
    implement_mock.assert_not_called()

    # The on-disk plan body is unchanged (the rewrite was never persisted).
    stored = agent.memory.get_plan("p-replan-vanished")
    assert stored is not None
    assert stored.proposed_changes == [
        "Add helper to maestro/foo.py",
        "Wire helper into maestro/bar.py",
    ]


@pytest.mark.asyncio
async def test_implement_plan_marks_failed_when_replan_output_still_fails_gate(make_agent) -> None:  # type: ignore[no-untyped-def]
    """If ``_replan_for_scope`` returns prose that STILL lacks per-file
    verification, the plan is demoted to ``failed`` with ``failure_count=1``
    and the original rejection reason is preserved."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-replan-bad",
        title="Replan bad",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Modify maestro/foo.py",
            "Modify maestro/bar.py",
        ],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    # Replan returns same shape — still no verification language → re-fails the gate.
    bad_rewrite = [
        "Modify maestro/foo.py with a tweak",
        "Modify maestro/bar.py with a tweak",
    ]
    update_spy = MagicMock(wraps=agent.memory.update_plan_proposed_changes)
    implement_mock = AsyncMock()

    with (
        patch.object(agent, "_replan_for_scope", AsyncMock(return_value=bad_rewrite)),
        patch.object(agent.memory, "update_plan_proposed_changes", update_spy),
        patch.object(agent.evolution, "implement", implement_mock),
    ):
        await agent.implement_plan("p-replan-bad")

    # update_plan_proposed_changes must NOT have been called — the rewritten
    # prose still failed the gate, so we never persisted it.
    update_spy.assert_not_called()
    implement_mock.assert_not_called()

    stored = agent.memory.get_plan("p-replan-bad")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1
    assert "scope gate" in (stored.last_failure_reason or "").lower()


@pytest.mark.asyncio
async def test_implement_plan_marks_failed_when_replan_returns_none(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When ``_replan_for_scope`` returns ``None`` (malformed LLM output
    or length mismatch), the plan is demoted to ``failed``
    without persisting any rewrite."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-replan-none",
        title="Replan none",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Modify maestro/foo.py",
            "Modify maestro/bar.py",
        ],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    update_spy = MagicMock(wraps=agent.memory.update_plan_proposed_changes)
    implement_mock = AsyncMock()

    with (
        patch.object(agent, "_replan_for_scope", AsyncMock(return_value=None)),
        patch.object(agent.memory, "update_plan_proposed_changes", update_spy),
        patch.object(agent.evolution, "implement", implement_mock),
    ):
        await agent.implement_plan("p-replan-none")

    update_spy.assert_not_called()
    implement_mock.assert_not_called()

    stored = agent.memory.get_plan("p-replan-none")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1


@pytest.mark.asyncio
async def test_implement_plan_does_not_replan_on_retry(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Replan happens at most once per plan lifetime — once
    ``failure_count >= 1``, the gate-rejection path skips the LLM call
    and goes straight to the failure path."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-replan-retry",
        title="Replan retry",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Modify maestro/foo.py",
            "Modify maestro/bar.py",
        ],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="failed",
        created_at="2025-01-01T00:00:00Z",
        failure_count=1,
    )
    agent.memory.save_evolution_plan(plan)

    replan_mock = AsyncMock(return_value=None)
    implement_mock = AsyncMock()

    with (
        patch.object(agent, "_replan_for_scope", replan_mock),
        patch.object(agent.evolution, "implement", implement_mock),
    ):
        await agent.implement_plan("p-replan-retry")

    # _replan_for_scope must NOT have been called — already replanned once.
    replan_mock.assert_not_called()
    implement_mock.assert_not_called()

    stored = agent.memory.get_plan("p-replan-retry")
    assert stored is not None
    assert stored.status == "failed"
    # bumped from 1 → 2
    assert stored.failure_count == 2


@pytest.mark.asyncio
async def test_implement_plan_scope_reject_slices_when_flag_on(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 118 Step 6: flag on + a genuinely-too-large scope-rejected plan.

    When ``propose_plan_chain`` yields an ``(EvolutionProposal, slices)`` chain,
    the scope-rejection path submits the chain, retires the original plan as
    ``obsolete`` (not ``failed``), and never calls ``_replan_for_scope``.
    """
    from maestro.memory import EvolutionPlan, EvolutionProposal

    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True

    plan = EvolutionPlan(
        plan_id="p-scope-slice",
        title="Too-large scope plan",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Modify maestro/foo.py",
            "Modify maestro/bar.py",
        ],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    proposal = EvolutionProposal(
        proposal_id="prop-scope-1",
        title="Sliced initiative",
        goal="Deliver the big change across ordered PRs",
        status="rolling_out",
        created_at="2025-01-01T00:00:00Z",
    )
    slices = [
        EvolutionPlan(
            plan_id=f"slice-{i}",
            title=f"Slice {i}",
            category="capability",
            rationale="slice",
            proposed_changes=[f"Change {i}; verify with tests"],
            files_affected=["maestro/foo.py"],
            expected_impact="better",
            status="pending_review" if i == 0 else "sliced",
            created_at="2025-01-01T00:00:00Z",
            parent_plan_id="prop-scope-1",
            phase_index=i,
        )
        for i in range(2)
    ]

    chain_mock = AsyncMock(return_value=(proposal, slices))
    # submit_proposal_chain returns True iff the head was accepted and the
    # chain persisted; the slicing helper retires the original only on True.
    submit_mock = AsyncMock(return_value=True)
    replan_mock = AsyncMock(return_value=None)
    implement_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "propose_plan_chain", chain_mock),
        patch.object(agent.evolution, "submit_proposal_chain", submit_mock),
        patch.object(agent, "_replan_for_scope", replan_mock),
        patch.object(agent.evolution, "implement", implement_mock),
    ):
        await agent.implement_plan("p-scope-slice")

    # Chain submitted; original retired as obsolete, not demoted; no replan.
    submit_mock.assert_awaited_once_with(
        proposal, slices, exempt_saturation=False,
    )
    replan_mock.assert_not_called()
    implement_mock.assert_not_called()
    stored = agent.memory.get_plan("p-scope-slice")
    assert stored is not None
    assert stored.status == "obsolete"
    assert "prop-scope-1" in (stored.obsolete_reason or "")
    # A self-prune must never populate rejection_reason.
    assert stored.rejection_reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize("red_gate", [False, True])
async def test_scope_sliced_gap_plan_pins_every_slice(make_agent, red_gate) -> None:  # type: ignore[no-untyped-def]
    """A sliced gap plan's chain keeps its issue and category (155)."""
    from maestro.memory import EvolutionPlan, EvolutionProposal

    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    plan = EvolutionPlan(
        plan_id="p-gap-slice", title="Too-large gap plan", category="maintenance",
        rationale="test", proposed_changes=["Modify maestro/foo.py", "Modify maestro/bar.py"],
        files_affected=["maestro/foo.py", "maestro/bar.py"], expected_impact="better",
        status="approved", created_at="2025-01-01T00:00:00Z", issue_number=9,
    )
    agent.memory.save_evolution_plan(plan)
    proposal = EvolutionProposal(
        proposal_id="prop-gap", title="Sliced", goal="g", status="rolling_out",
        created_at="2025-01-01T00:00:00Z",
    )
    slices = [
        EvolutionPlan(
            plan_id=f"gs-{i}", title=f"Gap slice {i}", category="capability",
            rationale="slice", proposed_changes=[f"Change {i}; verify with tests"],
            files_affected=["maestro/foo.py"], expected_impact="better",
            status="pending_review" if i == 0 else "sliced",
            created_at="2025-01-01T00:00:00Z", parent_plan_id="prop-gap", phase_index=i,
            problem_ref="x",
        )
        for i in range(2)
    ]
    submit_mock = AsyncMock(return_value=True)
    with (
        patch.object(agent.evolution, "propose_plan_chain", AsyncMock(return_value=(proposal, slices))),
        patch.object(agent.evolution, "submit_proposal_chain", submit_mock),
        patch.object(agent, "_replan_for_scope", AsyncMock(return_value=None)),
        patch.object(agent.evolution, "implement", AsyncMock()),
        patch.object(agent.evolution, "is_open_gap_issue", return_value=True),
        patch.object(agent.evolution, "gap_issue_bypasses_gates", return_value=red_gate),
    ):
        await agent.implement_plan("p-gap-slice")

    submit_mock.assert_awaited_once_with(
        proposal, slices, exempt_saturation=red_gate,
    )
    assert [s.issue_number for s in slices] == [9, 9]
    assert [s.category for s in slices] == ["maintenance", "maintenance"]
    assert [s.problem_ref for s in slices] == [None, None]


@pytest.mark.asyncio
async def test_scope_sliced_non_gap_issue_plan_is_not_pinned(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An ``issue_number`` that is no open gap issue is not propagated (155)."""
    from maestro.memory import EvolutionPlan, EvolutionProposal

    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    plan = EvolutionPlan(
        plan_id="p-llm-issue", title="Too-large plan", category="capability",
        rationale="test", proposed_changes=["Modify maestro/foo.py", "Modify maestro/bar.py"],
        files_affected=["maestro/foo.py", "maestro/bar.py"], expected_impact="better",
        status="approved", created_at="2025-01-01T00:00:00Z", issue_number=77,
    )
    agent.memory.save_evolution_plan(plan)
    proposal = EvolutionProposal(
        proposal_id="prop-llm", title="Sliced", goal="g", status="rolling_out",
        created_at="2025-01-01T00:00:00Z",
    )
    slices = [
        EvolutionPlan(
            plan_id=f"ls-{i}", title=f"Slice {i}", category="capability",
            rationale="slice", proposed_changes=[f"Change {i}; verify with tests"],
            files_affected=["maestro/foo.py"], expected_impact="better",
            status="pending_review" if i == 0 else "sliced",
            created_at="2025-01-01T00:00:00Z", parent_plan_id="prop-llm", phase_index=i,
            problem_ref="x",
        )
        for i in range(2)
    ]
    chain_mock = AsyncMock(return_value=(proposal, slices))
    bypass_mock = MagicMock(return_value=False)
    with (
        patch.object(agent.evolution, "propose_plan_chain", chain_mock),
        patch.object(agent.evolution, "submit_proposal_chain", AsyncMock(return_value=True)),
        patch.object(agent, "_replan_for_scope", AsyncMock(return_value=None)),
        patch.object(agent.evolution, "implement", AsyncMock()),
        patch.object(agent.evolution, "is_open_gap_issue", return_value=False),
        patch.object(agent.evolution, "gap_issue_bypasses_gates", bypass_mock),
    ):
        await agent.implement_plan("p-llm-issue")

    assert chain_mock.await_args.kwargs["gap_pinned"] is False
    bypass_mock.assert_not_called()
    assert [s.issue_number for s in slices] == [None, None]
    assert [s.category for s in slices] == ["capability", "capability"]
    assert [s.problem_ref for s in slices] == ["x", "x"]


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        (("open", "maestro:gate — the main quality gate is red"), True),
        (("closed", "maestro:gate — the main quality gate is red"), False),
        (("open", "an issue a planner cited"), False),
        (None, False),
    ],
)
def test_is_open_gap_issue_reads_state_and_door_title(make_agent, meta, expected) -> None:  # type: ignore[no-untyped-def]
    """Only an open, door-shaped gap issue counts; anything else fails closed."""
    from maestro.vcs import IssueMeta

    agent = make_agent()
    tracker = MagicMock()
    tracker.meta.return_value = (
        None if meta is None else IssueMeta(state=meta[0], title=meta[1])
    )
    agent.evolution._issues = tracker
    with (
        patch("maestro.evolution.build_gap_store"),
        patch("maestro.evolution.classify_gap_trust", return_value="rederived"),
        patch.object(type(agent.evolution), "code_host"),
    ):
        assert agent.evolution.is_open_gap_issue(9) is expected


@pytest.mark.parametrize(
    ("trust", "expected"),
    [("operator", True), ("rederived", True), ("unverified", False)],
)
def test_is_open_gap_issue_requires_gap_trust(make_agent, trust, expected) -> None:  # type: ignore[no-untyped-def]
    """A door-titled open issue no operator labelled and the door never wrote is not pinned."""
    from maestro.vcs import IssueMeta

    agent = make_agent()
    tracker = MagicMock()
    tracker.meta.return_value = IssueMeta(
        state="open", title="maestro:gate — the main quality gate is red",
        labels=("gap:gate",),
    )
    agent.evolution._issues = tracker
    store = MagicMock()
    with (
        patch("maestro.evolution.build_gap_store", return_value=store),
        patch("maestro.evolution.classify_gap_trust", return_value=trust) as classify,
        patch.object(type(agent.evolution), "code_host"),
    ):
        assert agent.evolution.is_open_gap_issue(9) is expected
    issue = classify.call_args.args[0]
    assert issue.number == 9
    assert issue.labels == ["gap:gate"]
    store.close.assert_called_once()


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        (("open", "maestro:gate — red gate"), True),
        (("closed", "maestro:gate — red gate"), False),
        (("open", "not a door title"), False),
        (None, False),
    ],
)
def test_gap_issue_bypasses_gates_reads_red_gate_title(make_agent, meta, expected) -> None:  # type: ignore[no-untyped-def]
    """Only an open issue whose door title names ``maestro:gate`` bypasses gates."""
    from unittest.mock import MagicMock

    from maestro.evolution_fuel import GATE_OWNER_ID, gap_title, parse_owner_id
    from maestro.vcs import IssueMeta

    agent = make_agent()
    tracker = MagicMock()
    if meta is None:
        tracker.meta.return_value = None
    else:
        owner = parse_owner_id(GATE_OWNER_ID)
        assert owner is not None
        title = gap_title(owner) if meta[1].startswith("maestro:gate") else meta[1]
        tracker.meta.return_value = IssueMeta(state=meta[0], title=title)
    agent.evolution._issues = tracker
    assert agent.evolution.gap_issue_bypasses_gates(9) is expected



@pytest.mark.asyncio
async def test_implement_plan_scope_reject_lone_plan_falls_back_to_replan(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 118 Step 6: flag on but chain emission collapses to a lone plan.

    When ``propose_plan_chain`` returns a lone ``EvolutionPlan`` (the intent
    still fits one PR), the helper returns ``False`` and the unchanged
    ``_replan_for_scope`` rewrite-or-fail fallback runs — here it fails and the
    plan demotes to ``failed`` exactly as without the flag.
    """
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True

    plan = EvolutionPlan(
        plan_id="p-scope-lone",
        title="Too-large scope plan",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Modify maestro/foo.py",
            "Modify maestro/bar.py",
        ],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    lone = EvolutionPlan(
        plan_id="lone-1",
        title="Single-PR intent",
        category="capability",
        rationale="fits one PR",
        proposed_changes=["Change; verify with tests"],
        files_affected=["maestro/foo.py"],
        expected_impact="better",
        status="pending_review",
        created_at="2025-01-01T00:00:00Z",
    )

    chain_mock = AsyncMock(return_value=lone)
    submit_mock = AsyncMock()
    replan_mock = AsyncMock(return_value=None)
    implement_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "propose_plan_chain", chain_mock),
        patch.object(agent.evolution, "submit_proposal_chain", submit_mock),
        patch.object(agent, "_replan_for_scope", replan_mock),
        patch.object(agent.evolution, "implement", implement_mock),
    ):
        await agent.implement_plan("p-scope-lone")

    # Chain emission was consulted but collapsed; no chain was submitted, the
    # replan fallback ran, and the plan demoted to failed as usual.
    chain_mock.assert_awaited_once()
    submit_mock.assert_not_called()
    replan_mock.assert_awaited_once()
    implement_mock.assert_not_called()
    stored = agent.memory.get_plan("p-scope-lone")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1


@pytest.mark.asyncio
async def test_implement_plan_scope_reject_no_slicing_when_flag_off(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 118 Step 6: flag off → chain emission is never attempted.

    The scope-rejection path is byte-identical to before proposal 118: the
    slicing helper is never called and the plan demotes to ``failed``.
    """
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    # multi_pr_proposals_enabled is False by default in the fixture; assert it
    # to document the precondition.
    assert agent.config.multi_pr_proposals_enabled is False

    plan = EvolutionPlan(
        plan_id="p-scope-flagoff",
        title="Too-large scope plan",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Modify maestro/foo.py",
            "Modify maestro/bar.py",
        ],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    slice_mock = AsyncMock(return_value=True)
    replan_mock = AsyncMock(return_value=None)
    implement_mock = AsyncMock()
    with (
        patch.object(agent, "_slice_scope_rejected_plan", slice_mock),
        patch.object(agent, "_replan_for_scope", replan_mock),
        patch.object(agent.evolution, "implement", implement_mock),
    ):
        await agent.implement_plan("p-scope-flagoff")

    # The slicing helper must never be reached with the flag off.
    slice_mock.assert_not_called()
    implement_mock.assert_not_called()
    stored = agent.memory.get_plan("p-scope-flagoff")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1


@pytest.mark.asyncio
async def test_implement_plan_scope_reject_skips_slicing_for_chain_slice(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 118 Step 6: a chain slice is never re-sliced at the scope gate.

    A plan that is itself a slice of an in-flight ``EvolutionProposal``
    (``parent_plan_id`` set) reaches ``implement_plan`` as the eligible head
    slice. Even with the flag on, the scope-rejection path must NOT route it to
    ``_slice_scope_rejected_plan`` — doing so mints a competing proposal and
    retires the slice as ``obsolete``, which strands the parent chain (the
    obsolete slice never opens a PR, so it never settles and its downstream
    slices are orphaned). A too-large slice must fall through to the unchanged
    ``_replan_for_scope`` rewrite-or-fail path instead, demoting to ``failed``.
    """
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True

    plan = EvolutionPlan(
        plan_id="p-scope-childslice",
        title="Too-large head slice",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Modify maestro/foo.py",
            "Modify maestro/bar.py",
        ],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
        parent_plan_id="prop-parent-1",
        phase_index=0,
    )
    agent.memory.save_evolution_plan(plan)

    slice_mock = AsyncMock(return_value=True)
    replan_mock = AsyncMock(return_value=None)
    implement_mock = AsyncMock()
    with (
        patch.object(agent, "_slice_scope_rejected_plan", slice_mock),
        patch.object(agent, "_replan_for_scope", replan_mock),
        patch.object(agent.evolution, "implement", implement_mock),
    ):
        await agent.implement_plan("p-scope-childslice")

    # The slicing helper must never fire for a plan that is itself a slice; the
    # unchanged replan-or-fail fallback runs and the plan demotes to failed.
    slice_mock.assert_not_called()
    replan_mock.assert_awaited_once()
    implement_mock.assert_not_called()
    stored = agent.memory.get_plan("p-scope-childslice")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1


@pytest.mark.asyncio
async def test_slice_scope_rejected_plan_tolerates_parse_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``_slice_scope_rejected_plan`` returns False on an unparseable chain reply.

    A ``ProposalParseError`` from ``propose_plan_chain`` must fall back to the
    replan path (return False), never crash the implementation.
    """
    from maestro.evolution import ProposalParseError
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True

    plan = EvolutionPlan(
        plan_id="p-scope-parsefail",
        title="Too-large scope plan",
        category="capability",
        rationale="test",
        proposed_changes=["Change one", "Change two"],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    chain_mock = AsyncMock(side_effect=ProposalParseError("bad json"))
    with patch.object(agent.evolution, "propose_plan_chain", chain_mock):
        result = await agent._slice_scope_rejected_plan(plan, "scope reason")

    assert result is False


@pytest.mark.asyncio
async def test_slice_scope_rejected_plan_returns_true_when_dispose_fails(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The chain is authoritative even if the original can't be retired.

    If ``mark_plan_obsolete`` returns ``False`` (the original vanished or its
    status changed between read and dispose), the helper still returns ``True``
    — the chain was already submitted — and surfaces the anomaly via a warning
    rather than silently demoting the sliced intent.
    """
    from maestro.memory import EvolutionPlan, EvolutionProposal

    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True

    # Deliberately NOT saved to memory, so mark_plan_obsolete finds no row
    # and returns False, exercising the dispose-failure branch.
    plan = EvolutionPlan(
        plan_id="p-gone",
        title="Vanished plan",
        category="capability",
        rationale="test",
        proposed_changes=["Change one", "Change two"],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    proposal = EvolutionProposal(
        proposal_id="prop-gone",
        title="Sliced initiative",
        goal="Deliver the change across ordered PRs",
        status="rolling_out",
        created_at="2025-01-01T00:00:00Z",
    )
    slices = [
        EvolutionPlan(
            plan_id=f"gone-slice-{i}",
            title=f"Slice {i}",
            category="capability",
            rationale="slice",
            proposed_changes=[f"Change {i}; verify with tests"],
            files_affected=["maestro/foo.py"],
            expected_impact="better",
            status="pending_review" if i == 0 else "sliced",
            created_at="2025-01-01T00:00:00Z",
            parent_plan_id=proposal.proposal_id,
            phase_index=i,
        )
        for i in range(2)
    ]

    chain_mock = AsyncMock(return_value=(proposal, slices))
    # Head accepted + chain persisted → True; the dispose of the (vanished)
    # original then fails, but the chain is authoritative so we still return True.
    submit_mock = AsyncMock(return_value=True)
    with (
        patch.object(agent.evolution, "propose_plan_chain", chain_mock),
        patch.object(agent.evolution, "submit_proposal_chain", submit_mock),
    ):
        result = await agent._slice_scope_rejected_plan(plan, "scope reason")

    assert result is True
    submit_mock.assert_awaited_once_with(
        proposal, slices, exempt_saturation=False,
    )
    # The original was never persisted, so it could not be retired.
    assert agent.memory.get_plan("p-gone") is None


@pytest.mark.asyncio
async def test_slice_scope_rejected_plan_refused_head_leaves_original(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A guard-refused chain head leaves the original plan intact (r1 regression).

    Exercises the *real* ``submit_proposal_chain`` (not a mock): a pre-seeded
    pending plan sharing the head slice's exact title makes ``submit_for_review``
    refuse the head, so the chain persists nothing. ``_slice_scope_rejected_plan``
    must then return ``False`` and leave the original approved plan untouched —
    never drop it with a false "superseded by proposal" reason — so the caller
    falls back to ``_replan_for_scope``. Before the fix the helper ignored the
    (``None``) return and obsoleted the original regardless.
    """
    from maestro.memory import EvolutionPlan, EvolutionProposal

    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True

    plan = EvolutionPlan(
        plan_id="p-orig",
        title="Original too-large plan",
        category="capability",
        rationale="test",
        proposed_changes=["Change one", "Change two"],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    # Pre-seed a pending plan sharing the head slice's exact title so
    # submit_for_review's exact-title dedup refuses the head without saving it.
    # (No parent_plan_id, so the sibling exemption never applies to it.)
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="blocker",
        title="Head slice",
        category="capability",
        rationale="already pending",
        proposed_changes=["change"],
        files_affected=[],
        expected_impact="",
        status="pending_review",
        created_at="2025-01-01T00:00:00Z",
    ))

    proposal = EvolutionProposal(
        proposal_id="prop-refused",
        title="Sliced initiative",
        goal="Deliver the change across ordered PRs",
        status="rolling_out",
        created_at="2025-01-01T00:00:00Z",
    )
    slices = [
        EvolutionPlan(
            plan_id=f"slice-{i}",
            title="Head slice" if i == 0 else f"Slice {i}",
            category="capability",
            rationale="slice",
            proposed_changes=[f"Change {i}; verify with tests"],
            files_affected=["maestro/foo.py"],
            expected_impact="better",
            status="pending_review" if i == 0 else "sliced",
            created_at="2025-01-01T00:00:00Z",
            parent_plan_id="prop-refused",
            phase_index=i,
        )
        for i in range(2)
    ]

    chain_mock = AsyncMock(return_value=(proposal, slices))
    with patch.object(agent.evolution, "propose_plan_chain", chain_mock):
        # submit_proposal_chain is REAL: the exact-title guard refuses the head.
        result = await agent._slice_scope_rejected_plan(plan, "scope reason")

    # The head was refused, so no chain was persisted and the original stands.
    assert result is False
    stored = agent.memory.get_plan("p-orig")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.obsolete_reason is None
    # No headless chain: neither the proposal nor the head slice was persisted.
    assert [p.proposal_id for p in agent.memory.get_evolution_proposals()] == []
    assert agent.memory.get_plan("slice-0") is None


@pytest.mark.asyncio
async def test_slice_scope_rejected_plan_pre_mint_gate_refuses(make_agent) -> None:  # type: ignore[no-untyped-def]
    """118+120: a saturated backlog blocks slicing before the priced call.

    With ``pre_mint_generation_gate_enabled`` on and the live-work backlog
    saturated, ``_slice_scope_rejected_plan`` consults the shared generation
    authority first: on a non-admit verdict it returns ``False`` without
    awaiting ``propose_plan_chain``, so the caller falls back to the
    zero-net-new-plan ``_replan_for_scope`` rewrite (proposal 120).
    """
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    agent.config.pre_mint_generation_gate_enabled = True
    # Saturate the live-work corpus at the submit-block threshold (default 5)
    # so the candidate-independent backlog arm refuses.
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=[_pending_review_plan(f"p{i}") for i in range(5)],
    )

    plan = EvolutionPlan(
        plan_id="p-scope-saturated",
        title="Too-large scope plan",
        category="capability",
        rationale="test",
        proposed_changes=["Change one", "Change two"],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    chain_mock = AsyncMock()
    with patch.object(agent.evolution, "propose_plan_chain", chain_mock):
        result = await agent._slice_scope_rejected_plan(plan, "scope reason")

    assert result is False
    chain_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_slice_scope_rejected_plan_pre_mint_gate_admits(make_agent) -> None:  # type: ignore[no-untyped-def]
    """118+120: with the gate on but the backlog unsaturated, slicing proceeds.

    The authority admits (empty backlog), so ``propose_plan_chain`` is spent
    exactly as with the flag off — a lone-plan result still returns ``False``
    for the caller's rewrite fallback.
    """
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    agent.config.pre_mint_generation_gate_enabled = True
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=[],
    )

    plan = EvolutionPlan(
        plan_id="p-scope-unsaturated",
        title="Too-large scope plan",
        category="capability",
        rationale="test",
        proposed_changes=["Change one", "Change two"],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    # A lone plan (not a tuple) → the intent still fits one PR → return False.
    chain_mock = AsyncMock(return_value=plan)
    with patch.object(agent.evolution, "propose_plan_chain", chain_mock):
        result = await agent._slice_scope_rejected_plan(plan, "scope reason")

    assert result is False
    chain_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_replan_for_scope_returns_none_on_length_mismatch(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``_replan_for_scope`` validates that the rewritten list has the
    same length as the original; mismatched length returns ``None``."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-len-mismatch",
        title="Length mismatch",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Edit maestro/foo.py",
            "Edit maestro/bar.py",
        ],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )

    # SDK returns a list with only one entry — should be two.
    bad_payload = json.dumps({"proposed_changes": ["only one entry, tests added"]})
    with patch(
        "maestro.loop.implementation.query",
        AsyncMock(return_value=MockQueryResult(response=bad_payload)),
    ):
        result = await agent._replan_for_scope(plan, "Plan-scope gate: ...")

    assert result is None


@pytest.mark.asyncio
async def test_replan_for_scope_returns_none_on_malformed_json(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Malformed JSON / wrong shape from the LLM returns ``None``."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-bad-json",
        title="Bad JSON",
        category="capability",
        rationale="test",
        proposed_changes=["Edit maestro/foo.py", "Edit maestro/bar.py"],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )

    # Not JSON at all.
    with patch(
        "maestro.loop.implementation.query",
        AsyncMock(return_value=MockQueryResult(response="not JSON at all")),
    ):
        assert await agent._replan_for_scope(plan, "reason") is None

    # JSON but wrong top-level shape.
    with patch(
        "maestro.loop.implementation.query",
        AsyncMock(return_value=MockQueryResult(response='["a", "b"]')),
    ):
        assert await agent._replan_for_scope(plan, "reason") is None

    # JSON with proposed_changes containing non-strings.
    with patch(
        "maestro.loop.implementation.query",
        AsyncMock(return_value=MockQueryResult(
            response=json.dumps({"proposed_changes": ["ok", 42]}),
        )),
    ):
        assert await agent._replan_for_scope(plan, "reason") is None


@pytest.mark.asyncio
async def test_replan_for_scope_returns_list_on_well_formed_response(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A well-formed JSON response with a list of strings of matching
    length is returned verbatim."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-good-json",
        title="Good JSON",
        category="capability",
        rationale="test",
        proposed_changes=["Edit maestro/foo.py", "Edit maestro/bar.py"],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )

    rewritten = [
        "Edit maestro/foo.py; tests added in tests/test_foo.py verify behavior",
        "Edit maestro/bar.py; verify integration in tests/test_bar.py",
    ]
    payload = json.dumps({"proposed_changes": rewritten})
    with patch(
        "maestro.loop.implementation.query",
        AsyncMock(return_value=MockQueryResult(response=payload)),
    ):
        result = await agent._replan_for_scope(plan, "Plan-scope gate: ...")

    # Equality is the only meaningful check here: ``parse_json`` round-trips
    # through ``json.loads``, so the list ``_replan_for_scope`` returns can
    # never be the same object as the test-local ``rewritten``. A non-identity
    # assertion (``assert result is not rewritten``) would therefore be
    # unconditionally true and would validate nothing about the function's
    # copy semantics.
    assert result == rewritten


@pytest.mark.asyncio
async def test_replan_for_scope_truncates_overlong_entries(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An over-long rewritten entry is clamped to
    ``_PLAN_CHANGE_ITEM_MAX_CHARS`` (truncated, not rejected), while an
    entry exactly at the cap is returned unchanged (no off-by-one)."""
    from maestro.memory import _PLAN_CHANGE_ITEM_MAX_CHARS, EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-overlong",
        title="Overlong rewrite",
        category="capability",
        rationale="test",
        proposed_changes=["Edit maestro/foo.py", "Edit maestro/bar.py"],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )

    over_long = "x" * (_PLAN_CHANGE_ITEM_MAX_CHARS + 500)
    at_limit = "y" * _PLAN_CHANGE_ITEM_MAX_CHARS
    payload = json.dumps({"proposed_changes": [over_long, at_limit]})
    with patch(
        "maestro.loop.implementation.query",
        AsyncMock(return_value=MockQueryResult(response=payload)),
    ):
        result = await agent._replan_for_scope(plan, "Plan-scope gate: ...")

    assert result is not None
    assert all(len(c) <= _PLAN_CHANGE_ITEM_MAX_CHARS for c in result)
    assert len(result[0]) == _PLAN_CHANGE_ITEM_MAX_CHARS
    assert result[0] == over_long[:_PLAN_CHANGE_ITEM_MAX_CHARS]
    # The exactly-at-limit entry is returned unchanged.
    assert result[1] == at_limit


def test_scope_replan_prompt_renders_max_chars() -> None:
    """``SCOPE_REPLAN_PROMPT`` exposes a ``{max_chars}`` field and renders
    the passed cap, guarding against the template and call site drifting
    apart."""
    from maestro.memory import _PLAN_CHANGE_ITEM_MAX_CHARS
    from maestro.prompts.defaults import SCOPE_REPLAN_PROMPT

    rendered = SCOPE_REPLAN_PROMPT.format(
        rejection_reason="reason",
        files_affected="- maestro/foo.py",
        proposed_changes="1. Edit maestro/foo.py",
        n_steps=1,
        gap_enumeration="",
        max_chars=_PLAN_CHANGE_ITEM_MAX_CHARS,
    )

    assert str(_PLAN_CHANGE_ITEM_MAX_CHARS) in rendered
    assert "{max_chars}" not in rendered


@pytest.mark.asyncio
async def test_replan_for_scope_uses_session_when_provided(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When a session is passed, ``_replan_for_scope`` routes through
    ``query_on_session`` instead of the unconditional ``query`` shape —
    matches the documented session-fallback contract enforced by
    ``test_session_attachment``."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-with-session",
        title="With session",
        category="capability",
        rationale="test",
        proposed_changes=["Edit maestro/foo.py", "Edit maestro/bar.py"],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )

    rewritten = [
        "Edit maestro/foo.py; tests added in tests/test_foo.py verify behavior",
        "Edit maestro/bar.py; verify integration in tests/test_bar.py",
    ]
    payload = json.dumps({"proposed_changes": rewritten})

    fake_session = MagicMock()
    qos_mock = AsyncMock(return_value=MockQueryResult(response=payload))
    q_mock = AsyncMock(return_value=MockQueryResult(response="should not be used"))
    with (
        patch("maestro.loop.implementation.query_on_session", qos_mock),
        patch("maestro.loop.implementation.query", q_mock),
    ):
        result = await agent._replan_for_scope(
            plan, "Plan-scope gate: ...", session=fake_session,
        )

    assert result == rewritten
    qos_mock.assert_awaited_once()
    q_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_replan_for_scope_propagates_sdk_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """SDK errors during replan propagate to ``implement_plan`` (#4063)."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-sdk-err",
        title="SDK error",
        category="capability",
        rationale="test",
        proposed_changes=["Edit maestro/foo.py", "Edit maestro/bar.py"],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )

    with patch(
        "maestro.loop.implementation.query",
        AsyncMock(side_effect=SDKError("rate limit", category=ErrorCategory.RATE_LIMIT)),
    ), pytest.raises(SDKError):
        await agent._replan_for_scope(plan, "reason")


@pytest.mark.asyncio
async def test_replan_for_scope_includes_gap_list(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``_replan_for_scope`` injects a concrete per-file gap enumeration
    into the prompt so the LLM knows exactly which files need
    verification language added."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-gap-list",
        title="Gap list test",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Edit maestro/foo.py to add helper",
            "Wire maestro/bar.py to use helper",
            "Update CLAUDE.md to document helper",
        ],
        files_affected=["maestro/foo.py", "maestro/bar.py", "CLAUDE.md"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )

    rewritten = [
        "Edit maestro/foo.py to add helper; verified by tests in tests/test_foo.py",
        "Wire maestro/bar.py to use helper; verified by tests in tests/test_bar.py",
        "Update CLAUDE.md to document helper",
    ]
    payload = json.dumps({"proposed_changes": rewritten})

    captured_prompt: str | None = None

    async def capture_query(**kwargs: Any) -> MockQueryResult:  # type: ignore[return]
        nonlocal captured_prompt
        captured_prompt = kwargs.get("prompt", "")
        return MockQueryResult(response=payload)

    with patch("maestro.loop.implementation.query", side_effect=capture_query):
        result = await agent._replan_for_scope(
            plan, "Plan-scope gate: multi-file plan lacks per-file verification for: maestro/foo.py, maestro/bar.py",
        )

    assert result == rewritten
    assert captured_prompt is not None
    # The prompt must enumerate both unverified files with their step text.
    assert "maestro/foo.py" in captured_prompt
    assert "maestro/bar.py" in captured_prompt
    assert "Edit maestro/foo.py to add helper" in captured_prompt
    assert "Wire maestro/bar.py to use helper" in captured_prompt
    # Doc files must NOT appear in the gap list. The gap_block bullets use
    # the unique ``- {path}: "{step_text}"`` format, so ``- CLAUDE.md: "`` only
    # ever appears inside the gap enumeration — asserting its absence directly
    # targets the gap block regardless of the surrounding template order.
    assert '- CLAUDE.md: "' not in captured_prompt, "doc file leaked into gap_block"


@pytest.mark.asyncio
async def test_replan_for_scope_empty_gap_list(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When ``_find_unverified_code_files`` returns no gaps (e.g. all
    files are doc-only), ``_replan_for_scope`` still produces a valid
    prompt with an empty gap-enumeration block."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    # Construct a plan where all files are doc-only — _find_unverified_code_files
    # returns [] for these.
    plan = EvolutionPlan(
        plan_id="p-empty-gaps",
        title="Empty gaps",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Update CLAUDE.md architecture section",
            "Update docs/design.md overview",
        ],
        files_affected=["CLAUDE.md", "docs/design.md"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )

    rewritten = [
        "Update CLAUDE.md architecture section with verification notes",
        "Update docs/design.md overview with verification notes",
    ]
    payload = json.dumps({"proposed_changes": rewritten})
    with patch(
        "maestro.loop.implementation.query",
        AsyncMock(return_value=MockQueryResult(response=payload)),
    ):
        result = await agent._replan_for_scope(plan, "some reason")

    assert result == rewritten


@pytest.mark.asyncio
async def test_replan_for_scope_reuses_precomputed_gaps(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When the caller threads in ``gaps``, ``_replan_for_scope`` reuses
    them instead of re-walking the steps — ``_find_unverified_code_files``
    is never invoked, and the passed pairs drive the prompt enumeration."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-reuse-gaps",
        title="Reuse gaps",
        category="capability",
        rationale="test",
        proposed_changes=["Edit maestro/foo.py", "Edit maestro/bar.py"],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )

    rewritten = [
        "Edit maestro/foo.py; tests verify the change",
        "Edit maestro/bar.py; tests verify the change",
    ]
    payload = json.dumps({"proposed_changes": rewritten})
    captured_prompt: str | None = None

    async def capture_query(**kwargs: Any) -> MockQueryResult:  # type: ignore[return]
        nonlocal captured_prompt
        captured_prompt = kwargs.get("prompt", "")
        return MockQueryResult(response=payload)

    walk_spy = MagicMock()
    with (
        patch("maestro.loop.implementation._find_unverified_code_files", walk_spy),
        patch("maestro.loop.implementation.query", side_effect=capture_query),
    ):
        result = await agent._replan_for_scope(
            plan, "reason", gaps=[("maestro/bar.py", "Edit maestro/bar.py")],
        )

    assert result == rewritten
    # The threaded gaps were reused — no second walk of the steps.
    walk_spy.assert_not_called()
    assert captured_prompt is not None
    assert '- maestro/bar.py: "Edit maestro/bar.py"' in captured_prompt


@pytest.mark.asyncio
async def test_replan_for_scope_self_computes_when_gaps_none(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The ``gaps=None`` fallback self-computes the gap list — calling
    ``_find_unverified_code_files`` exactly once from the synthetic steps."""
    from maestro.memory import EvolutionPlan
    from maestro.planner import _find_unverified_code_files as real_fuf

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-self-compute",
        title="Self compute",
        category="capability",
        rationale="test",
        proposed_changes=["Edit maestro/foo.py", "Edit maestro/bar.py"],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )

    rewritten = [
        "Edit maestro/foo.py; tests verify the change",
        "Edit maestro/bar.py; tests verify the change",
    ]
    payload = json.dumps({"proposed_changes": rewritten})
    walk_spy = MagicMock(wraps=real_fuf)
    with (
        patch("maestro.loop.implementation._find_unverified_code_files", walk_spy),
        patch(
            "maestro.loop.implementation.query",
            AsyncMock(return_value=MockQueryResult(response=payload)),
        ),
    ):
        result = await agent._replan_for_scope(plan, "reason")

    assert result == rewritten
    walk_spy.assert_called_once()


@pytest.mark.asyncio
async def test_implement_plan_scope_walk_runs_once_on_replan_path(make_agent) -> None:  # type: ignore[no-untyped-def]
    """On the scope-replan path the gap walk runs exactly once: the gate
    computes it via ``check_plan_scope_detailed`` and threads it into
    ``_replan_for_scope``, which no longer recomputes. With the replan
    returning malformed output (so no fresh recheck fires), the shared
    ``_find_unverified_code_files`` spy across both modules sees one call —
    proving the previously-duplicated walk is gone."""
    from maestro.memory import EvolutionPlan
    from maestro.planner import _find_unverified_code_files as real_fuf

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-walk-once",
        title="Walk once",
        category="capability",
        rationale="test",
        proposed_changes=[
            "Add helper to maestro/foo.py",
            "Wire helper into maestro/bar.py",
        ],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    walk_spy = MagicMock(wraps=real_fuf)
    # Replan returns malformed output → None → no recheck → plan marked failed.
    with (
        patch("maestro.loop.implementation._find_unverified_code_files", walk_spy),
        patch("maestro.planner._find_unverified_code_files", walk_spy),
        patch(
            "maestro.loop.implementation.query",
            AsyncMock(return_value=MockQueryResult(response="not JSON")),
        ),
    ):
        await agent.implement_plan("p-walk-once")

    assert walk_spy.call_count == 1


def test_scope_replan_prompt_has_single_blank_line_when_no_gaps() -> None:
    """When ``gap_enumeration`` is empty, the rendered prompt should keep a
    single blank line between the ``proposed_changes`` block and the
    'Rewrite each step' instruction — not two."""
    from maestro.prompts.defaults import SCOPE_REPLAN_PROMPT

    rendered = SCOPE_REPLAN_PROMPT.format(
        rejection_reason="reason",
        files_affected="- CLAUDE.md",
        proposed_changes="1. Update CLAUDE.md",
        n_steps=1,
        gap_enumeration="",
        max_chars=1000,
    )

    assert "\n\n\n" not in rendered
    # And the populated case must still produce blank lines on both sides
    # of the gap enumeration block.
    rendered_with_gap = SCOPE_REPLAN_PROMPT.format(
        rejection_reason="reason",
        files_affected="- maestro/foo.py",
        proposed_changes="1. Edit maestro/foo.py",
        n_steps=1,
        gap_enumeration='\nThe following 1 file(s) ...\n- maestro/foo.py: "Edit"\n\nFor each file above, add ...\n',
        max_chars=1000,
    )
    assert "\n\nThe following" in rendered_with_gap
    assert "...\n\nRewrite each step" in rendered_with_gap


def _approved_plan(plan_id: str):  # type: ignore[no-untyped-def]
    """A minimal ``approved`` evolution plan the implement channel would run."""
    from maestro.memory import EvolutionPlan

    return EvolutionPlan(
        plan_id=plan_id,
        title="Ship the approved capability",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_implement_approved_plans_pre_mint_gate_refuses_saturated(make_agent) -> None:  # type: ignore[no-untyped-def]
    """120: with the flag on and a saturated live-work backlog, the
    implement-approved-plans channel declines to open a net-new evolution PR —
    an otherwise-implementable approved plan is left un-implemented, in-flight
    slice rollouts still advance, and the bounded prune/land directive is written
    into the empty ``next_task_directive`` slot."""
    from maestro.generation_gate import GENERATION_STANDDOWN_DIRECTIVE

    agent = make_agent()
    agent.config.pre_mint_generation_gate_enabled = True
    # _wip_owns_slot short-circuits False so the new gate is what stands down.
    agent.config.wip_primacy_enabled = False
    agent._advance_slice_rollouts = AsyncMock()  # type: ignore[method-assign]
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]
    agent.notifier.resolve_plan = AsyncMock()  # type: ignore[method-assign]
    # 4 pending_review + 1 approved = 5 live plans → saturated at the default
    # block threshold (5). The approved plan would otherwise be implemented, so
    # its non-implementation proves the gate — not an empty eligible set — stands
    # it down.
    plans = [_pending_review_plan(f"p{i}") for i in range(4)]
    plans.append(_approved_plan("approved-1"))
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=plans,
    )

    await agent._implement_approved_plans()

    agent._advance_slice_rollouts.assert_awaited_once()
    agent.implement_plan.assert_not_awaited()
    assert (
        agent.consciousness.load_self_model().next_task_directive
        == GENERATION_STANDDOWN_DIRECTIVE
    )


@pytest.mark.asyncio
async def test_implement_approved_plans_pre_mint_gate_admits_unsaturated(make_agent) -> None:  # type: ignore[no-untyped-def]
    """120: with the flag on but a non-saturated backlog, the gate admits — the
    approved plan is implemented and no directive is authored."""
    agent = make_agent()
    agent.config.pre_mint_generation_gate_enabled = True
    agent.config.wip_primacy_enabled = False
    agent._advance_slice_rollouts = AsyncMock()  # type: ignore[method-assign]
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]
    agent.notifier.resolve_plan = AsyncMock()  # type: ignore[method-assign]
    # A single approved plan → 1 live plan, well below the block threshold.
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=[_approved_plan("approved-1")],
    )

    await agent._implement_approved_plans()

    agent._advance_slice_rollouts.assert_awaited_once()
    agent.implement_plan.assert_awaited_once_with("approved-1")
    assert agent.consciousness.load_self_model().next_task_directive is None


@pytest.mark.asyncio
async def test_implement_approved_plans_pre_mint_gate_flag_off_is_byte_identical(make_agent) -> None:  # type: ignore[no-untyped-def]
    """120: with the flag off, a saturated backlog is ignored — the approved
    plan is implemented and no directive is written, byte-identical to today."""
    agent = make_agent()
    agent.config.pre_mint_generation_gate_enabled = False
    agent.config.wip_primacy_enabled = False
    agent._advance_slice_rollouts = AsyncMock()  # type: ignore[method-assign]
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]
    agent.notifier.resolve_plan = AsyncMock()  # type: ignore[method-assign]
    plans = [_pending_review_plan(f"p{i}") for i in range(4)]
    plans.append(_approved_plan("approved-1"))
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=plans,
    )

    await agent._implement_approved_plans()

    agent.implement_plan.assert_awaited_once_with("approved-1")
    assert agent.consciousness.load_self_model().next_task_directive is None


@pytest.mark.asyncio
async def test_implement_approved_plans_skips_blocked(make_agent) -> None:  # type: ignore[no-untyped-def]
    """131: a ``blocked`` plan is inert on the implement channel — the eligibility
    filter selects only ``approved``/``failed``, so the ``blocked`` plan is never
    attempted while the sibling ``approved`` plan is implemented as usual."""
    agent = make_agent()
    agent.config.pre_mint_generation_gate_enabled = False
    agent.config.wip_primacy_enabled = False
    agent._advance_slice_rollouts = AsyncMock()  # type: ignore[method-assign]
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]
    agent.notifier.resolve_plan = AsyncMock()  # type: ignore[method-assign]
    blocked = _approved_plan("blocked-1")
    blocked.status = "blocked"
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=[_approved_plan("approved-1"), blocked],
    )

    await agent._implement_approved_plans()

    # Only the approved plan is attempted; the blocked plan is excluded by the
    # eligibility filter, so implement_plan is never called for it.
    agent.implement_plan.assert_awaited_once_with("approved-1")


@pytest.mark.asyncio
async def test_implement_approved_plans_wip_primacy_preempts_pre_mint_gate(make_agent) -> None:  # type: ignore[no-untyped-def]
    """120: the ``_wip_owns_slot`` short-circuit precedes the pre-mint gate here
    too — owned WIP stands the channel down before the gate runs, so no directive
    is written even under a saturated backlog. ``_advance_slice_rollouts`` still
    runs first (it precedes the wip check), and nothing is implemented."""
    from maestro.wip_primacy import TendOwnedPR

    agent = make_agent()
    agent.config.pre_mint_generation_gate_enabled = True
    agent.config.wip_primacy_enabled = True
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=TendOwnedPR(pr_number=99, owned_blocker="CONFLICTING"),
    )
    agent._advance_slice_rollouts = AsyncMock()  # type: ignore[method-assign]
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]
    plans = [_pending_review_plan(f"p{i}") for i in range(4)]
    plans.append(_approved_plan("approved-1"))
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=plans,
    )

    await agent._implement_approved_plans()

    agent._advance_slice_rollouts.assert_awaited_once()
    agent.implement_plan.assert_not_awaited()
    # The wip path writes no directive; the pre-mint gate (which would) never ran.
    assert agent.consciousness.load_self_model().next_task_directive is None


# ---------------------------------------------------------------------------
# Proposal 118 — one-slice-open sequencing (_advance_slice_rollouts,
# _slice_pr_settled) + implementation-time ordering gate
# ---------------------------------------------------------------------------


def _seed_proposal_118(
    agent: MaestroAgent,
    proposal_id: str = "prop",
    status: EvolutionProposalStatus = "rolling_out",
) -> None:
    from maestro.memory import EvolutionProposal

    agent.memory.write_evolution_proposal(EvolutionProposal(
        proposal_id=proposal_id, title="Multi-slice initiative",
        goal="Land it across slices",
        status=status,
        created_at="2025-01-01T00:00:00Z",
    ))


def _seed_slice_plan(
    agent: MaestroAgent,
    plan_id: str,
    *,
    phase_index: int,
    status: PlanStatus,
    parent_plan_id: str | None = "prop",
    pr_url: str | None = None,
) -> None:
    from maestro.memory import EvolutionPlan

    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id=plan_id, title=f"Slice {phase_index}",
        category="capability", rationale="slice work",
        proposed_changes=["change"], files_affected=["file.py"],
        expected_impact="better", status=status,
        parent_plan_id=parent_plan_id, phase_index=phase_index, pr_url=pr_url,
        created_at="2025-01-01T00:00:00Z",
    ))


def _slice_obj(plan_id: str, *, pr_url: str | None):
    from maestro.memory import EvolutionPlan

    return EvolutionPlan(
        plan_id=plan_id, title="s", category="capability",
        rationale="r", proposed_changes=["c"], files_affected=[],
        expected_impact="", pr_url=pr_url, parent_plan_id="prop",
        phase_index=0, created_at="2025-01-01T00:00:00Z",
    )


def test_slice_pr_settled_variants(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The settlement closure: no ``pr_url`` and an unparseable ``pr_url`` are
    unsettled without touching the merge-truth door; a valid ``pr_url`` reads
    through :meth:`is_pr_settled`."""
    from maestro.pr_facts import MergeTruth

    agent = make_agent()
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=7, settled=True, state="MERGED", branch_landed=True,
            source="host_api", observed_at=0.0,
        ),
    )

    assert agent._slice_pr_settled(_slice_obj("a", pr_url=None)) is False
    assert agent._slice_pr_settled(_slice_obj("b", pr_url="not-a-url")) is False
    agent.is_pr_settled.assert_not_called()
    good = _slice_obj("c", pr_url="https://github.com/o/r/pull/7")
    assert agent._slice_pr_settled(good) is True
    agent.is_pr_settled.assert_called_once_with(7)


@pytest.mark.asyncio
async def test_advance_slice_rollouts_promotes_next_after_predecessor_settles(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A held slice is promoted for review only once its predecessor settles."""
    from maestro.pr_facts import MergeTruth

    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    _seed_proposal_118(agent)
    _seed_slice_plan(
        agent, "s0", phase_index=0, status="implemented",
        pr_url="https://github.com/o/r/pull/100",
    )
    _seed_slice_plan(agent, "s1", phase_index=1, status="sliced")

    settled = {100: False}

    def _truth(pr_num: int) -> MergeTruth:
        return MergeTruth(
            pr_number=pr_num, settled=settled.get(pr_num, False), state="OPEN",
            branch_landed=False, source="host_api", observed_at=0.0,
        )

    agent.is_pr_settled = MagicMock(side_effect=_truth)  # type: ignore[method-assign]

    # Predecessor unsettled → nothing promoted; s1 stays dormant.
    await agent._advance_slice_rollouts()
    assert agent.memory.get_plan("s1").status == "sliced"

    # Predecessor settles → s1 becomes pending_review.
    settled[100] = True
    await agent._advance_slice_rollouts()
    assert agent.memory.get_plan("s1").status == "pending_review"


@pytest.mark.asyncio
async def test_advance_slice_rollouts_completes_when_all_slices_settle(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """Once every slice has settled the owning proposal advances to complete."""
    from maestro.pr_facts import MergeTruth

    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    _seed_proposal_118(agent)
    _seed_slice_plan(
        agent, "s0", phase_index=0, status="implemented",
        pr_url="https://github.com/o/r/pull/100",
    )
    _seed_slice_plan(
        agent, "s1", phase_index=1, status="implemented",
        pr_url="https://github.com/o/r/pull/101",
    )
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=0, settled=True, state="MERGED", branch_landed=True,
            source="host_api", observed_at=0.0,
        ),
    )

    await agent._advance_slice_rollouts()

    assert agent.memory.get_evolution_proposals()[0].status == "complete"


@pytest.mark.asyncio
async def test_advance_slice_rollouts_skips_non_rolling_out_proposal(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A proposal not in ``rolling_out`` is skipped without any merge read."""
    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    _seed_proposal_118(agent, status="slicing")
    _seed_slice_plan(agent, "s0", phase_index=0, status="sliced")
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]

    await agent._advance_slice_rollouts()

    assert agent.memory.get_plan("s0").status == "sliced"
    agent.is_pr_settled.assert_not_called()


@pytest.mark.asyncio
async def test_advance_slice_rollouts_noop_when_flag_off(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """Flag off → no proposal read, no promotion, no completion (byte-identical)."""
    agent = make_agent()
    # multi_pr_proposals_enabled defaults False.
    _seed_proposal_118(agent)
    _seed_slice_plan(
        agent, "s0", phase_index=0, status="implemented",
        pr_url="https://github.com/o/r/pull/100",
    )
    _seed_slice_plan(agent, "s1", phase_index=1, status="sliced")
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]

    await agent._advance_slice_rollouts()

    assert agent.memory.get_plan("s1").status == "sliced"
    assert agent.memory.get_evolution_proposals()[0].status == "rolling_out"
    agent.is_pr_settled.assert_not_called()


@pytest.mark.asyncio
async def test_implement_approved_plans_defers_out_of_order_slice(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """At implement time the oracle is the authority: only the next eligible
    slice implements; an out-of-order sibling is deferred."""
    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    agent.config.max_implementations_per_iteration = 3
    _seed_proposal_118(agent)
    _seed_slice_plan(agent, "s0", phase_index=0, status="approved")
    _seed_slice_plan(agent, "s1", phase_index=1, status="approved")

    implemented: list[str] = []

    async def capture(plan_id: str) -> None:
        implemented.append(plan_id)

    agent.implement_plan = AsyncMock(side_effect=capture)  # type: ignore[method-assign]
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]

    await agent._implement_approved_plans()

    assert implemented == ["s0"]


@pytest.mark.asyncio
async def test_implement_approved_plans_does_not_gate_dangling_parent(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A slice whose ``parent_plan_id`` names no real proposal earns no ordering
    gate and implements normally."""
    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    _seed_slice_plan(
        agent, "orphan", phase_index=1, status="approved",
        parent_plan_id="ghost",
    )
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]

    await agent._implement_approved_plans()

    agent.implement_plan.assert_awaited_once_with("orphan")


@pytest.mark.asyncio
async def test_implement_approved_plans_slice_not_gated_when_flag_off(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """Flag off → slice plans implement in order with no oracle gating (the
    byte-identical control)."""
    agent = make_agent()
    agent.config.max_implementations_per_iteration = 3
    _seed_proposal_118(agent)
    _seed_slice_plan(agent, "s0", phase_index=0, status="approved")
    _seed_slice_plan(agent, "s1", phase_index=1, status="approved")
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]

    await agent._implement_approved_plans()

    assert agent.implement_plan.await_count == 2


@pytest.mark.asyncio
async def test_implement_approved_plans_deferred_slice_frees_cap_slot(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A deferred out-of-order slice is filtered *before* the per-iteration cap,
    so with ``cap == 1`` and an adverse plan order the eligible slice still
    implements — it must not be starved of the sole cap slot (ledger r2)."""
    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    agent.config.max_implementations_per_iteration = 1
    _seed_proposal_118(agent)
    # Persist the out-of-order phase-1 slice FIRST so it sorts ahead of the
    # eligible phase-0 slice under the stable status/failure sort — the adverse
    # order that, without the pre-cap filter, would consume the lone cap slot and
    # leave the eligible slice unreached this iteration.
    _seed_slice_plan(agent, "s1", phase_index=1, status="approved")
    _seed_slice_plan(agent, "s0", phase_index=0, status="approved")

    implemented: list[str] = []

    async def capture(plan_id: str) -> None:
        implemented.append(plan_id)

    agent.implement_plan = AsyncMock(side_effect=capture)  # type: ignore[method-assign]
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]

    await agent._implement_approved_plans()

    assert implemented == ["s0"]
