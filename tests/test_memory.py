"""Tests for Memory persistence — TaskRecord, add_task, get_recent_tasks, task_count,
get_success_rate, validate_task_history.  Fully mocked I/O via mem_fs."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from maestro.memory import (
    EvolutionPlan,
    Memory,
    PlanStep,
    PublicationProposal,
    TaskRecord,
    compute_plan_content_hash,
)

FAKE_DIR = Path("/fake/mem")


def _make_memory(mem_fs: dict[str, str]) -> Memory:
    return Memory(FAKE_DIR)


# ---------------------------------------------------------------------------
# TaskRecord basics
# ---------------------------------------------------------------------------


def test_record_task_after_partial_tail_keeps_new_row(mem_fs: dict[str, str]) -> None:
    """A crash fragment at EOF must not swallow the next task row (#4164)."""
    mem = _make_memory(mem_fs)
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[tasks_path] = (
        json.dumps({"task_id": "t0", "goal": "g0", "outcome": "success"}) + "\n"
        + '{"task_id": "crashed", "goal": "ha'
    )
    mem.record_task(TaskRecord(
        task_id="t1", goal="g1", plan=[], outcome="success",
        result_summary="ok", errors=[], duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
    ))
    assert mem.get_task_record("t1") is not None
    assert "t1" in [t.task_id for t in mem.get_recent_tasks(10)]


def test_record_and_retrieve_task(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    record = TaskRecord(
        task_id="t1",
        goal="Test goal",
        plan=[PlanStep(step_id="s1", description="Do thing", status="done", result="ok")],
        outcome="success",
        result_summary="All good",
        errors=[],
        duration_secs=1.5,
        timestamp="2025-01-01T00:00:00Z",
    )
    mem.record_task(record)

    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    assert tasks_path in mem_fs
    assert "Test goal" in mem_fs[tasks_path]

    tasks = mem.get_recent_tasks()
    assert len(tasks) == 1
    assert tasks[0].goal == "Test goal"
    assert tasks[0].plan[0].description == "Do thing"


def test_record_task_raises_on_empty_task_id(mem_fs: dict[str, str]) -> None:
    """record_task raises ValueError when task_id is empty."""
    mem = _make_memory(mem_fs)
    with pytest.raises(ValueError, match="task_id"):
        mem.record_task(TaskRecord(
            task_id="",
            goal="test",
            plan=[],
            outcome="success",
            result_summary="",
            errors=[],
            duration_secs=1.0,
            timestamp="2025-01-01T00:00:00Z",
        ))


def test_record_task_uses_file_lock(mem_fs: dict[str, str]) -> None:
    """The append serializes on the same lock ``archive`` holds.

    ``Memory.archive`` rotates ``task_history.jsonl`` under ``file_lock``
    across ``read_text`` -> ``atomic_write``; an advisory ``flock`` only
    excludes writers who also take it, so an unlocked append landing inside
    that window is destroyed by the rename.  Mirrors
    ``test_write_artifact_review_uses_file_lock``.
    """
    mem = _make_memory(mem_fs)

    called_with: list[Any] = []

    @contextmanager
    def tracking_lock(path: Any) -> Iterator[None]:
        called_with.append(path)
        yield

    with patch("maestro.fileutil.file_lock", tracking_lock):
        mem.record_task(TaskRecord(
            task_id="t_lock",
            goal="lock me",
            plan=[],
            outcome="success",
            result_summary="",
            errors=[],
            duration_secs=1.0,
            timestamp="2025-01-01T00:00:00Z",
        ))

    assert len(called_with) == 1
    assert str(called_with[0]).endswith("task_history.jsonl")


def test_record_task_validates_before_locking(mem_fs: dict[str, str]) -> None:
    """The empty-``task_id`` guard rejects without acquiring the lock.

    Validation is pure and cannot need exclusion; taking the lock first would
    make a rejected call contend with a concurrent ``archive`` rotation for
    nothing.
    """
    mem = _make_memory(mem_fs)

    called_with: list[Any] = []

    @contextmanager
    def tracking_lock(path: Any) -> Iterator[None]:
        called_with.append(path)
        yield

    with (
        patch("maestro.memory.file_lock", tracking_lock),
        pytest.raises(ValueError, match="task_id"),
    ):
        mem.record_task(TaskRecord(
            task_id="",
            goal="test",
            plan=[],
            outcome="success",
            result_summary="",
            errors=[],
            duration_secs=1.0,
            timestamp="2025-01-01T00:00:00Z",
        ))

    assert called_with == []


def test_record_task_round_trips_criteria_verdicts(mem_fs: dict[str, str]) -> None:
    """A TaskRecord carrying criteria_verdicts survives write + read with the
    structured list preserved verbatim."""
    mem = _make_memory(mem_fs)
    verdicts = [
        {"criterion": "Feature A", "verdict": "MET", "evidence": "done in a.py"},
        {"criterion": "Feature B", "verdict": "UNMET", "evidence": "no tests"},
    ]
    mem.record_task(TaskRecord(
        task_id="t1",
        goal="Test goal",
        plan=[],
        outcome="partial",
        result_summary="ok",
        errors=[],
        duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
        criteria_verdicts=verdicts,
    ))

    tasks = mem.get_recent_tasks()
    assert len(tasks) == 1
    assert tasks[0].criteria_verdicts == verdicts


def test_legacy_task_row_deserializes_criteria_verdicts_as_none(
    mem_fs: dict[str, str],
) -> None:
    """A legacy task_history.jsonl row written before the field existed (no
    criteria_verdicts key) deserializes to criteria_verdicts is None."""
    mem = _make_memory(mem_fs)
    legacy = {
        "task_id": "t1", "goal": "legacy goal", "plan": [],
        "outcome": "success", "result_summary": "ok",
        "errors": [], "duration_secs": 1.0,
        "timestamp": "2025-01-01T00:00:00Z",
    }
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = json.dumps(legacy) + "\n"

    tasks = mem.get_recent_tasks()
    assert len(tasks) == 1
    assert tasks[0].criteria_verdicts is None


def test_record_task_round_trips_push_delivered_true(mem_fs: dict[str, str]) -> None:
    """A TaskRecord with push_delivered=True survives write + read (proposal
    091 step 1)."""
    mem = _make_memory(mem_fs)
    mem.record_task(TaskRecord(
        task_id="t1",
        goal="Remediate PR #91: fix",
        plan=[],
        outcome="partial",
        result_summary="ok",
        errors=[],
        duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
        push_delivered=True,
    ))

    got = mem.get_task_record("t1")
    assert got is not None
    assert got.push_delivered is True
    assert got.push_error is None


def test_record_task_round_trips_push_delivered_false_with_error(
    mem_fs: dict[str, str],
) -> None:
    """A failed-push lap round-trips push_delivered=False and a UTF-8
    push_error message (proposal 091 step 1)."""
    mem = _make_memory(mem_fs)
    mem.record_task(TaskRecord(
        task_id="t1",
        goal="Remediate PR #91: fix",
        plan=[],
        outcome="partial",
        result_summary="ok",
        errors=[],
        duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
        push_delivered=False,
        push_error="remote rejected: non-fast-forward — retry failed ✗",
    ))

    got = mem.get_task_record("t1")
    assert got is not None
    assert got.push_delivered is False
    assert got.push_error == "remote rejected: non-fast-forward — retry failed ✗"


def test_legacy_task_row_deserializes_push_fields_as_none(
    mem_fs: dict[str, str],
) -> None:
    """A legacy row written before the push fields existed deserializes both
    push_delivered and push_error to None (proposal 091 step 1)."""
    mem = _make_memory(mem_fs)
    legacy = {
        "task_id": "t1", "goal": "legacy goal", "plan": [],
        "outcome": "success", "result_summary": "ok",
        "errors": [], "duration_secs": 1.0,
        "timestamp": "2025-01-01T00:00:00Z",
    }
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = json.dumps(legacy) + "\n"

    got = mem.get_task_record("t1")
    assert got is not None
    assert got.push_delivered is None
    assert got.push_error is None


def test_record_task_round_trips_base_head_sha(mem_fs: dict[str, str]) -> None:
    """A TaskRecord with base_head_sha survives write + read (proposal 099
    step 1)."""
    mem = _make_memory(mem_fs)
    mem.record_task(TaskRecord(
        task_id="t1",
        goal="Remediate PR #99: fix",
        plan=[],
        outcome="partial",
        result_summary="ok",
        errors=[],
        duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
        base_head_sha="basemainsha0099",
    ))

    got = mem.get_task_record("t1")
    assert got is not None
    assert got.base_head_sha == "basemainsha0099"


def test_legacy_task_row_deserializes_base_head_sha_as_none(
    mem_fs: dict[str, str],
) -> None:
    """A legacy row written before base_head_sha existed deserializes the field
    to None (proposal 099 step 1)."""
    mem = _make_memory(mem_fs)
    legacy = {
        "task_id": "t1", "goal": "legacy goal", "plan": [],
        "outcome": "success", "result_summary": "ok",
        "errors": [], "duration_secs": 1.0,
        "timestamp": "2025-01-01T00:00:00Z",
    }
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = json.dumps(legacy) + "\n"

    got = mem.get_task_record("t1")
    assert got is not None
    assert got.base_head_sha is None


def test_record_task_round_trips_workspace_citation(mem_fs: dict[str, str]) -> None:
    """A TaskRecord carrying conscious-access citation provenance survives
    write + read with values intact (proposal 130)."""
    mem = _make_memory(mem_fs)
    mem.record_task(TaskRecord(
        task_id="t1",
        goal="goal",
        plan=[],
        outcome="success",
        result_summary="ok",
        errors=[],
        duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
        workspace_moment_id="moment-abc",
        workspace_item_ids=["item-1", "item-2"],
    ))

    got = mem.get_task_record("t1")
    assert got is not None
    assert got.workspace_moment_id == "moment-abc"
    assert got.workspace_item_ids == ["item-1", "item-2"]


def test_legacy_task_row_deserializes_workspace_citation_as_none(
    mem_fs: dict[str, str],
) -> None:
    """A legacy row written before the citation fields existed deserializes both
    to None — the never-cited default, distinct from [] (proposal 130)."""
    mem = _make_memory(mem_fs)
    legacy = {
        "task_id": "t1", "goal": "legacy goal", "plan": [],
        "outcome": "success", "result_summary": "ok",
        "errors": [], "duration_secs": 1.0,
        "timestamp": "2025-01-01T00:00:00Z",
    }
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = json.dumps(legacy) + "\n"

    got = mem.get_task_record("t1")
    assert got is not None
    assert got.workspace_moment_id is None
    assert got.workspace_item_ids is None


def test_task_workspace_item_ids_malformed_coerces(mem_fs: dict[str, str]) -> None:
    """_parse_task_dict coerces workspace_item_ids the same way the plan path
    does: non-str members are dropped from a list, and a non-list value (even a
    str) collapses to None rather than loading unsanitized (proposal 130)."""
    mem = _make_memory(mem_fs)
    base = {
        "task_id": "t", "goal": "g", "plan": [], "outcome": "success",
        "result_summary": "ok", "errors": [], "duration_secs": 1.0,
        "timestamp": "2025-01-01T00:00:00Z",
    }
    rows = [
        {**base, "task_id": "t-list",
         "workspace_item_ids": ["ok", 3, None, "also-ok"]},
        {**base, "task_id": "t-nonlist", "workspace_item_ids": "not-a-list"},
    ]
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = "".join(
        json.dumps(r) + "\n" for r in rows
    )

    listed = mem.get_task_record("t-list")
    assert listed is not None
    assert listed.workspace_item_ids == ["ok", "also-ok"]
    nonlist = mem.get_task_record("t-nonlist")
    assert nonlist is not None
    assert nonlist.workspace_item_ids is None


def test_task_record_extra_fields(mem_fs: dict[str, str]) -> None:
    """get_recent_tasks ignores extra unknown fields in JSON (forward compat)."""
    mem = _make_memory(mem_fs)
    task_data = {
        "task_id": "t1", "goal": "test goal", "plan": [{
            "step_id": "s1", "description": "step one",
            "status": "completed", "result": "done",
            "future_step_field": "should be ignored",
        }],
        "outcome": "success", "result_summary": "ok",
        "errors": [], "duration_secs": 1.5,
        "timestamp": "2026-01-01T00:00:00", "source": "external",
        "future_task_field": "should be ignored",
    }
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = json.dumps(task_data) + "\n"

    tasks = mem.get_recent_tasks(10)
    assert len(tasks) == 1
    assert tasks[0].goal == "test goal"
    assert tasks[0].plan[0].description == "step one"


# ---------------------------------------------------------------------------
# get_recent_tasks edge cases
# ---------------------------------------------------------------------------


def test_get_recent_tasks_zero_returns_empty(mem_fs: dict[str, str]) -> None:
    """get_recent_tasks(0) must return an empty list."""
    mem = _make_memory(mem_fs)
    for i in range(3):
        mem.record_task(TaskRecord(
            task_id=f"t{i}", goal=f"Goal {i}", plan=[],
            outcome="success", result_summary="",
            errors=[], duration_secs=1.0,
            timestamp="2025-01-01T00:00:00Z",
        ))
    assert mem.get_recent_tasks(0) == []


def test_get_recent_tasks_limit(mem_fs: dict[str, str]) -> None:
    """get_recent_tasks respects n parameter."""
    mem = _make_memory(mem_fs)
    for i in range(10):
        mem.record_task(TaskRecord(
            task_id=f"t{i}", goal=f"Goal {i}", plan=[],
            outcome="success", result_summary="",
            errors=[], duration_secs=1.0,
            timestamp="2025-01-01T00:00:00Z",
        ))
    assert len(mem.get_recent_tasks(3)) == 3
    assert len(mem.get_recent_tasks(100)) == 10


def test_get_recent_tasks_large_n_returns_all(mem_fs: dict[str, str]) -> None:
    """get_recent_tasks with n > actual count returns all tasks."""
    mem = _make_memory(mem_fs)
    record = {
        "task_id": "t1", "goal": "test", "plan": [],
        "outcome": "success", "result_summary": "ok",
        "errors": [], "duration_secs": 1.0, "timestamp": "2024-01-01",
    }
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = json.dumps(record) + "\n"
    assert len(mem.get_recent_tasks(9999)) == 1


def test_get_recent_tasks_skips_blank_lines(mem_fs: dict[str, str]) -> None:
    """get_recent_tasks skips blank/whitespace-only lines in JSONL."""
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "task_id": "t1", "goal": "ok", "plan": [],
        "outcome": "success", "result_summary": "", "errors": [],
        "duration_secs": 1.0, "timestamp": "2025-01-01T00:00:00Z",
    })
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[tasks_path] = valid + "\n\n   \n" + valid.replace("t1", "t2") + "\n"
    assert len(mem.get_recent_tasks()) == 2


