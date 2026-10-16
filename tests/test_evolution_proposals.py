"""Store tests for :class:`EvolutionProposal` (proposal 118).

Data-model only: nothing in the loop reads or writes a proposal yet, so these
tests exercise the persistence scaffolding directly — round-trip, the write
guards, and the corrupted/empty-row loader skips (mirroring the
``document_records`` store tests). The eager ``_migrate_evolution_proposals``
rewrite helper was retired with the rest of the startup migration sweep
(proposal 106); the loader normalises ``evolution_proposals.jsonl`` on read.

The ``next_eligible_slice`` sequencing oracle (proposal 118 step 2) is exercised
at the bottom of this file — its Testing Strategy home for slice-sequencing
cases. It is a pure predicate over injected slices and an injected settlement
seam (no git, host, network, or sleep), so those cases mock the seam with inline
lambdas following ``tests/test_audit_checks.py``'s ``check_reconcile_near_miss``
style.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.loop.implementation import next_eligible_slice
from maestro.memory import EvolutionPlan, EvolutionProposal, Memory
from maestro.notifications.base import NullNotifier
from maestro.types import EvolutionProposalStatus

DIR = Path("/fake/evolution_proposals")
STORE = str(DIR / "evolution_proposals.jsonl")


# --- store round-trip + write guards ---


def test_round_trip(mem_fs: dict[str, str]) -> None:
    memory = Memory(DIR)
    first = EvolutionProposal(
        proposal_id="p1",
        title="Split the big refactor",
        goal="Land the refactor across three reviewable slices.",
        status="rolling_out",
        created_at="2026-08-24T00:00:00Z",
    )
    # Defaults exercised: status defaults to "slicing", created_at to "".
    second = EvolutionProposal(
        proposal_id="p2",
        title="Second initiative",
        goal="Another multi-slice goal.",
    )
    memory.write_evolution_proposal(first)
    memory.write_evolution_proposal(second)

    loaded = memory.get_evolution_proposals()
    assert loaded == [first, second]
    assert loaded[1].status == "slicing"
    assert loaded[1].created_at == ""


def test_write_empty_proposal_id_raises(mem_fs: dict[str, str]) -> None:
    memory = Memory(DIR)
    with pytest.raises(ValueError, match="proposal_id must not be empty"):
        memory.write_evolution_proposal(
            EvolutionProposal(proposal_id="", title="t", goal="g"),
        )


def test_write_duplicate_proposal_id_raises(mem_fs: dict[str, str]) -> None:
    memory = Memory(DIR)
    memory.write_evolution_proposal(
        EvolutionProposal(proposal_id="dup", title="t", goal="g"),
    )
    with pytest.raises(ValueError, match="duplicate proposal_id"):
        memory.write_evolution_proposal(
            EvolutionProposal(proposal_id="dup", title="other", goal="g2"),
        )


# --- loader edge cases ---


def test_get_when_absent_returns_empty(mem_fs: dict[str, str]) -> None:
    assert Memory(DIR).get_evolution_proposals() == []


def test_corrupted_line_is_skipped(mem_fs: dict[str, str]) -> None:
    valid = {
        "proposal_id": "ok",
        "title": "t",
        "goal": "g",
        "status": "slicing",
        "created_at": "",
    }
    mem_fs[STORE] = json.dumps(valid) + "\n" + "{not valid json\n"

    loaded = Memory(DIR).get_evolution_proposals()
    assert len(loaded) == 1
    assert loaded[0].proposal_id == "ok"


def test_blank_line_is_skipped(mem_fs: dict[str, str]) -> None:
    # An interior blank line survives ``.strip()`` and must be skipped by the
    # loader (a trailing blank line would be stripped away before splitlines).
    first = {
        "proposal_id": "a",
        "title": "t",
        "goal": "g",
        "status": "slicing",
        "created_at": "",
    }
    second = {**first, "proposal_id": "b"}
    mem_fs[STORE] = json.dumps(first) + "\n\n" + json.dumps(second) + "\n"

    loaded = Memory(DIR).get_evolution_proposals()
    assert [p.proposal_id for p in loaded] == ["a", "b"]


def test_empty_proposal_id_row_is_skipped(mem_fs: dict[str, str]) -> None:
    empty_id = {
        "proposal_id": "",
        "title": "t",
        "goal": "g",
        "status": "slicing",
        "created_at": "",
    }
    mem_fs[STORE] = json.dumps(empty_id) + "\n"

    assert Memory(DIR).get_evolution_proposals() == []


def test_unknown_field_is_dropped_on_read(mem_fs: dict[str, str]) -> None:
    # Schema-on-read: the loader filters keys not on the dataclass, so a
    # legacy row carrying a removed field normalises transparently on load.
    # Proposal 106 retired the eager rewrite that used to do this, leaving the
    # loader as the sole normaliser — this pins that contract.
    raw = {
        "proposal_id": "p1",
        "title": "t",
        "goal": "g",
        "status": "slicing",
        "created_at": "",
        "legacy_removed_field": "drop me",
    }
    mem_fs[STORE] = json.dumps(raw) + "\n"

    assert Memory(DIR).get_evolution_proposals() == [
        EvolutionProposal(
            proposal_id="p1",
            title="t",
            goal="g",
            status="slicing",
            created_at="",
        ),
    ]


# --- whole-file save primitive ---


def test_save_all_round_trips(mem_fs: dict[str, str]) -> None:
    # ``_save_all_evolution_proposals`` is the whole-file rewrite primitive
    # (mirror of the sibling stores). With the startup migration sweep retired
    # (proposal 106) it has no production caller yet, so exercise it directly.
    memory = Memory(DIR)
    records = [
        EvolutionProposal(proposal_id="p1", title="t1", goal="g1"),
        EvolutionProposal(proposal_id="p2", title="t2", goal="g2"),
    ]
    memory._save_all_evolution_proposals(records)
    assert memory.get_evolution_proposals() == records


# --- next_eligible_slice sequencing oracle (proposal 118, step 2) ---


def _proposal(status: EvolutionProposalStatus = "rolling_out") -> EvolutionProposal:
    return EvolutionProposal(
        proposal_id="prop",
        title="Multi-slice initiative",
        goal="Land the refactor across ordered slices.",
        status=status,
        created_at="2026-08-24T00:00:00Z",
    )


def _slice(plan_id: str, phase_index: int) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title=f"Slice {phase_index}",
        category="capability",
        rationale="reason",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        parent_plan_id="prop",
        phase_index=phase_index,
        created_at="2026-08-24T00:00:00Z",
    )


@pytest.mark.parametrize("status", ["complete", "abandoned"])
def test_next_eligible_slice_none_for_terminal_proposal(
    status: EvolutionProposalStatus,
) -> None:
    """A ``complete``/``abandoned`` proposal has no next slice, and the status
    short-circuit fires *before* the settlement seam is ever consulted."""
    calls: list[str] = []

    def record(plan: EvolutionPlan) -> bool:
        calls.append(plan.plan_id)
        return False

    result = next_eligible_slice(
        _proposal(status),
        [_slice("s0", 0), _slice("s1", 1)],
        is_settled=record,
    )

    assert result is None
    # Non-invocation: the terminal short-circuit never touches the seam.
    assert calls == []


def test_next_eligible_slice_returns_head_when_nothing_settled() -> None:
    """With no slice settled, the head (lowest ``phase_index``) is eligible; the
    seam is consulted for each slice in ascending ``phase_index`` order."""
    calls: list[str] = []

    def record(plan: EvolutionPlan) -> bool:
        calls.append(plan.plan_id)
        return False

    head = _slice("s0", 0)
    result = next_eligible_slice(
        _proposal(),
        [head, _slice("s1", 1), _slice("s2", 2)],
        is_settled=record,
    )

    assert result is head
    # Every slice is probed, in phase order (call-arg assertion).
    assert calls == ["s0", "s1", "s2"]


def test_next_eligible_slice_returns_next_once_predecessor_settled() -> None:
    """Once the head slice's PR has settled, the next slice becomes eligible."""
    nxt = _slice("s1", 1)
    result = next_eligible_slice(
        _proposal(),
        [_slice("s0", 0), nxt],
        is_settled=lambda plan: plan.plan_id in {"s0"},
    )
    assert result is nxt


