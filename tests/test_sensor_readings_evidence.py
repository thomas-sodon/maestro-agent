"""SENSOR_THRESHOLD calibration bets score against persisted per-cycle readings.

Before this change every SENSOR_THRESHOLD reflection bet routed to the bare
``CalibrationEvidence()`` else-arm of ``_collect_calibration_evidence``, so no
reading was ever supplied and the whole family died ``unresolved`` — never scored,
never fed to ``author_reflection_verdict``. These tests pin the closed loop:

* ``contemplate`` persists this cycle's scalar sensor values to
  ``sensor_readings.jsonl`` (criterion 4 — the live write path is exercised, not
  just seeded data);
* the ``Memory`` reader is tail-bounded (criterion 3);
* ``_collect_calibration_evidence`` loads the readings at/after a bet's window and
  populates ``CalibrationEvidence.sensor_readings`` so a crossing scores true/false
  while an unseen tag still resolves ``None`` -> ``unresolved`` (criterion 1);
* ``maestro/self_calibration.py`` stays the pure owner — no file I/O, imports only
  ``maestro.types`` — with every reading injected by the caller (criterion 2).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.consciousness import SelfModel
from maestro.diagnostics import DiagnosticSnapshot
from maestro.memory import Memory, SensorReading, TaskRecord
from maestro.self_calibration import (
    LOSS_CONDITION_GRAMMAR,
    parse_loss_condition,
    score_loss_condition,
)
from maestro.stats import BacklogSnapshot, build_sensor_readings

FAKE_DIR = Path("/fake/mem")


def _make_memory() -> Memory:
    return Memory(FAKE_DIR)


# ---------------------------------------------------------------------------
# Criterion 3 — the reader is tail-bounded
# ---------------------------------------------------------------------------


def test_sensor_readings_round_trip(mem_fs: dict[str, str]) -> None:
    """An append round-trips through the tail-bounded reader, oldest-first."""
    mem = _make_memory()
    mem.record_sensor_readings([
        SensorReading("2025-01-01T00:00:00+00:00", "OWNED-NET-DELTA", 2.0, True),
        SensorReading("2025-01-01T00:00:01+00:00", "UNLANDED-REMEDIATION", 0.0, False),
    ])

    path = str(FAKE_DIR / "sensor_readings.jsonl")
    assert path in mem_fs

    readings = mem.get_recent_sensor_readings()
    assert [(r.tag, r.value, r.crossed) for r in readings] == [
        ("OWNED-NET-DELTA", 2.0, True),
        ("UNLANDED-REMEDIATION", 0.0, False),
    ]


def test_record_sensor_readings_empty_is_noop(mem_fs: dict[str, str]) -> None:
    """An empty iterable writes no file (no empty sidecar created)."""
    mem = _make_memory()
    mem.record_sensor_readings([])
    assert str(FAKE_DIR / "sensor_readings.jsonl") not in mem_fs


def test_get_recent_sensor_readings_is_tail_bounded(
    mem_fs: dict[str, str], caplog: object,
) -> None:
    """The reverse walk stops after *n* rows — it never reaches the file head.

    Seeds a history far longer than the requested window plus a non-JSON
    sentinel at the very front; requesting the newest 5 must return exactly the
    last 5 rows and must never parse (or warn about) the sentinel, proving the
    read is O(record-size * n), not O(file-size). Mirrors the ``get_recent_tasks``
    boundedness test.
    """
    import logging

    mem = _make_memory()
    rows = [
        json.dumps({
            "timestamp": f"2025-01-01T00:00:{i:02d}+00:00",
            "tag": "OWNED-NET-DELTA",
            "value": float(i),
            "crossed": i > 0,
        })
        for i in range(50)
    ]
    sentinel = "GARBAGE_SENTINEL_NEVER_PARSED_BY_sensor_reader"
    mem_fs[str(FAKE_DIR / "sensor_readings.jsonl")] = (
        sentinel + "\n" + "\n".join(rows) + "\n"
    )

    with caplog.at_level(logging.WARNING):  # type: ignore[attr-defined]
        readings = mem.get_recent_sensor_readings(5)

    assert [r.value for r in readings] == [45.0, 46.0, 47.0, 48.0, 49.0]
    assert not any(
        "corrupted sensor reading" in r.message or sentinel in r.message
        for r in caplog.records  # type: ignore[attr-defined]
    )


def test_get_recent_sensor_readings_caps_corrupted_skips(
    mem_fs: dict[str, str],
) -> None:
    """A corrupted tail cannot force a full-file scan — skipping is capped at n."""
    mem = _make_memory()
    valid = [
        json.dumps({
            "timestamp": f"2025-01-01T00:00:{i:02d}+00:00",
            "tag": "OWNED-NET-DELTA", "value": float(i), "crossed": True,
        })
        for i in range(3)
    ]
    corrupted = [json.dumps({"bad": "data"})] * 20
    mem_fs[str(FAKE_DIR / "sensor_readings.jsonl")] = (
        "\n".join(valid + corrupted) + "\n"
    )
    # n=5: after skipping 5 corrupted tail rows the walk breaks before reaching
    # any valid row further left.
    assert mem.get_recent_sensor_readings(5) == []


def test_get_recent_sensor_readings_caps_decode_level_corruption(
    mem_fs: dict[str, str], caplog: object, monkeypatch: object,
) -> None:
    """Decode-level corruption charges the same skip budget as parser rejection.

    Rows rejected by ``_decode_jsonl_line`` (invalid JSON, a non-object value,
    invalid UTF-8) are dropped *inside* ``iter_jsonl_reverse`` and never reach
    this reader's loop, so before the shared budget a decode-level corrupt tail
    forced a full-file decode — the local skip counter could not see it. The
    shared ``SkipBudget`` bounds that work: the decode count must stay within
    the budget rather than scaling with the corrupt tail. Mirrors
    ``test_get_recent_tasks_caps_parser_level_corruption``.
    """
    import logging

    from maestro import fileutil

    mem = _make_memory()
    # One valid row (oldest), then a long tail of decode-level corruption
    # (newer) that never yields a dict from iter_jsonl_reverse.
    valid = json.dumps({
        "timestamp": "2025-01-01T00:00:00+00:00",
        "tag": "OWNED-NET-DELTA", "value": 0.0, "crossed": True,
    })
    rows = [valid]
    rows.extend(["not-json", "[]", "null", "12345"] * 25)
    mem_fs[str(FAKE_DIR / "sensor_readings.jsonl")] = "\n".join(rows) + "\n"

    decode_calls = 0
    real_decode = fileutil._decode_jsonl_line

    def counting_decode(raw: bytes, path: object) -> object:
        nonlocal decode_calls
        decode_calls += 1
        return real_decode(raw, path)

    monkeypatch.setattr(  # type: ignore[attr-defined]
        fileutil, "_decode_jsonl_line", counting_decode,
    )

    with caplog.at_level(logging.WARNING):  # type: ignore[attr-defined]
        readings = mem.get_recent_sensor_readings(3)

    # Partial result: the valid row sits behind the corrupt tail, unreached.
    assert readings == []
    # Bounded work: the 3 charged malformed rows plus the free blank parts the
    # file's final newline produces — never the 100-row tail.
    assert decode_calls <= 5


def test_get_recent_sensor_readings_tolerates_undecodable_line_within_window(
    mem_fs: dict[str, str], monkeypatch: object,
) -> None:
    """An undecodable line inside the window is tolerated; the file is not walked.

    Seeds a long history whose newest rows include a non-JSON (undecodable)
    line interleaved with valid ones, then requests the newest 3. The reader
    must skip the undecodable line without raising, still return the 3 valid
    readings that fall in the bounded window, and never decode the older rows
    at the file head (proving O(record-size * n), not O(file-size)).
    """
    from maestro import fileutil

    mem = _make_memory()

    def _row(i: int) -> str:
        return json.dumps({
            "timestamp": f"2025-01-01T00:00:{i:02d}+00:00",
            "tag": "OWNED-NET-DELTA", "value": float(i), "crossed": True,
        })

    # Oldest -> newest. An undecodable line sits between value28 and value29,
    # i.e. inside the newest-3 window the reverse walk will read.
    rows = [_row(i) for i in range(28)]
    rows.extend([_row(27), _row(28), "not-json{{{", _row(29)])
    mem_fs[str(FAKE_DIR / "sensor_readings.jsonl")] = "\n".join(rows) + "\n"

    decode_calls = 0
    real_decode = fileutil._decode_jsonl_line

    def counting_decode(raw: bytes, path: object) -> object:
        nonlocal decode_calls
        decode_calls += 1
        return real_decode(raw, path)

    monkeypatch.setattr(  # type: ignore[attr-defined]
        fileutil, "_decode_jsonl_line", counting_decode,
    )

    readings = mem.get_recent_sensor_readings(3)

    # Tolerated: the undecodable line is skipped, the 3 newest valid readings
    # in the window are returned oldest-first (contract), no exception raised.
    assert [r.value for r in readings] == [27.0, 28.0, 29.0]
    # Not a full walk: only the window's rows (3 valid + 1 undecodable + the
    # free trailing-newline blank) are decoded — never the 28-row head.
    assert decode_calls <= 6


def test_get_recent_sensor_readings_nonpositive_is_empty(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory()
    mem.record_sensor_readings([
        SensorReading("2025-01-01T00:00:00+00:00", "OWNED-NET-DELTA", 1.0, True),
    ])
    assert mem.get_recent_sensor_readings(0) == []


def test_parse_sensor_reading_rejects_malformed() -> None:
    """A row missing a field or carrying an unconvertible value is dropped."""
    assert Memory._parse_sensor_reading({"tag": "X", "value": 1.0}) is None
    assert Memory._parse_sensor_reading(
        {"timestamp": "t", "tag": "X", "value": "nan-ish", "crossed": True},
    ) is None
    good = Memory._parse_sensor_reading(
        {"timestamp": "t", "tag": "X", "value": 3, "crossed": 1},
    )
    assert good == SensorReading("t", "X", 3.0, True)


# ---------------------------------------------------------------------------
# build_sensor_readings — the pure write-half helper
# ---------------------------------------------------------------------------


def test_build_sensor_readings_includes_owned_delta_with_two_snapshots() -> None:
    snaps = [
        BacklogSnapshot(iteration=2, count=5, timestamp="t2"),
        BacklogSnapshot(iteration=1, count=3, timestamp="t1"),
    ]  # newest-first
    readings = build_sensor_readings(3, snaps, now="2025-01-01T00:00:00+00:00")
    assert readings == [
        SensorReading("2025-01-01T00:00:00+00:00", "UNLANDED-REMEDIATION", 3.0, True),
        SensorReading("2025-01-01T00:00:00+00:00", "OWNED-NET-DELTA", 2.0, True),
    ]


def test_build_sensor_readings_omits_owned_delta_without_two_snapshots() -> None:
    readings = build_sensor_readings(0, [], now="t")
    assert readings == [SensorReading("t", "UNLANDED-REMEDIATION", 0.0, False)]


def test_build_sensor_readings_zero_owned_delta_is_not_crossed() -> None:
    snaps = [
        BacklogSnapshot(iteration=2, count=4, timestamp="t2"),
        BacklogSnapshot(iteration=1, count=4, timestamp="t1"),
    ]
    readings = build_sensor_readings(1, snaps, now="t")
    assert readings[1] == SensorReading("t", "OWNED-NET-DELTA", 0.0, False)


def test_build_sensor_readings_defaults_timestamp_when_omitted() -> None:
    readings = build_sensor_readings(0, [])
    assert len(readings) == 1
    assert readings[0].timestamp != ""


def test_build_sensor_readings_omits_forced_quiet_when_arg_absent() -> None:
    # The None default keeps the pre-existing two-tag behaviour: no
    # FORCED-QUIET-RATIO row is produced when contemplate does not pass tasks.
    readings = build_sensor_readings(1, [], now="t")
    tags = [r.tag for r in readings]
    assert "FORCED-QUIET-RATIO" not in tags


def test_build_sensor_readings_forced_quiet_ratio_is_forced_over_total() -> None:
    # One forced remediation quiet (goal matches the lineage + a forced marker)
    # and one chosen stand-down => forced/total = 1/2 = 0.5, persisted as the
    # fraction the grammar's "goes above 0.5" example resolves against — not the
    # integer forced count.
    tasks: list[dict[str, object]] = [
        {"goal": "Remediate PR #12: fix", "result_summary": "resolved, awaiting tend push"},
        {"goal": "Please check on PR #34", "result_summary": "nothing owed [stood-down]"},
    ]
    readings = build_sensor_readings(0, [], forced_quiet_tasks=tasks, now="t")
    assert SensorReading("t", "FORCED-QUIET-RATIO", 0.5, True) in readings


def test_build_sensor_readings_omits_forced_quiet_when_no_quiet_exists() -> None:
    # A task with no quiet marker is not a quiet: total == 0, so no
    # FORCED-QUIET-RATIO row is written (absence is not a 0.0 reading).
    tasks: list[dict[str, object]] = [
        {"goal": "Remediate PR #12: fix", "result_summary": "merged and done"},
    ]
    readings = build_sensor_readings(0, [], forced_quiet_tasks=tasks, now="t")
    tags = [r.tag for r in readings]
    assert "FORCED-QUIET-RATIO" not in tags


def test_build_sensor_readings_forced_quiet_all_forced_is_one() -> None:
    # Two forced quiets, no chosen => ratio 1.0, crossed (non-baseline) True.
    tasks: list[dict[str, object]] = [
        {"goal": "Remediate PR #1: a", "result_summary": "awaiting tend push"},
        {"goal": "Remediate PR #2: b", "result_summary": "[merge-block] held"},
    ]
    readings = build_sensor_readings(0, [], forced_quiet_tasks=tasks, now="t")
    assert SensorReading("t", "FORCED-QUIET-RATIO", 1.0, True) in readings


# ---------------------------------------------------------------------------
# Criterion 1 — SENSOR_THRESHOLD bets score through populated evidence
# ---------------------------------------------------------------------------


def _bet(pid: str, loss: str, ts: str) -> dict[str, str]:
    return {
        "prediction_id": pid,
        "prediction": "p",
        "loss_condition": loss,
        "timestamp": ts,
        "verdict": "pending",
        "self_verdict": "",
    }


def test_collect_evidence_populates_sensor_readings_at_or_after_window(
    mem_fs: dict[str, str], make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A crossing reading at ts >= the bet's is injected and scores the bet.

    An ``OCCURS`` bet whose tag crosses scores ``wrong``; the mirror
    ``DOES_NOT_OCCUR`` bet scores ``correct``. Both are *scored*, not
    ``unresolved`` — the defect this change fixes.
    """
    agent = make_agent()
    bet_ts = "2025-01-02T00:00:00+00:00"
    # One reading BEFORE the window (must be excluded) and one AT/after it.
    agent.memory.record_sensor_readings([
        SensorReading("2025-01-01T00:00:00+00:00", "UNLANDED-REMEDIATION", 99.0, True),
        SensorReading("2025-01-02T00:00:00+00:00", "UNLANDED-REMEDIATION", 5.0, True),
    ])
    occurs = _bet(
        "c1-p1", "sensor UNLANDED-REMEDIATION goes above 2 within 3 cycles", bet_ts,
    )
    does_not = _bet(
        "c1-p2",
        "sensor UNLANDED-REMEDIATION does not go above 2 within 3 cycles",
        bet_ts,
    )
    evidence = agent._collect_calibration_evidence([occurs, does_not], 1)

    # Only the at/after-window reading is injected (the earlier 99.0 excluded).
    assert evidence["c1-p1"].sensor_readings == (("UNLANDED-REMEDIATION", 5.0),)

    occurs_score = score_loss_condition(
        parse_loss_condition(occurs["loss_condition"]),
        evidence["c1-p1"],
        cycles_elapsed=0,
    )
    does_not_score = score_loss_condition(
        parse_loss_condition(does_not["loss_condition"]),
        evidence["c1-p2"],
        cycles_elapsed=0,
    )
    assert occurs_score.verdict == "wrong"
    assert does_not_score.verdict == "correct"


