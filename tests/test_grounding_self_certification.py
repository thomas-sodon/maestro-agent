"""A self-certifying BEHAVIOR_ABSENT grounding verdict is discounted (#4581).

A witness the plan itself proposes to add can be absent from the tree only
because it is not built yet, so a *grounded* verdict over it proves nothing and
is stamped unevaluated. A witness already present still refutes the plan.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from maestro.evolution import EvolutionEngine
from maestro.memory import EvolutionPlan, Memory
from maestro.plan_grounding import GroundingProbe, plan_introduces_witness
from tests.test_evolution import _make, _RecordingGateBackend


def _plan(plan_id: str, expectation: str, **extra: Any) -> EvolutionPlan:
    fields: dict[str, Any] = dict(
        plan_id=plan_id, title=f"Build approved-plan scan {plan_id}",
        category="capability", rationale="r",
        proposed_changes=["Add an approved-duplicate refusal reason"],
        files_affected=[], expected_impact="",
        grounding=GroundingProbe(
            kind="BEHAVIOR_ABSENT", locator="maestro/memory.py",
            expectation=expectation,
        ),
        created_at="2025-01-01T00:00:00Z",
    )
    fields.update(extra)
    return EvolutionPlan(**fields)


def _setup(
    mem_fs: dict[str, str], tree: str,
) -> tuple[EvolutionEngine, Memory, _RecordingGateBackend]:
    e, m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, plan_grounding_enabled=True)
    e._notifier.notify_plan_for_review = AsyncMock()  # type: ignore[method-assign]
    gates = _RecordingGateBackend()
    e._gate_activations = gates  # type: ignore[assignment]
    e._read_grounding_tree_text = lambda probe: tree  # type: ignore[method-assign]
    return e, m, gates


def _details(gates: _RecordingGateBackend) -> list[tuple[str, str]]:
    return [(r.action, r.detail) for r in gates.rows]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_present_self_introduced_witness_still_refutes(
    mem_fs: dict[str, str],
) -> None:
    e, m, gates = _setup(mem_fs, "reason = 'approved-duplicate'\n")
    await e.submit_for_review(_plan("dup1", "approved-duplicate"))
    stored = m.get_plan("dup1")
    assert stored is not None
    assert stored.status == "obsolete"
    assert stored.obsolete_reason is not None
    assert "[ungrounded:duplicate]" in stored.obsolete_reason
    assert _details(gates) == [
        ("obsolete", "[ungrounded:duplicate] BEHAVIOR_ABSENT maestro/memory.py"),
    ]


@pytest.mark.asyncio
async def test_absent_self_introduced_witness_is_unevaluated(
    mem_fs: dict[str, str],
) -> None:
    e, _m, gates = _setup(mem_fs, "def unrelated():\n    pass\n")
    await e.submit_for_review(_plan("self1", "approved-duplicate"))
    rows = _details(gates)
    assert rows[0] == (
        "unevaluated", "[self-certifying] BEHAVIOR_ABSENT maestro/memory.py",
    )
    assert ("obsolete", "[ungrounded:duplicate] BEHAVIOR_ABSENT maestro/memory.py") not in rows


@pytest.mark.asyncio
async def test_absent_witness_named_in_criteria_is_unevaluated(
    mem_fs: dict[str, str],
) -> None:
    e, _m, gates = _setup(mem_fs, "")
    await e.submit_for_review(_plan(
        "self2", "approved-duplicate", proposed_changes=["Extend scan"],
        acceptance_criteria=["refusal is tagged approved-duplicate"],
    ))
    assert _details(gates)[0][1].startswith("[self-certifying]")


@pytest.mark.asyncio
async def test_absent_witness_not_introduced_grounds_without_row(
    mem_fs: dict[str, str],
) -> None:
    e, _m, gates = _setup(mem_fs, "def unrelated():\n    pass\n")
    await e.submit_for_review(
        _plan("ok1", "get_live_plan_records", proposed_changes=["Extend scan"]),
    )
    flagged = [d for _a, d in _details(gates) if "[self-certifying]" in d]
    assert flagged == []


def test_plan_introduces_witness_matching() -> None:
    assert plan_introduces_witness("  Approved-Dup  ", ["add APPROVED-DUP tag"]) is True
    assert plan_introduces_witness("needle", ["a", "has needle"]) is True
    assert plan_introduces_witness("needle", ["nothing"]) is False
    assert plan_introduces_witness("needle", []) is False
    assert plan_introduces_witness("   ", ["anything"]) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "locator", "tree"),
    [
        ("DEFECT_PRESENT", "maestro/memory.py", "x = 'approved-duplicate'\n"),
        ("SYMBOL_ABSENT", "brand_new_symbol", "def unrelated():\n    pass\n"),
    ],
)
async def test_other_kinds_never_stamped_self_certifying(
    mem_fs: dict[str, str], kind: str, locator: str, tree: str,
) -> None:
    e, _m, gates = _setup(mem_fs, tree)
    plan = _plan(
        f"other-{kind}", "approved-duplicate",
        proposed_changes=["Remove approved-duplicate and add brand_new_symbol"],
        grounding=GroundingProbe(
            kind=kind, locator=locator, expectation="approved-duplicate",
        ),
    )
    await e.submit_for_review(plan)
    flagged = [d for _a, d in _details(gates) if "[self-certifying]" in d]
    assert flagged == []