def test_next_eligible_slice_none_when_successor_settles_out_of_order() -> None:
    """A settled slice above an unsettled predecessor is an inconsistent chain
    (a successor cannot settle first under in-order rollout) — no eligible
    slice, rather than a guessed one."""
    result = next_eligible_slice(
        _proposal(),
        [_slice("s0", 0), _slice("s1", 1)],
        is_settled=lambda plan: plan.plan_id in {"s1"},
    )
    assert result is None


def test_next_eligible_slice_none_when_all_settled() -> None:
    """Every slice settled means the proposal is complete in fact — no slice
    remains to work."""
    result = next_eligible_slice(
        _proposal(),
        [_slice("s0", 0), _slice("s1", 1)],
        is_settled=lambda plan: True,
    )
    assert result is None


def test_next_eligible_slice_none_when_no_slices() -> None:
    """A proposal with no slice plans yet has nothing eligible to work."""
    result = next_eligible_slice(
        _proposal(),
        [],
        is_settled=lambda plan: False,
    )
    assert result is None


def test_next_eligible_slice_orders_out_of_order_input() -> None:
    """Slices supplied out of ``phase_index`` order are sorted before the walk,
    so eligibility is by ``phase_index``, not input position."""
    nxt = _slice("s1", 1)
    result = next_eligible_slice(
        _proposal(),
        [_slice("s2", 2), _slice("s0", 0), nxt],
        is_settled=lambda plan: plan.plan_id in {"s0"},
    )
    assert result is nxt


