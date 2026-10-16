"""Tests for agent._snapshot_version — version archive snapshots on merge."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from maestro.memory import EvolutionPlan


def _make_plan(plan_id: str = "ep_1", pr_url: str | None = "https://github.com/o/r/pull/7") -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title="Example plan",
        category="capability",
        rationale="r",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        priority="capability",
        status="implemented",
        created_at="2026-01-01T00:00:00Z",
        pr_url=pr_url,
    )


def _stub_snapshot() -> MagicMock:
    snap = MagicMock()
    snap.sdk_usage = MagicMock(
        total_cost_usd=1.25,
        total_calls=7,
        cache_hit_rate=0.5,
        rate_limit_hits=0,
    )
    snap.recent_error_count = 2
    return snap


def test_snapshot_version_records_on_merge(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.memory.save_evolution_plan(_make_plan())
    agent.evolution._host = MagicMock()
    agent.evolution._host.get_commit_sha_of_pr.return_value = "sha-abc"
    agent.evolution._host.get_pr_check_conclusion.return_value = "success"

    with patch(
        "maestro.loop.implementation.diagnostics.build_diagnostic_snapshot",
        return_value=_stub_snapshot(),
    ):
        agent._snapshot_version("ep_1")

    versions = agent.memory.get_recent_versions()
    assert len(versions) == 1
    v = versions[0]
    assert v.plan_id == "ep_1"
    assert v.plan_title == "Example plan"
    assert v.commit_sha == "sha-abc"
    assert v.window_sdk_cost_usd == 1.25
    assert v.window_sdk_calls == 7
    assert v.window_cache_hit_rate == 0.5
    assert v.window_error_count == 2
    assert v.quality_gate_pass is True
    assert v.notes is None
    assert v.version_id.startswith("v")


def test_snapshot_version_same_second_yields_distinct_ids(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Two plans merged in the same wall-clock second get distinct ids (#1372).

    Before the fix, ``version_id`` truncated to second precision, so a batch of
    PRs detected as merged together produced identical ids.  Microsecond
    precision keeps them distinct while preserving the ``v``-prefixed, lexically
    sortable shape.
    """
    agent = make_agent()
    agent.memory.save_evolution_plan(_make_plan())
    agent.evolution._host = MagicMock()
    agent.evolution._host.get_commit_sha_of_pr.return_value = "sha"
    agent.evolution._host.get_pr_check_conclusion.return_value = "success"

    # Same second, different microseconds — and a zero-microsecond read whose
    # six-digit %f padding must survive (isoformat would have dropped it).
    t1 = datetime(2026, 1, 1, 12, 0, 0, 100000, tzinfo=UTC)
    t2 = datetime(2026, 1, 1, 12, 0, 0, 200000, tzinfo=UTC)
    t3 = datetime(2026, 1, 1, 12, 0, 0, 0, tzinfo=UTC)

    with patch(
        "maestro.loop.implementation.diagnostics.build_diagnostic_snapshot",
        return_value=_stub_snapshot(),
    ), patch("maestro.loop.implementation.datetime") as mock_dt:
        mock_dt.now.side_effect = [t1, t2, t3]
        agent._snapshot_version("ep_1")
        agent._snapshot_version("ep_1")
        agent._snapshot_version("ep_1")

    ids = [v.version_id for v in agent.memory.get_recent_versions(n=10)]
    assert len(ids) == 3
    assert len(set(ids)) == 3
    assert set(ids) == {
        "v20260101T120000100000",
        "v20260101T120000200000",
        "v20260101T120000000000",
    }
    for vid in ids:
        assert vid.startswith("v20260101T120000")


