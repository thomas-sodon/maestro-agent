"""Tests for health-summary injection in agent.contemplate()."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import conscious_workspace as cw
from maestro.agent import QuestionPair
from maestro.audit_checks_calibration import (
    record_reflection_prediction,
    record_reflection_verdict,
)
from maestro.consciousness import Consciousness, SelfModel
from maestro.diagnostics import DiagnosticSnapshot
from maestro.evolution_fuel import FuelSelection
from maestro.flag_lifecycle import (
    _HORIZON_APPROACH_DAYS,
    EXPERIMENTAL_FLAGS,
    parse_review_by,
)
from maestro.host_capacity import HostCapacityReading
from maestro.journal import JournalEntry
from maestro.memory import EvolutionPlan, EvolutionProposal, TaskRecord
from maestro.messaging import Message
from maestro.plan_grounding import GroundingProbe
from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import HANDOFF_RELAPSE_MESSAGE_WINDOW
from maestro.self_calibration import ClaimEvidenceSources
from maestro.source_budget import SPLIT_DUE, SourceBand, SourceBudgetReading
from tests.support.gap_fuel import make_fuel


def _remediation_lap(pr: int, marker: str) -> TaskRecord:
    """A ``Remediate PR #<pr>`` lap whose result_summary carries *marker*."""
    return TaskRecord(
        task_id=f"lap{pr}",
        goal=f"Remediate PR #{pr}: address comments",
        plan=[],
        outcome="partial",  # type: ignore[arg-type]
        result_summary=f"Lap done — {marker}",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00",
    )


def _task(outcome: str = "success") -> TaskRecord:
    return TaskRecord(
        task_id="t",
        goal="g",
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary="",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00",
    )


def _win_task(task_id: str, outcome: str) -> TaskRecord:
    """A ``TaskRecord`` with an explicit *task_id* and *outcome* — for the
    evidence-bound claim-source tests (proposal 129)."""
    return TaskRecord(
        task_id=task_id,
        goal="g",
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary="",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00",
    )


def _noteworthy_snapshot() -> DiagnosticSnapshot:
    return DiagnosticSnapshot(
        recent_error_count=10,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=0,
        summary="10 recent error(s)",
        cost_trend="spiking",
    )


def _clean_snapshot() -> DiagnosticSnapshot:
    return DiagnosticSnapshot(
        recent_error_count=0,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=0,
        summary="All clear",
        cost_trend="flat",
    )


@pytest.mark.asyncio
async def test_contemplate_injects_health_summary_when_noteworthy(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Health summary appears in introspection context when thresholds are crossed."""
    agent = make_agent()
    snapshot = _noteworthy_snapshot()
    # Build tasks that trigger is_health_noteworthy (high error count already does)
    failing_tasks = [_task("failure")] * 5

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=snapshot), \
         patch.object(agent.memory, "get_recent_tasks", return_value=failing_tasks), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert len(captured_context) == 1
    ctx = captured_context[0]
    assert "## Health Summary" in ctx
    assert "success_rate=0%" in ctx
    assert "cost_trend=spiking" in ctx


@pytest.mark.asyncio
async def test_contemplate_omits_health_summary_when_not_noteworthy(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Health summary is absent when all health signals are normal."""
    agent = make_agent()
    snapshot = _clean_snapshot()
    healthy_tasks = [_task("success")] * 5

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=snapshot), \
         patch.object(agent.memory, "get_recent_tasks", return_value=healthy_tasks), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert len(captured_context) == 1
    assert "## Health Summary" not in captured_context[0]


@pytest.mark.asyncio
async def test_contemplate_health_combines_with_diagnostic_context(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Both diagnostic status and health summary appear when both are noteworthy."""
    agent = make_agent()
    # Snapshot that triggers both diagnostics.is_noteworthy AND stats.is_health_noteworthy
    snapshot = _noteworthy_snapshot()
    failing_tasks = [_task("failure")] * 5

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=snapshot), \
         patch.object(agent.memory, "get_recent_tasks", return_value=failing_tasks), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    ctx = captured_context[0]
    # Diagnostic status appears first
    assert "Operational status:" in ctx
    # Health summary also present
    assert "## Health Summary" in ctx


@pytest.mark.asyncio
async def test_contemplate_health_summary_survives_exception(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """contemplate() continues when health summary computation raises."""
    agent = make_agent()
    # Use a snapshot that would normally be noteworthy
    snapshot = _noteworthy_snapshot()

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=snapshot), \
         patch("maestro.health_check.summarize_health", side_effect=RuntimeError("boom")), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        result = await agent.contemplate()

    assert result == "reflection"
    # Health summary not injected due to exception, but contemplate still ran
    assert "## Health Summary" not in captured_context[0]


def _no_op_task() -> TaskRecord:
    return TaskRecord(
        task_id="t",
        goal="g",
        plan=[],
        outcome="success",  # type: ignore[arg-type]
        result_summary="No changes needed.",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00",
    )


def _meaningful_task() -> TaskRecord:
    return TaskRecord(
        task_id="t",
        goal="g",
        plan=[],
        outcome="success",  # type: ignore[arg-type]
        result_summary="Shipped the feature.",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00",
    )


def _costume_task() -> TaskRecord:
    """An investigation-framed goal discharged as tracked construction (COSTUME).

    The goal classifies as ``investigation`` and the kept ``branch`` makes the
    discharge ``construction``, so the frame-discharge owner stamps ``COSTUME`` —
    the pattern the proposal-112 sensor surfaces.
    """
    return TaskRecord(
        task_id="costume-task",
        goal="Investigate the throttle stall path",
        plan=[],
        outcome="success",  # type: ignore[arg-type]
        result_summary="Built a helper.",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00",
        branch="maestro/task/costume-task",
    )


@pytest.mark.asyncio
async def test_contemplate_injects_no_op_rate_condition(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """High no-op task rate alone surfaces the condition string in the prompt.

    Regression — previously HealthSummary.conditions was populated by
    summarize_health but never read by agent.contemplate's formatter, so the
    no-op rate never reached the LLM. With the fix, conditions both flip
    is_noteworthy and are appended to the injected '## Health Summary'.
    """
    agent = make_agent()
    snapshot = _clean_snapshot()
    # 4 no-op out of 10 (40%) > 30% threshold; all-success so no other gate fires.
    tasks = [_meaningful_task()] * 6 + [_no_op_task()] * 4

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=snapshot), \
         patch.object(agent.memory, "get_recent_tasks", return_value=tasks), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    ctx = captured_context[0]
    assert "## Health Summary" in ctx
    assert "No-op task rate" in ctx
    assert "4/10" in ctx


@pytest.mark.asyncio
async def test_contemplate_injects_narration_conversion_line(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A narration→action activation trace surfaces a NARRATION-CONVERSION line
    in the introspection context (proposal 090).

    One ``rule_14_pr_less`` actuation grounded on a PR the
    stubbed merge oracle shows landed renders as 1 detection → 1 actuation → 1
    settled, wired into the ``## Already Known State`` block via
    ``format_narration_conversion_summary``.
    """
    agent = make_agent()
    snapshot = _clean_snapshot()
    mem_fs[str(agent.config.memory_dir / "gate_activations.jsonl")] = (
        '{"gate": "rule_14_pr_less", '
        '"action": "directive_set", '
        '"timestamp": "2026-06-01T00:00:00+00:00", "detail": "pr=#5"}\n'
    )
    agent._host_or_git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=True,
    )

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=snapshot), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_meaningful_task()]), \
         patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    ctx = captured_context[0]
    assert "NARRATION-CONVERSION:" in ctx
    assert "1 detections → 1 actuations (0 dropped) → 1 settled" in ctx
    assert "rule_14_pr_less 1/1/1" in ctx


def _seed_journal(mem_fs, agent, *contents: str) -> None:  # type: ignore[no-untyped-def]
    """Write journal entries carrying *contents* into the agent's mem_fs journal."""
    lines = [
        json.dumps(asdict(JournalEntry(
            entry_id="e",
            timestamp="2026-01-01T00:00:00+00:00",
            entry_type="reflection",
            content=content,
            triggered_by="contemplation",
        )))
        for content in contents
    ]
    mem_fs[str(agent.config.memory_dir / "journal.jsonl")] = "\n".join(lines) + "\n"


