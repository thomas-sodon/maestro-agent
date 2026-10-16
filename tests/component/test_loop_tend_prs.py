"""Component tests for the ``tend_evolution_prs`` loop phase dispatch."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from maestro.evolution_tend import TendPRsResult
from tests.component.conftest import write_self_model
from tests.component.sdk_mock import SDKMock


@pytest.mark.asyncio
async def test_tend_prs_invoked_on_first_iteration(make_agent, memory_dir):
    """First iteration after restart always runs tending, regardless of cadence."""
    write_self_model(memory_dir)
    agent, _ch, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(
        "Reflection 1", json.dumps({"identity_statements": ["I am"]}),
    )
    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"), \
         patch.object(
             agent.evolution, "tend_evolution_prs",
             new=AsyncMock(return_value=TendPRsResult()),
         ) as tend_mock:
        # tend_prs_every is large so only the first-iteration branch can fire.
        agent.config.tend_prs_every = 999
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10)
    tend_mock.assert_awaited_once()
    # Tend stays purely mechanical, but the orphan scan reads merge state
    # through the injected is_pr_settled chokepoint (#2958) — the sole kwarg.
    assert tend_mock.await_args.args == ()
    assert tend_mock.await_args.kwargs == {"settled_oracle": agent.is_pr_settled}


@pytest.mark.asyncio
async def test_tend_prs_not_invoked_when_disabled(make_agent, memory_dir):
    """``tend_prs_enabled=False`` prevents the phase from running at all."""
    write_self_model(memory_dir)
    agent, _ch, _vcs = make_agent()
    agent.config.tend_prs_enabled = False
    sdk = SDKMock()
    sdk.then(
        "Reflection", json.dumps({"identity_statements": ["I am"]}),
    )
    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"), \
         patch.object(
             agent.evolution, "tend_evolution_prs",
             new=AsyncMock(return_value=TendPRsResult()),
         ) as tend_mock:
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10)
    tend_mock.assert_not_called()


@pytest.mark.asyncio
async def test_tend_prs_runs_during_budget_throttle(
    make_agent, memory_dir,
):
    """Tend is purely mechanical and runs even under burn-rate throttle —
    no LLM steps to gate."""
    write_self_model(memory_dir)
    agent, _ch, _vcs = make_agent()
    # ``run_loop`` overwrites ``_budget_throttle_entered_at`` from
    # lifecycle.json at startup, so the flag must be persisted ahead of time
    # for the run to see throttling as active.
    (memory_dir / "lifecycle.json").write_text(json.dumps({
        "iteration": 0,
        "next_phase": None,
        "budget_throttle_entered_at": "2026-04-22T00:00:00+00:00",
    }))

    sdk = SDKMock()
    sdk.then(
        "Reflection", json.dumps({"identity_statements": ["I am"]}),
    )
    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"), \
         patch.object(
             agent.evolution, "tend_evolution_prs",
             new=AsyncMock(return_value=TendPRsResult()),
         ) as tend_mock:
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10)
    tend_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_tend_prs_runs_under_peak_throttle(
    make_agent, memory_dir,
):
    """PR tending is top priority — peak-throttle does not gate it."""
    write_self_model(memory_dir)
    agent, _ch, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(
        "Reflection", json.dumps({"identity_statements": ["I am"]}),
    )
    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"), \
         patch(
             "maestro.loop.lifecycle._is_in_peak_throttle_window",
             return_value=True,
         ), \
         patch.object(
             agent.evolution, "tend_evolution_prs",
             new=AsyncMock(return_value=TendPRsResult()),
         ) as tend_mock:
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10)
    tend_mock.assert_awaited_once()
    # Sanity: burn-rate input never engaged in this scenario.
    assert agent.burn_rate_throttle_active is False


@pytest.mark.asyncio
async def test_tend_prs_runs_under_maintenance_mode(
    make_agent, memory_dir,
):
    """Operator-initiated maintenance mode does not gate tend — Thomas
    asked for tending to stay top-priority while the rest of the loop
    pauses."""
    write_self_model(memory_dir)
    agent, _ch, _vcs = make_agent()
    # ``run_loop`` re-reads the maintenance sidecar at startup so the flag
    # must be persisted ahead of time for the run to see it as active.
    (memory_dir / "maintenance.json").write_text(json.dumps({
        "active": True, "since": "2026-04-22T00:00:00+00:00",
        "set_by": "telegram",
    }))
    sdk = SDKMock()
    sdk.then(
        "Reflection", json.dumps({"identity_statements": ["I am"]}),
    )
    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"), \
         patch.object(
             agent.evolution, "tend_evolution_prs",
             new=AsyncMock(return_value=TendPRsResult()),
         ) as tend_mock:
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10)
    tend_mock.assert_awaited_once()
    assert agent.budget_throttle_active is True
    assert agent.burn_rate_throttle_active is False
