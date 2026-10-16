"""Tests for proposal 136's generator reality grounding in ``EvolutionEngine``.

Flag-gated declaration of ``problem_ref`` / ``new_capability_names`` in
``propose_plan`` and their clearance against the reality inventory as
``submit_for_review``'s first guard. All via ``mem_fs`` and mocked SDK/notifier.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.audit_checks import GateActivation
from maestro.evolution import (
    _REALITY_NAMES_FIELDS_PROMPT,
    EvolutionEngine,
)
from maestro.memory import EvolutionPlan, Memory
from maestro.reality_inventory import MAX_CLEARANCE_NAMES, Problem, RealityInventory
from tests.conftest import MockQueryResult
from tests.support.gap_fuel import GAP
from tests.test_evolution import _make, _RecordingGateBackend

_INVENTORY = RealityInventory(
    shipped_capabilities=frozenset({"cli:reality-inventory"}),
    measured_problems=(
        Problem(id="audit:rule_x", source="audit", summary="s", weight=1),
    ),
)

_PLAN_JSON = json.dumps({
    "title": "Declare it", "category": "capability", "priority": "capability",
    "rationale": "test", "proposed_changes": ["change"],
    "files_affected": [], "expected_impact": "",
    "acceptance_criteria": ["x"],
    "problem_ref": "  audit:rule_x  ",
    "new_capability_names": ["new_fn", 5],
})


def _plan(
    plan_id: str,
    problem_ref: str | None = None,
    names: list[str] | None = None,
    *,
    title: str = "Ground the generator",
    rationale: str = "Clear declarations against the inventory",
) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id, title=title,
        category="capability", rationale=rationale,
        proposed_changes=["change"], files_affected=[],
        expected_impact="", created_at="2025-01-01T00:00:00Z",
        problem_ref=problem_ref, new_capability_names=names,
    )


@pytest.mark.asyncio
async def test_propose_plan_reality_fields_are_flag_gated(
    mem_fs: dict[str, str],
) -> None:
    """Flag off: prompt/schema carry nothing and the fields stay None. Flag on:
    the fragment and both optional properties appear and the fields parse."""
    e_off, _m, _c, _j = _make(mem_fs)
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_off:
        mock_off.return_value = MockQueryResult(response=_PLAN_JSON)
        plan_off = await e_off.propose_plan(gap=GAP)
    prompt_off = str(mock_off.call_args.kwargs["prompt"])
    schema_off = mock_off.call_args.kwargs["options"].output_format["schema"]
    assert "REALITY GROUNDING" not in prompt_off
    assert "problem_ref" not in schema_off["properties"]
    assert plan_off.problem_ref is None
    assert plan_off.new_capability_names is None

    e_on, _m2, _c2, _j2 = _make(mem_fs)
    e_on._config = replace(e_on._config, generator_reality_grounding_enabled=True)
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_on:
        mock_on.return_value = MockQueryResult(response=_PLAN_JSON)
        plan_on = await e_on.propose_plan(gap=GAP)
    prompt_on = str(mock_on.call_args.kwargs["prompt"])
    schema_on = mock_on.call_args.kwargs["options"].output_format["schema"]
    # The names-only fragment is the only difference (proposal 155 step 4: a
    # gap plan's problem is its issue, so no problem_ref is asked for).
    assert prompt_on.replace(_REALITY_NAMES_FIELDS_PROMPT, "", 1) == prompt_off
    assert "REALITY GROUNDING" in prompt_on
    assert '"cli:<subcommand>"' in prompt_on
    assert "problem_ref" not in prompt_on
    assert "problem_ref" not in schema_on["properties"]
    assert "new_capability_names" in schema_on["properties"]
    assert "new_capability_names" not in schema_on["required"]
    assert plan_on.problem_ref is None
    assert plan_on.new_capability_names == ["new_fn"]


def test_reality_declarations_malformed_collapse(mem_fs: dict[str, str]) -> None:
    """Flag on: a blank/non-str ref and a non-list name list collapse to None."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, generator_reality_grounding_enabled=True)
    spec = {"problem_ref": "   ", "new_capability_names": "not-a-list"}
    assert e._reality_declarations_from_spec(spec) == (None, None)
    assert e._reality_declarations_from_spec({"problem_ref": 3}) == (None, None)