def test_collect_evidence_unseen_tag_resolves_unresolved(
    mem_fs: dict[str, str], make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A bet whose tag was never seen still resolves None -> unresolved."""
    agent = make_agent()
    bet_ts = "2025-01-02T00:00:00+00:00"
    agent.memory.record_sensor_readings([
        SensorReading("2025-01-02T00:00:01+00:00", "OWNED-NET-DELTA", 4.0, True),
    ])
    bet = _bet("c1-p3", "sensor GHOST-TAG goes above 1 within 3 cycles", bet_ts)
    evidence = agent._collect_calibration_evidence([bet], 1)

    score = score_loss_condition(
        parse_loss_condition(bet["loss_condition"]),
        evidence["c1-p3"],
        cycles_elapsed=10,  # past the window cutoff
    )
    assert score.observed is None
    assert score.verdict == "unresolved"


def test_collect_evidence_empty_timestamp_falls_to_bare_arm(
    mem_fs: dict[str, str], make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A SENSOR bet with an empty window start carries no readings (bare arm)."""
    agent = make_agent()
    agent.memory.record_sensor_readings([
        SensorReading("2025-01-02T00:00:00+00:00", "UNLANDED-REMEDIATION", 5.0, True),
    ])
    bet = _bet(
        "c1-p4", "sensor UNLANDED-REMEDIATION goes above 2 within 3 cycles", "",
    )
    evidence = agent._collect_calibration_evidence([bet], 1)
    assert evidence["c1-p4"].sensor_readings == ()


def test_forced_quiet_ratio_grammar_example_bet_scores_not_unresolved(
    mem_fs: dict[str, str], make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The grammar's own FORCED-QUIET-RATIO teaching example now scores.

    Before this change ``build_sensor_readings`` emitted only
    ``UNLANDED-REMEDIATION`` and ``OWNED-NET-DELTA``, so a SENSOR_THRESHOLD bet on
    ``FORCED-QUIET-RATIO`` -- the literal ``LOSS_CONDITION_GRAMMAR`` example the
    reflection prompt teaches -- never matched a persisted reading and died
    ``unresolved`` forever. This exercises the closed loop end-to-end through the
    *real* write-half helper: build the reading from a forced-quiet task history,
    persist it, then score the grammar's exact taught bet.
    """
    agent = make_agent()
    bet_ts = "2025-01-02T00:00:00+00:00"
    # Two forced remediation quiets, no chosen stand-down => ratio 1.0 (> 0.5),
    # built through the same pure helper the loop's persist site uses.
    forced_tasks: list[dict[str, object]] = [
        {"goal": "Remediate PR #1: a", "result_summary": "awaiting tend push"},
        {"goal": "Remediate PR #2: b", "result_summary": "[merge-block] held"},
    ]
    agent.memory.record_sensor_readings(
        build_sensor_readings(0, [], forced_quiet_tasks=forced_tasks, now=bet_ts),
    )

    # Use the grammar's literal FORCED-QUIET-RATIO example verbatim, so this test
    # proves the tag the model is actually guided toward now scores.
    grammar_example = next(
        g for g in LOSS_CONDITION_GRAMMAR if "FORCED-QUIET-RATIO" in g
    )
    bet = _bet("fqr", grammar_example, bet_ts)
    evidence = agent._collect_calibration_evidence([bet], 1)
    # The evidence carries every in-window reading (the scorer filters by tag);
    # the crucial fact is that the FORCED-QUIET-RATIO reading is now present.
    assert ("FORCED-QUIET-RATIO", 1.0) in evidence["fqr"].sensor_readings

    score = score_loss_condition(
        parse_loss_condition(bet["loss_condition"]),
        evidence["fqr"],
        cycles_elapsed=0,
    )
    # The loss condition (ratio > 0.5) materialised, so the bet resolves to a
    # real verdict -- crucially NOT ``unresolved``, the failure this change fixes.
    assert score.observed is True
    assert score.verdict == "wrong"
    assert score.verdict != "unresolved"


# ---------------------------------------------------------------------------
# Criterion 4 — contemplate persists the per-cycle sensor values it computes
# ---------------------------------------------------------------------------


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
async def test_contemplate_persists_sensor_readings(
    mem_fs: dict[str, str], make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The live ``contemplate`` write path records this cycle's scalar sensors.

    Guards the dead-code risk that every other criterion could pass on seeded
    data while nothing ever writes a real reading. After one contemplation the
    ``UNLANDED-REMEDIATION`` reading the measured-data block computes must exist
    on the sidecar for later ``_collect_calibration_evidence`` scoring.
    """
    agent = make_agent()

    async def _fake_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        return MagicMock(response="A plain reflection with no markers.")

    with patch(
        "maestro.diagnostics.build_diagnostic_snapshot",
        return_value=_clean_snapshot(),
    ), patch("maestro.consciousness.query", side_effect=_fake_query), \
         patch.object(
             agent.consciousness,
             "update_self_model",
             AsyncMock(return_value=SelfModel()),
         ), patch.object(agent.journal, "write_thought"):
        await agent.contemplate(session=None)

    readings = agent.memory.get_recent_sensor_readings()
    tags = {r.tag for r in readings}
    assert "UNLANDED-REMEDIATION" in tags


@pytest.mark.asyncio
async def test_contemplate_persists_forced_quiet_ratio_when_quiet_exists(
    mem_fs: dict[str, str], make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The live ``contemplate`` path persists FORCED-QUIET-RATIO too.

    With a forced remediation quiet in recent task history, the measured-data
    block computes a ``FORCED-QUIET-RATIO`` line, and the same persist site must
    now write it to ``sensor_readings.jsonl`` -- so a SENSOR_THRESHOLD bet on the
    grammar's own teaching-example tag scores against a real reading instead of
    dying ``unresolved``. Complements the UNLANDED-REMEDIATION-only guard above by
    covering the full computed scalar set the block surfaces this cycle.
    """
    agent = make_agent()
    # A forced remediation quiet: lineage goal + an imposed-quiet marker.
    agent.memory.record_task(
        TaskRecord(
            task_id="rem-1",
            goal="Remediate PR #1: fix the thing",
            plan=[],
            outcome="partial",
            result_summary="resolved on branch, awaiting tend push",
            errors=[],
            duration_secs=1.0,
            timestamp="2025-01-01T00:00:00+00:00",
        ),
    )

    async def _fake_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        return MagicMock(response="A plain reflection with no markers.")

    with patch(
        "maestro.diagnostics.build_diagnostic_snapshot",
        return_value=_clean_snapshot(),
    ), patch("maestro.consciousness.query", side_effect=_fake_query), \
         patch.object(
             agent.consciousness,
             "update_self_model",
             AsyncMock(return_value=SelfModel()),
         ), patch.object(agent.journal, "write_thought"):
        await agent.contemplate(session=None)

    readings = agent.memory.get_recent_sensor_readings()
    by_tag = {r.tag: r for r in readings}
    assert "UNLANDED-REMEDIATION" in by_tag
    assert "FORCED-QUIET-RATIO" in by_tag
    # One forced quiet, none chosen => forced/total == 1.0, persisted as the
    # fraction (not the integer count) so a "goes above 0.5" bet scores.
    assert by_tag["FORCED-QUIET-RATIO"].value == 1.0


# ---------------------------------------------------------------------------
# Criterion 2 — self_calibration.py is the pure owner (AST/import assertion)
# ---------------------------------------------------------------------------


def test_self_calibration_is_pure_owner() -> None:
    """``maestro/self_calibration.py`` imports only ``maestro.types`` and does no I/O.

    The scorer must never read the filesystem — the caller
    (``_collect_calibration_evidence``) injects every reading. This pins the
    invariant structurally so a future edit that reaches for a file, or imports a
    non-``types`` maestro module, fails here.
    """
    source = Path(__file__).resolve().parent.parent / "maestro" / "self_calibration.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    maestro_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "maestro",
        ):
            maestro_imports.append(node.module or "")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("maestro"):
                    maestro_imports.append(alias.name)
    assert maestro_imports == ["maestro.types"], maestro_imports

    # No file-I/O call surface anywhere in the module.
    io_names = {"open", "read_text", "write_text", "read_bytes", "write_bytes"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in io_names:
                offenders.append(func.id)
            if isinstance(func, ast.Attribute) and func.attr in io_names:
                offenders.append(func.attr)
    assert offenders == [], offenders
