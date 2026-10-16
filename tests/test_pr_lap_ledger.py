"""Tests for the per-PR remediation-lap ledger.

This file is the verification of two code changes:

* :func:`maestro.stats.aggregate_pr_lap_ledger` — groups remediation
  task-history rows by extracted PR number, summing lap counts and durations,
  collecting distinct blocker labels, and keeping the latest outcome per PR.
* The ``pr-lap-ledger`` read subcommand (:func:`maestro.cli.cmd_pr_lap_ledger`)
  — prints one line per PR (and a ``--json`` object list), performing no
  state-file writes and exposing no PR-disposal/close path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

from maestro.cli import cmd_pr_lap_ledger
from maestro.config import Config
from maestro.memory import Memory, TaskRecord
from maestro.stats import PRLapLedgerRow, aggregate_pr_lap_ledger
from tests.conftest import capture_stdout

FAKE_DIR = Path("/fake/pr-lap-ledger")


def _make_config(**overrides: object) -> Config:
    defaults: dict[str, object] = {
        "memory_dir": FAKE_DIR,
        "workspace": FAKE_DIR,
        "enable_session_reuse": False,
        "publication_enabled": False,
    }
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


def _row(
    goal: str,
    *,
    duration_seconds: float | None = None,
    duration_secs: float | None = None,
    blocker: str | None = None,
    result_summary: str = "",
    outcome: str = "partial",
    lap_count: int | None = None,
) -> dict[str, object]:
    """Build a minimal task-history dict row for the aggregator."""
    row: dict[str, object] = {
        "goal": goal,
        "result_summary": result_summary,
        "outcome": outcome,
    }
    if duration_seconds is not None:
        row["duration_seconds"] = duration_seconds
    if duration_secs is not None:
        row["duration_secs"] = duration_secs
    if blocker is not None:
        row["blocker"] = blocker
    if lap_count is not None:
        row["lap_count"] = lap_count
    return row


def _remediation_task(
    task_id: str,
    pr_number: int,
    *,
    duration_secs: float = 60.0,
    result_summary: str = "",
    outcome: str = "partial",
) -> TaskRecord:
    """Build a real ``Remediate PR #<n>:`` TaskRecord for CLI seeding."""
    return TaskRecord(
        task_id=task_id,
        goal=f"Remediate PR #{pr_number}: fix the thing",
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary=result_summary,
        errors=[],
        duration_secs=duration_secs,
        timestamp="2026-07-24T10:00:00",
        source="external",
    )


# ── aggregate_pr_lap_ledger ──────────────────────────────────────────


def test_aggregate_groups_by_pr_and_sums_duration() -> None:
    """Criterion 1: rows group by PR; duration is the summed row durations."""
    rows = [
        _row("Remediate PR #101: a", duration_seconds=700.0),
        _row("Remediate PR #101: a", duration_seconds=800.0),
        _row("Remediate PR #202: b", duration_seconds=120.0),
    ]
    ledger = aggregate_pr_lap_ledger(rows)

    assert [r.pr_number for r in ledger] == [101, 202]
    first = ledger[0]
    assert first.pr_number == 101
    assert first.lap_count == 2
    assert first.total_duration_seconds == 1500.0
    second = ledger[1]
    assert second.pr_number == 202
    assert second.lap_count == 1
    assert second.total_duration_seconds == 120.0


def test_aggregate_sums_lap_count_field() -> None:
    """An explicit per-row lap_count is summed, not overwritten."""
    rows = [
        _row("Remediate PR #5: a", lap_count=3, duration_seconds=10.0),
        _row("Remediate PR #5: a", lap_count=2, duration_seconds=5.0),
    ]
    ledger = aggregate_pr_lap_ledger(rows)

    assert len(ledger) == 1
    assert ledger[0].lap_count == 5
    assert ledger[0].total_duration_seconds == 15.0


def test_aggregate_falls_back_to_duration_secs() -> None:
    """When only ``duration_secs`` is present it feeds the summed duration."""
    rows = [
        _row("Remediate PR #9: a", duration_secs=30.0),
        _row("Remediate PR #9: a", duration_secs=45.0),
    ]
    ledger = aggregate_pr_lap_ledger(rows)

    assert ledger[0].total_duration_seconds == 75.0