def test_reality_declarations_cap_names_at_clearance_limit(
    mem_fs: dict[str, str],
) -> None:
    """Flag on: the stored name list is exactly the list ``clear_mint`` tests."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, generator_reality_grounding_enabled=True)
    names = [f"n{i}" for i in range(MAX_CLEARANCE_NAMES + 3)]
    _ref, parsed = e._reality_declarations_from_spec(
        {"new_capability_names": names},
    )
    assert parsed == names[:MAX_CLEARANCE_NAMES]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan_id", "problem_ref", "names", "tag"),
    [
        ("dup1", None, ["reality-inventory"], "[ungrounded-generation:duplicate]"),
        (
            "unm1", "sensor:NOPE", None,
            "[ungrounded-generation:unmeasured-problem]",
        ),
    ],
)
async def test_submit_for_review_refuses_ungrounded_generation(
    mem_fs: dict[str, str],
    plan_id: str,
    problem_ref: str | None,
    names: list[str] | None,
    tag: str,
) -> None:
    """A shipped name or an unmeasured problem lands the plan obsolete, stamps a
    gate row, and never notifies."""
    e, m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, generator_reality_grounding_enabled=True)
    e._notifier.notify_plan_for_review = AsyncMock()
    gates = _RecordingGateBackend()
    e._gate_activations = gates
    e._load_generation_inventory = lambda: (_INVENTORY, "")

    await e.submit_for_review(_plan(plan_id, problem_ref, names))

    stored = m.get_plan(plan_id)
    assert stored is not None
    assert stored.status == "obsolete"
    assert stored.obsolete_reason is not None
    assert stored.obsolete_reason.startswith(tag)
    e._notifier.notify_plan_for_review.assert_not_awaited()
    assert len(gates.rows) == 1
    row = cast(GateActivation, gates.rows[0])
    assert row.gate == "generator_reality_grounding"
    assert row.action == "obsolete"

    # An idempotent retry records nothing new and stamps no second row.
    await e.submit_for_review(_plan(plan_id, problem_ref, names))
    assert len(gates.rows) == 1


@pytest.mark.asyncio
async def test_submit_for_review_reality_grounding_passes_through(
    mem_fs: dict[str, str],
) -> None:
    """A grounded plan and a field-less plan reach review; field-less never reads."""
    e, m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, generator_reality_grounding_enabled=True)
    e._notifier.notify_plan_for_review = AsyncMock()
    loader = MagicMock(return_value=(_INVENTORY, ""))
    e._load_generation_inventory = loader

    await e.submit_for_review(_plan("ok1", "audit:rule_x", ["novel_fn"]))
    await e.submit_for_review(_plan(
        "open1", title="Unrelated dashboard polish",
        rationale="Tidy the public page typography spacing",
    ))

    assert loader.call_count == 1
    for plan_id in ("ok1", "open1"):
        stored = m.get_plan(plan_id)
        assert stored is not None
        assert stored.status == "pending_review"
    assert e._notifier.notify_plan_for_review.await_count == 2


@pytest.mark.asyncio
async def test_submit_for_review_reality_grounding_fails_open(
    mem_fs: dict[str, str],
) -> None:
    """An unreadable inventory stamps ``unevaluated`` and the plan proceeds."""
    e, m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, generator_reality_grounding_enabled=True)
    e._notifier.notify_plan_for_review = AsyncMock()
    gates = _RecordingGateBackend()
    e._gate_activations = gates
    e._load_generation_inventory = lambda: None

    await e.submit_for_review(_plan("fo1", None, ["reality-inventory"]))

    stored = m.get_plan("fo1")
    assert stored is not None
    assert stored.status == "pending_review"
    actions = [cast(GateActivation, r).action for r in gates.rows]
    assert actions == ["unevaluated"]


@pytest.mark.asyncio
async def test_submit_for_review_reality_grounding_flag_off_never_reads(
    mem_fs: dict[str, str],
) -> None:
    """Flag off: the inventory seam is never called, even with declarations."""
    e, m, _c, _j = _make(mem_fs)
    e._notifier.notify_plan_for_review = AsyncMock()
    loader = MagicMock(return_value=(_INVENTORY, ""))
    e._load_generation_inventory = loader

    await e.submit_for_review(_plan("off1", None, ["reality-inventory"]))

    loader.assert_not_called()
    stored = m.get_plan("off1")
    assert stored is not None
    assert stored.status == "pending_review"


def test_load_generation_inventory_reads_and_fails_open(
    mem_fs: dict[str, str],
) -> None:
    """The real seam composes inventory + corpus; an OSError returns None."""
    e, _m, _c, _j = _make(mem_fs)
    with (
        patch("maestro.evolution.load_reality_inventory", return_value=_INVENTORY),
        patch.object(Path, "rglob", return_value=[]),
    ):
        assert e._load_generation_inventory() == (_INVENTORY, "")
    with patch(
        "maestro.evolution.load_reality_inventory", side_effect=OSError("boom"),
    ):
        assert e._load_generation_inventory() is None


# ── Shipped-CLI guard demoted to a backstop (proposal 136) ───────────────────


def _cli_plan(plan_id: str, names: list[str] | None) -> EvolutionPlan:
    plan = _plan(plan_id, None, names)
    plan.files_affected = ["maestro/cli.py"]
    plan.proposed_changes = ["Add a `maestro reality-inventory` subcommand"]
    return plan


def _cli_engine(
    mem_fs: dict[str, str], *, flag: bool, loaded: object = (_INVENTORY, ""),
) -> tuple[EvolutionEngine, Memory]:
    e, m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, generator_reality_grounding_enabled=flag)
    e._notifier.notify_plan_for_review = AsyncMock()
    e._gate_activations = _RecordingGateBackend()
    e._load_generation_inventory = lambda: cast(
        tuple[RealityInventory, str] | None, loaded,
    )
    return e, m


@pytest.mark.asyncio
async def test_cleared_cli_declaration_skips_shipped_cli_guard(
    mem_fs: dict[str, str],
) -> None:
    """A cleared novel ``cli:`` declaration is authoritative: the backstop never
    runs and the plan reaches review even though its prose names shipped surface."""
    e, m = _cli_engine(mem_fs, flag=True)
    spy = MagicMock(return_value={"reality-inventory"})
    with patch("maestro.evolution_submission._cli_grounding_verdict", spy):
        await e.submit_for_review(_cli_plan("cli1", ["cli:brand-new"]))

    spy.assert_not_called()
    stored = m.get_plan("cli1")
    assert stored is not None
    assert stored.status == "pending_review"
    e._notifier.notify_plan_for_review.assert_awaited_once()


@pytest.mark.asyncio
async def test_shipped_cli_declaration_refused_before_backstop(
    mem_fs: dict[str, str],
) -> None:
    """A declared already-shipped ``cli:`` name is refused by the clearance."""
    e, m = _cli_engine(mem_fs, flag=True)
    spy = MagicMock(return_value={"reality-inventory"})
    with patch("maestro.evolution_submission._cli_grounding_verdict", spy):
        await e.submit_for_review(_cli_plan("cli2", ["cli:reality-inventory"]))

    spy.assert_not_called()
    stored = m.get_plan("cli2")
    assert stored is not None
    assert stored.obsolete_reason is not None
    assert stored.obsolete_reason.startswith("[ungrounded-generation:duplicate]")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan_id", "flag", "names", "loaded"),
    [
        ("bare1", True, ["novel_fn"], (_INVENTORY, "")),
        ("none1", True, None, (_INVENTORY, "")),
        ("open1", True, ["cli:brand-new"], None),
        ("off1", False, ["cli:brand-new"], (_INVENTORY, "")),
    ],
)
async def test_shipped_cli_guard_backstop_still_runs(
    mem_fs: dict[str, str],
    plan_id: str,
    flag: bool,
    names: list[str] | None,
    loaded: object,
) -> None:
    """No cleared ``cli:`` declaration (bare name, nothing declared, fail-open
    inventory, flag off): the backstop runs and refuses as it does today."""
    e, m = _cli_engine(mem_fs, flag=flag, loaded=loaded)
    spy = MagicMock(return_value={"reality-inventory"})
    with patch("maestro.evolution_submission._cli_grounding_verdict", spy):
        await e.submit_for_review(_cli_plan(plan_id, names))

    spy.assert_called_once()
    stored = m.get_plan(plan_id)
    assert stored is not None
    assert stored.status == "obsolete"
    assert stored.obsolete_reason is not None
    assert stored.obsolete_reason.startswith(
        "Proposes only already-shipped CLI surface",
    )