@pytest.mark.asyncio
async def test_contemplate_injects_undefined_coinage_line_when_enabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag on and no lexicon file, a repeated undefined coinage in the
    journal surfaces an UNDEFINED-COINAGE line (proposal 101).

    Exercises the missing-lexicon guard: ``docs/lexicon.md`` does not exist, so
    the defined-spellings set is empty and the coinage is undefined.
    """
    agent = make_agent()
    agent.config.audit_undefined_coinage_display_enabled = True
    _seed_journal(mem_fs, agent, "flux-capacitor " * 5)

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_meaningful_task()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    ctx = captured_context[0]
    assert "UNDEFINED-COINAGE:" in ctx
    assert "flux-capacitor (5)" in ctx


@pytest.mark.asyncio
async def test_contemplate_undefined_coinage_self_extinguishes_when_defined(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag on and the coinage defined in ``docs/lexicon.md``, the line
    is suppressed — the sensor self-extinguishes once a term is defined.

    Exercises the lexicon-present branch and the case-insensitive subtraction.
    """
    agent = make_agent()
    agent.config.audit_undefined_coinage_display_enabled = True
    _seed_journal(mem_fs, agent, "flux-capacitor " * 5)
    mem_fs[str(agent.config.workspace / "docs" / "lexicon.md")] = (
        "| term | definition | names | anchor | variants | minted |\n"
        "|---|---|---|---|---|---|\n"
        "| flux-capacitor | Makes time travel possible. | The 1.21GW handle. "
        "| `flux` |  | back to the future |\n"
    )

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_meaningful_task()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert "UNDEFINED-COINAGE:" not in captured_context[0]


@pytest.mark.asyncio
async def test_contemplate_omits_undefined_coinage_line_when_disabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag off (default), no UNDEFINED-COINAGE line is injected even
    when the journal is actively minting an undefined coinage."""
    agent = make_agent()
    assert agent.config.audit_undefined_coinage_display_enabled is False
    _seed_journal(mem_fs, agent, "flux-capacitor " * 5)

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_meaningful_task()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert "UNDEFINED-COINAGE:" not in captured_context[0]


@pytest.mark.asyncio
async def test_contemplate_injects_measurement_costume_line_when_enabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag on and a COSTUME task in the recent window, a
    MEASUREMENT-COSTUME line naming its task id is injected (proposal 112)."""
    agent = make_agent()
    agent.config.audit_costume_display_enabled = True

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_costume_task()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    ctx = captured_context[0]
    assert "MEASUREMENT-COSTUME:" in ctx
    assert "costume-task" in ctx


@pytest.mark.asyncio
async def test_contemplate_measurement_costume_self_extinguishes_without_costume(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag on but no COSTUME task in the window, the line self-extinguishes.

    ``_meaningful_task`` has a frame-``other`` goal, so the owner stamps ``N/A`` and
    the sensor emits nothing.
    """
    agent = make_agent()
    agent.config.audit_costume_display_enabled = True

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_meaningful_task()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert "MEASUREMENT-COSTUME:" not in captured_context[0]


@pytest.mark.asyncio
async def test_contemplate_omits_measurement_costume_line_when_disabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag off (default), no MEASUREMENT-COSTUME line is injected even
    when a COSTUME task is present in the recent window."""
    agent = make_agent()
    assert agent.config.audit_costume_display_enabled is False

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_costume_task()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert "MEASUREMENT-COSTUME:" not in captured_context[0]


def _earliest_horizon() -> date:
    """The nearest ``review_by`` in the live manifest — the anchor the FLAG-HORIZON
    contemplate tests derive their reference dates from, so they stay green as the
    manifest's real dates move rather than pinning a fixed calendar day."""
    return min(parse_review_by(f.review_by) for f in EXPERIMENTAL_FLAGS)


@pytest.mark.asyncio
async def test_contemplate_injects_flag_horizon_line_when_enabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag on and the clock set one day before the nearest ``review_by``,
    a FLAG-HORIZON line surfaces under ``## Measured Behavioral Data`` (proposal 117).
    """
    agent = make_agent()
    agent.config.audit_flag_horizon_display_enabled = True
    near_due = _earliest_horizon() - timedelta(days=1)

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.loop.contemplation.datetime") as mock_dt, \
         patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_meaningful_task()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        mock_dt.now.return_value = datetime(
            near_due.year, near_due.month, near_due.day, tzinfo=UTC,
        )
        mock_dt.UTC = UTC
        await agent.contemplate()

    ctx = captured_context[0]
    assert "## Measured Behavioral Data" in ctx
    assert "FLAG-HORIZON:" in ctx


@pytest.mark.asyncio
async def test_contemplate_flag_horizon_self_extinguishes_when_none_due(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag on but the clock comfortably before every horizon, the line is
    absent — the sensor self-suppresses when no flag is near or past its horizon."""
    agent = make_agent()
    agent.config.audit_flag_horizon_display_enabled = True
    none_due = _earliest_horizon() - timedelta(days=_HORIZON_APPROACH_DAYS + 30)

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.loop.contemplation.datetime") as mock_dt, \
         patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_meaningful_task()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        mock_dt.now.return_value = datetime(
            none_due.year, none_due.month, none_due.day, tzinfo=UTC,
        )
        mock_dt.UTC = UTC
        await agent.contemplate()

    assert "FLAG-HORIZON:" not in captured_context[0]


@pytest.mark.asyncio
async def test_contemplate_omits_flag_horizon_line_when_disabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag off (default), no FLAG-HORIZON line is injected even when a
    horizon is imminent — the display is gated off by default."""
    agent = make_agent()
    assert agent.config.audit_flag_horizon_display_enabled is False
    near_due = _earliest_horizon() - timedelta(days=1)

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.loop.contemplation.datetime") as mock_dt, \
         patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_meaningful_task()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        mock_dt.now.return_value = datetime(
            near_due.year, near_due.month, near_due.day, tzinfo=UTC,
        )
        mock_dt.UTC = UTC
        await agent.contemplate()

    assert "FLAG-HORIZON:" not in captured_context[0]


async def _split_due_context(agent, readings):  # type: ignore[no-untyped-def]
    """Run ``contemplate`` over a patched source walk; return the context and walk mock."""
    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.source_budget.read_source_budget", return_value=readings) as walk, \
         patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_meaningful_task()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()
    return captured_context[0], walk


_SPLIT_DUE_READING = SourceBudgetReading("maestro/big.py", SPLIT_DUE, SourceBand.SPLIT_DUE)


@pytest.mark.asyncio
async def test_contemplate_injects_split_due_line_when_enabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on with one split-due file: exactly one SPLIT-DUE line names it (proposal 147)."""
    agent = make_agent()
    agent.config.audit_split_due_sensor_enabled = True
    ctx, _ = await _split_due_context(agent, [_SPLIT_DUE_READING])
    lines = [line for line in ctx.splitlines() if line.startswith("SPLIT-DUE:")]
    assert len(lines) == 1
    assert "maestro/big.py" in lines[0]


@pytest.mark.asyncio
async def test_contemplate_split_due_self_extinguishes_when_none_qualify(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on but no file at or above the split-due line: the line is absent."""
    agent = make_agent()
    agent.config.audit_split_due_sensor_enabled = True
    ok = SourceBudgetReading("maestro/small.py", 10, SourceBand.OK)
    ctx, _ = await _split_due_context(agent, [ok])
    assert "SPLIT-DUE:" not in ctx


@pytest.mark.asyncio
async def test_contemplate_omits_split_due_line_when_disabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off (default): the line is absent and the source tree is never walked."""
    agent = make_agent()
    assert agent.config.audit_split_due_sensor_enabled is False
    ctx, walk = await _split_due_context(agent, [_SPLIT_DUE_READING])
    assert "SPLIT-DUE:" not in ctx
    walk.assert_not_called()


@pytest.mark.asyncio
async def test_contemplate_skips_health_when_diagnostics_failed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When diagnostics snapshot raises, health summary is also skipped (snapshot is None)."""
    agent = make_agent()

    captured_context: list[str] = []
    captured_measured: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(context)
        captured_measured.append(measured_context)
        return "reflection"

    # Suppress the measured-behavioral-data block (independent of the
    # diagnostics snapshot) so this test stays focused on the
    # diagnostic/health-skip path.
    with patch("maestro.diagnostics.build_diagnostic_snapshot", side_effect=RuntimeError("boom")), \
         patch("maestro.loop.contemplation.format_known_state_summary", return_value=""), \
         patch("maestro.loop.contemplation.format_task_shape_summary", return_value=""), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        result = await agent.contemplate(context="ctx")

    assert result == "reflection"
    # Neither diagnostic nor health summary, just the original context
    assert captured_context[0] == "ctx"
    assert captured_measured[0] == ""


@pytest.mark.asyncio
async def test_contemplate_records_owned_backlog_snapshot_and_surfaces_delta(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """contemplate() writes one ledger row and injects the OWNED-NET-DELTA line.

    Pre-seed a prior snapshot (count=5) so the fresh snapshot of the live
    pending_review count (mocked to 2) produces a second ledger row and a
    computable signed delta, which must surface in the Measured Behavioral
    Data block.
    """
    agent = make_agent()
    ledger_path = str(agent.config.memory_dir / "owned_backlog_ledger.jsonl")
    mem_fs[ledger_path] = (
        '{"iteration": 1, "count": 5, "timestamp": "t1"}\n'
    )

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    pending = [MagicMock(), MagicMock()]  # live pending_review count == 2

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_task("success")] * 3), \
         patch.object(agent.memory, "get_pending_plans", return_value=pending), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(iteration=2)

    # Exactly one fresh row appended (prior seed + this snapshot == 2 rows).
    rows = [
        line for line in mem_fs[ledger_path].splitlines() if line.strip()
    ]
    assert len(rows) == 2
    assert '"count": 2' in rows[1]
    assert '"iteration": 2' in rows[1]

    ctx = captured_context[0]
    assert "## Measured Behavioral Data" in ctx
    assert "OWNED-NET-DELTA: prev=5 now=2 net=-3" in ctx


@pytest.mark.asyncio
async def test_contemplate_reuses_injected_snapshot(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An injected snapshot is used verbatim; contemplate builds none (#1211).

    The loop hoists one snapshot per fresh iteration for the status update
    and hands it to ``contemplate`` so the loop's most expensive phase does
    not repeat the bounded log-tail + ``usage.jsonl`` parse. ``build`` is
    guarded with an ``AssertionError`` side effect — but the build site's
    ``except Exception`` would swallow that, so ``assert_not_called`` is the
    load-bearing check, backed by the injected summary reaching the prompt.
    """
    agent = make_agent()
    snapshot = _noteworthy_snapshot()

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    must_not_call = MagicMock(side_effect=AssertionError(
        "build_diagnostic_snapshot must not be called when snapshot is injected",
    ))

    with patch("maestro.diagnostics.build_diagnostic_snapshot", must_not_call), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_task("success")]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(diag_snapshot=snapshot)

    must_not_call.assert_not_called()
    # The injected snapshot's summary reaches the introspection prompt,
    # proving the reused snapshot — not a rebuilt one — drove the context.
    assert len(captured_context) == 1
    assert "Operational status: 10 recent error(s)" in captured_context[0]