def test_aggregate_excludes_prless_rows() -> None:
    """Criterion 2: a PR-less row is dropped, never attributed elsewhere."""
    rows = [
        _row("Remediate PR #101: a", duration_seconds=100.0),
        _row("Investigate flaky test in dashboard", duration_seconds=999.0),
        _row("Ship a new feature #1234 in passing", duration_seconds=50.0),
    ]
    ledger = aggregate_pr_lap_ledger(rows)

    # Only the one true remediation lineage survives.
    assert [r.pr_number for r in ledger] == [101]
    # The PR-less rows' durations were not folded into #101.
    assert ledger[0].total_duration_seconds == 100.0
    assert ledger[0].lap_count == 1


def test_aggregate_distinct_blocker_labels() -> None:
    """Distinct blocker labels are collected as a sorted set per PR."""
    rows = [
        _row("Remediate PR #7: a", blocker="CI_FAILING"),
        _row("Remediate PR #7: a", blocker="THREADS_UNRESOLVED"),
        _row("Remediate PR #7: a", blocker="CI_FAILING"),
    ]
    ledger = aggregate_pr_lap_ledger(rows)

    assert ledger[0].blocker_labels == ("CI_FAILING", "THREADS_UNRESOLVED")


def test_aggregate_blocker_from_bracket_tag() -> None:
    """A bracketed result_summary tag is the fallback blocker label."""
    rows = [
        _row(
            "Remediate PR #7: a",
            result_summary="[stood-down] awaiting approval",
        ),
        _row(
            "Remediate PR #7: a",
            result_summary="[ci-pending-hold] gates still running",
        ),
    ]
    ledger = aggregate_pr_lap_ledger(rows)

    assert ledger[0].blocker_labels == ("ci-pending-hold", "stood-down")


def test_aggregate_explicit_blocker_beats_bracket_tag() -> None:
    """An explicit blocker field wins over a result_summary bracket tag."""
    rows = [
        _row(
            "Remediate PR #7: a",
            blocker="CONFLICTING",
            result_summary="[stood-down] ignored here",
        ),
    ]
    ledger = aggregate_pr_lap_ledger(rows)

    assert ledger[0].blocker_labels == ("CONFLICTING",)


def test_aggregate_latest_outcome_per_pr() -> None:
    """The latest (last-appended) row's outcome wins per PR."""
    rows = [
        _row("Remediate PR #8: a", outcome="partial"),
        _row("Remediate PR #8: a", outcome="success"),
    ]
    ledger = aggregate_pr_lap_ledger(rows)

    assert ledger[0].latest_outcome == "success"


def test_aggregate_extracts_from_title_fallback() -> None:
    """A row lacking ``goal`` still groups via its ``title`` field."""
    rows = [
        {"title": "Remediate PR #42: b", "duration_seconds": 12.0},
    ]
    ledger = aggregate_pr_lap_ledger(rows)

    assert [r.pr_number for r in ledger] == [42]


def test_aggregate_empty_input() -> None:
    """No rows yields an empty ledger."""
    assert aggregate_pr_lap_ledger([]) == []


def test_aggregate_invalid_lap_count_falls_back_to_one() -> None:
    """A bool or non-positive/non-int lap_count contributes exactly one lap."""
    rows = [
        _row("Remediate PR #3: a", lap_count=True),  # type: ignore[arg-type]
        _row("Remediate PR #3: a", lap_count=0),
        _row("Remediate PR #3: a", lap_count=-4),
    ]
    ledger = aggregate_pr_lap_ledger(rows)

    assert ledger[0].lap_count == 3


def test_aggregate_skips_non_mapping_rows() -> None:
    """A non-mapping entry is skipped rather than crashing the aggregate."""
    rows = [
        "not a dict",  # type: ignore[list-item]
        _row("Remediate PR #6: a", duration_seconds=5.0),
    ]
    ledger = aggregate_pr_lap_ledger(rows)

    assert [r.pr_number for r in ledger] == [6]


# ── cmd_pr_lap_ledger (human table) ──────────────────────────────────