def test_snapshot_version_records_quality_gate_fail(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.memory.save_evolution_plan(_make_plan())
    agent.evolution._host = MagicMock()
    agent.evolution._host.get_commit_sha_of_pr.return_value = "sha-abc"
    agent.evolution._host.get_pr_check_conclusion.return_value = "failure"

    with patch(
        "maestro.loop.implementation.diagnostics.build_diagnostic_snapshot",
        return_value=_stub_snapshot(),
    ):
        agent._snapshot_version("ep_1")

    [v] = agent.memory.get_recent_versions()
    assert v.quality_gate_pass is False


def test_snapshot_version_unknown_ci_records_quality_gate_fail(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.memory.save_evolution_plan(_make_plan())
    agent.evolution._host = MagicMock()
    agent.evolution._host.get_commit_sha_of_pr.return_value = "sha-abc"
    agent.evolution._host.get_pr_check_conclusion.return_value = ""

    with patch(
        "maestro.loop.implementation.diagnostics.build_diagnostic_snapshot",
        return_value=_stub_snapshot(),
    ):
        agent._snapshot_version("ep_1")

    [v] = agent.memory.get_recent_versions()
    assert v.quality_gate_pass is False


def test_snapshot_version_no_pr_url_records_quality_gate_fail(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.memory.save_evolution_plan(_make_plan(pr_url=None))
    agent.evolution._host = MagicMock()

    with patch(
        "maestro.loop.implementation.diagnostics.build_diagnostic_snapshot",
        return_value=_stub_snapshot(),
    ):
        agent._snapshot_version("ep_1")

    [v] = agent.memory.get_recent_versions()
    assert v.quality_gate_pass is False
    agent.evolution._host.get_pr_check_conclusion.assert_not_called()


def test_snapshot_version_disabled_no_record(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.version_archive_enabled = False
    agent.memory.save_evolution_plan(_make_plan())
    agent.evolution._host = MagicMock()

    with patch(
        "maestro.loop.implementation.diagnostics.build_diagnostic_snapshot",
        return_value=_stub_snapshot(),
    ):
        agent._snapshot_version("ep_1")

    assert agent.memory.get_recent_versions() == []


def test_snapshot_version_missing_plan_is_noop(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.evolution._host = MagicMock()

    with patch(
        "maestro.loop.implementation.diagnostics.build_diagnostic_snapshot",
        return_value=_stub_snapshot(),
    ):
        agent._snapshot_version("nonexistent")

    assert agent.memory.get_recent_versions() == []


def test_snapshot_version_no_pr_url_still_records(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.memory.save_evolution_plan(_make_plan(pr_url=None))
    agent.evolution._host = MagicMock()

    with patch(
        "maestro.loop.implementation.diagnostics.build_diagnostic_snapshot",
        return_value=_stub_snapshot(),
    ):
        agent._snapshot_version("ep_1")

    [v] = agent.memory.get_recent_versions()
    assert v.commit_sha == ""


def test_snapshot_version_invalid_pr_url_falls_back_empty_sha(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.memory.save_evolution_plan(_make_plan(pr_url="not a url"))
    agent.evolution._host = MagicMock()

    with patch(
        "maestro.loop.implementation.diagnostics.build_diagnostic_snapshot",
        return_value=_stub_snapshot(),
    ):
        agent._snapshot_version("ep_1")

    [v] = agent.memory.get_recent_versions()
    assert v.commit_sha == ""


def test_snapshot_version_host_side_exception_falls_back_empty_sha(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.memory.save_evolution_plan(_make_plan())
    agent.evolution._host = MagicMock()
    agent.evolution._host.get_commit_sha_of_pr.side_effect = RuntimeError("host boom")
    agent.evolution._host.get_pr_check_conclusion.return_value = "success"

    with patch(
        "maestro.loop.implementation.diagnostics.build_diagnostic_snapshot",
        return_value=_stub_snapshot(),
    ):
        agent._snapshot_version("ep_1")

    [v] = agent.memory.get_recent_versions()
    assert v.commit_sha == ""
    assert v.quality_gate_pass is False
    entries = agent.journal.read_recent_events(n=5)
    assert any(
        e.entry_type == "milestone" and e.triggered_by == "version_snapshot"
        for e in entries
    ), "snapshot must still record the milestone event"


def test_snapshot_version_handles_none_sdk_usage(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.memory.save_evolution_plan(_make_plan())
    agent.evolution._host = MagicMock()
    agent.evolution._host.get_commit_sha_of_pr.return_value = "sha"

    snap = MagicMock()
    snap.sdk_usage = None
    snap.recent_error_count = 0

    with patch(
        "maestro.loop.implementation.diagnostics.build_diagnostic_snapshot",
        return_value=snap,
    ):
        agent._snapshot_version("ep_1")

    [v] = agent.memory.get_recent_versions()
    assert v.window_sdk_cost_usd == 0.0
    assert v.window_sdk_calls == 0
    assert v.window_cache_hit_rate == 0.0
    assert v.window_rate_limit_hits == 0


def test_snapshot_version_swallows_exceptions(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.memory.save_evolution_plan(_make_plan())
    agent.evolution._host = MagicMock()

    with patch(
        "maestro.loop.implementation.diagnostics.build_diagnostic_snapshot",
        side_effect=RuntimeError("boom"),
    ):
        # Must not raise.
        agent._snapshot_version("ep_1")

    assert agent.memory.get_recent_versions() == []


def test_snapshot_version_records_milestone_event(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.memory.save_evolution_plan(_make_plan())
    agent.evolution._host = MagicMock()
    agent.evolution._host.get_commit_sha_of_pr.return_value = "sha"

    with patch(
        "maestro.loop.implementation.diagnostics.build_diagnostic_snapshot",
        return_value=_stub_snapshot(),
    ):
        agent._snapshot_version("ep_1")

    entries = agent.journal.read_recent_events(n=5)
    milestones = [e for e in entries if e.entry_type == "milestone"]
    assert any(m.triggered_by == "version_snapshot" for m in milestones)


def test_snapshot_version_counts_tasks_in_window(make_agent) -> None:  # type: ignore[no-untyped-def]
    from maestro.memory import TaskRecord

    agent = make_agent()
    agent.config.version_archive_window_iterations = 5
    agent.memory.save_evolution_plan(_make_plan())
    agent.evolution._host = MagicMock()
    agent.evolution._host.get_commit_sha_of_pr.return_value = "sha"

    for i in range(3):
        agent.memory.record_task(TaskRecord(
            task_id=f"t{i}", goal="g", plan=[],
            outcome="success", result_summary="", errors=[],
            duration_secs=1.0, timestamp="2026-01-01T00:00:00Z",
        ))
    for i in range(2):
        agent.memory.record_task(TaskRecord(
            task_id=f"f{i}", goal="g", plan=[],
            outcome="failure", result_summary="", errors=[],
            duration_secs=1.0, timestamp="2026-01-01T00:00:00Z",
        ))

    with patch(
        "maestro.loop.implementation.diagnostics.build_diagnostic_snapshot",
        return_value=_stub_snapshot(),
    ):
        agent._snapshot_version("ep_1")

    [v] = agent.memory.get_recent_versions()
    assert v.window_tasks_total == 5
    assert v.window_tasks_succeeded == 3
    assert v.window_tasks_failed == 2


def test_snapshot_version_uses_injected_snapshot(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When ``snapshot`` is passed in, the per-call diagnostic build is skipped."""
    agent = make_agent()
    agent.memory.save_evolution_plan(_make_plan())
    agent.evolution._host = MagicMock()
    agent.evolution._host.get_commit_sha_of_pr.return_value = "sha"
    agent.evolution._host.get_pr_check_conclusion.return_value = "success"

    must_not_call = MagicMock(side_effect=AssertionError(
        "build_diagnostic_snapshot must not be called when snapshot is injected",
    ))

    with patch("maestro.loop.implementation.diagnostics.build_diagnostic_snapshot", must_not_call):
        agent._snapshot_version(
            "ep_1",
            snapshot=_stub_snapshot(),
            recent_tasks=[],
        )

    [v] = agent.memory.get_recent_versions()
    assert v.plan_id == "ep_1"
    assert v.window_sdk_cost_usd == 1.25
    assert v.window_sdk_calls == 7
    must_not_call.assert_not_called()


def test_snapshot_version_uses_injected_recent_tasks(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When ``recent_tasks`` is passed in, ``Memory.get_recent_tasks`` is bypassed."""
    from maestro.memory import TaskRecord

    agent = make_agent()
    agent.memory.save_evolution_plan(_make_plan())
    agent.evolution._host = MagicMock()
    agent.evolution._host.get_commit_sha_of_pr.return_value = "sha"
    agent.evolution._host.get_pr_check_conclusion.return_value = "success"

    injected = [
        TaskRecord(
            task_id=f"t{i}", goal="g", plan=[],
            outcome="success", result_summary="", errors=[],
            duration_secs=1.0, timestamp="2026-01-01T00:00:00Z",
        )
        for i in range(3)
    ]
    spy = MagicMock(wraps=agent.memory.get_recent_tasks)
    agent.memory.get_recent_tasks = spy  # type: ignore[method-assign]

    with patch(
        "maestro.loop.implementation.diagnostics.build_diagnostic_snapshot",
        return_value=_stub_snapshot(),
    ):
        agent._snapshot_version("ep_1", recent_tasks=injected)

    [v] = agent.memory.get_recent_versions()
    assert v.window_tasks_total == 3
    assert v.window_tasks_succeeded == 3
    assert v.window_tasks_failed == 0
    spy.assert_not_called()