def test_corrupted_task_lines_skipped(mem_fs: dict[str, str]) -> None:
    """Corrupted JSONL lines are skipped without crashing."""
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "task_id": "t1", "goal": "ok", "plan": [],
        "outcome": "success", "result_summary": "", "errors": [],
        "duration_secs": 1.0, "timestamp": "2025-01-01T00:00:00Z",
    })
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[tasks_path] = valid + "\nTHIS IS NOT JSON\n" + valid.replace("t1", "t2") + "\n"
    assert len(mem.get_recent_tasks()) == 2


def test_get_recent_tasks_coerces_null_errors(mem_fs: dict[str, str]) -> None:
    """get_recent_tasks coerces null errors field to []."""
    mem = _make_memory(mem_fs)
    raw = json.dumps({
        "task_id": "t-null-err", "goal": "test null errors", "plan": [],
        "outcome": "success", "result_summary": "ok",
        "errors": None, "duration_secs": 1.0,
        "timestamp": "2025-01-01T00:00:00Z",
    })
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = raw + "\n"

    tasks = mem.get_recent_tasks()
    assert len(tasks) == 1
    assert tasks[0].errors == []


def test_get_recent_tasks_coerces_null_plan(mem_fs: dict[str, str]) -> None:
    """A task record with 'plan': null is loaded with plan=[]."""
    mem = _make_memory(mem_fs)
    record = {
        "task_id": "t1", "goal": "Test goal", "plan": None,
        "outcome": "success", "result_summary": "Done",
        "errors": [], "duration_secs": 1.5,
        "timestamp": "2025-01-01T00:00:00Z",
    }
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = json.dumps(record) + "\n"

    tasks = mem.get_recent_tasks()
    assert len(tasks) == 1
    assert tasks[0].plan == []


