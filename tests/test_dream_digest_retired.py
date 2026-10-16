"""Proposal 149 Step 5: the priced dream digest is deleted; the phase stays.

Pins the deletion — no ``dreaming`` module, no ``dream`` / ``dream_validate``
call policy, no dream_* knob or experimental flag, no ``read_dream_digest``
tool — and the surviving contract: the ``dream`` phase runs its zero-SDK
passes on ``dream_every``, neither budget-gated nor focus-suppressed, deduped
per frozen iteration, while historical cost rows still roll up to "Dreaming"
and legacy journal rows still load.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.agent import _LOOP_PHASES
from maestro.call_policy import CALL_POLICIES
from maestro.config import Config
from maestro.cost_tracking import CALLER_GROUPS, RETIRED_CALLERS, CostRateSnapshot
from maestro.flag_lifecycle import EXPERIMENTAL_FLAGS
from maestro.journal import Journal
from maestro.loop.lifecycle import (
    _BUDGET_GATED_PHASES,
    _FOCUS_SUPPRESSED_PHASES,
    DEDUPED_CADENCE_PHASES,
    LifecycleController,
)
from maestro.reflection_retrieval import READ_ONLY_REFLECTION_TOOLS
from maestro.tools import memory_tools
from tests.test_loop_lifecycle import _stub_agent

_PKG = Path(__file__).resolve().parent.parent / "maestro"
_DIR = Path("/fake/dream_retired")


# --- deletion ------------------------------------------------------------


def test_dreaming_module_deleted() -> None:
    assert not (_PKG / "dreaming.py").exists()


@pytest.mark.parametrize("caller", ["dream", "dream_validate"])
def test_dream_callers_retired_but_still_grouped(caller: str) -> None:
    assert caller not in CALL_POLICIES
    assert caller in RETIRED_CALLERS
    assert CALLER_GROUPS[caller] == "Dreaming"


def test_no_dream_knob_but_cadence_survives() -> None:
    names = {f.name for f in dataclasses.fields(Config)}
    dream_fields = {n for n in names if "dream" in n}
    assert dream_fields == {"loop_dream_every"}
    assert all("dream" not in f.flag for f in EXPERIMENTAL_FLAGS)


def test_read_dream_digest_tool_gone() -> None:
    assert "read_dream_digest" not in READ_ONLY_REFLECTION_TOOLS
    names = [t.name for t in memory_tools(MagicMock())]
    assert "read_dream_digest" not in names


# --- phase membership ----------------------------------------------------


def test_dream_phase_kept_but_ungated() -> None:
    assert "dream" in _LOOP_PHASES
    assert "dream" in DEDUPED_CADENCE_PHASES
    assert "dream" not in _BUDGET_GATED_PHASES
    assert "dream" not in _FOCUS_SUPPRESSED_PHASES


def _will_run(
    stub: SimpleNamespace, *, iteration: int = 10, smoke: bool = False,
) -> bool:
    return LifecycleController(stub).phase_will_run(  # type: ignore[arg-type]
        "dream",
        iteration=iteration,
        smoke=smoke,
        first_iteration_after_restart=False,
        task_every=0,
        evolve_every=0,
        dream_every=10,
        audit_self_every=0,
        body_frozen=False,
    )


def test_dream_runs_at_cadence_under_throttle() -> None:
    assert _will_run(_stub_agent(budget_throttle_active=True)) is True


def test_dream_runs_under_external_delivery() -> None:
    assert _will_run(_stub_agent(focus_profile="external_delivery")) is True


def test_dream_deduped_at_same_frozen_iteration() -> None:
    stub = _stub_agent(budget_throttle_active=True)
    stub._last_phase_iteration["dream"] = 10
    assert _will_run(stub) is False


def test_dream_off_cadence_does_not_run() -> None:
    assert _will_run(_stub_agent(), iteration=7) is False


def test_dream_skipped_under_smoke() -> None:
    assert _will_run(_stub_agent(), smoke=True) is False


# --- the zero-SDK passes --------------------------------------------------


@pytest.mark.asyncio
async def test_phase_dream_runs_passes_without_sdk(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    agent.config.archive_threshold_days = 30
    agent.memory.archive = MagicMock()  # type: ignore[method-assign]
    agent._open_owned_pr_states = MagicMock(return_value={})  # type: ignore[method-assign]
    with (
        patch("maestro.agent.audit_checks.log_gate_activation") as gate,
        patch("maestro.agent.create_session", new_callable=AsyncMock) as session,
        patch("maestro.call_policy.create_session", new_callable=AsyncMock) as single,
    ):
        await agent._phase_dream(iteration=5)

    report = mem_fs[str(agent.config.artifacts_dir / "phase_cost_report.md")]
    assert "# Phase Cost Report" in report
    agent.memory.archive.assert_called_once()
    gate.assert_not_called()
    session.assert_not_awaited()
    single.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_loop_frozen_throttle_runs_dream_once(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    agent.config.budget_rate_min_cost_usd = 0.0
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "last_shutdown": "2026-04-14T00:00:00+00:00",
        "iteration": 10,
        "next_phase": None,
        "budget_throttle_entered_at": "2026-04-14T00:10:00+00:00",
    })
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="")  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent._process_rejected_plans = AsyncMock()  # type: ignore[method-assign]
    agent._respond_to_messages = AsyncMock()  # type: ignore[method-assign]
    agent._audit_self_model = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(merged=[], rejected=[]),
    )
    dream_runner = AsyncMock()
    agent._phase_runners["dream"] = dream_runner
    snapshot = CostRateSnapshot(
        window_start="2026-04-14T00:00:00+00:00",
        window_end="2026-04-14T01:00:00+00:00",
        window_seconds=3600.0,
        total_cost_usd=100.0,
        total_input_tokens=0,
        total_output_tokens=0,
        total_cache_read_tokens=0,
        total_calls=1,
        cost_per_hour=100.0,
        cost_per_day=2400.0,
        cost_per_month=72000.0,
        tokens_in_per_hour=0.0,
        tokens_out_per_hour=0.0,
        by_caller={},
    )

    with (
        patch("maestro.agent.build_cost_rates", return_value=snapshot),
        patch("maestro.loop.lifecycle.build_cost_rates", return_value=snapshot),
    ):
        await agent.run_loop(
            interval=0, max_iters=2, task_every=0, evolve_every=999,
            dream_every=10,
        )

    assert agent.budget_throttle_active is True
    assert dream_runner.await_count == 1


# --- legacy journal rows ---------------------------------------------------


def _row(entry_id: str, **overrides: Any) -> str:
    row = {
        "entry_id": entry_id, "timestamp": "2026-01-01T00:00:00+00:00",
        "stage": "awakening", "entry_type": "reflection", "content": entry_id,
        "triggered_by": "agent_tool",
    }
    row.update(overrides)
    return json.dumps(row) + "\n"


def test_legacy_dream_entry_type_reads_as_reflection(mem_fs: dict[str, str]) -> None:
    mem_fs[str(_DIR / "journal.jsonl")] = _row("d1", entry_type="dream")
    [entry] = Journal(_DIR).read_by_type("reflection")
    assert entry.entry_id == "d1"
    assert entry.entry_type == "reflection"


def test_legacy_dreaming_rows_load_as_events(mem_fs: dict[str, str]) -> None:
    mem_fs[str(_DIR / "journal.jsonl")] = (
        _row("t1") + _row("d1", triggered_by="dreaming")
    )
    journal = Journal(_DIR)
    assert [e.entry_id for e in journal.read_recent(10)] == ["t1"]
    assert [e.entry_id for e in journal.read_recent_events(10)] == ["d1"]
