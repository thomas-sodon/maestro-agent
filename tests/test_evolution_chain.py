"""Multi-PR chain emission and slice-aware submission (proposal 118); split out of
``tests/test_evolution.py``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import (
    UNPARSEABLE_PLAN_PLACEHOLDER,
    EvolutionEngine,
    ProposalParseError,
)
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import (
    EvolutionPlan,
    EvolutionProposal,
    Memory,
    TaskRecord,
)
from maestro.notifications.base import NullNotifier
from maestro.plan_feedback import format_plan_ref
from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS, PlanStatus
from tests.conftest import MockQueryResult
from tests.support.gap_fuel import GAP
from tests.test_evolution import (
    _make,
    _make_engine,
)

# ---------------------------------------------------------------------------
# Proposal 118 — multi-PR chain emission (propose_plan_chain / submit_proposal_chain)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_plan_chain_single_pr_returns_lone_plan(
    mem_fs: dict[str, str],
) -> None:
    """A single-PR (``multi_pr`` false) reply yields a lone EvolutionPlan."""
    e = _make_engine(mem_fs)
    plan_json = json.dumps({
        "multi_pr": False,
        "title": "One focused change",
        "category": "capability",
        "priority": "capability",
        "rationale": "Fits one PR",
        "proposed_changes": ["Edit maestro/foo.py and add a test to verify it"],
        "acceptance_criteria": ["foo() returns 1"],
        "files_affected": ["maestro/foo.py"],
        "expected_impact": "Forward",
    })
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        result = await e.propose_plan_chain("some reflection")

    assert isinstance(result, EvolutionPlan)
    assert result.title == "One focused change"
    assert result.status == "pending_review"
    assert result.parent_plan_id is None
    assert result.phase_index == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [True, False])
async def test_propose_plan_chain_skips_pinned_fields_when_gap_pinned(
    mem_fs: dict[str, str], pinned: bool,
) -> None:
    """A gap-pinned caller pays for no category or issue-number fields."""
    e = _make_engine(mem_fs)
    plan_json = json.dumps({
        "multi_pr": False, "title": "T", "category": "capability",
        "priority": "capability", "rationale": "r",
        "proposed_changes": ["Edit maestro/foo.py and verify with a test"],
        "acceptance_criteria": ["x"], "files_affected": ["maestro/foo.py"],
        "expected_impact": "e",
    })
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        await e.propose_plan_chain("Close this gap", gap_pinned=pinned)
    prompt = mock_q.call_args.kwargs["prompt"]
    assert prompt.startswith("Plan from this intent:\nClose this gap")
    schema = mock_q.call_args.kwargs["options"].output_format["schema"]
    assert "value_claim" not in schema["properties"]
    assert ("issue_number" in schema["properties"]) is not pinned
    assert ('"category"' in prompt) is not pinned
    assert ('"issue_number"' in prompt) is not pinned


@pytest.mark.asyncio
async def test_propose_plan_chain_multi_pr_returns_proposal_and_slices(
    mem_fs: dict[str, str],
) -> None:
    """A multi-PR reply yields one rolling_out proposal plus N linked slices."""
    e = _make_engine(mem_fs)
    chain_json = json.dumps({
        "multi_pr": True,
        "proposal_title": "Big initiative",
        "proposal_goal": "Deliver X across three PRs",
        "slices": [
            {
                "title": "Slice one",
                "category": "capability",
                "priority": "capability",
                "rationale": "First slice",
                "proposed_changes": ["Edit maestro/a.py; verify with a test"],
                "acceptance_criteria": ["a works"],
                "files_affected": ["maestro/a.py"],
                "expected_impact": "1",
            },
            {
                "title": "Slice two",
                "category": "capability",
                "priority": "capability",
                "rationale": "Second slice",
                "proposed_changes": ["Edit maestro/b.py; verify with a test"],
                "acceptance_criteria": ["b works"],
                "files_affected": ["maestro/b.py"],
                "expected_impact": "2",
            },
            {
                "title": "Slice three",
                "category": "capability",
                "priority": "capability",
                "rationale": "Third slice",
                "proposed_changes": ["Edit maestro/c.py; verify with a test"],
                "acceptance_criteria": ["c works"],
                "files_affected": ["maestro/c.py"],
                "expected_impact": "3",
            },
        ],
    })
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=chain_json)
        result = await e.propose_plan_chain("some reflection")

    assert isinstance(result, tuple)
    proposal, slices = result
    assert isinstance(proposal, EvolutionProposal)
    assert proposal.status == "rolling_out"
    assert proposal.title == "Big initiative"
    assert proposal.goal == "Deliver X across three PRs"
    assert len(slices) == 3
    for i, s in enumerate(slices):
        assert s.parent_plan_id == proposal.proposal_id
        assert s.phase_index == i
    assert slices[0].status == "pending_review"
    assert slices[1].status == "sliced"
    assert slices[2].status == "sliced"
    assert slices[0].title == "Slice one"


@pytest.mark.asyncio
async def test_propose_plan_chain_fewer_than_two_slices_collapses(
    mem_fs: dict[str, str],
) -> None:
    """A ``multi_pr`` reply with a single valid slice collapses to a lone plan."""
    e = _make_engine(mem_fs)
    chain_json = json.dumps({
        "multi_pr": True,
        "proposal_title": "Only one slice",
        "proposal_goal": "Not really multi",
        "slices": [
            {
                "title": "Sole slice",
                "category": "capability",
                "priority": "capability",
                "rationale": "Only slice",
                "proposed_changes": ["Edit maestro/z.py; verify with a test"],
                "acceptance_criteria": ["z works"],
                "files_affected": ["maestro/z.py"],
                "expected_impact": "z",
            },
        ],
    })
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=chain_json)
        result = await e.propose_plan_chain("some reflection")

    # Fewer than two valid slices → collapse to the lone-plan path, adopting the
    # sole slice's fields.
    assert isinstance(result, EvolutionPlan)
    assert result.title == "Sole slice"
    assert result.status == "pending_review"
    assert result.parent_plan_id is None
    assert result.phase_index == 0


@pytest.mark.asyncio
async def test_submit_proposal_chain_persists_and_submits_head(
    mem_fs: dict[str, str],
) -> None:
    """The chain is persisted; only the phase-0 slice reaches pending_review."""
    e, m, _c, j = _make(mem_fs)

    def _slice(plan_id: str, phase: int, status: str) -> EvolutionPlan:
        return EvolutionPlan(
            plan_id=plan_id,
            title=f"Slice {phase}",
            category="capability",
            rationale=f"rationale {phase}",
            proposed_changes=[f"change {phase}"],
            files_affected=[f"maestro/slice{phase}.py"],
            expected_impact=f"impact {phase}",
            status=status,  # type: ignore[arg-type]
            parent_plan_id="prop-abc",
            phase_index=phase,
        )

    proposal = EvolutionProposal(
        proposal_id="prop-abc",
        title="Chain title",
        goal="Chain goal",
        status="rolling_out",
        created_at="2026-08-25T00:00:00+00:00",
    )
    slices = [
        _slice("s0", 0, "pending_review"),
        _slice("s1", 1, "sliced"),
        _slice("s2", 2, "sliced"),
    ]

    await e.submit_proposal_chain(proposal, slices)

    proposals = m.get_evolution_proposals()
    assert [p.proposal_id for p in proposals] == ["prop-abc"]
    assert proposals[0].status == "rolling_out"

    all_plans = m.get_all_plans()
    assert {p.plan_id for p in all_plans} == {"s0", "s1", "s2"}

    pending = m.get_pending_plans()
    assert [p.plan_id for p in pending] == ["s0"]

    by_id = {p.plan_id: p for p in all_plans}
    assert by_id["s1"].status == "sliced"
    assert by_id["s2"].status == "sliced"

    chain_notes = [
        entry for entry in j.read_events_by_type("evolution_note")
        if (entry.triggered_by or "").startswith("proposal_chain:")
    ]
    assert len(chain_notes) == 1
    assert "prop-abc" in chain_notes[0].content


@pytest.mark.asyncio
async def test_submit_proposal_chain_refused_head_leaves_no_orphan(
    mem_fs: dict[str, str],
) -> None:
    """A guard-refused head persists no proposal or slices (no headless chain)."""
    e, m, _c, j = _make(mem_fs)

    # Pre-seed a pending plan sharing the head slice's exact title so
    # submit_for_review's exact-title dedup refuses the head without saving it.
    m.save_evolution_plan(EvolutionPlan(
        plan_id="existing",
        title="Slice 0",
        category="capability",
        rationale="already pending",
        proposed_changes=["change"],
        files_affected=[],
        expected_impact="",
        status="pending_review",
    ))

    def _slice(plan_id: str, phase: int, status: str) -> EvolutionPlan:
        return EvolutionPlan(
            plan_id=plan_id,
            title=f"Slice {phase}",
            category="capability",
            rationale=f"rationale {phase}",
            proposed_changes=[f"change {phase}"],
            files_affected=[f"maestro/slice{phase}.py"],
            expected_impact=f"impact {phase}",
            status=status,  # type: ignore[arg-type]
            parent_plan_id="prop-refused",
            phase_index=phase,
        )

    proposal = EvolutionProposal(
        proposal_id="prop-refused",
        title="Refused chain",
        goal="Chain goal",
        status="rolling_out",
        created_at="2026-08-25T00:00:00+00:00",
    )
    slices = [
        _slice("s0", 0, "pending_review"),
        _slice("s1", 1, "sliced"),
        _slice("s2", 2, "sliced"),
    ]

    await e.submit_proposal_chain(proposal, slices)

    # No headless chain: the proposal and dormant slices were never persisted.
    assert [p.proposal_id for p in m.get_evolution_proposals()] == []
    plan_ids = {p.plan_id for p in m.get_all_plans()}
    assert plan_ids == {"existing"}
    # The refused head was not saved by the exact-title guard.
    assert m.get_plan("s0") is None

    # No false "minted / submitted the head slice" success note ...
    success_notes = [
        entry for entry in j.read_events_by_type("evolution_note")
        if (entry.triggered_by or "").startswith("proposal_chain:")
    ]
    assert success_notes == []
    # ... instead, exactly one honest refusal note is journalled.
    refusal_notes = [
        entry for entry in j.read_events_by_type("evolution_note")
        if (entry.triggered_by or "").startswith("proposal_chain_refused:")
    ]
    assert len(refusal_notes) == 1
    assert "prop-refused" in refusal_notes[0].content


@pytest.mark.asyncio
async def test_propose_plan_chain_uses_session_path(
    mem_fs: dict[str, str],
) -> None:
    """With a session, propose_plan_chain routes through query_on_session."""
    e = _make_engine(mem_fs)
    plan_json = json.dumps({
        "multi_pr": False,
        "title": "Session single",
        "rationale": "r",
        "proposed_changes": ["Edit maestro/x.py; verify with a test"],
    })
    with patch(
        "maestro.evolution.query_on_session", new_callable=AsyncMock,
    ) as mock_qs:
        mock_qs.return_value = MockQueryResult(response=plan_json)
        result = await e.propose_plan_chain("reflection", session=MagicMock())

    assert isinstance(result, EvolutionPlan)
    assert result.title == "Session single"
    mock_qs.assert_called_once()


@pytest.mark.asyncio
async def test_propose_plan_chain_single_pr_missing_changes_uses_response(
    mem_fs: dict[str, str],
) -> None:
    """A single-PR reply omitting proposed_changes falls back to the response."""
    e = _make_engine(mem_fs)
    plan_json = json.dumps({
        "multi_pr": False,
        "title": "No changes given",
        "rationale": "r",
    })
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        result = await e.propose_plan_chain("reflection")

    assert isinstance(result, EvolutionPlan)
    assert result.proposed_changes  # non-empty (response-text fallback)


@pytest.mark.asyncio
async def test_propose_plan_chain_slice_missing_changes_uses_placeholder(
    mem_fs: dict[str, str],
) -> None:
    """A slice spec with null proposed_changes gets the placeholder change."""
    e = _make_engine(mem_fs)
    chain_json = json.dumps({
        "multi_pr": True,
        "proposal_title": "T",
        "proposal_goal": "G",
        "slices": [
            {"title": "S0", "rationale": "r0", "proposed_changes": None},
            {
                "title": "S1",
                "rationale": "r1",
                "proposed_changes": ["Edit maestro/b.py; verify with a test"],
            },
        ],
    })
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=chain_json)
        result = await e.propose_plan_chain("reflection")

    assert isinstance(result, tuple)
    _proposal, slices = result
    assert slices[0].proposed_changes == [UNPARSEABLE_PLAN_PLACEHOLDER]


@pytest.mark.asyncio
async def test_propose_plan_chain_invalid_json_raises_parse_error(
    mem_fs: dict[str, str],
) -> None:
    """A non-JSON response raises ProposalParseError (mirrors propose_plan)."""
    e = _make_engine(mem_fs)
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="plain text no braces")
        with pytest.raises(ProposalParseError):
            await e.propose_plan_chain("reflection")


@pytest.mark.asyncio
async def test_propose_plan_chain_non_dict_response_wraps(
    mem_fs: dict[str, str],
) -> None:
    """A JSON list (non-dict) response is wrapped and collapses to a lone plan."""
    e = _make_engine(mem_fs)
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps(["a", "b"]))
        result = await e.propose_plan_chain("reflection")

    assert isinstance(result, EvolutionPlan)
    assert result.proposed_changes == ["a", "b"]


@pytest.mark.asyncio
async def test_submit_proposal_chain_idempotent_retry(
    mem_fs: dict[str, str],
) -> None:
    """A duplicate proposal id or slice id is skipped, not double-written."""
    e, m, _c, _j = _make(mem_fs)

    def _slice(plan_id: str, phase: int, status: str) -> EvolutionPlan:
        return EvolutionPlan(
            plan_id=plan_id,
            title=f"Slice {phase}",
            category="capability",
            rationale=f"rationale {phase}",
            proposed_changes=[f"change {phase}"],
            files_affected=[],
            expected_impact="",
            status=status,  # type: ignore[arg-type]
            parent_plan_id="dup",
            phase_index=phase,
        )

    proposal = EvolutionProposal(
        proposal_id="dup",
        title="Chain title",
        goal="Chain goal",
        status="rolling_out",
        created_at="2026-08-25T00:00:00+00:00",
    )
    # Pre-existing proposal and one downstream slice simulate a partial retry.
    m.write_evolution_proposal(proposal)
    m.save_evolution_plan(_slice("s1", 1, "sliced"))

    slices = [
        _slice("s0", 0, "pending_review"),
        _slice("s1", 1, "sliced"),
        _slice("s2", 2, "sliced"),
    ]

    await e.submit_proposal_chain(proposal, slices)  # must not raise

    assert [p.proposal_id for p in m.get_evolution_proposals()] == ["dup"]
    assert {p.plan_id for p in m.get_all_plans()} == {"s0", "s1", "s2"}
    assert [p.plan_id for p in m.get_pending_plans()] == ["s0"]


@pytest.mark.asyncio
async def test_propose_plan_passes_json_complete(mem_fs: dict[str, str]) -> None:
    """propose_plan wires json_complete through both paths."""
    from unittest.mock import MagicMock

    from maestro.sdk import json_complete

    e, _m, _c, _j = _make(mem_fs)
    plan_json = json.dumps({
        "title": "x", "category": "capability", "priority": "capability",
        "rationale": "x", "proposed_changes": ["x"],
        "files_affected": [], "expected_impact": "x",
    })

    with patch("maestro.evolution.query", AsyncMock(return_value=MockQueryResult(response=plan_json))) as mock_q:
        await e.propose_plan(gap=GAP)
    assert mock_q.call_args.kwargs.get("complete_on") is json_complete

    with patch(
        "maestro.evolution.query_on_session",
        AsyncMock(return_value=MockQueryResult(response=plan_json)),
    ) as mock_qos:
        await e.propose_plan(gap=GAP, session=MagicMock())
    assert mock_qos.call_args.kwargs.get("complete_on") is json_complete


@pytest.mark.asyncio
async def test_propose_plan_passes_through_valid_source_task_id(
    mem_fs: dict[str, str],
) -> None:
    """When the LLM emits a valid ``source_task_id``, it survives validation
    and lands on the resulting ``EvolutionPlan``."""
    e, m, _c, _j = _make(mem_fs)
    m.record_task(TaskRecord(
        task_id="kept", goal="g", plan=[], outcome="success",
        result_summary="r", errors=[], duration_secs=1.0,
        timestamp="2026-01-01", branch="maestro/task/kept",
    ))
    plan_json = json.dumps({
        "title": "Reuse my work",
        "category": "capability",
        "priority": "capability",
        "rationale": "Build on the kept task branch",
        "proposed_changes": ["Refine the parser"],
        "files_affected": ["maestro/agent.py"],
        "expected_impact": "Ships sooner",
        "issue_number": None,
        "source_task_id": "kept",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert plan.source_task_id == "kept"


@pytest.mark.asyncio
async def test_propose_plan(mem_fs: dict[str, str]) -> None:
    e, m, _c, _j = _make(mem_fs)

    plan_json = json.dumps({
        "title": "Better introspection",
        "category": "awareness",
        "priority": "resilience",
        "rationale": "Need deeper self-analysis",
        "proposed_changes": ["Modify consciousness.py"],
        "files_affected": ["maestro/consciousness.py"],
        "expected_impact": "Advance self_reflection",
        "issue_number": 7,
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert plan.title == "Better introspection"
    # A gap plan's category and issue are pinned to its gap, not the reply.
    assert plan.category == "maintenance"
    assert plan.priority == "resilience"
    assert plan.issue_number == GAP.issue.number
    assert plan.problem_ref is None
    assert plan.status == "pending_review"
    assert len(plan.proposed_changes) == 1
    mock_q.assert_called_once()
    options = mock_q.call_args.kwargs["options"]
    assert options.effort == "high"
    prompt = mock_q.call_args.kwargs.get("prompt", mock_q.call_args[1].get("prompt", ""))
    assert prompt.startswith("Close this capability gap:")
    assert f"Gap issue #{GAP.issue.number}" in prompt
    assert "priority" in prompt

    saved = m.get_plan(plan.plan_id)
    assert saved is None


@pytest.mark.asyncio
async def test_propose_plan_bad_json_raises(mem_fs: dict[str, str]) -> None:
    """When the LLM returns unparseable JSON, propose_plan must raise
    :class:`ProposalParseError` instead of submitting a junk fallback plan."""
    from maestro.evolution import ProposalParseError

    e, _m, _c, _j = _make(mem_fs)

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="Not JSON response")
        with pytest.raises(ProposalParseError):
            await e.propose_plan(gap=GAP)


@pytest.mark.asyncio
async def test_propose_plan_bad_json_logs_warning(mem_fs: dict[str, str]) -> None:
    """logger.warning is called when parse_json raises JSONDecodeError."""
    from maestro.evolution import ProposalParseError

    e, _m, _c, _j = _make(mem_fs)

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution.logger") as mock_logger:
        mock_q.return_value = MockQueryResult(response="Not JSON at all")
        with pytest.raises(ProposalParseError):
            await e.propose_plan(gap=GAP)

    mock_logger.warning.assert_called()
    warning_call = mock_logger.warning.call_args_list[0]
    assert "JSONDecodeError" in warning_call[0][0]


@pytest.mark.asyncio
async def test_propose_plan_list_response_logs_warning(mem_fs: dict[str, str]) -> None:
    """logger.warning is called when parse_json returns a list instead of a dict."""
    e, _m, _c, _j = _make(mem_fs)

    # parse_json on a JSON array returns a list, not a dict
    list_response = json.dumps([{"proposed_changes": ["do something"]}])

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution.logger") as mock_logger:
        mock_q.return_value = MockQueryResult(response=list_response)
        plan = await e.propose_plan(gap=GAP)

    mock_logger.warning.assert_called()
    # The warning uses %s formatting: check both format strings and positional args
    all_warning_args = [
        str(arg)
        for call in mock_logger.warning.call_args_list
        for arg in call[0]
    ]
    assert any("list" in arg for arg in all_warning_args)
    # Proposed changes should still be populated (from the list wrapping path)
    assert isinstance(plan.proposed_changes, list)


@pytest.mark.asyncio
async def test_propose_plan_null_proposed_changes(mem_fs: dict[str, str]) -> None:
    e, _m, _c, _j = _make(mem_fs)

    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": None,
        "files_affected": [], "expected_impact": "",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert plan.proposed_changes != ["None"]
    assert isinstance(plan.proposed_changes, list)
    assert all(isinstance(c, str) for c in plan.proposed_changes)


@pytest.mark.asyncio
async def test_propose_plan_null_files_affected(mem_fs: dict[str, str]) -> None:
    e, _m, _c, _j = _make(mem_fs)

    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": None, "expected_impact": "",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert plan.files_affected is not None
    assert plan.files_affected == []


@pytest.mark.asyncio
async def test_propose_plan_invalid_category_falls_back(mem_fs: dict[str, str]) -> None:
    e, _m, _c, _j = _make(mem_fs)

    plan_json = json.dumps({
        "title": "Test", "category": "invalid_cat", "priority": "capability",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
    })

    # A gap plan pins its category, so the spec normaliser is exercised directly.
    plan = e._plan_from_spec(json.loads(plan_json), default_rationale="r")

    assert plan.category == "capability"


@pytest.mark.asyncio
async def test_propose_plan_invalid_priority_falls_back(mem_fs: dict[str, str]) -> None:
    e, _m, _c, _j = _make(mem_fs)

    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "invalid_value",
        "rationale": "Test", "proposed_changes": [],
        "files_affected": [], "expected_impact": "",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert plan.priority == "capability"


@pytest.mark.asyncio
async def test_propose_plan_unhashable_priority_falls_back(mem_fs: dict[str, str]) -> None:
    """A list ``priority`` is narrowed, not fed to the remap lookup (issue #3794)."""
    e, _m, _c, _j = _make(mem_fs)

    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": [],
        "rationale": "Test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert plan.priority == "capability"


@pytest.mark.asyncio
async def test_propose_plan_unhashable_category_falls_back(mem_fs: dict[str, str]) -> None:
    """An object ``category`` is narrowed before the frozenset test (issue #3794)."""
    e, _m, _c, _j = _make(mem_fs)

    plan_json = json.dumps({
        "title": "Test", "category": {}, "priority": "capability",
        "rationale": "Test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
    })

    plan = e._plan_from_spec(json.loads(plan_json), default_rationale="r")

    assert plan.category == "capability"


@pytest.mark.asyncio
async def test_propose_plan_chain_unhashable_enums_fall_back(
    mem_fs: dict[str, str],
) -> None:
    """The chain slice route inherits the same enum narrowing (issue #3794)."""
    e = _make_engine(mem_fs)
    chain_json = json.dumps({
        "multi_pr": True,
        "proposal_title": "Big initiative",
        "proposal_goal": "Deliver X across two PRs",
        "slices": [
            {
                "title": "Slice one",
                "category": {},
                "priority": [],
                "rationale": "First slice",
                "proposed_changes": ["Edit maestro/a.py; verify with a test"],
                "acceptance_criteria": ["a works"],
                "files_affected": ["maestro/a.py"],
                "expected_impact": "1",
            },
            {
                "title": "Slice two",
                "category": ["capability"],
                "priority": {"p": 1},
                "rationale": "Second slice",
                "proposed_changes": ["Edit maestro/b.py; verify with a test"],
                "acceptance_criteria": ["b works"],
                "files_affected": ["maestro/b.py"],
                "expected_impact": "2",
            },
        ],
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=chain_json)
        result = await e.propose_plan_chain("some reflection")

    assert isinstance(result, tuple)
    _proposal, slices = result
    assert len(slices) == 2
    for s in slices:
        assert s.priority == "capability"
        assert s.category == "capability"


@pytest.mark.asyncio
async def test_propose_plan_string_proposed_changes_wrapped_in_list(
    mem_fs: dict[str, str],
) -> None:
    e, _m, _c, _j = _make(mem_fs)

    plan_json = json.dumps({
        "title": "Fix X", "category": "capability", "priority": "resilience",
        "rationale": "test", "proposed_changes": "do the thing",
        "files_affected": [], "expected_impact": "",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert isinstance(plan.proposed_changes, list)
    assert plan.proposed_changes == ["do the thing"]


@pytest.mark.asyncio
async def test_propose_plan_populates_acceptance_criteria(
    mem_fs: dict[str, str],
) -> None:
    """When the LLM JSON includes acceptance_criteria, the parsed plan keeps them."""
    e, _m, _c, _j = _make(mem_fs)
    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
        "acceptance_criteria": [
            "task_cost_gate rule 3 counts partial outcomes in the failure-cost numerator",
            "test_cost_gate asserts both failure and partial outcomes are summed correctly",
        ],
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert plan.acceptance_criteria == [
        "task_cost_gate rule 3 counts partial outcomes in the failure-cost numerator",
        "test_cost_gate asserts both failure and partial outcomes are summed correctly",
    ]


@pytest.mark.asyncio
async def test_propose_plan_acceptance_criteria_default_empty(
    mem_fs: dict[str, str],
) -> None:
    """Legacy LLM responses (no acceptance_criteria key) default to []."""
    e, _m, _c, _j = _make(mem_fs)
    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert plan.acceptance_criteria == []


@pytest.mark.asyncio
async def test_propose_plan_acceptance_criteria_truncated_to_cap(
    mem_fs: dict[str, str],
) -> None:
    """A bad reply over the cap is truncated to the first cap entries."""
    e, _m, _c, _j = _make(mem_fs)
    criteria = [
        f"BM25 scorer test criterion {i} ranks exact-match above partial-match by 2x"
        for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS + 3)
    ]
    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
        "acceptance_criteria": criteria,
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert len(plan.acceptance_criteria) == ACCEPTANCE_CRITERIA_MAX_ITEMS
    assert plan.acceptance_criteria == criteria[:ACCEPTANCE_CRITERIA_MAX_ITEMS]


@pytest.mark.asyncio
async def test_propose_plan_acceptance_criteria_drops_non_strings(
    mem_fs: dict[str, str],
) -> None:
    """Non-string entries (None, dicts, ints, blanks) are dropped, not coerced."""
    e, _m, _c, _j = _make(mem_fs)
    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
        "acceptance_criteria": [
            "task_cost_gate rule 3 counts partial outcomes in the failure-cost numerator",
            None,
            {"nope": True},
            42,
            "   ",
            "BM25 scorer ranks exact-match documents above partial-match by 2x",
        ],
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert plan.acceptance_criteria == [
        "task_cost_gate rule 3 counts partial outcomes in the failure-cost numerator",
        "BM25 scorer ranks exact-match documents above partial-match by 2x",
    ]


@pytest.mark.asyncio
async def test_propose_plan_acceptance_criteria_per_item_capped(
    mem_fs: dict[str, str],
) -> None:
    """Per-item char cap is applied so a single huge entry cannot bloat the prompt."""
    e, _m, _c, _j = _make(mem_fs)
    huge = "file " + "x" * 995
    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
        "acceptance_criteria": [huge],
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert len(plan.acceptance_criteria) == 1
    assert len(plan.acceptance_criteria[0]) == 200


@pytest.mark.asyncio
async def test_propose_plan_acceptance_criteria_non_list_falls_back(
    mem_fs: dict[str, str],
) -> None:
    """When the LLM returns a non-list value (string, dict, int) the field becomes []."""
    e, _m, _c, _j = _make(mem_fs)
    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
        "acceptance_criteria": "single string",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert plan.acceptance_criteria == []


@pytest.mark.asyncio
async def test_propose_plan_prompt_documents_acceptance_criteria(
    mem_fs: dict[str, str],
) -> None:
    """The prompt sent to the LLM must spell out the binding-criteria contract
    so the planner extracts them in this same call (not the same context window
    as the implementer/reviewer)."""
    e, _m, _c, _j = _make(mem_fs)
    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
        "acceptance_criteria": ["x"],
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        await e.propose_plan(gap=GAP)

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "acceptance_criteria" in prompt
    assert "binding" in prompt
    assert "testable" in prompt


@pytest.mark.asyncio
async def test_propose_plan_schema_requires_acceptance_criteria(
    mem_fs: dict[str, str],
) -> None:
    """The output_format JSON schema lists acceptance_criteria as required so the
    LLM cannot silently omit it on the structured-output path."""
    e, _m, _c, _j = _make(mem_fs)
    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
        "acceptance_criteria": ["x"],
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        await e.propose_plan(gap=GAP)

    schema = mock_q.call_args.kwargs["options"].output_format["schema"]
    assert "acceptance_criteria" in schema["properties"]
    assert schema["properties"]["acceptance_criteria"]["maxItems"] == ACCEPTANCE_CRITERIA_MAX_ITEMS
    assert schema["properties"]["acceptance_criteria"]["minItems"] == 1
    assert "acceptance_criteria" in schema["required"]


@pytest.mark.asyncio
async def test_propose_plan_prompt_includes_verification_language_requirement(
    mem_fs: dict[str, str],
) -> None:
    """The prompt must tell the LLM about the plan-scope gate's verification-
    language requirement so that proposed_changes entries include verbs like
    test/verify/check/validate/confirm for every file they describe.

    Without this instruction, CLAUDE.md entries typically read 'Update CLAUDE.md
    to add ...' — natural English, but missing the gate-required verification
    verb, causing 88% of plan-scope rejections.
    """
    e, _m, _c, _j = _make(mem_fs)
    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "feature",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
        "acceptance_criteria": ["x"],
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        await e.propose_plan(gap=GAP)

    prompt = mock_q.call_args.kwargs["prompt"]
    # The prompt must mention the plan-scope gate requirement
    assert "PLAN-SCOPE REQUIREMENT" in prompt
    # It must list the verification verbs the gate recognises, as the canonical phrase
    assert "test, verify, check, validate, or confirm" in prompt
    # It must give specific guidance for documentation entries
    assert "Verify the updated section" in prompt


async def _grab_propose_plan_prompt(
    mem_fs: dict[str, str], *, require_claude_md_update: bool,
) -> str:
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(require_claude_md_update=require_claude_md_update)
    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "feature",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
        "acceptance_criteria": ["x"],
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        await e.propose_plan(gap=GAP)

    return str(mock_q.call_args.kwargs["prompt"])


@pytest.mark.asyncio
async def test_propose_plan_prompt_documentation_is_flag_gated(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 108: the propose_plan CLAUDE.md mandate is now flag-selected.

    With the default (``require_claude_md_update`` False) the prompt carries
    the soft, conditional guidance and NOT the unconditional mandate; with the
    flag on the legacy hard mandate (Core Modules table example) reappears.
    """
    off = await _grab_propose_plan_prompt(mem_fs, require_claude_md_update=False)
    assert "MUST update CLAUDE.md" not in off
    assert "Core Modules table" not in off
    assert "only if an invariant changed" in off

    on = await _grab_propose_plan_prompt(mem_fs, require_claude_md_update=True)
    assert "MUST update CLAUDE.md" in on
    assert "Core Modules table" in on


@pytest.mark.asyncio
async def test_propose_plan_prompt_includes_minimalism_instruction(
    mem_fs: dict[str, str],
) -> None:
    """The propose_plan prompt must carry the MINIMALISM instruction so the
    plan is shaped toward the smallest change and reuse over rebuild before any
    code exists — a future deletion or rewording of that paragraph fails here.
    """
    e, _m, _c, _j = _make(mem_fs)
    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "feature",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
        "acceptance_criteria": ["x"],
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        await e.propose_plan(gap=GAP)

    prompt = mock_q.call_args.kwargs["prompt"]
    # The prompt must carry the anti-overbuild / anti-reinvention instruction
    assert "MINIMALISM:" in prompt
    # It must bias toward reusing what already exists over rebuilding it
    assert "reuse over rebuild" in prompt
    # It must forbid unrequested abstractions
    assert "no factory for one product" in prompt
    # It must legitimize subtraction as a first-class plan shape
    assert "net-subtractive" in prompt


@pytest.mark.asyncio
async def test_submit_for_review(mem_fs: dict[str, str]) -> None:
    e, _m, _c, j = _make(mem_fs)
    plan = EvolutionPlan(
        plan_id="p1", title="Test Plan",
        category="capability", rationale="Test", proposed_changes=["Change X"],
        files_affected=[], expected_impact="Improve",
        created_at="2025-01-01T00:00:00Z",
    )

    await e.submit_for_review(plan)

    entries = j.read_events_by_type("evolution_note")
    assert len(entries) == 1
    assert "Test Plan" in entries[0].content
    assert "Priority:" in entries[0].content


@pytest.mark.asyncio
async def test_submit_for_review_blocks_pending_restatement(
    mem_fs: dict[str, str],
) -> None:
    """A near-restatement of a pending plan is recorded obsolete, not pending.

    The title-exact dedup only catches identical titles; this covers the
    duplicate-pending-plan guard (different title, overlapping body). The
    candidate is persisted then marked ``obsolete`` (so no second
    ``pending_review`` row burdens Thomas) and a journal note cites the
    open plan it duplicates.
    """
    e, m, _c, j = _make(mem_fs)
    original = EvolutionPlan(
        plan_id="orig", title="Escalate unlanded remediation to Thomas",
        category="capability",
        rationale="Deliver one merge-or-close message when a lineage stalls past threshold",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    )
    await e.submit_for_review(original)

    restatement = EvolutionPlan(
        plan_id="dup", title="Escalate unlanded remediation to Thomas once",
        category="capability",
        rationale="Deliver a single merge-or-close message when the lineage stalls past threshold",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-01-02T00:00:00Z",
    )
    await e.submit_for_review(restatement)

    # No second pending_review row: the original stays pending, the
    # near-duplicate is recorded obsolete instead.
    pending_ids = [p.plan_id for p in m.get_pending_plans()]
    assert pending_ids == ["orig"]
    dup = m.get_plan("dup")
    assert dup is not None
    assert dup.status == "obsolete"
    assert dup.refused_at_submission is True
    # The refusal journal note cites the open plan it duplicates.
    notes = j.read_events_by_type("evolution_note")
    assert len(notes) == 2
    assert "orig" in notes[1].content


# ---------------------------------------------------------------------------
# Proposal 118 — slice-aware submission (sibling dedup exemption) + promote_slice
# ---------------------------------------------------------------------------


def _make_multipr(
    mem_fs: dict[str, str], *, enabled: bool = True,
) -> tuple[EvolutionEngine, Memory, Consciousness, Journal]:
    """Engine builder mirroring ``_make`` but with the proposal-118 flag set.

    The sibling-slice dedup exemption only engages when
    ``[loop] multi_pr_proposals_enabled`` is on; pass ``enabled=False`` to
    exercise the flag-off byte-identical path.
    """
    d = Path("/fake/evo_async")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    cfg = replace(Config(), multi_pr_proposals_enabled=enabled)
    e = EvolutionEngine(m, c, j, n, issues=issues, config=cfg)
    return e, m, c, j


def _slice_plan(
    plan_id: str,
    *,
    title: str,
    rationale: str,
    parent_plan_id: str | None,
    phase_index: int = 0,
    source_task_id: str | None = None,
    status: PlanStatus = "pending_review",
    created_at: str = "2026-08-24T00:00:00Z",
    reviewed_at: str | None = None,
) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id, title=title, category="capability",
        rationale=rationale, proposed_changes=["change"], files_affected=[],
        expected_impact="", parent_plan_id=parent_plan_id,
        phase_index=phase_index, source_task_id=source_task_id, status=status,
        created_at=created_at, reviewed_at=reviewed_at,
    )


@pytest.mark.asyncio
async def test_submit_for_review_exempts_sibling_slices(
    mem_fs: dict[str, str],
) -> None:
    """Two slices of one real EvolutionProposal both reach pending_review.

    The sibling shares the head's exact title (exact-title dedup) and its source
    branch (recited-source guard) and near-restates it (duplicate-pending
    guard); every one of those cross-plan refusals is suppressed because the
    collision is entirely inside the proposal's sibling set, so neither slice is
    recorded ``obsolete``.
    """
    e, m, _c, _j = _make_multipr(mem_fs)
    m.write_evolution_proposal(EvolutionProposal(
        proposal_id="prop", title="Multi-slice initiative",
        goal="Land the refactor across ordered slices.",
        status="rolling_out", created_at="2026-08-24T00:00:00Z",
    ))
    head = _slice_plan(
        "s0", title="Refactor the oversized loader",
        rationale="Split the oversized loader into a reader and a parser step",
        parent_plan_id="prop", phase_index=0, source_task_id="chain-branch",
    )
    await e.submit_for_review(head)

    sibling = _slice_plan(
        "s1", title="Refactor the oversized loader",
        rationale="Split the oversized loader into a reader and a parser step now",
        parent_plan_id="prop", phase_index=1, source_task_id="chain-branch",
        created_at="2026-08-25T00:00:00Z",
    )
    await e.submit_for_review(sibling)

    pending_ids = sorted(p.plan_id for p in m.get_pending_plans())
    assert pending_ids == ["s0", "s1"]
    assert m.get_plan("s1").status == "pending_review"


@pytest.mark.asyncio
async def test_submit_for_review_sibling_exempts_recited_source_specifically(
    mem_fs: dict[str, str],
) -> None:
    """Two siblings sharing only a source branch survive ``is_source_task_recited``.

    Proposal 118 Step 6 regression (no new guard code): the sibling exemption
    for the recited-source guard is locked in isolation. The two slices share
    the same ``source_task_id`` (so ``is_source_task_recited`` would fire) but
    carry *distinct* titles and non-restating rationales — the exact-title,
    rejection-cooldown, duplicate-pending, and cluster guards therefore never
    engage. Without the ``parent_plan_id`` exemption the second slice would be
    recorded ``obsolete`` for re-citing the head's live base branch; with it,
    both slices reach ``pending_review``. The dangling-parent negative control
    (``test_submit_for_review_dangling_parent_earns_no_exemption``) proves the
    same recited-source guard still fires when the parent link is not a real
    proposal.
    """
    e, m, _c, _j = _make_multipr(mem_fs)
    m.write_evolution_proposal(EvolutionProposal(
        proposal_id="prop", title="Multi-slice initiative",
        goal="Land the change across ordered slices.",
        status="rolling_out", created_at="2026-08-24T00:00:00Z",
    ))
    head = _slice_plan(
        "s0", title="Add the reader stage",
        rationale="Introduce a streaming file reader component",
        parent_plan_id="prop", phase_index=0, source_task_id="shared-branch",
    )
    await e.submit_for_review(head)

    sibling = _slice_plan(
        "s1", title="Add the parser stage",
        rationale="Emit typed tokens from the raw byte spans downstream",
        parent_plan_id="prop", phase_index=1, source_task_id="shared-branch",
        created_at="2026-08-25T00:00:00Z",
    )
    await e.submit_for_review(sibling)

    stored = m.get_plan("s1")
    assert stored is not None
    # Not obsoleted by the recited-source guard — the sibling exemption held.
    assert stored.status == "pending_review"
    assert stored.obsolete_reason is None
    pending_ids = sorted(p.plan_id for p in m.get_pending_plans())
    assert pending_ids == ["s0", "s1"]


@pytest.mark.asyncio
async def test_submit_for_review_exempts_rejected_sibling_within_cooldown(
    mem_fs: dict[str, str],
) -> None:
    """A within-cooldown *rejected* sibling does not block a new sibling slice.

    The rejection-cooldown guard would normally refuse a same-title resubmission
    inside the window; because the rejected loser is a sibling of the same
    proposal, the cooldown is exempted and the new slice reaches review.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    e, m, _c, _j = _make_multipr(mem_fs)
    m.write_evolution_proposal(EvolutionProposal(
        proposal_id="prop", title="Multi-slice initiative", goal="Land it.",
        status="rolling_out", created_at="2026-08-24T00:00:00Z",
    ))
    fixed_now = _dt(2026, 8, 26, tzinfo=_UTC)
    recent_ts = (fixed_now - _td(days=2)).isoformat()
    rejected_sibling = _slice_plan(
        "s0", title="Refactor the loader",
        rationale="Split the loader into a reader and a parser",
        parent_plan_id="prop", phase_index=0, status="rejected",
        created_at=recent_ts, reviewed_at=recent_ts,
    )
    m.save_evolution_plan(rejected_sibling)

    new_sibling = _slice_plan(
        "s1", title="Refactor the loader",
        rationale="Split the loader into a reader and a parser",
        parent_plan_id="prop", phase_index=1, created_at=fixed_now.isoformat(),
    )

    class _FrozenDateTime(_dt):  # type: ignore[misc]
        @classmethod
        def now(cls, tz: object = None) -> _dt:  # type: ignore[override]
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    with patch("maestro.evolution_submission.datetime", _FrozenDateTime):
        await e.submit_for_review(new_sibling)

    stored = m.get_plan("s1")
    assert stored is not None
    assert stored.status == "pending_review"


@pytest.mark.asyncio
async def test_submit_for_review_dangling_parent_earns_no_exemption(
    mem_fs: dict[str, str],
) -> None:
    """A plan whose ``parent_plan_id`` names no real proposal is still deduped.

    The negative control: the impostor cites a live plan's source branch (as a
    real sibling would) but its parent points at no persisted ``EvolutionProposal``,
    so ``_sibling_slice_ids`` yields the empty set and the recited-source guard
    records it ``obsolete`` — the exemption cannot be forged by a dangling link.
    The original plan carries no parent, exercising the ``parent is None`` arm.
    """
    e, m, _c, _j = _make_multipr(mem_fs)
    original = _slice_plan(
        "orig", title="Wire a delivery sensor",
        rationale="Add a behavioral sensor over remediation delivery accuracy",
        parent_plan_id=None, source_task_id="stranded-branch",
        created_at="2026-08-24T00:00:00Z",
    )
    await e.submit_for_review(original)

    impostor = _slice_plan(
        "impostor", title="Emit a delivery gauge",
        rationale="Surface how often a forecast matched its landing",
        parent_plan_id="ghost", phase_index=1, source_task_id="stranded-branch",
        created_at="2026-08-25T00:00:00Z",
    )
    await e.submit_for_review(impostor)

    pending_ids = [p.plan_id for p in m.get_pending_plans()]
    assert pending_ids == ["orig"]
    assert m.get_plan("impostor").status == "obsolete"


@pytest.mark.asyncio
async def test_promote_slice_flips_sliced_to_pending_and_notifies(
    mem_fs: dict[str, str],
) -> None:
    """``promote_slice`` flips a held ``"sliced"`` slice to ``pending_review``,
    stamps the notified content hash, journals a promotion note, and sends the
    review notification — reusing submit_for_review's happy-path tail."""
    e, m, _c, j = _make_multipr(mem_fs)
    slice_plan = _slice_plan(
        "s1", title="Second slice", rationale="The next slice of the chain",
        parent_plan_id="prop", phase_index=1, status="sliced",
    )
    m.save_evolution_plan(slice_plan)
    e._notifier.notify_plan_for_review = AsyncMock()  # type: ignore[method-assign]

    await e.promote_slice(slice_plan)

    promoted = m.get_plan("s1")
    assert promoted is not None
    assert promoted.status == "pending_review"
    assert promoted.notified_content_hash == promoted.plan_content_hash
    e._notifier.notify_plan_for_review.assert_awaited_once()
    assert e._notifier.notify_plan_for_review.await_args.args[2] == (
        promoted.plan_content_hash
    )
    notes = j.read_events_by_type("evolution_note")
    assert any("Promoting evolution slice" in n.content for n in notes)


@pytest.mark.asyncio
async def test_promote_slice_noop_when_not_sliced(
    mem_fs: dict[str, str],
) -> None:
    """Promotion is idempotent: a missing plan or an already-promoted (non-
    ``"sliced"``) slice is left untouched and no notification is sent."""
    e, m, _c, _j = _make_multipr(mem_fs)
    e._notifier.notify_plan_for_review = AsyncMock()  # type: ignore[method-assign]

    # Missing plan — current is None.
    await e.promote_slice(_slice_plan(
        "ghost", title="X", rationale="y", parent_plan_id="prop",
    ))

    # Already promoted — status is not "sliced".
    live = _slice_plan(
        "s1", title="Live slice", rationale="already open",
        parent_plan_id="prop", phase_index=1, status="pending_review",
    )
    m.save_evolution_plan(live)
    await e.promote_slice(live)

    assert m.get_plan("s1").status == "pending_review"
    e._notifier.notify_plan_for_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_slice_skips_when_slice_left_sliced_concurrently(
    mem_fs: dict[str, str],
) -> None:
    """A slice that leaves ``"sliced"`` between the read and the stamp is not
    flipped to ``pending_review`` and no notification is sent (#4473)."""
    e, m, _c, _j = _make_multipr(mem_fs)
    slice_plan = _slice_plan(
        "s1", title="Second slice", rationale="The next slice of the chain",
        parent_plan_id="prop", phase_index=1, status="sliced",
    )
    m.save_evolution_plan(slice_plan)
    e._notifier.notify_plan_for_review = AsyncMock()  # type: ignore[method-assign]
    real_update = m.update_plan_status

    def _race(*args: object, **kwargs: object) -> bool:
        real_update("s1", "rejected", expect_status="sliced")
        return real_update(*args, **kwargs)  # type: ignore[arg-type]

    with patch.object(m, "update_plan_status", side_effect=_race):
        await e.promote_slice(slice_plan)

    assert m.get_plan("s1").status == "rejected"
    e._notifier.notify_plan_for_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_slice_survives_notification_failure(
    mem_fs: dict[str, str],
) -> None:
    """A notifier outage during promotion is swallowed (best-effort tail): the
    status flip + journal note already landed, so the slice stays promoted and
    no exception escapes."""
    e, m, _c, _j = _make_multipr(mem_fs)
    slice_plan = _slice_plan(
        "s1", title="Second slice", rationale="The next slice of the chain",
        parent_plan_id="prop", phase_index=1, status="sliced",
    )
    m.save_evolution_plan(slice_plan)
    e._notifier.notify_plan_for_review = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("telegram down"),
    )

    # Must not raise despite the notifier failure.
    await e.promote_slice(slice_plan)

    assert m.get_plan("s1").status == "pending_review"


@pytest.mark.asyncio
async def test_submit_for_review_sibling_exemption_refuses_cocolliding_nonsibling(
    mem_fs: dict[str, str],
) -> None:
    """The duplicate-guard sibling exemption must not admit a slice that also
    near-restates a *non-sibling* plan just because
    ``find_duplicate_pending_plan`` returned a sibling first (ledger r3).

    The candidate near-restates both a sibling head (``s0``, first in file order
    → the first match) and an unrelated non-sibling pending plan (``nonsib``).
    The exact-title / cooldown / recited-source guards are inert (distinct
    titles, no rejected plans, no source branch) and the theme/pending cluster
    guards are neutralised via config, so ``find_duplicate_pending_plan`` is the
    sole operative guard. Because a non-sibling co-collides, the slice must still
    be recorded ``obsolete`` — the first-match-only accessor cannot hide it
    behind the sibling.
    """
    d = Path("/fake/evo_r3")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    # Neutralise every guard *after* find_duplicate so it alone decides: the
    # theme-cluster count can never reach 999, and pending_cluster_max <= 0
    # disables the pending-cluster stop.
    cfg = replace(
        Config(),
        multi_pr_proposals_enabled=True,
        loop_pending_plan_cluster_threshold=999,
        loop_pending_cluster_max=0,
    )
    e = EvolutionEngine(m, c, j, n, issues=issues, config=cfg)

    m.write_evolution_proposal(EvolutionProposal(
        proposal_id="prop", title="Multi-slice initiative",
        goal="Land the refactor across ordered slices.",
        status="rolling_out", created_at="2026-08-24T00:00:00Z",
    ))
    shared_rationale = (
        "Split the oversized data loader module into a dedicated reader and a "
        "separate parser and validator for clarity and testability"
    )
    # Sibling head — saved first so it is find_duplicate's first match.
    m.save_evolution_plan(_slice_plan(
        "s0", title="Alpha groundwork", rationale=shared_rationale,
        parent_plan_id="prop", phase_index=0,
    ))
    # Unrelated non-sibling — same body theme, no parent proposal.
    m.save_evolution_plan(_slice_plan(
        "nonsib", title="Zulu cleanup", rationale=shared_rationale,
        parent_plan_id=None,
    ))

    candidate = _slice_plan(
        "s1", title="Bravo slice", rationale=shared_rationale,
        parent_plan_id="prop", phase_index=1, created_at="2026-08-25T00:00:00Z",
    )
    await e.submit_for_review(candidate)

    assert m.get_plan("s1").status == "obsolete"
    assert m.get_plan("nonsib").status == "pending_review"


def test_compose_review_message_renders_issue_line_only_when_present(
    mem_fs: dict[str, str],
) -> None:
    """The shared review-message helper (ledger r4) renders the "Addresses
    issue" line only when the plan carries an ``issue_number`` — the single body
    both submit_for_review and promote_slice send, so a format change to one can
    never silently drift the other."""
    e, _m, _c, _j = _make_multipr(mem_fs)
    with_issue = replace(
        _slice_plan(
            "p", title="Wire a sensor", rationale="Add a delivery gauge",
            parent_plan_id=None,
        ),
        issue_number=42, proposed_changes=["do X"],
    )

    msg = e._compose_review_message(with_issue)
    assert f"New Evolution Plan {format_plan_ref(with_issue)}: Wire a sensor" in msg
    assert "  - do X" in msg
    assert "Addresses issue #42" in msg
    assert msg.endswith("Review on dashboard or use the buttons below.")

    without_issue = replace(with_issue, issue_number=None)
    assert "Addresses issue" not in e._compose_review_message(without_issue)


def test_compose_review_message_renders_hashed_lists(mem_fs: dict[str, str]) -> None:
    """Files and acceptance criteria are covered by the content hash the
    buttons approve, so the message renders them — capped (#4146)."""
    from maestro.plan_review_message import (
        REVIEW_MSG_ITEM_CHARS as _REVIEW_MSG_ITEM_CHARS,
    )
    from maestro.plan_review_message import (
        REVIEW_MSG_MAX_ITEMS as _REVIEW_MSG_MAX_ITEMS,
    )

    e, _m, _c, _j = _make_multipr(mem_fs)
    plan = replace(
        _slice_plan("p", title="T", rationale="R", parent_plan_id=None),
        files_affected=[f"f{i}.py" for i in range(_REVIEW_MSG_MAX_ITEMS + 2)],
        acceptance_criteria=["x" * (_REVIEW_MSG_ITEM_CHARS + 50)],
    )

    msg = e._compose_review_message(plan, "Refined Evolution Plan")

    assert msg.startswith(f"Refined Evolution Plan {format_plan_ref(plan)}: T")
    assert "Files:\n  - f0.py" in msg
    assert f"f{_REVIEW_MSG_MAX_ITEMS - 1}.py" in msg
    assert f"f{_REVIEW_MSG_MAX_ITEMS}.py" not in msg
    assert "  (+2 more)" in msg
    assert "Acceptance criteria:\n  - " + "x" * _REVIEW_MSG_ITEM_CHARS + "\n" in msg

    bare = replace(plan, files_affected=[], acceptance_criteria=[])
    bare_msg = e._compose_review_message(bare)
    assert "Files:" not in bare_msg
    assert "Acceptance criteria:" not in bare_msg