def test_get_recent_tasks_skips_non_dict_plan_steps(mem_fs: dict[str, str]) -> None:
    """Records with non-dict plan steps are skipped."""
    mem = _make_memory(mem_fs)
    good = {
        "task_id": "t1", "goal": "good", "plan": [
            {"step_id": "s1", "description": "step", "status": "done"},
        ],
        "outcome": "success", "result_summary": "ok",
        "errors": [], "duration_secs": 1.0, "timestamp": "2024-01-01",
    }
    bad = {
        "task_id": "t2", "goal": "bad", "plan": ["not a dict", 42],
        "outcome": "success", "result_summary": "ok",
        "errors": [], "duration_secs": 1.0, "timestamp": "2024-01-01",
    }
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = (
        json.dumps(good) + "\n" + json.dumps(bad) + "\n"
    )

    tasks = mem.get_recent_tasks(10)
    assert len(tasks) == 1
    assert tasks[0].task_id == "t1"


def test_get_recent_tasks_old_lines_at_start_never_parsed(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """The reverse walk stops once *n* records have been collected.

    Earlier lines in the file — including a non-JSON sentinel — must never
    be parsed, mirroring the cost-sample short-circuit.  This proves the
    refactor is asymptotic (read O(record-size * n) bytes, not O(file-size)).
    """
    import logging

    mem = _make_memory(mem_fs)
    rows = []
    for i in range(50):
        rows.append(json.dumps({
            "task_id": f"t{i}", "goal": f"goal {i}", "plan": [],
            "outcome": "success", "result_summary": "", "errors": [],
            "duration_secs": 1.0, "timestamp": "2025-01-01T00:00:00Z",
        }))
    # A non-JSON sentinel that would log a warning if reached.
    sentinel = "GARBAGE_SENTINEL_NEVER_PARSED_BY_get_recent_tasks"
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = (
        sentinel + "\n" + "\n".join(rows) + "\n"
    )

    with caplog.at_level(logging.WARNING):
        tasks = mem.get_recent_tasks(5)

    # Got the newest 5 records, in oldest-first order.
    assert [t.task_id for t in tasks] == [f"t{i}" for i in range(45, 50)]
    # The sentinel sits before the 5 newest records and is older — the
    # generator must have stopped before reading it, so no malformed-line
    # warning fires.
    assert not any(
        "malformed" in r.message or "corrupted" in r.message or sentinel in r.message
        for r in caplog.records
    )


def test_get_recent_tasks_caps_corrupted_skips(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """When corrupted records exceed the skip cap (n), the walk terminates early.

    Write 5 valid records followed by 20 structurally-invalid JSON dicts
    (valid JSON but missing required TaskRecord fields) and request n=5.
    The reverse walk hits the corrupted tail first; after skipping n=5
    corrupted records it breaks before reaching any of the 5 valid records,
    so an empty list is returned.

    Note: iter_jsonl_reverse filters out non-JSON and non-dict lines itself,
    so the corruption must be valid-JSON dicts that fail _parse_task_dict.
    """
    import logging

    mem = _make_memory(mem_fs)
    valid_rows = []
    for i in range(5):
        valid_rows.append(json.dumps({
            "task_id": f"t{i}", "goal": f"goal {i}", "plan": [],
            "outcome": "success", "result_summary": "", "errors": [],
            "duration_secs": 1.0, "timestamp": "2025-01-01T00:00:00Z",
        }))
    # Valid JSON dicts but missing required TaskRecord fields → _parse_task_dict returns None.
    corrupted = [json.dumps({"bad": "data"})] * 20
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[tasks_path] = "\n".join(valid_rows + corrupted) + "\n"

    with caplog.at_level(logging.WARNING):
        tasks = mem.get_recent_tasks(5)

    # The walk hits the skip cap on the 5th corrupted record and breaks before
    # reaching any valid record, so no tasks are returned.
    assert tasks == []
    # The "Stopped scanning" warning must have fired.
    assert any("Stopped scanning" in r.message for r in caplog.records)


def _valid_task_row(i: int) -> str:
    """One well-formed ``task_history.jsonl`` row for task ``t<i>``."""
    return json.dumps({
        "task_id": f"t{i}", "goal": f"goal {i}", "plan": [],
        "outcome": "success", "result_summary": "", "errors": [],
        "duration_secs": 1.0, "timestamp": "2025-01-01T00:00:00Z",
    })


def test_get_recent_tasks_caps_parser_level_corruption(
    mem_fs: dict[str, str], caplog: Any, monkeypatch: Any,
) -> None:
    """Parser-level corruption charges the same skip budget as validation.

    Rows rejected by ``_decode_jsonl_line`` (invalid JSON, a non-object
    value, invalid UTF-8) used to be dropped inside ``iter_jsonl_reverse``
    without the caller seeing them, so a corrupt tail forced a full-file
    decode.  The shared budget bounds that work: the decode count must stay
    within the budget rather than scaling with the corrupt tail.
    """
    import logging

    from maestro import fileutil

    mem = _make_memory(mem_fs)
    # One valid row, then a long tail of parser-level corruption (newer).
    rows = [_valid_task_row(0)]
    rows.extend(["not-json", "[]", "null", "12345"] * 25)
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[tasks_path] = "\n".join(rows) + "\n"

    decode_calls = 0
    real_decode = fileutil._decode_jsonl_line

    def counting_decode(raw: bytes, path: Any) -> Any:
        nonlocal decode_calls
        decode_calls += 1
        return real_decode(raw, path)

    monkeypatch.setattr(fileutil, "_decode_jsonl_line", counting_decode)

    with caplog.at_level(logging.WARNING):
        tasks = mem.get_recent_tasks(3)

    # Partial result: the valid row sits behind the corrupt tail.
    assert tasks == []
    # Bounded work: the 3 charged malformed rows plus the free blank parts
    # the file's final newline produces — never the 100-row tail.
    assert decode_calls <= 5
    assert any("Stopped scanning" in r.message for r in caplog.records)


def test_get_recent_tasks_budget_shared_across_both_layers(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """Parser-level and validation-level rejections exhaust one budget together."""
    import logging

    mem = _make_memory(mem_fs)
    # Newest-first the walk sees: 2 parser-level rows then 2 validation-level
    # rows, which together spend a budget of 3 before reaching the valid row.
    rows = [
        _valid_task_row(0),
        json.dumps({"bad": "data"}),
        json.dumps({"bad": "data"}),
        "not-json",
        "[]",
    ]
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[tasks_path] = "\n".join(rows) + "\n"

    with caplog.at_level(logging.WARNING):
        tasks = mem.get_recent_tasks(3)

    assert tasks == []
    assert any("Stopped scanning" in r.message for r in caplog.records)


def test_get_recent_tasks_recovers_within_parser_level_budget(
    mem_fs: dict[str, str],
) -> None:
    """Corruption below the budget still yields the older valid rows, oldest-first."""
    mem = _make_memory(mem_fs)
    rows = [_valid_task_row(0), _valid_task_row(1), "not-json", "null"]
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[tasks_path] = "\n".join(rows) + "\n"

    tasks = mem.get_recent_tasks(5)

    assert [t.task_id for t in tasks] == ["t0", "t1"]


def test_get_recent_tasks_skips_within_cap(mem_fs: dict[str, str]) -> None:
    """Corruption below the skip cap is handled normally — all n records returned.

    Interleave 10 valid records with 3 corrupted JSON dicts and request n=5.
    Since 3 < 5, the skip cap is never hit and all 5 valid records are found.
    """
    mem = _make_memory(mem_fs)
    rows: list[str] = []
    for i in range(10):
        rows.append(json.dumps({
            "task_id": f"t{i}", "goal": f"goal {i}", "plan": [],
            "outcome": "success", "result_summary": "", "errors": [],
            "duration_secs": 1.0, "timestamp": "2025-01-01T00:00:00Z",
        }))
        # Inject a corrupted dict after every third valid record.
        if i in (2, 5, 8):
            rows.append(json.dumps({"bad": "data"}))
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[tasks_path] = "\n".join(rows) + "\n"

    tasks = mem.get_recent_tasks(5)
    assert len(tasks) == 5
    # The 5 newest valid records (reverse walk picks t9, t8, t7, t6, t5).
    assert [t.task_id for t in tasks] == [f"t{i}" for i in range(5, 10)]


# ---------------------------------------------------------------------------
# task_count
# ---------------------------------------------------------------------------


def test_task_count_empty(mem_fs: dict[str, str]) -> None:
    """task_count returns 0 when no tasks have been recorded."""
    mem = _make_memory(mem_fs)
    assert mem.task_count() == 0


def test_task_count_matches_recorded_tasks(mem_fs: dict[str, str]) -> None:
    """task_count returns the exact number of recorded tasks without parsing."""
    mem = _make_memory(mem_fs)
    for i in range(5):
        mem.record_task(TaskRecord(
            task_id=f"t{i}", goal=f"Goal {i}", plan=[],
            outcome="success", result_summary="",
            errors=[], duration_secs=1.0,
            timestamp="2025-01-01T00:00:00Z",
        ))
    assert mem.task_count() == 5


def test_task_count_includes_corrupted_lines(mem_fs: dict[str, str]) -> None:
    """task_count counts all non-empty lines (fast path), including corrupt ones.

    Per-entry JSON validation is in validate_task_history().
    """
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "task_id": "t1", "goal": "ok", "plan": [],
        "outcome": "success", "result_summary": "", "errors": [],
        "duration_secs": 1.0, "timestamp": "2025-01-01T00:00:00Z",
    })
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[tasks_path] = valid + "\nCORRUPT LINE\n" + valid.replace("t1", "t2") + "\n"
    assert mem.task_count() == 3
    assert mem.validate_task_history() == 2


def test_task_count_returns_zero_for_empty_file(mem_fs: dict[str, str]) -> None:
    """task_count returns 0 when file contains only whitespace."""
    mem = _make_memory(mem_fs)
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = "   \n   \n"
    assert mem.task_count() == 0


def test_task_count_returns_zero_for_blank_lines_only(mem_fs: dict[str, str]) -> None:
    """task_count returns 0 for file with only blank lines."""
    mem = _make_memory(mem_fs)
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = "\n\n\n"
    assert mem.task_count() == 0


def test_task_count_returns_zero_on_oserror(mem_fs: dict[str, str]) -> None:
    """task_count returns 0 when read_text raises OSError."""
    mem = _make_memory(mem_fs)
    tasks_path_mock = MagicMock(spec=Path)
    tasks_path_mock.exists.return_value = True
    tasks_path_mock.read_text.side_effect = OSError("disk error")
    mem._tasks_path = tasks_path_mock
    assert mem.task_count() == 0


def test_task_count_handles_oserror_toctou() -> None:
    """task_count returns 0 if file disappears between exists() and read_text()."""
    import tempfile
    import unittest.mock as mock

    with tempfile.TemporaryDirectory() as tmpdir:
        mem = Memory(Path(tmpdir))
        tasks_path_mock = mock.MagicMock(spec=Path)
        tasks_path_mock.exists.return_value = True
        tasks_path_mock.read_text.side_effect = FileNotFoundError(
            "File deleted between exists() and read_text()"
        )
        mem._tasks_path = tasks_path_mock
        assert mem.task_count() == 0


def test_task_count_uses_utf8_encoding() -> None:
    """task_count() passes encoding='utf-8' to read_text()."""
    import tempfile

    original_read_text = Path.read_text
    encoding_used: list[str | None] = []

    def spy_read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        encoding_used.append(encoding)
        return original_read_text(self, encoding=encoding, errors=errors)

    with tempfile.TemporaryDirectory() as tmpdir:
        mem_dir = Path(tmpdir)
        tasks_file = mem_dir / "task_history.jsonl"
        record = {
            "task_id": "t1", "goal": "Unicode goal: \u4e2d\u6587",
            "plan": [], "outcome": "success", "result_summary": "",
            "errors": [], "duration_secs": 1.0,
            "timestamp": "2025-01-01T00:00:00Z",
        }
        tasks_file.write_text(json.dumps(record) + "\n", encoding="utf-8")

        with patch.object(Path, "read_text", spy_read_text):
            mem = Memory(mem_dir)
            count = mem.task_count()

    assert count == 1
    assert encoding_used
    assert encoding_used[0] == "utf-8"


def test_task_count_does_not_json_parse_per_line(mem_fs: dict[str, str]) -> None:
    """task_count() uses simple line count, not json.loads per line.

    Deterministic regression guard: task_count() must count non-empty lines
    without parsing each one (the O(n) high-constant regression a per-line
    json.loads would reintroduce).  Asserting json.loads is never called is a
    zero-timing, zero-real-I/O proxy for that invariant — it fails loudly the
    moment task_count() is reimplemented to parse JSON per line.
    """
    mem = _make_memory(mem_fs)
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    records = [
        json.dumps({
            "task_id": f"t{i}", "goal": f"Goal {i}", "plan": [],
            "outcome": "success", "result_summary": "", "errors": [],
            "duration_secs": 1.0, "timestamp": "2025-01-01T00:00:00Z",
        })
        for i in range(5)
    ]
    mem_fs[tasks_path] = "\n".join(records) + "\n"

    with patch("maestro.memory.json.loads") as mock_loads:
        count = mem.task_count()

    assert count == 5
    mock_loads.assert_not_called()


def test_task_count_vs_get_recent_tasks_consistency(mem_fs: dict[str, str]) -> None:
    """task_count counts raw lines >= get_recent_tasks parsed count."""
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "task_id": "t1", "goal": "test", "plan": [],
        "outcome": "success", "result_summary": "ok",
        "errors": [], "duration_secs": 1.0, "timestamp": "2024-01-01",
    })
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = f"{valid}\n{{invalid json\n"
    assert mem.task_count() >= len(mem.get_recent_tasks(9999))