@pytest.mark.asyncio
async def test_contemplate_builds_when_no_snapshot_injected(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With no injected snapshot, contemplate builds exactly one (fallback path)."""
    agent = make_agent()

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        return "reflection"

    build = MagicMock(return_value=_clean_snapshot())

    with patch("maestro.diagnostics.build_diagnostic_snapshot", build), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_task("success")]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    build.assert_called_once()


@pytest.mark.asyncio
async def test_contemplate_surfaces_lap_delivery_split_when_delivering(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A delivering remediation lap in the window surfaces the LAP-DELIVERY line
    in the Measured Behavioral Data block (proposal 116)."""
    agent = make_agent()
    delivering = _remediation_lap(4242, str(Marker.DELIVERED_AWAITING_MERGE))

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[delivering]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(iteration=2)

    ctx = captured_context[0]
    assert "## Measured Behavioral Data" in ctx
    assert "LAP-DELIVERY: 1/1" in ctx


@pytest.mark.asyncio
async def test_contemplate_omits_lap_delivery_split_without_delivery_lap(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The sensor self-extinguishes: a window with no delivery-relevant
    remediation lap injects no LAP-DELIVERY line (proposal 116)."""
    agent = make_agent()

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_task("success")]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(iteration=2)

    assert "LAP-DELIVERY:" not in captured_context[0]


def _handoff_msg(pr: int) -> Message:
    """A delivered ``escalation_bridge`` handoff (``handoff-<pr>-<head_tag>``)."""
    return Message(
        message_id=f"handoff-{pr}-abc12345",
        timestamp="2026-01-01T00:00:00+00:00",
        sender="maestro",
        content=f"Handoff — PR #{pr} carries an implemented cure ...",
        message_type="question",
    )


def _relapse_lap(pr: int) -> TaskRecord:
    """A ``Remediate PR #<pr>`` lap strictly after :func:`_handoff_msg`."""
    return TaskRecord(
        task_id=f"relapse{pr}",
        goal=f"Remediate PR #{pr}: re-read merge blocker",
        plan=[],
        outcome="partial",  # type: ignore[arg-type]
        result_summary="lap done",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T10:00:00+00:00",
    )


async def _handoff_relapse_context(
    agent,  # type: ignore[no-untyped-def]
) -> str:
    """Run contemplate over one relapse lap and return the measured context."""
    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_relapse_lap(2579)]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(iteration=2)
    return captured_context[0]


@pytest.mark.asyncio
async def test_contemplate_surfaces_handoff_relapse_when_lineage_reengaged(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A lap re-engaging a lineage after its escalation_bridge handoff surfaces
    the HANDOFF-RELAPSE line in the measured-data block (#4316)."""
    agent = make_agent()
    agent.message_store = MagicMock()
    agent.message_store.get_recent.return_value = [_handoff_msg(2579)]

    ctx = await _handoff_relapse_context(agent)

    assert "HANDOFF-RELAPSE:" in ctx
    assert "#2579 (x1)" in ctx
    agent.message_store.get_recent.assert_called_once_with(
        HANDOFF_RELAPSE_MESSAGE_WINDOW
    )


@pytest.mark.asyncio
async def test_contemplate_omits_handoff_relapse_without_handoff(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """No delivered handoff → the sensor self-suppresses (#4316)."""
    agent = make_agent()
    agent.message_store = MagicMock()
    agent.message_store.get_recent.return_value = []

    ctx = await _handoff_relapse_context(agent)

    assert "HANDOFF-RELAPSE" not in ctx


@pytest.mark.asyncio
async def test_contemplate_omits_handoff_relapse_without_message_store(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """No message store → no messages read, no tag, no raise (#4316)."""
    agent = make_agent()
    agent.message_store = None

    ctx = await _handoff_relapse_context(agent)

    assert "HANDOFF-RELAPSE" not in ctx


def _doa_plan(
    plan_id: str = "ep1",
    status: str = "obsolete",
    obsolete_reason: str | None = (
        "Admission gate refused net-new mint: backlog saturated (5 >= 5) — land "
        "or dispose an existing plan before proposing more"
    ),
) -> EvolutionPlan:
    """An EvolutionPlan carrying the fields the DOA sensor reads."""
    return EvolutionPlan(
        plan_id=plan_id,
        title="Add a widget",
        category="capability",
        rationale="r",
        proposed_changes=["x"],
        files_affected=["maestro/x.py"],
        expected_impact="none",
        status=status,  # type: ignore[arg-type]
        obsolete_reason=obsolete_reason,
        created_at="2026-01-01T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_contemplate_surfaces_dead_on_arrival_mints_when_door_obsoleted(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A plan obsoleted at the submission door surfaces the DEAD-ON-ARRIVAL-MINTS
    line in the Measured Behavioral Data block (proposal 120)."""
    agent = make_agent()

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_task("success")]), \
         patch.object(agent.memory, "get_all_plans", return_value=[_doa_plan()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(iteration=2)

    ctx = captured_context[0]
    assert "## Measured Behavioral Data" in ctx
    assert "DEAD-ON-ARRIVAL-MINTS: 1/1" in ctx
    assert "evolve=1" in ctx


@pytest.mark.asyncio
async def test_contemplate_omits_dead_on_arrival_mints_without_door_death(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The sensor self-extinguishes: a window whose plans none died at the door
    injects no DEAD-ON-ARRIVAL-MINTS line (proposal 120)."""
    agent = make_agent()

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_task("success")]), \
         patch.object(agent.memory, "get_all_plans",
                      return_value=[_doa_plan(status="merged", obsolete_reason=None)]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(iteration=2)

    assert "DEAD-ON-ARRIVAL-MINTS:" not in captured_context[0]


_UNGROUNDED_PROBE = GroundingProbe(kind="SYMBOL_ABSENT", locator="my_new_gate")
_UNGROUNDED_REASON = (
    "[ungrounded:duplicate] grounding probe SYMBOL_ABSENT on my_new_gate is "
    "refuted by a fresh reading of the tree"
)


def _ungrounded_plan(
    plan_id: str = "ep1",
    status: str = "obsolete",
    obsolete_reason: str | None = _UNGROUNDED_REASON,
    grounding: GroundingProbe | None = _UNGROUNDED_PROBE,
) -> EvolutionPlan:
    """An EvolutionPlan carrying the fields the ungrounded-rate sensor reads."""
    return EvolutionPlan(
        plan_id=plan_id,
        title="Add a widget",
        category="capability",
        rationale="r",
        proposed_changes=["x"],
        files_affected=["maestro/x.py"],
        expected_impact="none",
        status=status,  # type: ignore[arg-type]
        obsolete_reason=obsolete_reason,
        grounding=grounding,
        created_at="2026-01-01T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_contemplate_surfaces_ungrounded_rate_when_premise_refuted(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A premise-carrying plan the tree refuted surfaces the UNGROUNDED-RATE line
    in the Measured Behavioral Data block (proposal 114)."""
    agent = make_agent()

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_task("success")]), \
         patch.object(agent.memory, "get_all_plans", return_value=[_ungrounded_plan()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(iteration=2)

    ctx = captured_context[0]
    assert "## Measured Behavioral Data" in ctx
    assert "UNGROUNDED-RATE: 1/1" in ctx
    assert "duplicate=1" in ctx


@pytest.mark.asyncio
async def test_contemplate_omits_ungrounded_rate_without_refuted_premise(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The sensor self-extinguishes: a window whose premise-carrying plans none
    were tree-refuted injects no UNGROUNDED-RATE line (proposal 114)."""
    agent = make_agent()

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_task("success")]), \
         patch.object(agent.memory, "get_all_plans",
                      return_value=[_ungrounded_plan(status="pending_review", obsolete_reason=None)]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(iteration=2)

    assert "UNGROUNDED-RATE:" not in captured_context[0]


@pytest.mark.asyncio
async def test_contemplate_surfaces_throttle_residency(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An event log dominated by throttle milestones injects a residency line.

    Seeds an open ``budget_throttle_enter`` milestone 23h before ``now`` (no
    matching exit), so the trailing-24h residency exceeds the 50% surfacing
    threshold and the health summary carries the residency condition — even
    when the diagnostic snapshot itself is clean.
    """
    agent = make_agent()
    snapshot = _clean_snapshot()
    healthy_tasks = [_task("success")] * 5

    now = datetime.now(UTC)
    throttle_entries = [
        JournalEntry(
            entry_id="e1",
            timestamp=(now - timedelta(hours=23)).isoformat(),
            entry_type="milestone",
            content="Entering budget throttle",
            triggered_by="budget_throttle_enter",
        ),
    ]

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=snapshot), \
         patch.object(agent.memory, "get_recent_tasks", return_value=healthy_tasks), \
         patch.object(agent.journal, "events_by_date_range", return_value=throttle_entries), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert len(captured_context) == 1
    ctx = captured_context[0]
    assert "## Health Summary" in ctx
    assert "Budget-throttle residency" in ctx


@pytest.mark.asyncio
async def test_contemplate_surfaces_residency_for_open_spell_beyond_window(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A throttle open >24h ago (still active) still surfaces the residency line.

    The ``budget_throttle_enter`` milestone fell outside the trailing-24h
    window, so ``events_by_date_range`` returns nothing — but the loop is still
    throttled (``_budget_throttle_entered_at`` set). Contemplate seeds that open
    spell so residency saturates rather than under-reporting to 0.0 (fix-2570:
    a loop pinned at the ceiling for >24h must remain visible).
    """
    agent = make_agent()
    snapshot = _clean_snapshot()
    healthy_tasks = [_task("success")] * 5

    now = datetime.now(UTC)
    agent._budget_throttle_entered_at = (now - timedelta(hours=30)).isoformat()

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=snapshot), \
         patch.object(agent.memory, "get_recent_tasks", return_value=healthy_tasks), \
         patch.object(agent.journal, "events_by_date_range", return_value=[]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert len(captured_context) == 1
    ctx = captured_context[0]
    assert "Budget-throttle residency 100% of the last 24h" in ctx


@pytest.mark.asyncio
async def test_contemplate_residency_read_failure_preserves_health_summary(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An event-log read failure drops only the residency line, not the summary.

    The residency read is isolated in its own try/except, so a
    ``events_by_date_range`` error degrades to no residency line while the rest
    of the health summary (built from the failing-task streak) still injects.
    """
    agent = make_agent()
    snapshot = _clean_snapshot()
    failing_tasks = [_task("failure")] * 5  # failure_streak >= 3 → noteworthy

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=snapshot), \
         patch.object(agent.memory, "get_recent_tasks", return_value=failing_tasks), \
         patch.object(agent.journal, "events_by_date_range", side_effect=OSError("journal read boom")), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert len(captured_context) == 1
    ctx = captured_context[0]
    assert "## Health Summary" in ctx
    assert "Budget-throttle residency" not in ctx


# ── MULTI-PR-ROLLOUT injection (proposal 118) ────────────────────────


def _rolling_proposal() -> EvolutionProposal:
    return EvolutionProposal(
        proposal_id="prop-42",
        title="Split the refactor",
        goal="Land it across ordered slices.",
        status="rolling_out",
        created_at="2026-08-24T00:00:00Z",
    )


def _rollout_slice(plan_id: str, phase_index: int, status: str) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title=f"Slice {phase_index}",
        category="capability",
        rationale="reason",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        status=status,  # type: ignore[arg-type]
        parent_plan_id="prop-42",
        phase_index=phase_index,
        created_at="2026-08-24T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_contemplate_injects_multi_pr_rollout_when_flag_on(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag on and an in-flight proposal, the rollout line appears under
    the ``## Measured Behavioral Data`` header (proposal 118)."""
    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    proposals = [_rolling_proposal()]
    slices = [
        _rollout_slice("s0", 0, "merged"),
        _rollout_slice("s1", 1, "approved"),
    ]

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[]), \
         patch.object(agent.memory, "get_evolution_proposals", return_value=proposals), \
         patch.object(agent.memory, "get_slices_for_proposal", return_value=slices), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert len(captured_context) == 1
    ctx = captured_context[0]
    assert "## Measured Behavioral Data" in ctx
    assert "MULTI-PR-ROLLOUT: prop-42" in ctx
    assert "1/2 slices landed" in ctx
    assert "next=phase1(approved)" in ctx


@pytest.mark.asyncio
async def test_contemplate_skips_multi_pr_rollout_read_when_flag_off(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag off (the default) the proposal read is skipped entirely —
    not merely suppressed — so the off-path is byte-identical (proposal 118)."""
    agent = make_agent()
    get_proposals = MagicMock(return_value=[_rolling_proposal()])

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[]), \
         patch.object(agent.memory, "get_evolution_proposals", get_proposals), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert "MULTI-PR-ROLLOUT" not in captured_context[0]
    get_proposals.assert_not_called()


@pytest.mark.asyncio
async def test_contemplate_omits_multi_pr_rollout_when_no_rolling_out(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on but no ``rolling_out`` proposal: the self-suppressing formatter
    returns "" and no line is injected (proposal 118)."""
    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    proposals = [
        EvolutionProposal(
            proposal_id="p",
            title="t",
            goal="g",
            status="slicing",
            created_at="2026-08-24T00:00:00Z",
        ),
    ]

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[]), \
         patch.object(agent.memory, "get_evolution_proposals", return_value=proposals), \
         patch.object(agent.memory, "get_slices_for_proposal", return_value=[]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert "MULTI-PR-ROLLOUT" not in captured_context[0]


@pytest.mark.asyncio
async def test_contemplate_threads_host_capacity_reading_when_flag_on(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the sensor flag on, contemplate folds a live host-capacity reading.

    Closes proposal 125's wiring on the fallback-build path: the reader is passed
    to ``build_diagnostic_snapshot`` so a real iteration exposes host capacity
    (not just a unit test passing the arg directly). The loop-hoist path is
    covered by the diagnostics threading tests.
    """
    agent = make_agent()
    agent.config.host_capacity_sensor_enabled = True
    reading = HostCapacityReading(
        load_per_core=0.5,
        memory_used_fraction=0.5,
        swap_used_fraction=0.0,
        docker_root_free_gib=100.0,
    )
    snapshot = DiagnosticSnapshot(
        recent_error_count=2,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=1,
        summary="2 recent error(s)",
    )
    build = MagicMock(return_value=snapshot)

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        return "Some reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", build), \
         patch("maestro.loop.contemplation.host_capacity.read_host_capacity", return_value=reading), \
         patch("maestro.loop.contemplation.latest_context_window", return_value=None), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(context="")

    assert build.call_args.kwargs["host_capacity"] is reading


@pytest.mark.asyncio
async def test_contemplate_passes_none_host_capacity_when_flag_off(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the sensor flag off, contemplate takes no reading and passes None.

    Default-off must be silent and cost nothing: ``read_host_capacity`` is never
    called and the snapshot's ``host_capacity`` argument stays ``None``.
    """
    agent = make_agent()
    agent.config.host_capacity_sensor_enabled = False
    snapshot = DiagnosticSnapshot(
        recent_error_count=2,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=1,
        summary="2 recent error(s)",
    )
    build = MagicMock(return_value=snapshot)
    read = MagicMock()

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        return "Some reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", build), \
         patch("maestro.loop.contemplation.host_capacity.read_host_capacity", read), \
         patch("maestro.loop.contemplation.latest_context_window", return_value=None), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(context="")

    assert build.call_args.kwargs["host_capacity"] is None
    read.assert_not_called()


# ---------------------------------------------------------------------------
# SELF-CALIBRATION sensor (proposal 129) — read the reflection ledger back on
# itself, flag-gated on the calibration-authority switch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contemplate_injects_self_calibration_when_scored(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on + a verdict_source-stamped bet ⇒ the SELF-CALIBRATION line appears."""
    agent = make_agent()
    agent.config.audit_calibration_verdict_authority_enabled = True
    record_reflection_prediction(agent.predictions, "reflection-1", "p", "loss")
    record_reflection_verdict(
        agent.predictions, "reflection-1", "correct",
        verdict_source="measured", self_verdict="correct",
    )

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_task()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(iteration=2)

    assert len(captured_context) == 1
    assert "SELF-CALIBRATION" in captured_context[0]


@pytest.mark.asyncio
async def test_contemplate_omits_self_calibration_when_nothing_scored(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on but no verdict_source-stamped bet ⇒ the sensor self-suppresses."""
    agent = make_agent()
    agent.config.audit_calibration_verdict_authority_enabled = True

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_task()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(iteration=2)

    assert len(captured_context) == 1
    assert "SELF-CALIBRATION" not in captured_context[0]


@pytest.mark.asyncio
async def test_contemplate_skips_self_calibration_read_when_flag_off(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off ⇒ the ledger read is skipped entirely, even with stamped rows
    present, so the assembled block stays byte-identical to before."""
    agent = make_agent()
    agent.config.audit_calibration_verdict_authority_enabled = False
    record_reflection_prediction(agent.predictions, "reflection-1", "p", "loss")
    record_reflection_verdict(
        agent.predictions, "reflection-1", "correct", verdict_source="measured",
    )

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "reflection"

    calibration_reader = MagicMock(return_value="")
    with patch("maestro.loop.contemplation.format_self_calibration_summary", calibration_reader), \
         patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_task()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(iteration=2)

    assert len(captured_context) == 1
    assert "SELF-CALIBRATION" not in captured_context[0]
    calibration_reader.assert_not_called()


# ---------------------------------------------------------------------------
# Evidence-bound capability claims — the sources bundle (proposal 129)
# ---------------------------------------------------------------------------


def _version_record(sha: str):  # type: ignore[no-untyped-def]
    """A minimal ``VersionRecord`` carrying *sha* for the archived-SHA source."""
    from maestro.memory import VersionRecord

    return VersionRecord(
        version_id="v1",
        plan_id="p1",
        plan_title="t",
        commit_sha=sha,
        merged_at="2026-01-01T00:00:00+00:00",
        window_tasks_total=1,
        window_tasks_succeeded=1,
        window_tasks_failed=0,
        window_sdk_cost_usd=0.0,
        window_sdk_calls=1,
        window_cache_hit_rate=0.0,
        window_rate_limit_hits=0,
        window_error_count=0,
        quality_gate_pass=True,
    )


_MINIMAL_SENSOR_LEXICON = (
    "| term | definition | names | anchor | variants | minted |\n"
    "|---|---|---|---|---|---|\n"
    "| forced-quiet-ratio | ratio | n | `some_sensor` | `FORCED-QUIET-RATIO` "
    "| behavioral-sensor tag map |\n"
)


def test_collect_claim_evidence_sources_reads_ledger_and_lexicon(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """The bundle splits recent task ids by outcome, collects archived SHAs, and
    pulls the canonical sensor-tag set from the lexicon (the exists → read branch).
    """
    agent = make_agent()
    mem_fs[str(agent.config.workspace / "docs" / "lexicon.md")] = (
        _MINIMAL_SENSOR_LEXICON
    )
    tasks = [
        _win_task("aaaaaaaaaaaa", "success"),
        _win_task("bbbbbbbbbbbb", "failure"),
        _win_task("cccccccccccc", "partial"),
    ]
    with patch.object(agent.memory, "get_recent_tasks", return_value=tasks), \
         patch.object(
             agent.memory, "read_version_records",
             return_value=[_version_record("1a2b3c4d5e6f")],
         ):
        sources = agent._collect_claim_evidence_sources()

    assert sources.successful_task_ids == frozenset({"aaaaaaaaaaaa"})
    assert sources.failed_task_ids == frozenset({"bbbbbbbbbbbb"})
    assert sources.known_task_ids == frozenset(
        {"aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"}
    )
    assert sources.archived_shas == ("1a2b3c4d5e6f",)
    assert sources.sensor_tags == frozenset({"FORCED-QUIET-RATIO"})


def test_collect_claim_evidence_sources_no_lexicon_empty_tags(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When ``docs/lexicon.md`` is absent, the sensor-tag set is empty (the
    exists → skip branch) — the ledger sources are still built."""
    agent = make_agent()
    with patch.object(
             agent.memory, "get_recent_tasks",
             return_value=[_win_task("aaaaaaaaaaaa", "success")],
         ), \
         patch.object(agent.memory, "read_version_records", return_value=[]):
        sources = agent._collect_claim_evidence_sources()

    assert sources.sensor_tags == frozenset()
    assert sources.successful_task_ids == frozenset({"aaaaaaaaaaaa"})
    assert sources.archived_shas == ()


@pytest.mark.asyncio
async def test_contemplate_flag_off_skips_claim_evidence_sources(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off ⇒ the sources bundle is never built (zero version-record reads) and
    ``update_self_model`` is called with the flag off and no sources."""
    agent = make_agent()
    agent.config.audit_evidence_bound_claims_enabled = False
    version_reader = MagicMock(return_value=[])
    usm = AsyncMock(return_value=SelfModel())

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_task()]), \
         patch.object(agent.memory, "read_version_records", version_reader), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", usm), \
         patch.object(agent.consciousness, "load_self_model",
                      return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    version_reader.assert_not_called()
    assert usm.call_args.kwargs["evidence_bound_claims_enabled"] is False
    assert usm.call_args.kwargs["claim_evidence_sources"] is None


@pytest.mark.asyncio
async def test_contemplate_flag_on_builds_claim_evidence_sources(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on ⇒ the sources bundle is built (version records are read) and passed
    to ``update_self_model`` with the flag on."""
    agent = make_agent()
    agent.config.audit_evidence_bound_claims_enabled = True
    version_reader = MagicMock(return_value=[_version_record("1a2b3c4d5e6f")])
    usm = AsyncMock(return_value=SelfModel())

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        return "reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot",
               return_value=_clean_snapshot()), \
         patch.object(
             agent.memory, "get_recent_tasks",
             return_value=[_win_task("aaaaaaaaaaaa", "success")],
         ), \
         patch.object(agent.memory, "read_version_records", version_reader), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", usm), \
         patch.object(agent.consciousness, "load_self_model",
                      return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    version_reader.assert_called_once()
    assert usm.call_args.kwargs["evidence_bound_claims_enabled"] is True
    sources = usm.call_args.kwargs["claim_evidence_sources"]
    assert isinstance(sources, ClaimEvidenceSources)
    assert sources.successful_task_ids == frozenset({"aaaaaaaaaaaa"})
    assert sources.archived_shas == ("1a2b3c4d5e6f",)


# ---------------------------------------------------------------------------
# Conscious-access workspace broadcast (proposal 130) — _broadcast_moment_prefix
# ---------------------------------------------------------------------------


def _broadcast_moment() -> cw.ConsciousMoment:
    """Assemble a real moment carrying one admitted, above-floor item."""
    item = cw.build_workspace_item(
        item_id="i1", source_kind="audit_finding", source_ref="ref",
        observed_at="2026-01-01T00:00:00+00:00", trust="mechanical_observation",
        confidence=0.5, expires_at=None, summary="a bounded summary",
    )
    return cw.assemble_moment(
        [item], assembled_at="2026-01-01T00:00:00+00:00",
        permission_summary="all", blind_spots=["gap"],
        predecessor_id=None, continuity="genesis",
    )


def test_broadcast_moment_prefix_off_returns_empty(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off: no block is rendered and no receipt is written, even with an
    active moment in memory."""
    agent = make_agent()
    agent.config.conscious_workspace_broadcast_enabled = False
    agent._active_conscious_moment = _broadcast_moment()
    agent._active_broadcast_receipts = []

    assert agent._broadcast_moment_prefix("contemplation") == ""
    assert agent._active_broadcast_receipts == []


def test_broadcast_moment_prefix_delivers_and_records_receipt(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on: the in-memory moment is rendered and one receipt is recorded,
    stamped with the moment's selection_hash."""
    agent = make_agent()
    agent.config.conscious_workspace_broadcast_enabled = True
    moment = _broadcast_moment()
    agent._active_conscious_moment = moment
    agent._active_broadcast_receipts = []

    block = agent._broadcast_moment_prefix("contemplation")

    assert "broadcast to contemplation" in block
    assert moment.moment_id in block
    assert moment.selection_hash in block
    assert len(agent._active_broadcast_receipts) == 1
    receipt = agent._active_broadcast_receipts[0]
    assert receipt.moment_hash == moment.selection_hash
    assert receipt.consumer == "contemplation"


def test_broadcast_moment_prefix_same_moment_three_consumers(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """One selected moment reaches all three consumers: the three receipts share
    the moment's selection_hash and exactly one receipt is recorded per call.
    ``task_generation`` is the third consumer and does not route through
    ``introspect``, so its delivery is proven here alongside the two priced
    consumers (proposal 130)."""
    agent = make_agent()
    agent.config.conscious_workspace_broadcast_enabled = True
    moment = _broadcast_moment()
    agent._active_conscious_moment = moment
    agent._active_broadcast_receipts = []

    block_c = agent._broadcast_moment_prefix("contemplation")
    block_e = agent._broadcast_moment_prefix("evolution")
    block_t = agent._broadcast_moment_prefix("task_generation")

    assert moment.selection_hash in block_c
    assert moment.selection_hash in block_e
    assert moment.selection_hash in block_t
    assert "broadcast to task_generation" in block_t
    assert len(agent._active_broadcast_receipts) == 3
    consumers = [r.consumer for r in agent._active_broadcast_receipts]
    assert consumers == ["contemplation", "evolution", "task_generation"]
    hashes = {r.moment_hash for r in agent._active_broadcast_receipts}
    assert hashes == {moment.selection_hash}


def test_broadcast_moment_prefix_reconstructs_from_ledger_on_resume(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Cold mid-iteration resume: the in-memory moment is gone but its id is
    restored, so the helper reconstructs the moment from the episode ledger."""
    agent = make_agent()
    agent.config.conscious_workspace_broadcast_enabled = True
    moment = _broadcast_moment()
    episode = cw.ConsciousEpisode(
        moment=moment, receipts=(), cited_task_ids=(), cited_plan_ids=(),
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    agent._active_conscious_moment = None
    agent._active_conscious_moment_id = moment.moment_id
    agent._active_broadcast_receipts = []

    with patch.object(
        agent.conscious_episodes, "read_recent", return_value=[episode],
    ):
        block = agent._broadcast_moment_prefix("contemplation")

    assert moment.moment_id in block
    assert len(agent._active_broadcast_receipts) == 1
    assert agent._active_broadcast_receipts[0].moment_hash == moment.selection_hash


def test_broadcast_moment_prefix_resume_id_mismatch_or_empty_returns_empty(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Resume with a restored id that the ledger does not corroborate (a
    different newest moment, or an empty ledger) yields no delivery."""
    agent = make_agent()
    agent.config.conscious_workspace_broadcast_enabled = True
    moment = _broadcast_moment()
    other = cw.ConsciousEpisode(
        moment=moment, receipts=(), cited_task_ids=(), cited_plan_ids=(),
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    agent._active_conscious_moment = None
    agent._active_conscious_moment_id = "cm-different"
    agent._active_broadcast_receipts = []

    with patch.object(agent.conscious_episodes, "read_recent", return_value=[other]):
        assert agent._broadcast_moment_prefix("contemplation") == ""
    with patch.object(agent.conscious_episodes, "read_recent", return_value=[]):
        assert agent._broadcast_moment_prefix("contemplation") == ""
    assert agent._active_broadcast_receipts == []


def test_broadcast_moment_prefix_no_moment_and_no_id_returns_empty(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on but neither an in-memory moment nor a restored id: no delivery,
    and the ledger is never consulted."""
    agent = make_agent()
    agent.config.conscious_workspace_broadcast_enabled = True
    agent._active_conscious_moment = None
    agent._active_conscious_moment_id = None
    agent._active_broadcast_receipts = []

    with patch.object(agent.conscious_episodes, "read_recent") as read_recent:
        assert agent._broadcast_moment_prefix("contemplation") == ""
    read_recent.assert_not_called()
    assert agent._active_broadcast_receipts == []


def test_deliver_moment_visible_ids_exclude_redacted(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Proposal 130: the delivered admitted set is the consumer-*visible* ids.

    A moment carrying an untrusted-external item (withheld by
    ``render_active_moment``) yields a ``visible_item_ids`` set that excludes the
    redacted id, and the block omits its summary — so a downstream citation filter
    keyed off that set can never accept an id the consumer never saw. The receipt
    still records the redaction."""
    agent = make_agent()
    agent.config.conscious_workspace_broadcast_enabled = True
    trusted = cw.build_workspace_item(
        item_id="wi-ok", source_kind="audit_finding", source_ref="ref",
        observed_at="2026-01-01T00:00:00+00:00", trust="mechanical_observation",
        confidence=0.5, expires_at=None, summary="visible summary",
    )
    untrusted = cw.build_workspace_item(
        item_id="wi-bad", source_kind="audit_finding", source_ref="ref",
        observed_at="2026-01-01T00:00:00+00:00", trust="untrusted_external",
        confidence=0.5, expires_at=None, summary="withheld summary",
    )
    schema = cw.build_attention_schema(
        focus="f", uncertainty=0.1, capacity_used=2, omitted_count=0,
        permission_summary="all", blind_spots=[],
    )
    moment = cw.build_conscious_moment(
        moment_id="cm-redact", predecessor_id=None, continuity="genesis",
        assembled_at="2026-01-01T00:00:00+00:00", policy_version="v1",
        items=(trusted, untrusted), excluded=(), schema=schema,
    )
    agent._active_conscious_moment = moment
    agent._active_broadcast_receipts = []

    delivery = agent._deliver_moment_to_consumer("evolution")

    assert delivery is not None
    block, delivered_moment, visible_item_ids = delivery
    assert delivered_moment is moment
    assert visible_item_ids == frozenset({"wi-ok"})
    assert "withheld summary" not in block
    assert agent._active_broadcast_receipts[0].redacted_item_ids == ("wi-bad",)


def test_deliver_moment_off_or_no_moment_returns_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``_deliver_moment_to_consumer`` returns ``None`` with the flag off (even with
    a moment in memory) and with the flag on but no moment — the two byte-identical
    fall-back conditions the block-only wrapper collapses to ``""``."""
    agent = make_agent()
    agent._active_broadcast_receipts = []
    agent.config.conscious_workspace_broadcast_enabled = False
    agent._active_conscious_moment = _broadcast_moment()
    assert agent._deliver_moment_to_consumer("evolution") is None

    agent.config.conscious_workspace_broadcast_enabled = True
    agent._active_conscious_moment = None
    agent._active_conscious_moment_id = None
    assert agent._deliver_moment_to_consumer("evolution") is None
    assert agent._active_broadcast_receipts == []


# ---------------------------------------------------------------------------
# _resolve_active_moment (proposal 130) — the extracted moment-resolution helper
# shared by _broadcast_moment_prefix and _finalize_conscious_episode.
# ---------------------------------------------------------------------------


def test_resolve_active_moment_returns_in_memory(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The in-memory active moment is returned as-is, without touching the ledger."""
    agent = make_agent()
    moment = _broadcast_moment()
    agent._active_conscious_moment = moment

    with patch.object(agent.conscious_episodes, "read_recent") as read_recent:
        assert agent._resolve_active_moment() is moment
    read_recent.assert_not_called()


def test_resolve_active_moment_reconstructs_from_ledger_on_resume(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Cold resume: in-memory moment gone but id restored and corroborated by the
    newest ledger row, so the moment is reconstructed from the ledger."""
    agent = make_agent()
    moment = _broadcast_moment()
    episode = cw.ConsciousEpisode(
        moment=moment, receipts=(), cited_task_ids=(), cited_plan_ids=(),
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    agent._active_conscious_moment = None
    agent._active_conscious_moment_id = moment.moment_id

    with patch.object(
        agent.conscious_episodes, "read_recent", return_value=[episode],
    ):
        resolved = agent._resolve_active_moment()

    assert resolved is not None
    assert resolved.moment_id == moment.moment_id


def test_resolve_active_moment_no_id_never_reads_ledger(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """No in-memory moment and no restored id: None, and the ledger is untouched."""
    agent = make_agent()
    agent._active_conscious_moment = None
    agent._active_conscious_moment_id = None

    with patch.object(agent.conscious_episodes, "read_recent") as read_recent:
        assert agent._resolve_active_moment() is None
    read_recent.assert_not_called()


def test_resolve_active_moment_resume_id_mismatch_returns_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Restored id the newest ledger row does not corroborate yields None."""
    agent = make_agent()
    moment = _broadcast_moment()
    other = cw.ConsciousEpisode(
        moment=moment, receipts=(), cited_task_ids=(), cited_plan_ids=(),
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    agent._active_conscious_moment = None
    agent._active_conscious_moment_id = "cm-different"

    with patch.object(agent.conscious_episodes, "read_recent", return_value=[other]):
        assert agent._resolve_active_moment() is None


# ---------------------------------------------------------------------------
# evolve() plan-proposal citation wiring (proposal 130) — the lone-propose_plan
# path delivers the active moment and captures the cited plan id.
# ---------------------------------------------------------------------------


def _pin_evolve_for_citation(agent) -> None:  # type: ignore[no-untyped-def]
    """Pin the levers so evolve() reaches the lone-``propose_plan`` submission
    tail with no priced stage/gate calls, and mock the collaborators
    around ``propose_plan`` so only the citation wiring is exercised."""
    agent.config.wip_primacy_enabled = False
    agent.config.multi_pr_proposals_enabled = False
    agent.config.pre_mint_generation_gate_enabled = False
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(raw_output="gates ok"),
    )
    agent.consciousness.introspect = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent.evolution.submit_for_review = AsyncMock()  # type: ignore[method-assign]
    # Fuel the tick (proposal 155 step 4) so evolve reaches propose_plan.
    agent._admitted_evolution_fuel = MagicMock(  # type: ignore[method-assign]
        return_value=FuelSelection(selected=make_fuel()),
    )


def _cited_plan() -> EvolutionPlan:
    return EvolutionPlan(
        plan_id="cited-plan-1", title="Cited plan",
        category="capability", rationale="r", proposed_changes=["c"],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_evolve_delivers_moment_and_captures_cited_plan(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Broadcast on with an active moment: evolve() hands ``propose_plan`` the
    rendered block, the moment id, and the admitted item ids, then appends the
    returned plan's id to the per-iteration cited-plan accumulator."""
    agent = make_agent()
    _pin_evolve_for_citation(agent)
    agent.config.conscious_workspace_broadcast_enabled = True
    moment = _broadcast_moment()
    agent._active_conscious_moment = moment
    expected_ids = frozenset(item.item_id for item in moment.items)
    propose = AsyncMock(return_value=_cited_plan())
    agent.evolution.propose_plan = propose  # type: ignore[method-assign]

    await agent.evolve()

    assert propose.await_count == 1
    kwargs = propose.await_args.kwargs
    assert kwargs["workspace_moment"].startswith("## Conscious moment")
    assert kwargs["workspace_moment_id"] == moment.moment_id
    assert kwargs["admitted_item_ids"] == expected_ids
    assert agent._active_cited_plan_ids == ["cited-plan-1"]


@pytest.mark.asyncio
async def test_evolve_flag_off_passes_no_citation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Broadcast off: evolve() calls ``propose_plan`` with the no-citation defaults
    and the cited-plan accumulator stays empty, even with a moment in memory."""
    agent = make_agent()
    _pin_evolve_for_citation(agent)
    agent.config.conscious_workspace_broadcast_enabled = False
    agent._active_conscious_moment = _broadcast_moment()
    propose = AsyncMock(return_value=_cited_plan())
    agent.evolution.propose_plan = propose  # type: ignore[method-assign]

    await agent.evolve()

    assert propose.await_count == 1
    kwargs = propose.await_args.kwargs
    assert kwargs["workspace_moment"] == ""
    assert kwargs["workspace_moment_id"] is None
    assert kwargs["admitted_item_ids"] == frozenset()
    assert agent._active_cited_plan_ids == []


# ---------------------------------------------------------------------------
# evolve() fail-closed citation guard (proposal 130) — a stamped plan citation
# is re-validated against the delivered moment before it is committed.
# ---------------------------------------------------------------------------


def _stamped_plan(
    moment_id: str | None, item_ids: list[str] | None,
) -> EvolutionPlan:
    """A lone plan already stamped with a conscious-access citation."""
    return EvolutionPlan(
        plan_id="cited-plan-1", title="Cited plan",
        category="capability", rationale="r", proposed_changes=["c"],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
        workspace_moment_id=moment_id, workspace_item_ids=item_ids,
    )


@pytest.mark.asyncio
async def test_evolve_valid_stamped_citation_survives_guard(
    make_agent,  # type: ignore[no-untyped-def]
    caplog,  # type: ignore[no-untyped-def]
) -> None:
    """A plan stamped with the live moment id and a visible item id passes the
    guard: its provenance is preserved, its id is appended to the cited-plan
    accumulator, and no rejection is logged."""
    agent = make_agent()
    _pin_evolve_for_citation(agent)
    agent.config.conscious_workspace_broadcast_enabled = True
    moment = _broadcast_moment()  # one admitted item with item_id "i1"
    agent._active_conscious_moment = moment
    propose = AsyncMock(
        return_value=_stamped_plan(moment.moment_id, ["i1"]),
    )
    agent.evolution.propose_plan = propose  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="maestro"):
        await agent.evolve()

    assert agent._active_cited_plan_ids == ["cited-plan-1"]
    submitted = agent.evolution.submit_for_review.await_args.args[0]
    assert submitted.workspace_moment_id == moment.moment_id
    assert submitted.workspace_item_ids == ["i1"]
    assert "Nulling invalid conscious-access citation" not in caplog.text


@pytest.mark.asyncio
async def test_evolve_cross_moment_citation_is_nulled_and_not_appended(
    make_agent,  # type: ignore[no-untyped-def]
    caplog,  # type: ignore[no-untyped-def]
) -> None:
    """A plan stamped with a *different* moment id fails the guard: its provenance
    is nulled, the plan id is NOT appended to the accumulator, one WARNING is
    logged, and the cleaned plan is still submitted (advisory citations never drop
    legitimate work)."""
    agent = make_agent()
    _pin_evolve_for_citation(agent)
    agent.config.conscious_workspace_broadcast_enabled = True
    moment = _broadcast_moment()
    agent._active_conscious_moment = moment
    propose = AsyncMock(
        return_value=_stamped_plan("cm-tampered", ["i1"]),
    )
    agent.evolution.propose_plan = propose  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="maestro"):
        await agent.evolve()

    assert agent._active_cited_plan_ids == []
    assert agent.evolution.submit_for_review.await_count == 1
    submitted = agent.evolution.submit_for_review.await_args.args[0]
    assert submitted.plan_id == "cited-plan-1"
    assert submitted.workspace_moment_id is None
    assert submitted.workspace_item_ids is None
    assert "Nulling invalid conscious-access citation on plan cited-plan-1" in caplog.text
    assert "cross_moment" in caplog.text


@pytest.mark.asyncio
async def test_evolve_unknown_item_citation_is_nulled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A plan citing an id absent from the moment's visible admitted set fails the
    guard (unknown_item) and has its provenance nulled."""
    agent = make_agent()
    _pin_evolve_for_citation(agent)
    agent.config.conscious_workspace_broadcast_enabled = True
    moment = _broadcast_moment()  # visible ids == {"i1"}
    agent._active_conscious_moment = moment
    propose = AsyncMock(
        return_value=_stamped_plan(moment.moment_id, ["ghost"]),
    )
    agent.evolution.propose_plan = propose  # type: ignore[method-assign]

    await agent.evolve()

    assert agent._active_cited_plan_ids == []
    submitted = agent.evolution.submit_for_review.await_args.args[0]
    assert submitted.workspace_moment_id is None
    assert submitted.workspace_item_ids is None


@pytest.mark.asyncio
async def test_evolve_flag_off_leaves_stamped_plan_byte_identical(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With broadcast off the guard never runs: even a tampered stamped plan is
    submitted unchanged (its provenance is preserved) and the accumulator stays
    empty — flag-off is byte-identical to today."""
    agent = make_agent()
    _pin_evolve_for_citation(agent)
    agent.config.conscious_workspace_broadcast_enabled = False
    agent._active_conscious_moment = _broadcast_moment()
    tampered = _stamped_plan("cm-tampered", ["ghost"])
    agent.evolution.propose_plan = AsyncMock(return_value=tampered)  # type: ignore[method-assign]

    await agent.evolve()

    assert agent._active_cited_plan_ids == []
    submitted = agent.evolution.submit_for_review.await_args.args[0]
    assert submitted.workspace_moment_id == "cm-tampered"
    assert submitted.workspace_item_ids == ["ghost"]


@pytest.mark.asyncio
async def test_contemplate_resolution_reads_post_subcap_open_questions(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Bug #3791: dispositions are classified against the *persisted* list.

    ``contemplate`` saves the post-``surface_limitations_as_questions`` model
    and then classifies dispositions against it. The save enforces the audit
    sub-cap, so the in-memory list is only trustworthy if the save's normalized
    return is rebound. The eviction itself is mechanical, not an LLM verdict,
    so the resulting row must read ``kept`` — never ``removed``.

    The mocked ``update_self_model`` returns an already-capped list (as the
    real first save would), so the eviction here comes only from the
    *second* (contemplate re-save) cap point: the ``[limitation]`` re-mirror
    from ``limitations_known`` pushes the total over ``MAX_LIST_ITEMS`` and
    the oldest plain question is dropped. The first save is covered by the
    ``first_save`` tests below (#4566).
    """
    agent = make_agent()
    # Four audit questions (exactly the audit sub-cap) plus six plain
    # questions fill MAX_LIST_ITEMS; disjoint content tokens throughout.
    audit_questions = [
        f"[audit] {a} {b}?" for a, b in (
            ("alpha", "bravo"), ("charlie", "delta"),
            ("foxtrot", "golf"), ("hotel", "india"),
        )
    ]
    plain_questions = [
        f"{a} {b}?" for a, b in (
            ("juliet", "kilo"), ("lima", "mike"), ("november", "oscar"),
            ("papa", "quebec"), ("romeo", "sierra"), ("tango", "uniform"),
        )
    ]
    dropped = plain_questions[0]

    model = agent.consciousness.load_self_model()
    model.open_questions = [dropped, plain_questions[1]]
    agent.consciousness.save_self_model(model)
    agent.memory.record_task(TaskRecord(
        task_id="t-audit",
        goal="g",
        plan=[],
        outcome="success",
        result_summary="",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00+00:00",
        source="self_directed",
        key_findings="Probed the juliet kilo question.",
        linked_question=dropped,
    ))

    # A capped return: 4 audit + 6 plain == MAX_LIST_ITEMS. The re-mirrored
    # [limitation] row is the 11th, so the re-save evicts ``dropped``.
    returned = SelfModel(
        open_questions=[*plain_questions, *audit_questions],
        limitations_known=["victor whiskey xray"],
    )
    assert len(returned.open_questions) == Consciousness.MAX_LIST_ITEMS
    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch.object(agent.consciousness, "update_self_model",
                      new_callable=AsyncMock, return_value=returned):
        await agent.contemplate(iteration=7)

    persisted = agent.consciousness.load_self_model()
    assert dropped not in persisted.open_questions
    assert "[limitation] victor whiskey xray" in persisted.open_questions
    rows = agent.memory.get_recent_question_resolutions(50)
    by_task = {r.task_id: r for r in rows}
    assert by_task["t-audit"].disposition == "kept"


def test_classify_disposition_cap_evicted_records_kept(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A key the persist-time sub-cap dropped is not an LLM closure.

    ``contemplate`` routes those keys through the same ``suppressed_keys``
    channel proposal 116 uses for a mechanically-withheld question, so the
    disposition reads ``kept`` rather than ``removed``.
    """
    agent = make_agent()
    pair = QuestionPair(
        question="[audit] alpha bravo?",
        finding="f",
        task_id="t",
        outcome="success",
        normalized_key="[audit] alpha bravo?",
    )
    before = ["[audit] alpha bravo?"]
    after: list[str] = []
    assert agent._classify_disposition(pair, before, after) == ("removed", None)
    assert agent._classify_disposition(
        pair, before, after, {"[audit] alpha bravo?"},
    ) == ("kept", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("authority_enabled", [False, True])
async def test_contemplate_same_iteration_retry_is_idempotent(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
    authority_enabled: bool,
) -> None:
    """A resumed-in-place contemplate at the same iteration re-reads, not re-writes (#4106).

    The first attempt places ``reflection-5`` and snapshots the backlog, then
    ``update_self_model`` raises a transient ``STREAM_STALL``. The retry must
    still confront ``reflection-4`` (not the bet it just placed), never score
    ``reflection-5``, and append no second bet, snapshot, or sensor row.
    """
    from pathlib import Path

    from maestro.audit_checks_calibration import (
        PREDICTIONS_FILENAME,
    )
    from maestro.sdk import ErrorCategory, SDKError

    agent = make_agent()
    agent.config.audit_calibration_verdict_authority_enabled = authority_enabled
    record_reflection_prediction(
        agent.predictions, "reflection-4",
        "Prior-cycle bet text.", "No commit reaches origin/main.",
    )
    reflection = (
        "Reflecting.\nPRIOR VERDICT: wrong\n"
        "PREDICTION: This-cycle bet text.\n"
        "LOSS CONDITION: I close the cycle without a real gate exit code."
    )
    prompts: list[str] = []

    async def _fake_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        prompts.append(kwargs.get("prompt", ""))
        return MagicMock(response=reflection)

    stall = SDKError("stalled", category=ErrorCategory.STREAM_STALL)
    update = AsyncMock(side_effect=[stall, SelfModel()])
    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch("maestro.consciousness.query", side_effect=_fake_query), \
         patch.object(agent.consciousness, "update_self_model", update), \
         patch.object(agent.journal, "write_thought"):
        with pytest.raises(SDKError):
            await agent.contemplate(iteration=5)
        await agent.contemplate(iteration=5)

    assert len(prompts) == 2
    assert "Prior-cycle bet text." in prompts[1]
    assert "This-cycle bet text." not in prompts[1]
    rows = [
        json.loads(line)
        for line in mem_fs[str(Path("/fake/agent") / PREDICTIONS_FILENAME)].splitlines()
        if line.strip()
    ]
    bets = [
        r for r in rows
        if r.get("source") == "reflection" and r.get("prediction_id") == "reflection-5"
    ]
    assert len(bets) == 1
    verdicts_on_new = [
        r for r in rows
        if r.get("source") == "reflection_verdict"
        and r.get("prediction_id") == "reflection-5"
    ]
    assert verdicts_on_new == []
    snapshots = agent.owned_backlog.recent(10)
    assert [s.iteration for s in snapshots] == [5]
    sensor_path = str(agent.config.memory_dir / "sensor_readings.jsonl")
    sensor_rows = [line for line in mem_fs[sensor_path].splitlines() if line.strip()]
    assert len(sensor_rows) == 1


@pytest.mark.asyncio
async def test_contemplate_iteration_zero_always_records_backlog(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A non-loop contemplate (iteration 0) is never deduped, matching the bet path."""
    agent = make_agent()
    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch("maestro.consciousness.query", AsyncMock(return_value=MagicMock(response="r"))), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()
        await agent.contemplate()

    assert [s.iteration for s in agent.owned_backlog.recent(10)] == [0, 0]
    sensor_path = str(agent.config.memory_dir / "sensor_readings.jsonl")
    unlanded_rows = [
        line for line in mem_fs[sensor_path].splitlines()
        if "UNLANDED-REMEDIATION" in line
    ]
    assert len(unlanded_rows) == 2


def _seed_paired_task(agent, question: str) -> None:  # type: ignore[no-untyped-def]
    agent.memory.record_task(TaskRecord(
        task_id="t-paired",
        goal="g",
        plan=[],
        outcome="success",
        result_summary="",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00+00:00",
        source="self_directed",
        key_findings="Probed the paired question.",
        linked_question=question,
    ))


async def _contemplate_with_reply(agent, open_questions: list[str]) -> None:  # type: ignore[no-untyped-def]
    """Drive the *real* ``update_self_model`` with a stubbed SDK reply."""
    reply = MagicMock()
    reply.parse_json.return_value = {"open_questions": open_questions}
    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch("maestro.consciousness.query", AsyncMock(return_value=reply)), \
         patch("maestro.consciousness.query_on_session",
               AsyncMock(return_value=reply)):
        await agent.contemplate(iteration=7)


@pytest.mark.asyncio
async def test_contemplate_first_save_audit_subcap_eviction_records_kept(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Bug #4566: the ``update_self_model`` persist's audit sub-cap drop reads ``kept``."""
    agent = make_agent()
    audit = [
        f"[audit] {a} {b}?" for a, b in (
            ("alpha", "bravo"), ("charlie", "delta"), ("foxtrot", "golf"),
            ("hotel", "india"), ("juliet", "kilo"), ("lima", "mike"),
        )
    ]
    paired = audit[0]
    model = agent.consciousness.load_self_model()
    model.open_questions = [paired, audit[1]]
    agent.consciousness.save_self_model(model)
    _seed_paired_task(agent, paired)

    await _contemplate_with_reply(agent, audit)

    assert paired not in agent.consciousness.load_self_model().open_questions
    row = {
        r.task_id: r for r in agent.memory.get_recent_question_resolutions(50)
    }["t-paired"]
    assert row.disposition == "kept"
    assert row.refined_to is None


@pytest.mark.asyncio
async def test_contemplate_first_save_total_cap_eviction_not_refined(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Bug #4566: a total-cap drop is ``kept``, never a false ``refined`` edge."""
    agent = make_agent()
    words = [
        "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform",
        "victor", "whiskey",
    ]
    paired = "alpha bravo charlie delta?"
    seeded = [paired] + [f"{w} {w}x question?" for w in words]
    model = agent.consciousness.load_self_model()
    model.open_questions = list(seeded)
    agent.consciousness.save_self_model(model)
    _seed_paired_task(agent, paired)
    successor = "alphas bravos xray yankee zulu?"

    await _contemplate_with_reply(agent, [*seeded, successor])

    persisted = agent.consciousness.load_self_model().open_questions
    assert paired not in persisted
    assert successor in persisted
    row = {
        r.task_id: r for r in agent.memory.get_recent_question_resolutions(50)
    }["t-paired"]
    assert row.disposition == "kept"
    assert row.refined_to is None


@pytest.mark.asyncio
async def test_contemplate_passes_settled_prs_to_churn_summary(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """contemplate() hands the settlement ledger's settled set to the churn
    formatter so a merged PR's stale PR-READY/PR-STUCK line is suppressed
    (#4586)."""
    agent = make_agent()

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        return "reflection"

    seen: list[frozenset[int]] = []

    def _seen_settled(*args, settled_prs, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(settled_prs())
        return ""

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_meaningful_task()]), \
         patch("maestro.loop.contemplation.load_settled_prs", return_value=frozenset({77})), \
         patch("maestro.loop.contemplation.format_pr_churn_summary", side_effect=_seen_settled) as churn, \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    assert churn.call_count == 1
    assert seen == [frozenset({77})]
