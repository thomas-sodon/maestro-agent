"""Fresh-settlement grounding of contemplate()'s recent-tasks feed (audit #1263).

A ``Remediate PR #<n>`` task's cached ``result_summary`` records its *pre-merge*
verdict ("not yet landed per merge-truth"). When that PR has since merged, the
recent-tasks context handed to the priced reflection would narrate the lineage
as active churn unless the settlement ledger is re-read fresh at contemplation
time. ``annotate_settled_remediation_feed`` closes that narration-lag by
appending a ``[since merged]`` marker to a settled lineage's rendered findings —
reading only :func:`load_settled_prs` (a fetch-free ledger read), consulting it
at most once, and only when a recent task actually cites a PR number.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.consciousness import SelfModel
from maestro.diagnostics import DiagnosticSnapshot
from maestro.loop.known_state import (
    SINCE_MERGED_MARKER,
    annotate_settled_remediation_feed,
)
from maestro.memory import TaskRecord


def _remediation_lap(pr: int, summary: str = "not yet landed per merge-truth") -> TaskRecord:
    """A ``Remediate PR #<pr>`` lap whose result_summary carries *summary*."""
    return TaskRecord(
        task_id=f"lap{pr}",
        goal=f"Remediate PR #{pr}: address review comments",
        plan=[],
        outcome="partial",  # type: ignore[arg-type]
        result_summary=summary,
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00",
    )


def _plain_task() -> TaskRecord:
    """A non-remediation task whose goal cites no PR number."""
    return TaskRecord(
        task_id="t",
        goal="Add a widget to the dashboard",
        plan=[],
        outcome="success",  # type: ignore[arg-type]
        result_summary="Shipped the widget.",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00",
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


# ── Pure-helper tests: the settlement lookup in isolation ────────────────────


def test_annotate_marks_settled_cited_pr() -> None:
    """A remediation task citing a settled PR gets its findings marked (criterion 1)."""
    tasks = [_remediation_lap(3971)]
    result = annotate_settled_remediation_feed(tasks, lambda: frozenset({3971}))

    assert len(result) == 1
    findings = result[0].key_findings or result[0].result_summary or ""
    assert findings.startswith(SINCE_MERGED_MARKER)
    # The original verdict is preserved after the marker so the reflection still
    # reads what the lap recorded — only the settlement fact is prepended.
    assert "not yet landed per merge-truth" in findings


def test_annotate_prefix_survives_truncation() -> None:
    """The marker leads the findings so it survives the renderer's [:300] slice."""
    tasks = [_remediation_lap(3971, summary="x" * 500)]
    result = annotate_settled_remediation_feed(tasks, lambda: frozenset({3971}))

    findings = result[0].key_findings or ""
    assert findings[:300].startswith(SINCE_MERGED_MARKER)


def test_annotate_leaves_unsettled_cited_pr_unchanged() -> None:
    """A cited-but-unsettled PR passes through by identity (criterion 1, inverse)."""
    original = _remediation_lap(3972)
    result = annotate_settled_remediation_feed([original], lambda: frozenset({3971}))

    assert result[0] is original
    findings = result[0].key_findings or result[0].result_summary or ""
    assert SINCE_MERGED_MARKER not in findings


def test_annotate_skips_settlement_read_without_pr() -> None:
    """No task cites a PR ⇒ the settlement source is never consulted (criterion 2)."""
    load_settled = MagicMock(return_value=frozenset({3971}))
    tasks = [_plain_task()]

    result = annotate_settled_remediation_feed(tasks, load_settled)

    load_settled.assert_not_called()
    # The list is returned unchanged (same object) on the zero-cost common path.
    assert result is tasks


def test_annotate_consults_settlement_source_once() -> None:
    """Even with several cited PRs, the settlement read fires exactly once (criterion 2/3)."""
    load_settled = MagicMock(return_value=frozenset({3971}))
    tasks = [_remediation_lap(3971), _remediation_lap(3972), _remediation_lap(3973)]

    annotate_settled_remediation_feed(tasks, load_settled)

    assert load_settled.call_count == 1


def test_annotate_marks_only_the_settled_lineage_in_a_mixed_window() -> None:
    """In a mixed window, only the settled lineage's line changes."""
    settled = _remediation_lap(3971)
    unsettled = _remediation_lap(3972)
    plain = _plain_task()

    result = annotate_settled_remediation_feed(
        [settled, unsettled, plain], lambda: frozenset({3971}),
    )

    assert result[0].key_findings is not None
    assert result[0].key_findings.startswith(SINCE_MERGED_MARKER)
    assert result[1] is unsettled
    assert result[2] is plain


# ── Integration: the marker reaches contemplate()'s injected recent-tasks feed ─


def _mock_introspect_capturing(captured: list[list[TaskRecord]]):  # type: ignore[no-untyped-def]
    async def _introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured.append(recent)
        return "reflection"

    return _introspect


@pytest.mark.asyncio
async def test_contemplate_annotates_settled_remediation_line(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """contemplate() hands introspect a settled remediation lap marked '[since merged]'."""
    agent = make_agent()
    captured: list[list[TaskRecord]] = []

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch("maestro.loop.contemplation.load_settled_prs", return_value=frozenset({3971})), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_remediation_lap(3971)]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect_capturing(captured)), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(iteration=2)

    assert len(captured) == 1
    recent = captured[0]
    findings = recent[0].key_findings or recent[0].result_summary or ""
    assert findings.startswith(SINCE_MERGED_MARKER)


@pytest.mark.asyncio
async def test_contemplate_leaves_unsettled_remediation_line_unannotated(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An unsettled cited PR reaches introspect with no '[since merged]' marker."""
    agent = make_agent()
    captured: list[list[TaskRecord]] = []

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch("maestro.loop.contemplation.load_settled_prs", return_value=frozenset()), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_remediation_lap(3972)]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect_capturing(captured)), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(iteration=2)

    assert len(captured) == 1
    findings = captured[0][0].key_findings or captured[0][0].result_summary or ""
    assert SINCE_MERGED_MARKER not in findings


@pytest.mark.asyncio
async def test_contemplate_skips_settlement_read_when_no_pr_cited(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """No recent task cites a PR ⇒ load_settled_prs is never called (criterion 2).

    A call-count assertion on the patched settlement reader proves the common
    path pays zero new cost, and — with introspect mocked and the reader stubbed
    — that the feed reaches no SDK, push, or merge seam (criterion 3).
    """
    agent = make_agent()
    captured: list[list[TaskRecord]] = []
    load_settled = MagicMock(return_value=frozenset({3971}))

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=_clean_snapshot()), \
         patch("maestro.loop.contemplation.load_settled_prs", load_settled), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[_plain_task()]), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect_capturing(captured)), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(iteration=2)

    load_settled.assert_not_called()