# ---------------------------------------------------------------------------
# get_success_rate
# ---------------------------------------------------------------------------


def test_success_rate(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    for i, outcome in enumerate(["success", "success", "failure"]):
        mem.record_task(TaskRecord(
            task_id=f"t{i}", goal=f"Goal {i}", plan=[],
            outcome=outcome, result_summary="",
            errors=[], duration_secs=1.0,
            timestamp="2025-01-01T00:00:00Z",
        ))
    assert abs(mem.get_success_rate() - 2 / 3) < 0.01


def test_get_success_rate_respects_n(mem_fs: dict[str, str]) -> None:
    """get_success_rate accepts an n parameter to control window size."""
    mem = _make_memory(mem_fs)
    for i in range(10):
        outcome = "success" if i < 5 else "failure"
        line = json.dumps({
            "task_id": f"t{i}", "goal": "test", "plan": [],
            "outcome": outcome, "result_summary": "ok",
            "errors": [], "duration_secs": 1.0,
            "timestamp": "2025-01-01T00:00:00", "source": "external",
        })
        mem_fs[str(mem._tasks_path)] = mem_fs.get(str(mem._tasks_path), "") + line + "\n"
    # Last 3 are all failures
    assert mem.get_success_rate(n=3) == 0.0
    # Last 10 are 50% success
    assert mem.get_success_rate(n=10) == 0.5


# ---------------------------------------------------------------------------
# get_task — public lookup helper for the publication pipeline
# ---------------------------------------------------------------------------


def test_get_task_returns_matching_record(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.record_task(TaskRecord(
        task_id="t1", goal="first", plan=[],
        outcome="success", result_summary="", errors=[],
        duration_secs=1.0, timestamp="2025-01-01T00:00:00Z",
    ))
    mem.record_task(TaskRecord(
        task_id="t2", goal="second", plan=[],
        outcome="success", result_summary="", errors=[],
        duration_secs=1.0, timestamp="2025-01-02T00:00:00Z",
    ))
    found = mem.get_task("t2")
    assert found is not None
    assert found.goal == "second"


def test_get_task_returns_none_when_missing(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.record_task(TaskRecord(
        task_id="t1", goal="only", plan=[],
        outcome="success", result_summary="", errors=[],
        duration_secs=1.0, timestamp="2025-01-01T00:00:00Z",
    ))
    assert mem.get_task("nope") is None


def test_get_task_returns_most_recent_when_duplicate_ids(
    mem_fs: dict[str, str],
) -> None:
    """The JSONL log is append-only — a re-recorded task must surface
    the latest version so the publication pipeline reads current state."""
    mem = _make_memory(mem_fs)
    mem.record_task(TaskRecord(
        task_id="t1", goal="old", plan=[],
        outcome="failure", result_summary="", errors=[],
        duration_secs=1.0, timestamp="2025-01-01T00:00:00Z",
        artifacts_dir="/old/dir",
    ))
    mem.record_task(TaskRecord(
        task_id="t1", goal="new", plan=[],
        outcome="success", result_summary="", errors=[],
        duration_secs=1.0, timestamp="2025-01-02T00:00:00Z",
        artifacts_dir="/new/dir",
    ))
    found = mem.get_task("t1")
    assert found is not None
    assert found.goal == "new"
    assert found.artifacts_dir == "/new/dir"


def test_get_task_empty_id_returns_none(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    assert mem.get_task("") is None


# ---------------------------------------------------------------------------
# validate_task_history
# ---------------------------------------------------------------------------


def test_validate_task_history_oserror_returns_zero(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    tasks_path_mock = MagicMock(spec=Path)
    tasks_path_mock.exists.return_value = True
    tasks_path_mock.read_text.side_effect = OSError("permission denied")
    mem._tasks_path = tasks_path_mock
    assert mem.validate_task_history() == 0


def test_validate_task_history_empty_file_returns_zero(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = "   \n   \n"
    assert mem.validate_task_history() == 0


def test_validate_task_history_skips_blank_lines(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "task_id": "t1", "goal": "ok", "plan": [],
        "outcome": "success", "result_summary": "", "errors": [],
        "duration_secs": 1.0, "timestamp": "2025-01-01T00:00:00Z",
    })
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = valid + "\n\n   \n" + valid.replace("t1", "t2") + "\n"
    assert mem.validate_task_history() == 2


def test_validate_task_history_counts_only_valid_json(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "task_id": "t1", "goal": "ok", "plan": [],
        "outcome": "success", "result_summary": "", "errors": [],
        "duration_secs": 1.0, "timestamp": "2025-01-01T00:00:00Z",
    })
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = valid + "\nNOT JSON\n" + valid.replace("t1", "t2") + "\n"
    assert mem.validate_task_history() == 2


def test_validate_task_history_file_not_exists_returns_zero(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    assert tasks_path not in mem_fs
    assert mem.validate_task_history() == 0


# ---------------------------------------------------------------------------
# empty_memory
# ---------------------------------------------------------------------------


def test_empty_memory(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    assert mem.get_recent_tasks() == []
    assert mem.get_success_rate() == 0.0
    assert mem.get_pending_plans() == []
    assert mem.get_plan("nonexistent") is None


# ---------------------------------------------------------------------------
# key_findings round-trip
# ---------------------------------------------------------------------------


def test_key_findings_round_trip(mem_fs: dict[str, str]) -> None:
    """TaskRecord with key_findings set round-trips through JSONL."""
    mem = _make_memory(mem_fs)
    record = TaskRecord(
        task_id="t1",
        goal="Analyze codebase",
        plan=[PlanStep(step_id="s1", description="read files", status="done")],
        outcome="success",
        result_summary="Read 10 files",
        errors=[],
        duration_secs=2.0,
        timestamp="2025-01-01T00:00:00Z",
        key_findings="Found 3 security issues in auth.py and 2 performance bottlenecks.",
    )
    mem.record_task(record)

    tasks = mem.get_recent_tasks()
    assert len(tasks) == 1
    assert tasks[0].key_findings == "Found 3 security issues in auth.py and 2 performance bottlenecks."


def test_key_findings_defaults_to_none(mem_fs: dict[str, str]) -> None:
    """TaskRecord without key_findings defaults to None."""
    mem = _make_memory(mem_fs)
    record = TaskRecord(
        task_id="t1",
        goal="test",
        plan=[],
        outcome="success",
        result_summary="ok",
        errors=[],
        duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
    )
    assert record.key_findings is None
    mem.record_task(record)

    tasks = mem.get_recent_tasks()
    assert tasks[0].key_findings is None


def test_key_findings_absent_in_old_jsonl(mem_fs: dict[str, str]) -> None:
    """Old JSONL records without key_findings field still load correctly."""
    mem = _make_memory(mem_fs)
    old_record = {
        "task_id": "t1", "goal": "old task", "plan": [],
        "outcome": "success", "result_summary": "done",
        "errors": [], "duration_secs": 1.0, "timestamp": "2024-01-01",
        "source": "external", "branch": None,
    }
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[tasks_path] = json.dumps(old_record) + "\n"

    tasks = mem.get_recent_tasks()
    assert len(tasks) == 1
    assert tasks[0].key_findings is None


# ---------------------------------------------------------------------------
# _load_all_tasks edge cases
# ---------------------------------------------------------------------------


def test_load_all_tasks_missing_file(mem_fs: dict[str, str]) -> None:
    """_load_all_tasks returns [] when the tasks file does not exist."""
    mem = _make_memory(mem_fs)
    assert mem._load_all_tasks() == []


def test_load_all_tasks_blank_lines(mem_fs: dict[str, str]) -> None:
    """_load_all_tasks skips blank lines embedded in the JSONL file."""
    mem = _make_memory(mem_fs)
    record = {
        "task_id": "t1", "goal": "g", "plan": [],
        "outcome": "success", "result_summary": "ok",
        "errors": [], "duration_secs": 1.0, "timestamp": "2024-01-01",
        "source": "external", "branch": None,
    }
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[tasks_path] = "\n" + json.dumps(record) + "\n\n"

    tasks = mem._load_all_tasks()
    assert len(tasks) == 1
    assert tasks[0].task_id == "t1"


def test_load_all_tasks_preserves_record_with_malformed_plan_step(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """A single malformed plan step drops only that step, not the whole record.

    ``_load_all_tasks`` is the "preserve every record" migration path, so one
    bad step must not discard its parent task or its sibling valid steps.  The
    middle record mixes a valid step with a step missing the required
    ``description`` (``TypeError``) and a non-dict step (``AttributeError``);
    both malformed steps are dropped with a per-step warning while all three
    records survive in order.
    """
    import logging

    mem = _make_memory(mem_fs)

    def _record(task_id: str, plan: list[Any]) -> dict[str, Any]:
        return {
            "task_id": task_id, "goal": "g", "plan": plan,
            "outcome": "success", "result_summary": "ok",
            "errors": [], "duration_secs": 1.0, "timestamp": "2024-01-01",
        }

    first = _record("t1", [{"step_id": "a1", "description": "ok"}])
    middle = _record("t2", [
        {"step_id": "s1", "description": "ok"},  # valid, survives
        {"step_id": "s2"},                        # missing description -> TypeError
        "garbage",                                # not a mapping -> AttributeError
    ])
    last = _record("t3", [{"step_id": "c1", "description": "ok"}])
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = (
        json.dumps(first) + "\n" + json.dumps(middle) + "\n" + json.dumps(last) + "\n"
    )

    with caplog.at_level(logging.WARNING):
        tasks = mem._load_all_tasks()

    # All three records preserved, in order — the middle one is not dropped.
    assert [t.task_id for t in tasks] == ["t1", "t2", "t3"]
    # Only the valid step survives in the middle record's plan.
    assert [s.step_id for s in tasks[1].plan] == ["s1"]
    # A per-step warning fired for the malformed steps (not a record-level drop).
    assert any("malformed plan step" in r.message for r in caplog.records)
    assert not any(
        "corrupted task record" in r.message for r in caplog.records
    )


@pytest.mark.parametrize(
    ("bad_plan", "warns"),
    [
        (5, True),       # truthy non-iterable int -> would raise `for s in 5`
        (True, True),    # truthy non-iterable bool
        ("garbage", True),  # str is iterable char-by-char but is not a step list
        ({"a": 1}, True),   # dict iterates keys, none coerce to a step
        (0, False),      # falsy non-list -> treated as absent, no warning
        (False, False),  # falsy non-list
        (None, False),   # absent plan
    ],
)
def test_load_all_tasks_preserves_record_with_non_list_plan(
    mem_fs: dict[str, str], caplog: Any, bad_plan: Any, warns: bool,
) -> None:
    """A non-list ``plan`` container preserves the record instead of dropping it.

    The iteration in ``_coerce_plan_steps_lenient`` sits after an ``isinstance``
    guard, so a truthy non-iterable plan (``5``/``True``) no longer raises a
    ``TypeError`` that escapes to the record-level ``except`` and discards the
    whole task (#223's silent loss, for the rarer container-level malformation).
    Every non-list value yields an empty plan; truthy ones log a single
    container-level warning, falsy ones are treated as an absent plan silently.
    """
    import logging

    mem = _make_memory(mem_fs)
    record = {
        "task_id": "t1", "goal": "g", "plan": bad_plan,
        "outcome": "success", "result_summary": "ok",
        "errors": [], "duration_secs": 1.0, "timestamp": "2024-01-01",
    }
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = json.dumps(record) + "\n"

    with caplog.at_level(logging.WARNING):
        tasks = mem._load_all_tasks()

    # The record survives with an empty plan — never dropped.
    assert [t.task_id for t in tasks] == ["t1"]
    assert tasks[0].plan == []
    # No record-level drop regardless of the container shape.
    assert not any("corrupted task record" in r.message for r in caplog.records)
    # Truthy non-list containers log exactly one container-level warning.
    container_warnings = [
        r for r in caplog.records if "non-list plan container" in r.message
    ]
    assert len(container_warnings) == (1 if warns else 0)


def test_load_all_tasks_coerces_null_errors(mem_fs: dict[str, str]) -> None:
    """A task row with a JSON-null ``errors`` loads with ``errors=[]``.

    ``_load_all_tasks`` normalises a legacy ``errors: null`` on read (the
    schema-on-read path proposal 106 relies on, no rewrite).
    """
    mem = _make_memory(mem_fs)
    record = {
        "task_id": "t1", "goal": "g", "plan": [],
        "outcome": "success", "result_summary": "ok",
        "errors": None, "duration_secs": 1.0, "timestamp": "2024-01-01",
    }
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = json.dumps(record) + "\n"

    [task] = mem._load_all_tasks()
    assert task.task_id == "t1"
    assert task.errors == []


def test_load_all_tasks_skips_unparseable_line(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """An invalid-JSON task line is skipped with a warning; valid records survive."""
    import logging

    mem = _make_memory(mem_fs)
    valid = {
        "task_id": "t1", "goal": "g", "plan": [],
        "outcome": "success", "result_summary": "ok",
        "errors": [], "duration_secs": 1.0, "timestamp": "2024-01-01",
    }
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = (
        "{not valid json\n" + json.dumps(valid) + "\n"
    )

    with caplog.at_level(logging.WARNING):
        tasks = mem._load_all_tasks()

    assert [t.task_id for t in tasks] == ["t1"]
    assert any("corrupted task record" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# artifacts_dir round-trip
# ---------------------------------------------------------------------------


def test_artifacts_dir_round_trip(mem_fs: dict[str, str]) -> None:
    """TaskRecord with artifacts_dir set round-trips through JSONL."""
    mem = _make_memory(mem_fs)
    record = TaskRecord(
        task_id="t1",
        goal="Generate report",
        plan=[PlanStep(step_id="s1", description="write report", status="done")],
        outcome="success",
        result_summary="Report written",
        errors=[],
        duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
        artifacts_dir="/fake/workspace/artifacts/t1",
    )
    mem.record_task(record)

    tasks = mem.get_recent_tasks()
    assert len(tasks) == 1
    assert tasks[0].artifacts_dir == "/fake/workspace/artifacts/t1"


def test_artifacts_dir_defaults_to_none(mem_fs: dict[str, str]) -> None:
    """TaskRecord without artifacts_dir defaults to None."""
    mem = _make_memory(mem_fs)
    record = TaskRecord(
        task_id="t1",
        goal="test",
        plan=[],
        outcome="success",
        result_summary="ok",
        errors=[],
        duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
    )
    assert record.artifacts_dir is None
    mem.record_task(record)

    tasks = mem.get_recent_tasks()
    assert tasks[0].artifacts_dir is None


def test_artifacts_dir_absent_in_old_jsonl(mem_fs: dict[str, str]) -> None:
    """Old JSONL records without artifacts_dir field still load correctly."""
    mem = _make_memory(mem_fs)
    old_record = {
        "task_id": "t1", "goal": "old task", "plan": [],
        "outcome": "success", "result_summary": "done",
        "errors": [], "duration_secs": 1.0, "timestamp": "2024-01-01",
        "source": "external", "branch": None, "key_findings": None,
    }
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[tasks_path] = json.dumps(old_record) + "\n"

    tasks = mem.get_recent_tasks()
    assert len(tasks) == 1
    assert tasks[0].artifacts_dir is None


# ---------------------------------------------------------------------------
# linked_question round-trip
# ---------------------------------------------------------------------------


def test_linked_question_round_trip(mem_fs: dict[str, str]) -> None:
    """TaskRecord with linked_question set round-trips through JSONL."""
    mem = _make_memory(mem_fs)
    record = TaskRecord(
        task_id="t1",
        goal="Probe whether curiosity is a value",
        plan=[PlanStep(step_id="s1", description="introspect", status="done")],
        outcome="success",
        result_summary="reflected",
        errors=[],
        duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
        source="self_directed",
        linked_question="Is curiosity a core value?",
    )
    mem.record_task(record)

    tasks = mem.get_recent_tasks()
    assert len(tasks) == 1
    assert tasks[0].linked_question == "Is curiosity a core value?"


def test_linked_question_defaults_to_none(mem_fs: dict[str, str]) -> None:
    """TaskRecord without linked_question defaults to None."""
    mem = _make_memory(mem_fs)
    record = TaskRecord(
        task_id="t1",
        goal="test",
        plan=[],
        outcome="success",
        result_summary="ok",
        errors=[],
        duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
    )
    assert record.linked_question is None
    mem.record_task(record)

    tasks = mem.get_recent_tasks()
    assert tasks[0].linked_question is None


def test_linked_question_absent_in_old_jsonl(mem_fs: dict[str, str]) -> None:
    """Old JSONL records without linked_question field load with None."""
    mem = _make_memory(mem_fs)
    old_record = {
        "task_id": "t1", "goal": "old task", "plan": [],
        "outcome": "success", "result_summary": "done",
        "errors": [], "duration_secs": 1.0, "timestamp": "2024-01-01",
        "source": "external", "branch": None, "key_findings": None,
        "artifacts_dir": None, "replans": 0,
    }
    tasks_path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[tasks_path] = json.dumps(old_record) + "\n"

    tasks = mem.get_recent_tasks()
    assert len(tasks) == 1
    assert tasks[0].linked_question is None


# ---------------------------------------------------------------------------
# Evolution PR tending fields — last_pr_reminder_at /
# last_conflict_resolution_attempt_at
# ---------------------------------------------------------------------------


def _sample_plan(plan_id: str = "ep1") -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title="Sample",
        category="capability",
        rationale="reason",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Plan-restatement gate — is_plan_restatement + save_evolution_plan opt-in
# ---------------------------------------------------------------------------


def _plan(
    plan_id: str,
    title: str,
    rationale: str,
    status: str = "pending_review",
) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category="capability",
        rationale=rationale,
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        status=status,  # type: ignore[arg-type]
        created_at="2025-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Mechanical pending-plan cluster sweep — sweep_clustered_pending_plans
# ---------------------------------------------------------------------------

_LEVER_LEDGER = str(FAKE_DIR / "lever_attempts.jsonl")


# ---------------------------------------------------------------------------
# PublicationProposal
# ---------------------------------------------------------------------------


def _make_proposal(
    *,
    proposal_id: str = "p1",
    task_id: str = "t1",
    review_id: str = "r1",
    publishable_files: list[str] | None = None,
) -> PublicationProposal:
    return PublicationProposal(
        proposal_id=proposal_id,
        task_id=task_id,
        review_id=review_id,
        proposed_name="nice-thing",
        description="a thing",
        proposed_readme=None,
        publishable_files=publishable_files if publishable_files is not None else ["README.md"],
        created_at="2026-04-23T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# AST regression: every text-mode open() / read_text() / write_text() call
# anywhere in maestro/ must use encoding="utf-8"
# ---------------------------------------------------------------------------


def test_all_open_calls_specify_utf8_encoding() -> None:
    """Every text-mode file I/O call in the maestro package is explicit UTF-8.

    Parses every module under ``maestro/`` (subpackages included) and
    asserts ``encoding='utf-8'`` on builtin ``open()`` calls (binary
    modes exempt) and on ``Path.read_text`` / ``Path.write_text`` calls.
    This prevents a regression where new file I/O relies on
    platform-default encoding, causing UnicodeEncodeError or mojibake on
    non-UTF-8 locales (#1650/#1489).
    """
    import ast
    from pathlib import Path

    package_root = Path(__file__).resolve().parent.parent / "maestro"

    def has_utf8_keyword(node: ast.Call) -> bool:
        for kw in node.keywords:
            if kw.arg == "encoding":
                return (
                    isinstance(kw.value, ast.Constant)
                    and kw.value.value == "utf-8"
                )
        return False

    def is_binary_mode(node: ast.Call) -> bool:
        mode: ast.expr | None = None
        if len(node.args) >= 2:
            mode = node.args[1]
        for kw in node.keywords:
            if kw.arg == "mode":
                mode = kw.value
        return (
            isinstance(mode, ast.Constant)
            and isinstance(mode.value, str)
            and "b" in mode.value
        )

    missing: list[str] = []
    for source_path in sorted(package_root.rglob("*.py")):
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"),
            filename=str(source_path),
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Match bare ``open(...)`` calls (not ``os.open`` etc.)
            if isinstance(func, ast.Name) and func.id == "open":
                if is_binary_mode(node):
                    continue
                # Positional form: open(path, mode, buffering, encoding)
                if len(node.args) >= 4:
                    enc = node.args[3]
                    if isinstance(enc, ast.Constant) and enc.value == "utf-8":
                        continue
                if not has_utf8_keyword(node):
                    missing.append(
                        f"{source_path.relative_to(package_root)}:"
                        f"{node.lineno} open()"
                    )
            elif (
                isinstance(func, ast.Attribute)
                and func.attr in ("read_text", "write_text")
                and not has_utf8_keyword(node)
            ):
                missing.append(
                    f"{source_path.relative_to(package_root)}:"
                    f"{node.lineno} .{func.attr}()"
                )

    assert not missing, (
        f"text-mode file I/O missing encoding='utf-8' in maestro/: {missing}"
        " — all text I/O must use explicit UTF-8 encoding to prevent "
        "platform-default mojibake (#1650/#1489)"
    )


# ---------------------------------------------------------------------------
# get_cited_source_task_ids
# ---------------------------------------------------------------------------


def test_get_cited_source_task_ids_collects_across_statuses(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    statuses: list[str] = [
        "pending_review",
        "approved",
        "implemented",
        "merged",
        "rejected",
        "failed",
    ]
    for i, status in enumerate(statuses):
        plan = _sample_plan(plan_id=f"ep{i}")
        plan.source_task_id = f"task_{i}"
        plan.status = status  # type: ignore[assignment]
        mem.save_evolution_plan(plan)

    cited = mem.get_cited_source_task_ids()
    assert cited == {f"task_{i}" for i in range(len(statuses))}


def test_get_cited_source_task_ids_excludes_none_and_empty(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    plan_a = _sample_plan(plan_id="ep_none")
    plan_a.source_task_id = None
    mem.save_evolution_plan(plan_a)

    plan_b = _sample_plan(plan_id="ep_empty")
    plan_b.source_task_id = ""
    mem.save_evolution_plan(plan_b)

    plan_c = _sample_plan(plan_id="ep_real")
    plan_c.source_task_id = "real_task"
    mem.save_evolution_plan(plan_c)

    assert mem.get_cited_source_task_ids() == {"real_task"}


def test_get_cited_source_task_ids_returns_empty_set_when_no_plans_file(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    assert mem.get_cited_source_task_ids() == set()


# ---------------------------------------------------------------------------
# get_promotable_branches
# ---------------------------------------------------------------------------


def test_get_promotable_branches_filters_cited_tasks(
    mem_fs: dict[str, str],
) -> None:
    """Tasks whose task_id appears as source_task_id in a plan are excluded."""
    mem = _make_memory(mem_fs)
    # Three successful tasks with branches.
    for i in range(3):
        mem.record_task(TaskRecord(
            task_id=f"t{i}",
            goal=f"Goal {i}",
            plan=[],
            outcome="success",
            result_summary="ok",
            errors=[],
            duration_secs=1.0,
            timestamp="2025-01-01T00:00:00Z",
            branch=f"maestro/task/t{i}",
        ))
    # One plan cites t1 as its source_task_id.
    plan = _sample_plan(plan_id="ep_promo")
    plan.source_task_id = "t1"
    mem.save_evolution_plan(plan)

    result = mem.get_promotable_branches()
    task_ids = [r["task_id"] for r in result]
    assert "t0" in task_ids
    assert "t2" in task_ids
    assert "t1" not in task_ids
    assert len(result) == 2


def test_get_promotable_branches_empty_when_no_successful_branches(
    mem_fs: dict[str, str],
) -> None:
    """No successful tasks with branches => empty list."""
    mem = _make_memory(mem_fs)
    # A failed task with a branch.
    mem.record_task(TaskRecord(
        task_id="t_fail",
        goal="failed goal",
        plan=[],
        outcome="failure",
        result_summary="fail",
        errors=["oops"],
        duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
        branch="maestro/task/t_fail",
    ))
    # A successful task without a branch.
    mem.record_task(TaskRecord(
        task_id="t_nobranch",
        goal="no branch goal",
        plan=[],
        outcome="success",
        result_summary="ok",
        errors=[],
        duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
    ))

    assert mem.get_promotable_branches() == []


def test_get_promotable_branches_respects_max_results(
    mem_fs: dict[str, str],
) -> None:
    """max_results keeps the most recent qualifying branches, newest-first."""
    mem = _make_memory(mem_fs)
    for i in range(10):
        mem.record_task(TaskRecord(
            task_id=f"t{i}",
            goal=f"Goal {i}",
            plan=[],
            outcome="success",
            result_summary="ok",
            errors=[],
            duration_secs=1.0,
            timestamp="2025-01-01T00:00:00Z",
            branch=f"maestro/task/t{i}",
        ))

    result = mem.get_promotable_branches(max_results=3)
    # t0..t9 were recorded in order; the cap must keep the three most
    # recent branches (t9, t8, t7) in newest-first order, not the oldest.
    assert [r["task_id"] for r in result] == ["t9", "t8", "t7"]


def test_get_promotable_branches_excludes_tasks_without_branches(
    mem_fs: dict[str, str],
) -> None:
    """Tasks without a branch field are never returned."""
    mem = _make_memory(mem_fs)
    mem.record_task(TaskRecord(
        task_id="t_with",
        goal="has branch",
        plan=[],
        outcome="success",
        result_summary="ok",
        errors=[],
        duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
        branch="maestro/task/t_with",
    ))
    mem.record_task(TaskRecord(
        task_id="t_without",
        goal="no branch",
        plan=[],
        outcome="success",
        result_summary="ok",
        errors=[],
        duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
    ))

    result = mem.get_promotable_branches()
    assert len(result) == 1
    assert result[0]["task_id"] == "t_with"
    assert result[0]["branch"] == "maestro/task/t_with"
    assert result[0]["goal"] == "has branch"


def test_plan_grounded_reads_round_trip_and_empty_distinct(
    mem_fs: dict[str, str],
) -> None:
    """grounded_reads survives save + load, [] stays [] and an unset field
    stays None (proposal 143)."""
    mem = _make_memory(mem_fs)
    read = _plan("p1", "title", "rationale")
    read.grounded_reads = ["maestro/a.py", "Symbol"]
    empty = _plan("p2", "title two", "rationale two")
    empty.grounded_reads = []
    unset = _plan("p3", "title three", "rationale three")
    for plan in (read, empty, unset):
        mem.save_evolution_plan(plan)

    got_read = mem.get_plan("p1")
    assert got_read is not None
    assert got_read.grounded_reads == ["maestro/a.py", "Symbol"]
    got_empty = mem.get_plan("p2")
    assert got_empty is not None
    assert got_empty.grounded_reads == []
    got_unset = mem.get_plan("p3")
    assert got_unset is not None
    assert got_unset.grounded_reads is None


def test_plan_grounded_reads_malformed_coerces(mem_fs: dict[str, str]) -> None:
    """Non-str members are dropped; a non-list value collapses to None."""
    mem = _make_memory(mem_fs)
    base = {
        "title": "t", "stage": "awakening", "category": "capability",
        "rationale": "r", "proposed_changes": [], "files_affected": [],
        "expected_impact": "", "status": "pending_review",
        "created_at": "2025-01-01T00:00:00Z",
    }
    rows = [
        {**base, "plan_id": "p-list", "grounded_reads": ["ok", 3, None, "b"]},
        {**base, "plan_id": "p-nonlist", "grounded_reads": "maestro/a.py"},
    ]
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = "".join(
        json.dumps(r) + "\n" for r in rows
    )

    listed = mem.get_plan("p-list")
    assert listed is not None
    assert listed.grounded_reads == ["ok", "b"]
    nonlist = mem.get_plan("p-nonlist")
    assert nonlist is not None
    assert nonlist.grounded_reads is None


def test_plan_content_hash_ignores_grounded_reads() -> None:
    """grounded_reads is provenance, outside the content hash (proposal 143)."""
    bare = _plan("p1", "title", "rationale")
    read = _plan("p1", "title", "rationale")
    read.grounded_reads = ["maestro/a.py"]
    assert compute_plan_content_hash(bare) == compute_plan_content_hash(read)


def test_plan_reality_declarations_round_trip_and_coerce(
    mem_fs: dict[str, str],
) -> None:
    """problem_ref / new_capability_names round-trip, legacy rows load None, and
    malformed values coerce (proposal 136)."""
    mem = _make_memory(mem_fs)
    declared = _plan("p1", "title", "rationale")
    declared.problem_ref = "audit:rule_x"
    declared.new_capability_names = ["new_fn"]
    mem.save_evolution_plan(declared)
    got = mem.get_plan("p1")
    assert got is not None
    assert got.problem_ref == "audit:rule_x"
    assert got.new_capability_names == ["new_fn"]

    base = {
        "title": "t", "stage": "awakening", "category": "capability",
        "rationale": "r", "proposed_changes": [], "files_affected": [],
        "expected_impact": "", "status": "pending_review",
        "created_at": "2025-01-01T00:00:00Z",
    }
    rows = [
        {**base, "plan_id": "legacy"},
        {**base, "plan_id": "bad", "problem_ref": 7,
         "new_capability_names": ["ok", 3]},
        {**base, "plan_id": "nonlist", "new_capability_names": "x"},
    ]
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = "".join(
        json.dumps(r) + "\n" for r in rows
    )
    legacy = mem.get_plan("legacy")
    assert legacy is not None
    assert legacy.problem_ref is None
    assert legacy.new_capability_names is None
    bad = mem.get_plan("bad")
    assert bad is not None
    assert bad.problem_ref is None
    assert bad.new_capability_names == ["ok"]
    nonlist = mem.get_plan("nonlist")
    assert nonlist is not None
    assert nonlist.new_capability_names is None



