"""Split-due directive bridge in ``audit_self_model`` (proposal 147)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from maestro.source_budget import (
    SPLIT_DUE,
    DefinitionSize,
    SourceBand,
    SourceBudgetReading,
)

_DUE = SourceBudgetReading("maestro/due.py", SPLIT_DUE, SourceBand.SPLIT_DUE)
_OK = SourceBudgetReading("maestro/ok.py", 10, SourceBand.OK)
_CLUSTERS = [DefinitionSize("a", 4096), DefinitionSize("b", 2048)]


def _agent(make_agent: Any, *, enabled: bool = True, directive: str | None = None) -> Any:
    agent = make_agent()
    agent.config.audit_split_due_directive_enabled = enabled
    model = agent.consciousness.load_self_model()
    model.next_task_directive = directive
    agent.consciousness.save_self_model(model)
    return agent


async def _audit(agent: Any, readings: list[SourceBudgetReading]) -> Any:
    with patch("maestro.audit_checks.run_all_checks", return_value=[]), \
         patch("maestro.source_budget.read_source_budget",
               return_value=readings) as walk, \
         patch("maestro.source_budget.largest_definitions", return_value=_CLUSTERS):
        await agent._audit_self_model(iteration=1)
    return walk


def _levers(agent: Any) -> list[Any]:
    return [a for a in agent.lever_attempts.load_all() if a.lever == "split_due_directive"]


def _gates(agent: Any) -> list[Any]:
    return [r for r in agent.gate_activations.read_all() if r.gate == "split_due_directive"]


@pytest.mark.asyncio
async def test_fills_empty_slot_once_and_records_one_row(make_agent: Any) -> None:
    agent = _agent(make_agent)
    await _audit(agent, [_OK, _DUE])
    directive = agent.consciousness.load_self_model().next_task_directive
    assert directive is not None
    assert directive.startswith("Split maestro/due.py")
    assert "a (4 KiB), b (2 KiB)" in directive
    levers = _levers(agent)
    assert len(levers) == 1
    assert levers[0].verified is True
    assert levers[0].result is True
    gates = _gates(agent)
    assert len(gates) == 1
    assert gates[0].action == "directive_set"
    assert gates[0].detail == directive

    await _audit(agent, [_OK, _DUE])
    assert agent.consciousness.load_self_model().next_task_directive == directive
    assert len(_levers(agent)) == 1
    assert len(_gates(agent)) == 1


@pytest.mark.asyncio
async def test_nothing_due_writes_nothing(make_agent: Any) -> None:
    agent = _agent(make_agent)
    await _audit(agent, [_OK])
    assert agent.consciousness.load_self_model().next_task_directive is None
    assert _levers(agent) == []


@pytest.mark.asyncio
async def test_flag_off_does_not_walk(make_agent: Any) -> None:
    agent = _agent(make_agent, enabled=False)
    walk = await _audit(agent, [_DUE])
    walk.assert_not_called()
    assert agent.consciousness.load_self_model().next_task_directive is None
    assert _levers(agent) == []


@pytest.mark.asyncio
async def test_occupied_slot_untouched(make_agent: Any) -> None:
    agent = _agent(make_agent, directive="Finish PR #42")
    await _audit(agent, [_DUE])
    assert agent.consciousness.load_self_model().next_task_directive == "Finish PR #42"
    assert _levers(agent) == []


@pytest.mark.asyncio
async def test_owned_wip_blocks_the_fill(make_agent: Any) -> None:
    agent = _agent(make_agent)
    with patch.object(type(agent), "_wip_owns_slot", return_value=True):
        await _audit(agent, [_DUE])
    assert agent.consciousness.load_self_model().next_task_directive is None
    assert _levers(agent) == []


@pytest.mark.asyncio
async def test_unpersisted_write_records_false_and_no_gate(make_agent: Any) -> None:
    agent = _agent(make_agent)
    with patch.object(agent.consciousness, "mutate_self_model", return_value=True):
        await _audit(agent, [_DUE])
    assert agent.consciousness.load_self_model().next_task_directive is None
    levers = _levers(agent)
    assert len(levers) == 1
    assert levers[0].result is False
    assert _gates(agent) == []


@pytest.mark.asyncio
async def test_fill_refuses_a_slot_filled_concurrently(make_agent: Any) -> None:
    """The transactional fill re-checks the live slot and refuses to overwrite."""
    agent = _agent(make_agent)
    real_mutate = agent.consciousness.mutate_self_model

    def _racing_mutate(fn: Any) -> Any:
        def _occupy(model: Any) -> None:
            model.next_task_directive = "raced"
        real_mutate(_occupy)
        return real_mutate(fn)

    with patch.object(agent.consciousness, "mutate_self_model", side_effect=_racing_mutate):
        await _audit(agent, [_DUE])
    assert agent.consciousness.load_self_model().next_task_directive == "raced"
    levers = _levers(agent)
    assert len(levers) == 1
    assert levers[0].result is False