# --- chain mint → oracle integration (proposal 118 chain emission) ---


@pytest.mark.asyncio
async def test_minted_chain_round_trips_to_next_eligible_slice(
    mem_fs: dict[str, str],
) -> None:
    """A chain minted by ``submit_proposal_chain`` round-trips through the store
    so ``next_eligible_slice`` returns the head slice under an unsettled seam."""
    memory = Memory(DIR)
    consciousness = Consciousness(DIR)
    consciousness.load_self_model()
    journal = Journal(DIR)
    engine = EvolutionEngine(
        memory, consciousness, journal, NullNotifier(),
        issues=IssueTracker(github_repo=None), config=Config(),
    )

    proposal = _proposal()
    slices = [
        replace(_slice("s0", 0), status="pending_review"),
        replace(_slice("s1", 1), status="sliced"),
        replace(_slice("s2", 2), status="sliced"),
    ]

    await engine.submit_proposal_chain(proposal, slices)

    # Round-trip: read the proposal and its slices back out of the store.
    loaded_proposal = memory.get_evolution_proposals()[0]
    loaded_slices = [
        p for p in memory.get_all_plans()
        if p.parent_plan_id == loaded_proposal.proposal_id
    ]
    assert {p.plan_id for p in loaded_slices} == {"s0", "s1", "s2"}

    eligible = next_eligible_slice(
        loaded_proposal, loaded_slices, is_settled=lambda plan: False,
    )
    assert eligible is not None
    assert eligible.plan_id == "s0"
    assert eligible.phase_index == 0


# --- update_evolution_proposal_status + get_slices_for_proposal (step 4) ---


def test_update_evolution_proposal_status_flips_and_reports_hit(
    mem_fs: dict[str, str],
) -> None:
    """The updater flips a persisted proposal's status and returns the hit flag,
    via a file_lock read-modify-write."""
    memory = Memory(DIR)
    memory.write_evolution_proposal(_proposal(status="rolling_out"))

    hit = memory.update_evolution_proposal_status("prop", "complete")

    assert hit is True
    assert memory.get_evolution_proposals()[0].status == "complete"


def test_update_evolution_proposal_status_miss_returns_false(
    mem_fs: dict[str, str],
) -> None:
    """An unknown proposal id leaves the store untouched and returns False."""
    memory = Memory(DIR)
    memory.write_evolution_proposal(_proposal(status="rolling_out"))

    hit = memory.update_evolution_proposal_status("absent", "complete")

    assert hit is False
    assert memory.get_evolution_proposals()[0].status == "rolling_out"


def test_get_slices_for_proposal_filters_by_parent_and_sorts(
    mem_fs: dict[str, str],
) -> None:
    """Slices are the plans whose ``parent_plan_id`` matches, ascending by
    ``phase_index``; a plan under a different parent is excluded."""
    memory = Memory(DIR)
    # Persist out of phase order, plus one plan owned by a different proposal.
    memory.save_evolution_plan(_slice("s1", 1))
    memory.save_evolution_plan(_slice("s0", 0))
    memory.save_evolution_plan(_slice("s2", 2))
    memory.save_evolution_plan(
        replace(_slice("other", 0), parent_plan_id="elsewhere"),
    )

    slices = memory.get_slices_for_proposal("prop")

    assert [s.plan_id for s in slices] == ["s0", "s1", "s2"]


def test_get_slices_for_proposal_empty_when_no_match(
    mem_fs: dict[str, str],
) -> None:
    """A proposal id with no matching slice plans yields an empty list."""
    memory = Memory(DIR)
    memory.save_evolution_plan(
        replace(_slice("x", 0), parent_plan_id="elsewhere"),
    )

    assert memory.get_slices_for_proposal("prop") == []
