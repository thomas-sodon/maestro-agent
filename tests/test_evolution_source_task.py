"""Tests for ``EvolutionEngine._validate_source_task_id`` — which recorded
task branches may serve as an evolution plan's implementation base."""

from __future__ import annotations

import pytest

from maestro.memory import TaskRecord
from tests.test_evolution import _make


def test_validate_source_task_id_accepts_kept_branch(
    mem_fs: dict[str, str],
) -> None:
    """A task_id that resolves to a success/partial record with a non-empty
    ``branch`` is accepted verbatim."""
    e, m, _c, _j = _make(mem_fs)
    m.record_task(TaskRecord(
        task_id="ok1", goal="g", plan=[], outcome="success",
        result_summary="r", errors=[], duration_secs=1.0,
        timestamp="2026-01-01", branch="maestro/task/ok1",
    ))
    assert e._validate_source_task_id("ok1") == "ok1"


def test_validate_source_task_id_strips_whitespace(
    mem_fs: dict[str, str],
) -> None:
    e, m, _c, _j = _make(mem_fs)
    m.record_task(TaskRecord(
        task_id="ok2", goal="g", plan=[], outcome="partial",
        result_summary="r", errors=[], duration_secs=1.0,
        timestamp="2026-01-01", branch="maestro/task/ok2",
    ))
    assert e._validate_source_task_id("  ok2  ") == "ok2"


def test_validate_source_task_id_none_returns_none(
    mem_fs: dict[str, str],
) -> None:
    e, _m, _c, _j = _make(mem_fs)
    assert e._validate_source_task_id(None) is None


@pytest.mark.parametrize("raw", [42, "", "   ", ["t1"]])
def test_validate_source_task_id_invalid_shape_returns_none(
    mem_fs: dict[str, str], raw: object,
) -> None:
    """Non-string, blank, or non-scalar values are dropped without lookup."""
    e, _m, _c, _j = _make(mem_fs)
    assert e._validate_source_task_id(raw) is None



def test_validate_source_task_id_rejects_failed_outcome(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    e, m, _c, _j = _make(mem_fs)
    m.record_task(TaskRecord(
        task_id="bad1", goal="g", plan=[], outcome="failure",
        result_summary="r", errors=["boom"], duration_secs=1.0,
        timestamp="2026-01-01", branch="maestro/task/bad1",
    ))
    with caplog.at_level("WARNING", logger="maestro"):
        assert e._validate_source_task_id("bad1") is None
    assert any(
        "outcome=" in rec.message and "bad1" in rec.message
        for rec in caplog.records
    )


def test_validate_source_task_id_rejects_record_without_branch(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    e, m, _c, _j = _make(mem_fs)
    m.record_task(TaskRecord(
        task_id="bad2", goal="g", plan=[], outcome="success",
        result_summary="r", errors=[], duration_secs=1.0,
        timestamp="2026-01-01", branch=None,
    ))
    with caplog.at_level("WARNING", logger="maestro"):
        assert e._validate_source_task_id("bad2") is None
    assert any(
        "no branch on record" in rec.message for rec in caplog.records
    )


def test_validate_source_task_id_rejects_unknown_id(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    e, _m, _c, _j = _make(mem_fs)
    with caplog.at_level("WARNING", logger="maestro"):
        assert e._validate_source_task_id("ghost") is None
    assert any(
        "no matching recent task" in rec.message for rec in caplog.records
    )


def test_validate_source_task_id_duplicate_id_picks_latest_retry(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Duplicate ``task_id`` rows resolve to the latest record, matching
    ``Memory.get_task_record``. An older failure followed by a newer
    success-with-branch must accept the id (not log ``outcome=`` on the
    earlier failure)."""
    e, m, _c, _j = _make(mem_fs)
    m.record_task(TaskRecord(
        task_id="dup", goal="g", plan=[], outcome="failure",
        result_summary="r", errors=["boom"], duration_secs=1.0,
        timestamp="2026-01-01", branch=None,
    ))
    m.record_task(TaskRecord(
        task_id="dup", goal="g-retry", plan=[], outcome="success",
        result_summary="r2", errors=[], duration_secs=2.0,
        timestamp="2026-01-02", branch="maestro/task/dup",
    ))
    with caplog.at_level("WARNING", logger="maestro"):
        assert e._validate_source_task_id("dup") == "dup"
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_validate_source_task_id_skips_non_matching_records(
    mem_fs: dict[str, str],
) -> None:
    """The validator must walk past records with non-matching ids before
    finding the target. The validator iterates newest-first, so the
    non-matching record is recorded last."""
    e, m, _c, _j = _make(mem_fs)
    m.record_task(TaskRecord(
        task_id="target", goal="g", plan=[], outcome="success",
        result_summary="r", errors=[], duration_secs=1.0,
        timestamp="2026-01-01", branch="maestro/task/target",
    ))
    m.record_task(TaskRecord(
        task_id="other", goal="g", plan=[], outcome="success",
        result_summary="r", errors=[], duration_secs=1.0,
        timestamp="2026-01-02", branch="maestro/task/other",
    ))
    assert e._validate_source_task_id("target") == "target"