def test_cli_human_table_one_line_per_pr(mem_fs: dict[str, str]) -> None:
    """Criterion 3: exactly one data line per distinct PR with the fields."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_remediation_task(
        "t1", 101, duration_secs=700.0,
        result_summary="[ci-pending-hold] running",
    ))
    memory.record_task(_remediation_task(
        "t2", 101, duration_secs=800.0,
        result_summary="[stood-down] waiting", outcome="partial",
    ))
    memory.record_task(_remediation_task(
        "t3", 202, duration_secs=120.0,
        result_summary="[merged] landed", outcome="success",
    ))

    args = argparse.Namespace(last=10, json=False)
    output = capture_stdout(cmd_pr_lap_ledger, args, config)

    pr_lines = [ln for ln in output.splitlines() if ln.startswith("#")]
    assert len(pr_lines) == 2
    assert any(
        ln.startswith("#101:") and "laps=2" in ln and "1500s" in ln
        for ln in pr_lines
    )
    assert any(
        ln.startswith("#202:") and "laps=1" in ln and "outcome=success" in ln
        for ln in pr_lines
    )
    # Distinct blockers for #101 surface both bracket tags.
    line_101 = next(ln for ln in pr_lines if ln.startswith("#101:"))
    assert "ci-pending-hold" in line_101
    assert "stood-down" in line_101


def test_cli_human_table_dash_fallbacks(mem_fs: dict[str, str]) -> None:
    """A lap with no blocker label renders a ``-`` placeholder, not a crash."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_remediation_task(
        "t1", 303, duration_secs=42.0, result_summary="clean run",
    ))

    args = argparse.Namespace(last=10, json=False)
    output = capture_stdout(cmd_pr_lap_ledger, args, config)

    line = next(ln for ln in output.splitlines() if ln.startswith("#303:"))
    assert "blockers=[-]" in line


def test_cli_empty_history(mem_fs: dict[str, str]) -> None:
    """With no remediation laps the command prints the empty message."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=10, json=False)
    output = capture_stdout(cmd_pr_lap_ledger, args, config)

    assert "No remediation laps recorded." in output


# ── cmd_pr_lap_ledger (--json) ───────────────────────────────────────


def test_cli_json_object_list(mem_fs: dict[str, str]) -> None:
    """Criterion 3: --json emits an equivalent list of objects."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_remediation_task(
        "t1", 101, duration_secs=700.0,
        result_summary="[ci-pending-hold] running",
    ))
    memory.record_task(_remediation_task(
        "t2", 101, duration_secs=800.0,
        result_summary="[stood-down] waiting", outcome="partial",
    ))

    args = argparse.Namespace(last=10, json=True)
    output = capture_stdout(cmd_pr_lap_ledger, args, config)
    payload = json.loads(output)

    assert isinstance(payload, list)
    assert len(payload) == 1
    obj = payload[0]
    assert obj["pr_number"] == 101
    assert obj["lap_count"] == 2
    assert obj["total_duration_seconds"] == 1500.0
    assert obj["latest_outcome"] == "partial"
    assert sorted(obj["blocker_labels"]) == ["ci-pending-hold", "stood-down"]


# ── read-only guarantees ─────────────────────────────────────────────


def test_cli_performs_no_state_writes_and_no_close(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 4: no atomic_write and no PR-disposal/close path fires.

    Patches ``atomic_write`` in every module the command's read path could
    touch and every code-host close/disposal actuator, then confirms none is
    invoked — the ledger is structurally a read, incapable of 'giving up'.
    """
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_remediation_task("t1", 101, duration_secs=700.0))

    args = argparse.Namespace(last=10, json=False)
    with (
        patch("maestro.fileutil.atomic_write") as fu_write,
        patch("maestro.memory.atomic_write") as mem_write,
        patch("maestro.stats.file_lock") as stats_lock,
        patch(
            "maestro.vcs.SubprocessCodeHost.is_pr_closed",
        ) as is_closed,
        patch("subprocess.run") as subproc_run,
    ):
        capture_stdout(cmd_pr_lap_ledger, args, config)

    fu_write.assert_not_called()
    mem_write.assert_not_called()
    stats_lock.assert_not_called()
    # No PR-disposal/close actuator and no `gh pr close` shell-out.
    is_closed.assert_not_called()
    subproc_run.assert_not_called()


def test_ledger_row_is_frozen() -> None:
    """PRLapLedgerRow is immutable — a read instrument cannot be mutated."""
    row = PRLapLedgerRow(
        pr_number=1,
        lap_count=1,
        total_duration_seconds=1.0,
        blocker_labels=(),
        latest_outcome="partial",
    )
    try:
        row.lap_count = 5  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("PRLapLedgerRow should be frozen")
