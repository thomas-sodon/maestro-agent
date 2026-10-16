"""Version, rejection, review, publication, audit-history, question-resolution and
document records; split out of ``tests/test_memory.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from maestro.memory import (
    ArtifactReview,
    DocumentRecord,
    DuplicateProposalError,
    JsonlVersionArchiveLedger,
    Memory,
    RejectionRecord,
    VersionRecord,
)
from tests.test_memory import (
    FAKE_DIR,
    _make_memory,
    _make_proposal,
)

# ---------------------------------------------------------------------------
# VersionRecord persistence
# ---------------------------------------------------------------------------


def _make_version(**overrides: Any) -> VersionRecord:
    defaults: dict[str, Any] = dict(
        version_id="v20260101120000",
        plan_id="ep_1",
        plan_title="Example plan",
        commit_sha="abc123",
        merged_at="2026-01-01T12:00:00+00:00",
        window_tasks_total=3,
        window_tasks_succeeded=2,
        window_tasks_failed=1,
        window_sdk_cost_usd=1.25,
        window_sdk_calls=7,
        window_cache_hit_rate=0.5,
        window_rate_limit_hits=0,
        window_error_count=0,
        quality_gate_pass=True,
        notes=None,
    )
    defaults.update(overrides)
    return VersionRecord(**defaults)


def test_record_and_retrieve_version(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.record_version(_make_version())

    path = str(FAKE_DIR / "version_archive.jsonl")
    assert path in mem_fs
    assert "Example plan" in mem_fs[path]

    versions = mem.get_recent_versions()
    assert len(versions) == 1
    assert versions[0].plan_title == "Example plan"
    assert versions[0].quality_gate_pass is True


def test_record_version_after_partial_tail_keeps_new_row(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A crash fragment at EOF must not swallow the next version row (#4588)."""
    mem = _make_memory(mem_fs)
    mem_fs[str(FAKE_DIR / "version_archive.jsonl")] = (
        json.dumps(asdict(_make_version(version_id="v1"))) + "\n"
        + '{"version_id": "v2", "pl'
    )
    with caplog.at_level(logging.WARNING):
        mem.record_version(_make_version(version_id="v3"))
    assert [v.version_id for v in mem.get_recent_versions()] == ["v1", "v3"]
    messages = [r.getMessage() for r in caplog.records]
    assert any("Dropping trailing partial JSONL line" in m for m in messages)


def test_jsonl_version_ledger_record_after_partial_tail(mem_fs: dict[str, str]) -> None:
    """The JSONL backend itself cuts a torn tail before appending (#4588)."""
    mem_fs[str(FAKE_DIR / "version_archive.jsonl")] = (
        json.dumps(asdict(_make_version(version_id="v1"))) + "\n" + '{"version_id": "v2'
    )
    ledger = JsonlVersionArchiveLedger(FAKE_DIR)
    ledger.record(_make_version(version_id="v3"))
    assert [v.version_id for v in ledger.load_all()] == ["v1", "v3"]


def test_record_version_empty_id_raises(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    with pytest.raises(ValueError, match="version_id"):
        mem.record_version(_make_version(version_id=""))


def test_record_version_notes_truncated_to_cap(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    long_notes = "x" * 1000
    mem.record_version(_make_version(notes=long_notes))
    [v] = mem.get_recent_versions()
    assert v.notes is not None
    assert len(v.notes) == 280
    assert v.notes == "x" * 280


def test_record_version_short_notes_preserved(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.record_version(_make_version(notes="brief"))
    [v] = mem.get_recent_versions()
    assert v.notes == "brief"


def test_get_recent_versions_zero_returns_empty(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.record_version(_make_version())
    assert mem.get_recent_versions(0) == []


def test_get_recent_versions_tail_order(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    for i in range(5):
        mem.record_version(_make_version(version_id=f"v{i}"))
    tail = mem.get_recent_versions(3)
    assert [v.version_id for v in tail] == ["v2", "v3", "v4"]


def test_get_recent_versions_missing_file(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    assert mem.get_recent_versions() == []


def test_get_recent_versions_skips_blank_and_corrupt_lines(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "version_id": "v1",
        "plan_id": "ep_1",
        "plan_title": "ok",
        "commit_sha": "sha",
        "merged_at": "2026-01-01T00:00:00+00:00",
        "window_tasks_total": 0,
        "window_tasks_succeeded": 0,
        "window_tasks_failed": 0,
        "window_sdk_cost_usd": 0.0,
        "window_sdk_calls": 0,
        "window_cache_hit_rate": 0.0,
        "window_rate_limit_hits": 0,
        "window_error_count": 0,
        "quality_gate_pass": True,
        "notes": None,
    })
    mem_fs[str(FAKE_DIR / "version_archive.jsonl")] = (
        valid + "\n\n   \n" + "{not-json}\n" + valid + "\n"
    )
    versions = mem.get_recent_versions(10)
    assert len(versions) == 2


def test_version_record_ignores_extra_fields(mem_fs: dict[str, str]) -> None:
    """Forward-compat: extra unknown fields are silently dropped on load."""
    mem = _make_memory(mem_fs)
    raw = {
        "version_id": "v1",
        "plan_id": "ep_1",
        "plan_title": "ok",
        "commit_sha": "sha",
        "merged_at": "2026-01-01T00:00:00+00:00",
        "window_tasks_total": 0,
        "window_tasks_succeeded": 0,
        "window_tasks_failed": 0,
        "window_sdk_cost_usd": 0.0,
        "window_sdk_calls": 0,
        "window_cache_hit_rate": 0.0,
        "window_rate_limit_hits": 0,
        "window_error_count": 0,
        "quality_gate_pass": True,
        "notes": None,
        "unknown_future_field": "ignored",
    }
    mem_fs[str(FAKE_DIR / "version_archive.jsonl")] = json.dumps(raw) + "\n"
    [v] = mem.get_recent_versions(10)
    assert v.version_id == "v1"


def test_load_all_versions(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    for i in range(4):
        mem.record_version(_make_version(version_id=f"v{i}"))
    all_versions = mem._load_all_versions()
    assert [v.version_id for v in all_versions] == ["v0", "v1", "v2", "v3"]


def test_load_all_versions_missing_file(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    assert mem._load_all_versions() == []


def test_load_all_versions_skips_corrupt_lines(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "version_id": "v1",
        "plan_id": "ep_1",
        "plan_title": "ok",
        "commit_sha": "sha",
        "merged_at": "2026-01-01T00:00:00+00:00",
        "window_tasks_total": 0,
        "window_tasks_succeeded": 0,
        "window_tasks_failed": 0,
        "window_sdk_cost_usd": 0.0,
        "window_sdk_calls": 0,
        "window_cache_hit_rate": 0.0,
        "window_rate_limit_hits": 0,
        "window_error_count": 0,
        "quality_gate_pass": True,
        "notes": None,
    })
    mem_fs[str(FAKE_DIR / "version_archive.jsonl")] = (
        valid + "\n" + "corrupt-line\n" + "\n" + valid + "\n"
    )
    assert len(mem._load_all_versions()) == 2


def test_read_version_records_happy_path(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    for i in range(3):
        mem.record_version(_make_version(version_id=f"v{i}"))
    records = mem.read_version_records()
    # Oldest-first public contract.
    assert [v.version_id for v in records] == ["v0", "v1", "v2"]
    assert records[0].plan_title == "Example plan"


def test_read_version_records_tail_bound(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    for i in range(5):
        mem.record_version(_make_version(version_id=f"v{i}"))
    tail = mem.read_version_records(n=2)
    assert [v.version_id for v in tail] == ["v3", "v4"]


def test_read_version_records_missing_file(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    assert mem.read_version_records() == []


def test_read_version_records_empty_file(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem_fs[str(FAKE_DIR / "version_archive.jsonl")] = ""
    assert mem.read_version_records() == []


def test_read_version_records_zero_returns_empty(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.record_version(_make_version())
    assert mem.read_version_records(0) == []


def test_read_version_records_skips_corrupt_tail_line(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "version_id": "v1",
        "plan_id": "ep_1",
        "plan_title": "ok",
        "commit_sha": "sha",
        "merged_at": "2026-01-01T00:00:00+00:00",
        "window_tasks_total": 0,
        "window_tasks_succeeded": 0,
        "window_tasks_failed": 0,
        "window_sdk_cost_usd": 0.0,
        "window_sdk_calls": 0,
        "window_cache_hit_rate": 0.0,
        "window_rate_limit_hits": 0,
        "window_error_count": 0,
        "quality_gate_pass": True,
        "notes": None,
    })
    # A schema-corrupted tail row — valid JSON but missing required
    # VersionRecord fields — must be skipped without raising; the
    # well-formed record before it still parses. (``iter_jsonl_reverse``
    # already drops non-JSON lines, so the parse-level skip exercised here
    # needs a decodable-but-incomplete dict.)
    corrupt = json.dumps({"version_id": "incomplete"})
    mem_fs[str(FAKE_DIR / "version_archive.jsonl")] = (
        valid + "\n" + corrupt + "\n"
    )
    records = mem.read_version_records(10)
    assert len(records) == 1
    assert records[0].version_id == "v1"


def test_read_version_records_corrupt_skip_bounded(
    mem_fs: dict[str, str],
) -> None:
    """Every corrupt row is dropped — no bounded reverse-skip cap truncates.

    The old reader walked the archive backwards and stopped after *n* corrupt
    skips, so a valid record buried beneath a burst of corrupt tail lines could
    be missed. The ``load_all``-based reader parses every row forward and drops
    each corrupt one, so the valid record surfaces regardless of how many corrupt
    lines surround it, and *n* only slices the resulting tail.
    """
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "version_id": "v_survivor",
        "plan_id": "ep_1",
        "plan_title": "ok",
        "commit_sha": "sha",
        "merged_at": "2026-01-01T00:00:00+00:00",
        "window_tasks_total": 0,
        "window_tasks_succeeded": 0,
        "window_tasks_failed": 0,
        "window_sdk_cost_usd": 0.0,
        "window_sdk_calls": 0,
        "window_cache_hit_rate": 0.0,
        "window_rate_limit_hits": 0,
        "window_error_count": 0,
        "quality_gate_pass": True,
        "notes": None,
    })
    corrupt = json.dumps({"version_id": "incomplete"})
    mem_fs[str(FAKE_DIR / "version_archive.jsonl")] = (
        valid + "\n" + corrupt + "\n" + corrupt + "\n" + corrupt + "\n"
    )
    # Old semantics: n=2 would stop the reverse walk after two corrupt skips and
    # never reach ``v_survivor``. New semantics: the forward ``load_all`` drops
    # all three corrupt rows and returns the surviving record.
    records = mem.read_version_records(n=2)
    assert [r.version_id for r in records] == ["v_survivor"]


def test_load_all_versions_returns_empty_on_os_error(
    mem_fs: dict[str, str],
) -> None:
    """An ``OSError`` reading the archive is swallowed and ``[]`` returned."""
    from maestro.memory import load_all_versions

    mem_fs[str(FAKE_DIR / "version_archive.jsonl")] = "{}"

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("simulated")

    with patch.object(Path, "read_text", _boom):
        assert load_all_versions(FAKE_DIR) == []


def test_uninjected_memory_defaults_to_jsonl_version_ledger(
    mem_fs: dict[str, str],
) -> None:
    """An un-injected ``Memory`` keeps today's JSONL behaviour unchanged."""
    from maestro.memory import JsonlVersionArchiveLedger

    mem = Memory(FAKE_DIR)
    assert isinstance(mem._versions, JsonlVersionArchiveLedger)
    v = _make_version(version_id="v_1")
    mem.record_version(v)
    assert mem.get_recent_versions() == [v]
    # Persisted to the JSONL sidecar (the flag-off substrate).
    assert str(FAKE_DIR / "version_archive.jsonl") in mem_fs


def test_injected_version_archive_backend_is_used(
    mem_fs: dict[str, str],
) -> None:
    """An injected backend receives the rows; the JSONL sidecar stays empty."""
    from maestro.state_store import SqliteVersionArchiveLedger, StateStore

    sqlite = SqliteVersionArchiveLedger(StateStore(":memory:"))
    mem = Memory(FAKE_DIR, version_archive=sqlite)
    v = _make_version(version_id="v_9")
    mem.record_version(v)
    assert mem.get_recent_versions() == [v]
    assert mem.read_version_records() == [v]
    assert mem._load_all_versions() == [v]
    # Routed through the injected backend, not the JSONL sidecar.
    assert sqlite.load_all() == [v]
    assert str(FAKE_DIR / "version_archive.jsonl") not in mem_fs


_STAMP_CLAIM = {"kind": "cost_reduction", "callers": ["reflect"],
                "expected_usd_per_iteration": 0.5, "income_kind": None,
                "enables": None}
_STAMP_BASELINE = {
    "window_days": 30, "requested_window_days": 30, "iterations": 4,
    "spend_usd": 4.0, "spend_usd_per_day": 0.1, "spend_usd_per_iteration": 1.0,
    "by_caller": [{"caller": "reflect", "group": "Reflect", "cost_usd": 4.0,
                   "calls": 2, "usd_per_iteration": 1.0}],
    "spend_source": "sdk_reported_total_cost_usd",
    "income_usd_per_day": 0.0, "income_source": "absent",
}



# ---------------------------------------------------------------------------
# RejectionRecord persistence
# ---------------------------------------------------------------------------


def _make_rejection(**overrides: Any) -> RejectionRecord:
    defaults: dict[str, Any] = dict(
        plan_id="ep_1",
        title="Example plan",
        category="capability",
        priority="capability",
        rejection_reason="Too aggressive",
        timestamp="2026-04-23T12:00:00+00:00",
        rejection_category="scope_too_large",
    )
    defaults.update(overrides)
    return RejectionRecord(**defaults)


def test_record_and_retrieve_rejection(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.record_rejection(_make_rejection())

    path = str(FAKE_DIR / "rejection_history.jsonl")
    assert path in mem_fs
    assert "Example plan" in mem_fs[path]

    rejections = mem.get_recent_rejections()
    assert len(rejections) == 1
    assert rejections[0].plan_id == "ep_1"
    assert rejections[0].rejection_category == "scope_too_large"


def test_record_rejection_empty_plan_id_raises(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    with pytest.raises(ValueError, match="plan_id"):
        mem.record_rejection(_make_rejection(plan_id=""))


def test_get_recent_rejections_tail_order(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    for i in range(10):
        mem.record_rejection(_make_rejection(plan_id=f"ep_{i}"))
    tail = mem.get_recent_rejections(3)
    assert [r.plan_id for r in tail] == ["ep_7", "ep_8", "ep_9"]


def test_get_recent_rejections_zero_returns_empty(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.record_rejection(_make_rejection())
    assert mem.get_recent_rejections(0) == []


def test_get_recent_rejections_missing_file(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    assert mem.get_recent_rejections() == []


def test_get_recent_rejections_skips_blank_and_corrupt_lines(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "plan_id": "ep_1",
        "title": "ok",
        "category": "capability",
        "priority": "capability",
        "rejection_reason": "no",
        "timestamp": "2026-04-23T00:00:00+00:00",
        "rejection_category": "other",
    })
    mem_fs[str(FAKE_DIR / "rejection_history.jsonl")] = (
        valid + "\n\n   \n" + "{not-json}\n" + valid + "\n"
    )
    with caplog.at_level("WARNING", logger="maestro"):
        rejections = mem.get_recent_rejections(10)
    assert len(rejections) == 2
    assert any("corrupted rejection" in r.message for r in caplog.records)


def test_get_recent_rejections_skips_record_failing_construction(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A valid-JSON rejection row missing required fields is skipped, not fatal.

    Field-filtering leaves ``RejectionRecord(**filtered)`` short of its required
    args, so ``_parse_rejection_dict`` raises ``TypeError``, catches it, and
    returns ``None`` — the row is dropped with a warning, valid rows survive.
    """
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "plan_id": "ep_1", "title": "ok", "category": "capability",
        "priority": "capability", "rejection_reason": "no",
        "timestamp": "2026-04-23T00:00:00+00:00", "rejection_category": "other",
    })
    incomplete = json.dumps({"title": "orphan"})  # missing every required field
    mem_fs[str(FAKE_DIR / "rejection_history.jsonl")] = (
        valid + "\n" + incomplete + "\n"
    )
    with caplog.at_level("WARNING", logger="maestro"):
        rejections = mem.get_recent_rejections(10)
    assert [r.plan_id for r in rejections] == ["ep_1"]
    assert any("corrupted rejection" in r.message for r in caplog.records)


def test_rejection_record_ignores_extra_fields(mem_fs: dict[str, str]) -> None:
    """Forward-compat: extra unknown fields are silently dropped on load."""
    mem = _make_memory(mem_fs)
    raw = {
        "plan_id": "ep_1",
        "title": "ok",
        "category": "capability",
        "priority": "capability",
        "rejection_reason": "no",
        "timestamp": "2026-04-23T00:00:00+00:00",
        "rejection_category": "other",
        "unknown_future_field": "ignored",
    }
    mem_fs[str(FAKE_DIR / "rejection_history.jsonl")] = json.dumps(raw) + "\n"
    [r] = mem.get_recent_rejections(10)
    assert r.plan_id == "ep_1"


def test_load_all_rejections(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    for i in range(4):
        mem.record_rejection(_make_rejection(plan_id=f"ep_{i}"))
    all_rej = mem._load_all_rejections()
    assert [r.plan_id for r in all_rej] == ["ep_0", "ep_1", "ep_2", "ep_3"]


def test_load_all_rejections_missing_file(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    assert mem._load_all_rejections() == []


def test_load_all_rejections_skips_corrupt_lines(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "plan_id": "ep_1",
        "title": "ok",
        "category": "capability",
        "priority": "capability",
        "rejection_reason": "no",
        "timestamp": "2026-04-23T00:00:00+00:00",
        "rejection_category": "other",
    })
    mem_fs[str(FAKE_DIR / "rejection_history.jsonl")] = (
        valid + "\n" + "corrupt-line\n" + "\n" + valid + "\n"
    )
    assert len(mem._load_all_rejections()) == 2


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ("bug_fix", "resilience"),
        ("security", "resilience"),
        ("performance", "capability"),
        ("feature", "capability"),
    ],
)
def test_rejection_record_legacy_priority_remap(
    mem_fs: dict[str, str], legacy: str, expected: str,
) -> None:
    """Legacy ``rejection_history.jsonl`` rows hydrate with the same
    ``LEGACY_PRIORITY_REMAP`` ``Memory._parse_plan_line`` applies, so the
    priority field stays consistent across both record types."""
    mem = _make_memory(mem_fs)
    raw = {
        "plan_id": "ep_legacy", "title": "old", "category": "capability",
        "priority": legacy, "rejection_reason": "no",
        "timestamp": "2026-04-23T00:00:00+00:00", "rejection_category": "other",
    }
    mem_fs[str(FAKE_DIR / "rejection_history.jsonl")] = json.dumps(raw) + "\n"

    [recent] = mem.get_recent_rejections(10)
    assert recent.priority == expected
    [allrec] = mem._load_all_rejections()
    assert allrec.priority == expected


# ---------------------------------------------------------------------------
# RejectionHistoryBackend seam (proposal 106)
# ---------------------------------------------------------------------------


def test_jsonl_rejection_history_ledger_round_trip(mem_fs: dict[str, str]) -> None:
    """The default JSONL adapter records to and loads from rejection_history.jsonl."""
    from maestro.memory import JsonlRejectionHistoryLedger

    ledger = JsonlRejectionHistoryLedger(FAKE_DIR)
    a = _make_rejection(plan_id="ep_a")
    b = _make_rejection(plan_id="ep_b")
    ledger.record(a)
    ledger.record(b)
    # Lands on the JSONL sidecar and reads back oldest-first.
    assert str(FAKE_DIR / "rejection_history.jsonl") in mem_fs
    assert ledger.load_all() == [a, b]


def test_load_all_rejection_history_missing_file_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    from maestro.memory import load_all_rejection_history

    assert load_all_rejection_history(FAKE_DIR) == []


def test_load_all_rejection_history_oldest_first(mem_fs: dict[str, str]) -> None:
    from maestro.memory import (
        JsonlRejectionHistoryLedger,
        load_all_rejection_history,
    )

    ledger = JsonlRejectionHistoryLedger(FAKE_DIR)
    for i in (1, 2, 3):
        ledger.record(_make_rejection(plan_id=f"ep_{i}"))
    assert [r.plan_id for r in load_all_rejection_history(FAKE_DIR)] == [
        "ep_1", "ep_2", "ep_3",
    ]


def test_load_all_rejection_history_skips_corrupt_never_raises(
    mem_fs: dict[str, str],
) -> None:
    """Every corrupt row — interior *or* trailing — is skipped, never raised.

    Preserves the two rejection readers' historical tolerance: a malformed line
    anywhere is dropped and the surviving rows still come back.
    """
    from maestro.memory import load_all_rejection_history

    good1 = json.dumps(asdict(_make_rejection(plan_id="ep_1")))
    good2 = json.dumps(asdict(_make_rejection(plan_id="ep_3")))
    mem_fs[str(FAKE_DIR / "rejection_history.jsonl")] = (
        good1 + "\nBROKEN interior\n" + good2 + '\n{"partial trailing'
    )
    history = load_all_rejection_history(FAKE_DIR)
    assert [r.plan_id for r in history] == ["ep_1", "ep_3"]


def test_load_all_rejection_history_returns_empty_on_os_error(
    mem_fs: dict[str, str],
) -> None:
    """An ``OSError`` reading the file is swallowed and ``[]`` returned."""
    from maestro.memory import load_all_rejection_history

    mem_fs[str(FAKE_DIR / "rejection_history.jsonl")] = "{}"

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("simulated")

    with patch.object(Path, "read_text", _boom):
        assert load_all_rejection_history(FAKE_DIR) == []


def test_uninjected_memory_defaults_to_jsonl_rejection_ledger(
    mem_fs: dict[str, str],
) -> None:
    """An un-injected ``Memory`` keeps today's JSONL behaviour unchanged."""
    from maestro.memory import JsonlRejectionHistoryLedger

    mem = Memory(FAKE_DIR)
    assert isinstance(mem._rejections, JsonlRejectionHistoryLedger)
    r = _make_rejection(plan_id="ep_1")
    mem.record_rejection(r)
    assert mem.get_recent_rejections() == [r]
    # Persisted to the JSONL sidecar (the flag-off substrate).
    assert str(FAKE_DIR / "rejection_history.jsonl") in mem_fs


def test_injected_rejection_history_backend_is_used(
    mem_fs: dict[str, str],
) -> None:
    """An injected backend receives the rows; the JSONL sidecar stays empty."""
    from maestro.state_store import SqliteRejectionHistoryLedger, StateStore

    sqlite = SqliteRejectionHistoryLedger(StateStore(":memory:"))
    mem = Memory(FAKE_DIR, rejection_history=sqlite)
    r = _make_rejection(plan_id="ep_9")
    mem.record_rejection(r)
    assert mem.get_recent_rejections() == [r]
    assert mem._load_all_rejections() == [r]
    # Routed through the injected backend, not the JSONL sidecar.
    assert sqlite.load_all() == [r]
    assert str(FAKE_DIR / "rejection_history.jsonl") not in mem_fs


# ---------------------------------------------------------------------------
# ArtifactReview
# ---------------------------------------------------------------------------


def _make_review(
    *,
    review_id: str = "r1",
    task_id: str = "t1",
    verdict: str = "publish",
    bundle_file_names: list[str] | None = None,
) -> ArtifactReview:
    return ArtifactReview(
        review_id=review_id,
        task_id=task_id,
        verdict=verdict,  # type: ignore[arg-type]
        title="A nice thing",
        reasoning="Worth sharing",
        bundle_file_names=bundle_file_names if bundle_file_names is not None else ["README.md"],
        proposed_name="nice-thing",
        proposed_description="a thing",
        timestamp="2026-04-23T00:00:00Z",
    )


def test_write_artifact_review_round_trip(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_artifact_review(_make_review())
    [loaded] = mem._load_all_reviews()
    assert loaded.review_id == "r1"
    assert loaded.task_id == "t1"
    assert loaded.bundle_file_names == ["README.md"]


def test_write_artifact_review_empty_review_id_raises(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    with pytest.raises(ValueError, match="review_id"):
        mem.write_artifact_review(_make_review(review_id=""))


def test_load_all_reviews_drops_empty_review_id(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    mem = _make_memory(mem_fs)
    empty = json.dumps({
        "review_id": "",
        "task_id": "t_empty",
        "verdict": "publish",
        "title": "x",
        "reasoning": "y",
        "bundle_file_names": ["a"],
        "proposed_name": "n",
        "proposed_description": "d",
        "timestamp": "2026-04-23T00:00:00Z",
    })
    valid = json.dumps({
        "review_id": "r1",
        "task_id": "t1",
        "verdict": "publish",
        "title": "x",
        "reasoning": "y",
        "bundle_file_names": ["a"],
        "proposed_name": "n",
        "proposed_description": "d",
        "timestamp": "2026-04-23T00:00:00Z",
    })
    mem_fs[str(FAKE_DIR / "artifact_reviews.jsonl")] = empty + "\n" + valid + "\n"

    with caplog.at_level("WARNING", logger="maestro"):
        reviews = mem._load_all_reviews()

    assert [r.review_id for r in reviews] == ["r1"]
    assert any("empty review_id" in r.message for r in caplog.records)


def test_get_reviewed_task_ids_excludes_empty_review_id(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    empty = json.dumps({
        "review_id": "",
        "task_id": "t_empty",
        "verdict": "publish",
        "title": "x",
        "reasoning": "y",
        "bundle_file_names": ["a"],
        "proposed_name": "n",
        "proposed_description": "d",
        "timestamp": "2026-04-23T00:00:00Z",
    })
    valid = json.dumps({
        "review_id": "r1",
        "task_id": "t1",
        "verdict": "publish",
        "title": "x",
        "reasoning": "y",
        "bundle_file_names": ["a"],
        "proposed_name": "n",
        "proposed_description": "d",
        "timestamp": "2026-04-23T00:00:00Z",
    })
    mem_fs[str(FAKE_DIR / "artifact_reviews.jsonl")] = empty + "\n" + valid + "\n"

    assert mem.get_reviewed_task_ids() == {"t1"}


def test_write_artifact_review_append_only(mem_fs: dict[str, str]) -> None:
    """Subsequent writes append to the JSONL file rather than replacing it."""
    mem = _make_memory(mem_fs)
    mem.write_artifact_review(_make_review(review_id="r1"))
    mem.write_artifact_review(_make_review(review_id="r2", task_id="t2"))

    reviews = mem._load_all_reviews()
    assert [r.review_id for r in reviews] == ["r1", "r2"]


def test_write_artifact_review_uses_file_lock(mem_fs: dict[str, str]) -> None:
    """The append serializes on the same lock the rewrite path holds.

    Without the lock an append interleaving with
    ``update_artifact_review_verdict``'s load-then-rewrite is clobbered by
    the atomic rename. Mirrors ``test_write_publication_proposal_uses_file_lock``.
    """
    mem = _make_memory(mem_fs)

    called_with: list[Any] = []

    @contextmanager
    def tracking_lock(path: Any) -> Iterator[None]:
        called_with.append(path)
        yield

    with patch("maestro.memory.file_lock", tracking_lock):
        mem.write_artifact_review(_make_review())

    assert len(called_with) == 1
    assert str(called_with[0]).endswith("artifact_reviews.jsonl")


def test_get_artifact_reviews_returns_tail(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    for i in range(5):
        mem.write_artifact_review(_make_review(review_id=f"r{i}", task_id=f"t{i}"))

    last_two = mem.get_artifact_reviews(n=2)
    assert [r.review_id for r in last_two] == ["r3", "r4"]


def test_get_artifact_reviews_zero_returns_empty(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_artifact_review(_make_review())
    assert mem.get_artifact_reviews(n=0) == []


def test_get_artifact_reviews_default_n_returns_last_30(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    for i in range(35):
        mem.write_artifact_review(_make_review(review_id=f"r{i:02d}", task_id=f"t{i}"))

    tail = mem.get_artifact_reviews()
    assert len(tail) == 30
    assert tail[0].review_id == "r05"
    assert tail[-1].review_id == "r34"


def test_get_reviewed_task_ids(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_artifact_review(_make_review(review_id="r1", task_id="t1"))
    mem.write_artifact_review(_make_review(review_id="r2", task_id="t2"))
    # Same task reviewed twice — the set collapses
    mem.write_artifact_review(_make_review(review_id="r3", task_id="t1"))

    assert mem.get_reviewed_task_ids() == {"t1", "t2"}


def test_get_reviewed_task_ids_empty(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    assert mem.get_reviewed_task_ids() == set()


def test_load_all_reviews_missing_file(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    assert mem._load_all_reviews() == []


def test_load_all_reviews_skips_corrupt_and_blank_lines(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "review_id": "r1",
        "task_id": "t1",
        "verdict": "publish",
        "title": "x",
        "reasoning": "y",
        "bundle_file_names": ["a"],
        "proposed_name": "n",
        "proposed_description": "d",
        "timestamp": "2026-04-23T00:00:00Z",
    })
    mem_fs[str(FAKE_DIR / "artifact_reviews.jsonl")] = (
        valid + "\n" + "corrupt\n" + "\n" + valid.replace("r1", "r2") + "\n"
    )
    assert len(mem._load_all_reviews()) == 2


def test_load_all_reviews_drops_unknown_fields(mem_fs: dict[str, str]) -> None:
    """Forward-compat: unknown JSON fields are silently dropped."""
    mem = _make_memory(mem_fs)
    row = {
        "review_id": "r1",
        "task_id": "t1",
        "verdict": "publish",
        "title": "x",
        "reasoning": "y",
        "bundle_file_names": ["a"],
        "proposed_name": "n",
        "proposed_description": "d",
        "timestamp": "2026-04-23T00:00:00Z",
        "future_unknown_field": "ignored",
    }
    mem_fs[str(FAKE_DIR / "artifact_reviews.jsonl")] = json.dumps(row) + "\n"

    reviews = mem._load_all_reviews()
    assert len(reviews) == 1
    assert reviews[0].review_id == "r1"


def test_load_all_reviews_coerces_bundle_file_names(mem_fs: dict[str, str]) -> None:
    """Malformed ``bundle_file_names`` (null, string, non-strings) are coerced."""
    mem = _make_memory(mem_fs)
    base = {
        "review_id": "r1",
        "task_id": "t1",
        "verdict": "publish",
        "title": "x",
        "reasoning": "y",
        "proposed_name": "n",
        "proposed_description": "d",
        "timestamp": "2026-04-23T00:00:00Z",
    }
    null_row = dict(base, review_id="r-null", bundle_file_names=None)
    str_row = dict(base, review_id="r-str", bundle_file_names="a\nb")
    mixed_row = dict(base, review_id="r-mixed", bundle_file_names=["a", 42, "b"])
    noncoercible_row = dict(base, review_id="r-bad", bundle_file_names=123)

    mem_fs[str(FAKE_DIR / "artifact_reviews.jsonl")] = (
        json.dumps(null_row) + "\n"
        + json.dumps(str_row) + "\n"
        + json.dumps(mixed_row) + "\n"
        + json.dumps(noncoercible_row) + "\n"
    )

    reviews = mem._load_all_reviews()
    by_id = {r.review_id: r for r in reviews}
    assert by_id["r-null"].bundle_file_names == []
    assert by_id["r-str"].bundle_file_names == ["a", "b"]
    assert by_id["r-mixed"].bundle_file_names == ["a", "b"]
    assert by_id["r-bad"].bundle_file_names == []


# ---------------------------------------------------------------------------
# Memory._coerce_str_list (defensive list[str] coercion)
# ---------------------------------------------------------------------------


def test_coerce_str_list_string_branch_strips_each_line() -> None:
    """Multi-line strings are split, blank lines dropped, kept lines stripped."""
    result = Memory._coerce_str_list(
        "  README.md  \nLICENSE\n   \n  docs/index.md",
    )
    assert result == ["README.md", "LICENSE", "docs/index.md"]


def test_coerce_str_list_string_branch_empty_inputs_yield_empty_list() -> None:
    assert Memory._coerce_str_list("") == []
    assert Memory._coerce_str_list("\n   \n") == []


def test_coerce_str_list_list_branch_does_not_strip_entries() -> None:
    """Fix is scoped to the string branch: list entries pass through verbatim."""
    assert Memory._coerce_str_list(["  a  ", "b"]) == ["  a  ", "b"]


def test_coerce_str_list_list_branch_filters_non_strings() -> None:
    assert Memory._coerce_str_list(["a", 42, "b", None]) == ["a", "b"]


def test_coerce_str_list_other_types_return_empty() -> None:
    assert Memory._coerce_str_list(None) == []
    assert Memory._coerce_str_list(42) == []
    assert Memory._coerce_str_list({}) == []


def test_update_artifact_review_verdict_mutates_row(
    mem_fs: dict[str, str],
) -> None:
    """Demoting a publish row clears publish-only fields and rewrites the file."""
    mem = _make_memory(mem_fs)
    mem.write_artifact_review(
        _make_review(review_id="r-pub", task_id="t1", verdict="publish"),
    )
    mem.write_artifact_review(
        _make_review(review_id="r-skp", task_id="t2", verdict="skip"),
    )

    changed = mem.update_artifact_review_verdict(
        "r-pub", "improve",
        "original reason [scrub_blocked:anthropic_key]",
    )
    assert changed is True

    reviews = {r.review_id: r for r in mem._load_all_reviews()}
    demoted = reviews["r-pub"]
    untouched = reviews["r-skp"]

    assert demoted.verdict == "improve"
    assert demoted.reasoning == "original reason [scrub_blocked:anthropic_key]"
    assert demoted.proposed_name is None
    assert demoted.proposed_description is None
    assert demoted.proposed_readme is None
    # Other rows must be preserved verbatim.
    assert untouched.verdict == "skip"
    assert untouched.proposed_name == "nice-thing"


def test_update_artifact_review_verdict_returns_false_when_missing(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    mem.write_artifact_review(_make_review(review_id="r-known"))
    assert (
        mem.update_artifact_review_verdict(
            "r-unknown", "improve", "x",
        )
        is False
    )
    # Existing row is not mutated.
    [r] = mem._load_all_reviews()
    assert r.verdict == "publish"


def test_update_artifact_review_verdict_type_error_on_non_string(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    mem.write_artifact_review(_make_review(review_id="r-pub"))
    with pytest.raises(TypeError, match="reasoning"):
        mem.update_artifact_review_verdict(
            "r-pub", "improve", None,  # type: ignore[arg-type]
        )


def test_write_publication_proposal_round_trip(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal())

    proposals = mem._load_all_proposals()
    assert len(proposals) == 1
    assert proposals[0].proposal_id == "p1"
    assert proposals[0].status == "pending_approval"


def test_write_publication_proposal_empty_id_raises(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    with pytest.raises(ValueError, match="proposal_id"):
        mem.write_publication_proposal(_make_proposal(proposal_id=""))


def test_write_publication_proposal_duplicate_raises(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="dup-id"))

    with pytest.raises(DuplicateProposalError, match="dup-id"):
        mem.write_publication_proposal(_make_proposal(proposal_id="dup-id", task_id="t2"))

    # File still contains only one entry
    assert sum(1 for p in mem._load_all_proposals() if p.proposal_id == "dup-id") == 1


def test_write_publication_proposal_skips_blank_lines_in_dup_check(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    proposals_path = str(FAKE_DIR / "publication_proposals.jsonl")
    mem_fs[proposals_path] = "\n   \n"

    mem.write_publication_proposal(_make_proposal(proposal_id="p-new"))
    assert mem.get_proposal("p-new") is not None


def test_write_publication_proposal_skips_corrupt_in_dup_check(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    proposals_path = str(FAKE_DIR / "publication_proposals.jsonl")
    mem_fs[proposals_path] = "NOT JSON\n"

    mem.write_publication_proposal(_make_proposal(proposal_id="p-new"))
    assert mem.get_proposal("p-new") is not None


def test_load_all_proposals_missing_file(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    assert mem._load_all_proposals() == []


def test_load_all_proposals_drops_unknown_fields(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    raw = {
        "proposal_id": "p1",
        "task_id": "t1",
        "review_id": "r1",
        "proposed_name": "nice-thing",
        "description": "a thing",
        "proposed_readme": None,
        "publishable_files": ["README.md"],
        "status": "pending_approval",
        "created_at": "2026-04-23T00:00:00Z",
        "legacy_unused_field": "ignored",
    }
    mem_fs[str(FAKE_DIR / "publication_proposals.jsonl")] = json.dumps(raw) + "\n"
    [p] = mem._load_all_proposals()
    assert p.proposal_id == "p1"


def test_load_all_proposals_skips_corrupt_and_blank_lines(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "proposal_id": "p1",
        "task_id": "t1",
        "review_id": "r1",
        "proposed_name": "nice-thing",
        "description": "a thing",
        "proposed_readme": None,
        "publishable_files": ["README.md"],
        "status": "pending_approval",
        "created_at": "2026-04-23T00:00:00Z",
    })
    mem_fs[str(FAKE_DIR / "publication_proposals.jsonl")] = (
        valid + "\n" + "corrupt\n" + "\n" + valid.replace("p1", "p2") + "\n"
    )
    assert len(mem._load_all_proposals()) == 2


def test_load_all_proposals_coerces_list_fields(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    base = {
        "proposal_id": "p-coerce",
        "task_id": "t1",
        "review_id": "r1",
        "proposed_name": "nice-thing",
        "description": "a thing",
        "proposed_readme": None,
        "status": "pending_approval",
        "created_at": "2026-04-23T00:00:00Z",
        "publishable_files": None,
        "scrub_findings": "finding-a\nfinding-b",
    }
    mem_fs[str(FAKE_DIR / "publication_proposals.jsonl")] = json.dumps(base) + "\n"

    [p] = mem._load_all_proposals()
    assert p.publishable_files == []
    assert p.scrub_findings == ["finding-a", "finding-b"]


def test_load_all_proposals_drops_empty_proposal_id(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    raw = {
        "proposal_id": "",
        "task_id": "t1",
        "review_id": "r1",
        "proposed_name": "nice-thing",
        "description": "a thing",
        "proposed_readme": None,
        "publishable_files": [],
        "status": "pending_approval",
        "created_at": "2026-04-23T00:00:00Z",
    }
    mem_fs[str(FAKE_DIR / "publication_proposals.jsonl")] = json.dumps(raw) + "\n"

    assert mem._load_all_proposals() == []


def test_get_publication_proposals_no_filter_returns_all(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    for i in range(3):
        mem.write_publication_proposal(_make_proposal(proposal_id=f"p{i}"))

    proposals = mem.get_publication_proposals()
    assert [p.proposal_id for p in proposals] == ["p0", "p1", "p2"]


def test_get_publication_proposals_filters_by_status(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))
    mem.write_publication_proposal(_make_proposal(proposal_id="p2"))
    mem.update_proposal_status("p1", "approved")

    pending = mem.get_publication_proposals(status_filter="pending_approval")
    approved = mem.get_publication_proposals(status_filter="approved")
    rejected = mem.get_publication_proposals(status_filter="rejected")

    assert [p.proposal_id for p in pending] == ["p2"]
    assert [p.proposal_id for p in approved] == ["p1"]
    assert rejected == []


def test_get_publication_proposals_n_tails(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    for i in range(5):
        mem.write_publication_proposal(_make_proposal(proposal_id=f"p{i}"))

    tail = mem.get_publication_proposals(n=2)
    assert [p.proposal_id for p in tail] == ["p3", "p4"]


def test_get_publication_proposals_n_zero_returns_empty(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal())
    assert mem.get_publication_proposals(n=0) == []


def test_get_proposal_returns_match(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))
    mem.write_publication_proposal(_make_proposal(proposal_id="p2"))

    p = mem.get_proposal("p2")
    assert p is not None
    assert p.proposal_id == "p2"


def test_get_proposal_returns_none_for_unknown(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal())
    assert mem.get_proposal("missing") is None


def test_update_proposal_status_mutates_each_field(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))

    assert mem.update_proposal_status(
        "p1",
        "executing",
        reviewed_at="2026-04-23T01:00:00Z",
        rejection_reason=None,
        executing_started_at="2026-04-23T01:01:00Z",
        published_url=None,
        scrub_findings=["finding-1"],
        error=None,
    )

    p = mem.get_proposal("p1")
    assert p is not None
    assert p.status == "executing"
    assert p.reviewed_at == "2026-04-23T01:00:00Z"
    assert p.executing_started_at == "2026-04-23T01:01:00Z"
    assert p.scrub_findings == ["finding-1"]


def test_update_proposal_status_published(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))
    assert mem.update_proposal_status(
        "p1",
        "published",
        published_url="https://github.com/thomas-sodon/nice-thing",
    )
    p = mem.get_proposal("p1")
    assert p is not None
    assert p.status == "published"
    assert p.published_url == "https://github.com/thomas-sodon/nice-thing"


def test_update_proposal_status_failed(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))
    assert mem.update_proposal_status("p1", "failed", error="push failed")
    p = mem.get_proposal("p1")
    assert p is not None
    assert p.status == "failed"
    assert p.error == "push failed"


def test_update_proposal_status_rejected_with_reason(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))
    assert mem.update_proposal_status("p1", "rejected", rejection_reason="not ready")
    p = mem.get_proposal("p1")
    assert p is not None
    assert p.status == "rejected"
    assert p.rejection_reason == "not ready"


def test_update_proposal_status_leaves_unset_fields_unchanged(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))
    mem.update_proposal_status("p1", "rejected", rejection_reason="bad")

    # Approve it now without passing rejection_reason — it should persist.
    mem.update_proposal_status("p1", "pending_approval")
    p = mem.get_proposal("p1")
    assert p is not None
    assert p.rejection_reason == "bad"


def test_update_proposal_status_unknown_id_returns_false(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))
    assert mem.update_proposal_status("missing", "approved") is False


def test_update_proposal_status_expect_status_match_succeeds(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))
    assert mem.update_proposal_status(
        "p1", "approved", expect_status="pending_approval",
    ) is True
    p = mem.get_proposal("p1")
    assert p is not None and p.status == "approved"


def test_update_proposal_status_expect_status_mismatch_returns_false(
    mem_fs: dict[str, str],
) -> None:
    """Lock-checked status mismatch returns False without mutating the row."""
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))
    # First surface wins: pending_approval → approved.
    assert mem.update_proposal_status(
        "p1", "approved", expect_status="pending_approval",
        reviewed_at="2026-04-25T00:00:00+00:00",
    ) is True
    # Second surface tries to flip to rejected, expecting still-pending.
    assert mem.update_proposal_status(
        "p1", "rejected", expect_status="pending_approval",
        rejection_reason="too late",
        reviewed_at="2026-04-25T00:00:01+00:00",
    ) is False
    # Earlier write is preserved verbatim — no rejection_reason leaked.
    p = mem.get_proposal("p1")
    assert p is not None
    assert p.status == "approved"
    assert p.rejection_reason is None


def test_update_proposal_status_expect_status_unknown_id_returns_false(
    mem_fs: dict[str, str],
) -> None:
    """Missing row returns False even with expect_status set — no exception."""
    mem = _make_memory(mem_fs)
    assert mem.update_proposal_status(
        "no-such-id", "approved", expect_status="pending_approval",
    ) is False


def test_update_proposal_status_rejects_non_string_reviewed_at(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))
    with pytest.raises(TypeError, match="reviewed_at"):
        mem.update_proposal_status("p1", "approved", reviewed_at=42)  # type: ignore[arg-type]


def test_update_proposal_status_rejects_non_string_rejection_reason(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))
    with pytest.raises(TypeError, match="rejection_reason"):
        mem.update_proposal_status("p1", "rejected", rejection_reason=42)  # type: ignore[arg-type]


def test_update_proposal_status_rejects_non_string_executing_started_at(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))
    with pytest.raises(TypeError, match="executing_started_at"):
        mem.update_proposal_status("p1", "executing", executing_started_at=42)  # type: ignore[arg-type]


def test_update_proposal_status_rejects_non_string_published_url(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))
    with pytest.raises(TypeError, match="published_url"):
        mem.update_proposal_status("p1", "published", published_url=42)  # type: ignore[arg-type]


def test_update_proposal_status_rejects_bad_scrub_findings(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))
    with pytest.raises(TypeError, match="scrub_findings"):
        mem.update_proposal_status("p1", "approved", scrub_findings=["ok", 42])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="scrub_findings"):
        mem.update_proposal_status("p1", "approved", scrub_findings="not a list")  # type: ignore[arg-type]


def test_update_proposal_status_rejects_non_string_error(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))
    with pytest.raises(TypeError, match="error"):
        mem.update_proposal_status("p1", "failed", error=42)  # type: ignore[arg-type]


def test_update_proposal_status_uses_file_lock(mem_fs: dict[str, str]) -> None:
    """update_proposal_status acquires the publication_proposals file lock."""
    mem = _make_memory(mem_fs)
    mem.write_publication_proposal(_make_proposal(proposal_id="p1"))

    called_with: list[Any] = []

    @contextmanager
    def tracking_lock(path: Any) -> Iterator[None]:
        called_with.append(path)
        yield

    with patch("maestro.memory.file_lock", tracking_lock):
        mem.update_proposal_status("p1", "approved")

    assert len(called_with) == 1
    assert str(called_with[0]).endswith("publication_proposals.jsonl")


def test_write_publication_proposal_uses_file_lock(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)

    called_with: list[Any] = []

    @contextmanager
    def tracking_lock(path: Any) -> Iterator[None]:
        called_with.append(path)
        yield

    with patch("maestro.memory.file_lock", tracking_lock):
        mem.write_publication_proposal(_make_proposal())

    assert len(called_with) == 1
    assert str(called_with[0]).endswith("publication_proposals.jsonl")


# ---------------------------------------------------------------------------
# Audit history
# ---------------------------------------------------------------------------


def test_audit_result_roundtrip(mem_fs: dict[str, str]) -> None:
    """``append_audit_result`` + ``get_most_recent_audit`` roundtrip cleanly."""
    from maestro.audit_checks import AuditFinding
    from maestro.memory import AuditResult

    mem = _make_memory(mem_fs)
    findings = [
        AuditFinding(
            kind="contradicted_claim",
            statement="s1",
            cites=["t1", "t2"],
            rule="success_claim_vs_rate",
        ),
        AuditFinding(
            kind="undeclared_limitation",
            statement="s2",
            cites=[],
            rule="repeated_error_without_limitation",
        ),
    ]
    result = AuditResult(
        iteration=42,
        timestamp="2026-04-24T00:00:00Z",
        findings=findings,
        rules_fired=[
            "repeated_error_without_limitation",
            "success_claim_vs_rate",
        ],
    )
    mem.append_audit_result(result)

    latest = mem.get_most_recent_audit()
    assert latest is not None
    assert latest.iteration == 42
    assert latest.timestamp == "2026-04-24T00:00:00Z"
    assert len(latest.findings) == 2
    assert latest.findings[0].kind == "contradicted_claim"
    assert latest.findings[0].statement == "s1"
    assert latest.findings[0].cites == ["t1", "t2"]
    assert latest.findings[0].rule == "success_claim_vs_rate"
    assert latest.findings[1].cites == []
    assert latest.rules_fired == [
        "repeated_error_without_limitation",
        "success_claim_vs_rate",
    ]


def test_get_most_recent_audit_returns_latest(mem_fs: dict[str, str]) -> None:
    """Multiple appended results: only the latest row is returned."""
    from maestro.memory import AuditResult

    mem = _make_memory(mem_fs)
    mem.append_audit_result(AuditResult(iteration=1, timestamp="t1"))
    mem.append_audit_result(AuditResult(iteration=2, timestamp="t2"))
    mem.append_audit_result(AuditResult(iteration=3, timestamp="t3"))

    latest = mem.get_most_recent_audit()
    assert latest is not None
    assert latest.iteration == 3


def test_get_most_recent_audit_none_when_missing(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    assert mem.get_most_recent_audit() is None


def test_empty_findings_persists(mem_fs: dict[str, str]) -> None:
    """Zero-finding audit cycles still persist an AuditResult row."""
    from maestro.memory import AuditResult

    mem = _make_memory(mem_fs)
    mem.append_audit_result(
        AuditResult(iteration=5, timestamp="t5", findings=[], rules_fired=[]),
    )

    latest = mem.get_most_recent_audit()
    assert latest is not None
    assert latest.iteration == 5
    assert latest.findings == []
    assert latest.rules_fired == []


def test_get_most_recent_audit_handles_legacy_row_without_rule(
    mem_fs: dict[str, str],
) -> None:
    """Legacy JSONL rows missing the ``rule`` field default cleanly."""
    path = "/fake/mem/audit_history.jsonl"
    # Legacy-shaped row: finding without ``rule`` field; result
    # without ``rules_fired``.
    legacy_row = json.dumps({
        "iteration": 11,
        "timestamp": "legacy_ts",
        "findings": [
            {
                "kind": "contradicted_claim",
                "statement": "legacy",
                "cites": ["tL1"],
            },
        ],
    })
    mem_fs[path] = legacy_row + "\n"

    mem = _make_memory(mem_fs)
    latest = mem.get_most_recent_audit()
    assert latest is not None
    assert latest.iteration == 11
    assert len(latest.findings) == 1
    # Missing ``rule`` becomes the dataclass default of "".
    assert latest.findings[0].rule == ""
    # Missing ``rules_fired`` becomes the dataclass default of [].
    assert latest.rules_fired == []


def test_get_most_recent_audit_drops_finding_with_invalid_kind(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """A finding whose ``kind`` is outside ``AuditKind`` is dropped.

    The row itself still parses (a bad kind is not a corrupt row); only
    the offending finding is filtered out, so the valid finding survives.
    A warning is logged so the drop stays visible.
    """
    import logging

    path = "/fake/mem/audit_history.jsonl"
    row = json.dumps({
        "iteration": 12,
        "timestamp": "ts12",
        "findings": [
            {
                "kind": "contradicted_claim",
                "statement": "valid",
                "cites": ["tV1"],
            },
            {
                "kind": "garbage",
                "statement": "bad kind",
                "cites": ["tB1"],
            },
        ],
    })
    mem_fs[path] = row + "\n"

    mem = _make_memory(mem_fs)
    with caplog.at_level(logging.WARNING):
        latest = mem.get_most_recent_audit()
    assert latest is not None
    assert latest.iteration == 12
    # Only the valid finding survives; the "garbage" kind is dropped.
    assert len(latest.findings) == 1
    assert latest.findings[0].kind == "contradicted_claim"
    assert latest.findings[0].statement == "valid"
    # The drop is surfaced as a warning naming the offending kind.
    assert any(
        "invalid kind" in r.message and "garbage" in r.message
        for r in caplog.records
    )


def test_get_most_recent_audit_skips_row_when_all_findings_invalid(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """A row whose *every* declared finding is malformed is treated as corrupt.

    Dropping findings one at a time must not surface an empty-but-valid
    cycle that masks corruption as "no findings fired". When a row declared
    findings but none survive parsing, the row is skipped so the caller
    falls through to an older valid row. A genuine zero-finding cycle
    (empty ``findings``) is unaffected — see
    :func:`test_empty_findings_persists`.
    """
    import logging

    path = "/fake/mem/audit_history.jsonl"
    older = json.dumps({
        "iteration": 20,
        "timestamp": "ts20",
        "findings": [
            {"kind": "contradicted_claim", "statement": "old valid", "cites": []},
        ],
    })
    # Newer row: one invalid-kind finding and one missing ``kind`` entirely,
    # so nothing survives.
    newer_all_bad = json.dumps({
        "iteration": 21,
        "timestamp": "ts21",
        "findings": [
            {"kind": "garbage", "statement": "bad kind", "cites": []},
            {"statement": "missing kind", "cites": []},
        ],
    })
    mem_fs[path] = older + "\n" + newer_all_bad + "\n"

    mem = _make_memory(mem_fs)
    with caplog.at_level(logging.WARNING):
        latest = mem.get_most_recent_audit()
    assert latest is not None
    # The all-malformed newer row is skipped; the older valid row wins.
    assert latest.iteration == 20
    assert len(latest.findings) == 1
    assert latest.findings[0].statement == "old valid"
    # The corrupt row is surfaced as a warning, not silently emptied.
    assert any(
        "no salvageable findings" in r.message for r in caplog.records
    )


def test_get_most_recent_audit_skips_corrupt_rows(mem_fs: dict[str, str]) -> None:
    """Corrupt JSONL rows are skipped; the most recent well-formed row wins."""
    from maestro.memory import AuditResult

    mem = _make_memory(mem_fs)
    mem.append_audit_result(AuditResult(iteration=1, timestamp="t1"))
    # Append a corrupted line between two valid rows.
    path = str(mem._audit_history_path)
    mem_fs[path] += "{not-valid-json\n"
    mem.append_audit_result(AuditResult(iteration=3, timestamp="t3"))

    latest = mem.get_most_recent_audit()
    assert latest is not None
    assert latest.iteration == 3


def test_get_most_recent_audit_returns_none_when_all_corrupt(
    mem_fs: dict[str, str],
) -> None:
    """All rows unparseable → ``None`` (no silent fallback to empty result)."""
    path = "/fake/mem/audit_history.jsonl"
    mem_fs[path] = "{broken\n{also broken\n"
    mem = _make_memory(mem_fs)
    assert mem.get_most_recent_audit() is None


def test_get_most_recent_audit_returns_none_on_empty_file(
    mem_fs: dict[str, str],
) -> None:
    """An existing file with only blank/whitespace lines returns None."""
    path = "/fake/mem/audit_history.jsonl"
    mem_fs[path] = "\n   \n\t\n"
    mem = _make_memory(mem_fs)
    assert mem.get_most_recent_audit() is None


def test_get_recent_audit_results_returns_newest_last_in_file_order(
    mem_fs: dict[str, str],
) -> None:
    """Returned list preserves file order (newest-last) within the limit window."""
    from maestro.memory import AuditResult

    mem = _make_memory(mem_fs)
    for i in range(1, 6):
        mem.append_audit_result(AuditResult(iteration=i, timestamp=f"t{i}"))

    rows = mem.get_recent_audit_results(3)
    assert [r.iteration for r in rows] == [3, 4, 5]


def test_get_recent_audit_results_empty_when_missing(
    mem_fs: dict[str, str],
) -> None:
    """Missing file → ``[]`` (mirrors ``get_most_recent_audit``'s ``None``)."""
    mem = _make_memory(mem_fs)
    assert mem.get_recent_audit_results(10) == []


def test_get_recent_audit_results_zero_limit_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """``limit <= 0`` short-circuits without reading the file."""
    from maestro.memory import AuditResult

    mem = _make_memory(mem_fs)
    mem.append_audit_result(AuditResult(iteration=1, timestamp="t1"))
    assert mem.get_recent_audit_results(0) == []
    assert mem.get_recent_audit_results(-3) == []


def test_get_recent_audit_results_skips_corrupt_rows(
    mem_fs: dict[str, str],
) -> None:
    """Corrupt JSONL rows are skipped; the surviving rows still come back."""
    from maestro.memory import AuditResult

    mem = _make_memory(mem_fs)
    mem.append_audit_result(AuditResult(iteration=1, timestamp="t1"))
    path = str(mem._audit_history_path)
    mem_fs[path] += "{not-valid-json\n"
    mem.append_audit_result(AuditResult(iteration=3, timestamp="t3"))

    rows = mem.get_recent_audit_results(5)
    assert [r.iteration for r in rows] == [1, 3]


def test_get_recent_audit_results_blank_lines_ignored(
    mem_fs: dict[str, str],
) -> None:
    """Blank-only files yield ``[]`` like ``get_most_recent_audit``."""
    path = "/fake/mem/audit_history.jsonl"
    mem_fs[path] = "\n   \n\t\n"
    mem = _make_memory(mem_fs)
    assert mem.get_recent_audit_results(10) == []


def test_get_recent_audit_results_limit_exceeds_history(
    mem_fs: dict[str, str],
) -> None:
    """``limit`` larger than the file: every row comes back, newest-last."""
    from maestro.memory import AuditResult

    mem = _make_memory(mem_fs)
    mem.append_audit_result(AuditResult(iteration=10, timestamp="t10"))
    mem.append_audit_result(AuditResult(iteration=11, timestamp="t11"))
    rows = mem.get_recent_audit_results(50)
    assert [r.iteration for r in rows] == [10, 11]


def test_get_recent_audit_results_returns_empty_on_os_error(
    mem_fs: dict[str, str],
) -> None:
    """OSError reading the file is swallowed and ``[]`` returned."""
    path = "/fake/mem/audit_history.jsonl"
    mem_fs[path] = "{}"

    mem = _make_memory(mem_fs)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("simulated")

    with patch.object(Path, "read_text", _boom):
        assert mem.get_recent_audit_results(5) == []


def test_get_recent_audit_results_skips_non_dict_finding_entry(
    mem_fs: dict[str, str],
) -> None:
    """Non-dict entries inside ``findings`` are silently dropped, row survives."""
    path = "/fake/mem/audit_history.jsonl"
    mem_fs[path] = json.dumps({
        "iteration": 8,
        "timestamp": "tx",
        "findings": [
            "stray-string-not-a-finding",
            {
                "kind": "contradicted_claim",
                "statement": "valid",
                "cites": [],
                "rule": "r",
            },
        ],
    }) + "\n"
    mem = _make_memory(mem_fs)
    rows = mem.get_recent_audit_results(5)
    assert len(rows) == 1
    assert len(rows[0].findings) == 1
    assert rows[0].findings[0].statement == "valid"


def test_get_most_recent_audit_skips_non_dict_finding_entry(
    mem_fs: dict[str, str],
) -> None:
    """Findings array with non-dict entries get filtered out of the result."""
    path = "/fake/mem/audit_history.jsonl"
    mem_fs[path] = json.dumps({
        "iteration": 9,
        "timestamp": "tx",
        "findings": [
            "not a dict",
            {"kind": "contradicted_claim", "statement": "s", "cites": ["t1"],
             "rule": "r"},
            42,
        ],
    }) + "\n"
    mem = _make_memory(mem_fs)
    latest = mem.get_most_recent_audit()
    assert latest is not None
    assert len(latest.findings) == 1
    assert latest.findings[0].statement == "s"


def test_get_most_recent_audit_returns_none_on_os_error(
    mem_fs: dict[str, str],
) -> None:
    """OSError reading the file is swallowed and ``None`` returned."""
    path = "/fake/mem/audit_history.jsonl"
    mem_fs[path] = "{}"

    mem = _make_memory(mem_fs)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("simulated")

    with patch.object(Path, "read_text", _boom):
        assert mem.get_most_recent_audit() is None


def test_parse_audit_line_valid_row(mem_fs: dict[str, str]) -> None:
    """``_parse_audit_line`` round-trips a valid JSON row into an AuditResult."""
    from maestro.audit_checks import AuditFinding
    from maestro.memory import AuditResult

    mem = _make_memory(mem_fs)
    row = json.dumps({
        "iteration": 7,
        "timestamp": "2026-04-27T00:00:00Z",
        "findings": [
            {
                "kind": "contradicted_claim",
                "statement": "s1",
                "cites": ["t1"],
                "rule": "success_claim_vs_rate",
            },
        ],
        "rules_fired": ["success_claim_vs_rate"],
    })
    result = mem._parse_audit_line(row)
    assert result is not None
    assert isinstance(result, AuditResult)
    assert result.iteration == 7
    assert result.timestamp == "2026-04-27T00:00:00Z"
    assert len(result.findings) == 1
    assert isinstance(result.findings[0], AuditFinding)
    assert result.findings[0].kind == "contradicted_claim"
    assert result.findings[0].statement == "s1"
    assert result.findings[0].cites == ["t1"]
    assert result.findings[0].rule == "success_claim_vs_rate"
    assert result.rules_fired == ["success_claim_vs_rate"]


def test_parse_audit_line_corrupt_row_returns_none(
    mem_fs: dict[str, str],
) -> None:
    """``_parse_audit_line`` returns ``None`` on unparseable input."""
    mem = _make_memory(mem_fs)
    assert mem._parse_audit_line("{not-valid-json") is None
    assert mem._parse_audit_line("") is None


def test_parse_audit_line_non_dict_payload_returns_none(
    mem_fs: dict[str, str],
) -> None:
    """A valid-JSON but non-dict row (bare int / string) is corrupt → ``None``.

    ``json.loads`` succeeds, so the wrapper delegates to ``_parse_audit_dict``
    whose structural ``except`` fires — ``.get`` on a non-mapping raises
    ``AttributeError`` — and the row is skipped rather than crashing the reader.
    """
    mem = _make_memory(mem_fs)
    assert mem._parse_audit_line("42") is None
    assert mem._parse_audit_line('"just a string"') is None


# ---------------------------------------------------------------------------
# Audit-history backend seam (proposal 106)
# ---------------------------------------------------------------------------


def test_jsonl_audit_history_ledger_round_trip(mem_fs: dict[str, str]) -> None:
    """The default JSONL adapter records to and loads from audit_history.jsonl."""
    from maestro.memory import AuditResult, JsonlAuditHistoryLedger

    ledger = JsonlAuditHistoryLedger(FAKE_DIR)
    a = AuditResult(iteration=1, timestamp="2026-08-15T00:00:00Z")
    b = AuditResult(iteration=2, timestamp="2026-08-15T01:00:00Z")
    ledger.record(a)
    ledger.record(b)
    # Lands on the JSONL sidecar and reads back oldest-first.
    assert str(FAKE_DIR / "audit_history.jsonl") in mem_fs
    assert ledger.load_all() == [a, b]


def test_load_all_audit_history_missing_file_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    from maestro.memory import load_all_audit_history

    assert load_all_audit_history(FAKE_DIR) == []


def test_load_all_audit_history_oldest_first(mem_fs: dict[str, str]) -> None:
    from maestro.memory import AuditResult, JsonlAuditHistoryLedger, load_all_audit_history

    ledger = JsonlAuditHistoryLedger(FAKE_DIR)
    for i in (1, 2, 3):
        ledger.record(AuditResult(iteration=i, timestamp=f"t{i}"))
    assert [r.iteration for r in load_all_audit_history(FAKE_DIR)] == [1, 2, 3]


def test_load_all_audit_history_skips_corrupt_never_raises(
    mem_fs: dict[str, str],
) -> None:
    """Every corrupt row — interior *or* trailing — is skipped, never raised.

    The audit reader preserves the two audit readers' historical tolerance: a
    malformed line anywhere is dropped and the surviving rows still come back.
    """
    from maestro.memory import AuditResult, load_all_audit_history

    good1 = json.dumps(asdict(AuditResult(iteration=1, timestamp="t1")))
    good2 = json.dumps(asdict(AuditResult(iteration=3, timestamp="t3")))
    mem_fs[str(FAKE_DIR / "audit_history.jsonl")] = (
        good1 + "\nBROKEN interior\n" + good2 + '\n{"partial trailing'
    )
    history = load_all_audit_history(FAKE_DIR)
    assert [r.iteration for r in history] == [1, 3]


def test_load_all_audit_history_returns_empty_on_os_error(
    mem_fs: dict[str, str],
) -> None:
    """An ``OSError`` reading the file is swallowed and ``[]`` returned."""
    from maestro.memory import load_all_audit_history

    mem_fs[str(FAKE_DIR / "audit_history.jsonl")] = "{}"

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("simulated")

    with patch.object(Path, "read_text", _boom):
        assert load_all_audit_history(FAKE_DIR) == []


def test_audit_history_backend_parity(mem_fs: dict[str, str]) -> None:
    """JSONL and SQLite adapters return identical ``AuditResult`` lists.

    A shared corpus including a multi-finding cycle round-trips identically
    through both substrates, so flipping ``[paths] sqlite_state_enabled`` never
    changes what the audit readers see.
    """
    from maestro.audit_checks import AuditFinding
    from maestro.memory import AuditResult, JsonlAuditHistoryLedger
    from maestro.state_store import SqliteAuditHistoryLedger, StateStore

    corpus = [
        AuditResult(iteration=1, timestamp="2026-08-15T00:00:00Z"),
        AuditResult(
            iteration=2,
            timestamp="2026-08-15T01:00:00Z",
            findings=[
                AuditFinding(
                    kind="contradicted_claim",
                    statement="s",
                    cites=["t1", "t2"],
                    rule="success_claim_vs_rate",
                ),
            ],
            rules_fired=["success_claim_vs_rate"],
        ),
    ]
    jsonl = JsonlAuditHistoryLedger(FAKE_DIR)
    sqlite = SqliteAuditHistoryLedger(StateStore(":memory:"))
    for result in corpus:
        jsonl.record(result)
        sqlite.record(result)
    assert jsonl.load_all() == corpus
    assert sqlite.load_all() == corpus
    assert jsonl.load_all() == sqlite.load_all()


def test_uninjected_memory_defaults_to_jsonl_audit_ledger(
    mem_fs: dict[str, str],
) -> None:
    """An un-injected ``Memory`` keeps today's JSONL behaviour unchanged."""
    from maestro.memory import AuditResult, JsonlAuditHistoryLedger

    mem = Memory(FAKE_DIR)
    assert isinstance(mem._audit_history, JsonlAuditHistoryLedger)
    r = AuditResult(iteration=1, timestamp="t1")
    mem.append_audit_result(r)
    assert mem.get_most_recent_audit() == r
    # Persisted to the JSONL sidecar (the flag-off substrate).
    assert str(FAKE_DIR / "audit_history.jsonl") in mem_fs


def test_injected_audit_history_backend_is_used(mem_fs: dict[str, str]) -> None:
    """An injected backend receives the rows; the JSONL sidecar stays empty."""
    from maestro.memory import AuditResult
    from maestro.state_store import SqliteAuditHistoryLedger, StateStore

    sqlite = SqliteAuditHistoryLedger(StateStore(":memory:"))
    mem = Memory(FAKE_DIR, audit_history=sqlite)
    r = AuditResult(iteration=4, timestamp="t4")
    mem.append_audit_result(r)
    assert mem.get_most_recent_audit() == r
    assert mem.get_recent_audit_results(5) == [r]
    # Routed through the injected backend, not the JSONL sidecar.
    assert sqlite.load_all() == [r]
    assert str(FAKE_DIR / "audit_history.jsonl") not in mem_fs


# ---------------------------------------------------------------------------
# QuestionResolution (linked-question feedback loop)
# ---------------------------------------------------------------------------


def _make_resolution(
    *,
    resolution_id: str = "r1",
    iteration: int = 1,
    question: str = "What am I capable of?",
    task_id: str = "t1",
    outcome: str = "success",
    disposition: str = "removed",
    finding: str = "I can refactor multi-file changes.",
    timestamp: str = "2026-04-26T00:00:00+00:00",
    refined_to: str | None = None,
):  # type: ignore[no-untyped-def]
    from maestro.memory import QuestionResolution
    return QuestionResolution(
        resolution_id=resolution_id,
        iteration=iteration,
        timestamp=timestamp,
        question=question,
        task_id=task_id,
        outcome=outcome,  # type: ignore[arg-type]
        disposition=disposition,  # type: ignore[arg-type]
        finding=finding,
        refined_to=refined_to,
    )


def test_question_resolution_roundtrip(mem_fs: dict[str, str]) -> None:
    """append_question_resolution + get_recent_question_resolutions roundtrip."""
    mem = _make_memory(mem_fs)
    row = _make_resolution()
    mem.append_question_resolution(row)

    path = str(mem._question_resolutions_path)
    assert path in mem_fs
    assert "What am I capable of?" in mem_fs[path]

    rows = mem.get_recent_question_resolutions(10)
    assert len(rows) == 1
    assert rows[0].resolution_id == "r1"
    assert rows[0].disposition == "removed"
    assert rows[0].finding == "I can refactor multi-file changes."


def test_append_question_resolution_rejects_empty_id(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    with pytest.raises(ValueError, match="resolution_id"):
        mem.append_question_resolution(_make_resolution(resolution_id=""))


def test_append_question_resolution_truncates_long_finding(
    mem_fs: dict[str, str],
) -> None:
    """Finding text over the cap is head-truncated on append."""
    from maestro.memory import _QUESTION_RESOLUTION_FINDING_MAX_CHARS

    mem = _make_memory(mem_fs)
    long_finding = "A" * (_QUESTION_RESOLUTION_FINDING_MAX_CHARS + 200)
    mem.append_question_resolution(_make_resolution(finding=long_finding))

    rows = mem.get_recent_question_resolutions(1)
    assert len(rows[0].finding) == _QUESTION_RESOLUTION_FINDING_MAX_CHARS


def test_append_question_resolution_truncates_long_refined_to(
    mem_fs: dict[str, str],
) -> None:
    """``refined_to`` over the cap is head-truncated on append.

    Mirrors the ``finding`` cap so a pathological successor wording
    cannot bloat the JSONL row.
    """
    from maestro.memory import _QUESTION_RESOLUTION_FINDING_MAX_CHARS

    mem = _make_memory(mem_fs)
    long_successor = "Q" * (_QUESTION_RESOLUTION_FINDING_MAX_CHARS + 50)
    mem.append_question_resolution(_make_resolution(
        disposition="refined",
        refined_to=long_successor,
    ))

    rows = mem.get_recent_question_resolutions(1)
    assert (
        len(rows[0].refined_to or "")
        == _QUESTION_RESOLUTION_FINDING_MAX_CHARS
    )


def test_append_question_resolution_preserves_refined_to_when_short(
    mem_fs: dict[str, str],
) -> None:
    """``refined_to`` under the cap roundtrips verbatim."""
    mem = _make_memory(mem_fs)
    mem.append_question_resolution(_make_resolution(
        disposition="refined",
        refined_to="Successor question?",
    ))

    rows = mem.get_recent_question_resolutions(1)
    assert rows[0].refined_to == "Successor question?"


def test_append_question_resolution_truncates_long_question(
    mem_fs: dict[str, str],
) -> None:
    """``question`` over the cap is head-truncated on append.

    Symmetric with the ``finding`` and ``refined_to`` caps so a
    pathological caller cannot break ``lineage_keys`` by writing a
    ``refined_to`` (truncated) that fails to match a successor's
    ``question`` (previously verbatim).
    """
    from maestro.memory import _QUESTION_RESOLUTION_FINDING_MAX_CHARS

    mem = _make_memory(mem_fs)
    long_question = "Q" * (_QUESTION_RESOLUTION_FINDING_MAX_CHARS + 50)
    mem.append_question_resolution(_make_resolution(question=long_question))

    rows = mem.get_recent_question_resolutions(1)
    assert len(rows[0].question) == _QUESTION_RESOLUTION_FINDING_MAX_CHARS


def test_append_question_resolution_preserves_question_when_short(
    mem_fs: dict[str, str],
) -> None:
    """``question`` under the cap roundtrips verbatim."""
    mem = _make_memory(mem_fs)
    mem.append_question_resolution(_make_resolution(
        question="Short question?",
    ))

    rows = mem.get_recent_question_resolutions(1)
    assert rows[0].question == "Short question?"


def test_get_recent_question_resolutions_zero_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    mem.append_question_resolution(_make_resolution())
    assert mem.get_recent_question_resolutions(0) == []


def test_get_recent_question_resolutions_missing_file(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory(mem_fs)
    assert mem.get_recent_question_resolutions(10) == []


def test_get_recent_question_resolutions_skips_corrupt_rows(
    mem_fs: dict[str, str],
) -> None:
    """Corrupt JSON lines, blank lines, and structurally bad rows are filtered out."""
    mem = _make_memory(mem_fs)
    mem.append_question_resolution(_make_resolution(resolution_id="r1"))
    # Append blank lines, an unparseable line, and a row missing required fields.
    path = str(mem._question_resolutions_path)
    mem_fs[path] += "\n   \n"
    mem_fs[path] += "{not json\n"
    mem_fs[path] += json.dumps({"resolution_id": "rX"}) + "\n"
    mem.append_question_resolution(_make_resolution(resolution_id="r3"))

    rows = mem.get_recent_question_resolutions(50)
    ids = [r.resolution_id for r in rows]
    assert ids == ["r1", "r3"]


def test_append_question_resolution_uses_file_lock(
    mem_fs: dict[str, str],
) -> None:
    """Append goes through ``file_lock`` so concurrent writers don't tear lines."""
    mem = _make_memory(mem_fs)
    seen: list[Path] = []

    @contextmanager
    def _spy(path: Path) -> Iterator[None]:
        seen.append(path)
        yield

    with patch("maestro.memory.file_lock", _spy):
        mem.append_question_resolution(_make_resolution())
    assert mem._question_resolutions_path in seen


def test_load_all_question_resolutions_returns_empty_on_os_error(
    mem_fs: dict[str, str],
) -> None:
    """OSError reading the file yields an empty list, not a crash."""
    mem = _make_memory(mem_fs)
    mem_fs[str(mem._question_resolutions_path)] = "{}"

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("simulated")

    with patch.object(Path, "read_text", _boom):
        assert mem._load_all_question_resolutions() == []


def test_jsonl_question_resolution_ledger_round_trip(
    mem_fs: dict[str, str],
) -> None:
    """The default adapter appends to and reads back from the JSONL sidecar."""
    from maestro.memory import JsonlQuestionResolutionLedger

    ledger = JsonlQuestionResolutionLedger(FAKE_DIR)
    a = _make_resolution(resolution_id="r1")
    b = _make_resolution(resolution_id="r2")
    ledger.record(a)
    ledger.record(b)
    # Oldest-first, and persisted to the JSONL sidecar (the flag-off substrate).
    assert ledger.load_all() == [a, b]
    assert str(FAKE_DIR / "question_resolutions.jsonl") in mem_fs


def test_load_all_question_resolutions_missing_file(
    mem_fs: dict[str, str],
) -> None:
    """A missing ledger file yields an empty list, not a crash."""
    from maestro.memory import load_all_question_resolutions

    assert load_all_question_resolutions(FAKE_DIR) == []


def test_load_all_question_resolutions_oldest_first_skips_corrupt(
    mem_fs: dict[str, str],
) -> None:
    """The shared reader is oldest-first and skips corrupt rows without raising.

    A blank line, an unparseable interior line, a structurally-bad row (missing
    required fields), and a trailing unparseable line are all dropped — never
    propagated — while the well-formed rows round-trip in file order.
    """
    from maestro.memory import load_all_question_resolutions

    path = str(FAKE_DIR / "question_resolutions.jsonl")
    good1 = json.dumps(asdict(_make_resolution(resolution_id="r1")))
    good2 = json.dumps(asdict(_make_resolution(resolution_id="r2")))
    mem_fs[path] = (
        good1 + "\n"
        + "   \n"  # blank
        + "{not json\n"  # interior unparseable
        + json.dumps({"resolution_id": "rX"}) + "\n"  # structurally bad
        + good2 + "\n"
        + "{still not json\n"  # trailing unparseable
    )
    rows = load_all_question_resolutions(FAKE_DIR)
    assert [r.resolution_id for r in rows] == ["r1", "r2"]


def test_load_all_question_resolutions_module_returns_empty_on_os_error(
    mem_fs: dict[str, str],
) -> None:
    """An ``OSError`` from the shared reader yields ``[]``, not a crash."""
    from maestro.memory import load_all_question_resolutions

    mem_fs[str(FAKE_DIR / "question_resolutions.jsonl")] = "{}"

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("simulated")

    with patch.object(Path, "read_text", _boom):
        assert load_all_question_resolutions(FAKE_DIR) == []


def test_uninjected_memory_defaults_to_jsonl_question_resolutions_ledger(
    mem_fs: dict[str, str],
) -> None:
    """An un-injected ``Memory`` keeps today's JSONL behaviour unchanged."""
    from maestro.memory import JsonlQuestionResolutionLedger

    mem = Memory(FAKE_DIR)
    assert isinstance(mem._question_resolutions, JsonlQuestionResolutionLedger)
    r = _make_resolution(resolution_id="r1")
    mem.append_question_resolution(r)
    assert mem.get_recent_question_resolutions(10) == [r]
    # Persisted to the JSONL sidecar (the flag-off substrate).
    assert str(FAKE_DIR / "question_resolutions.jsonl") in mem_fs


def test_injected_question_resolutions_backend_is_used(
    mem_fs: dict[str, str],
) -> None:
    """An injected backend receives the rows; the JSONL sidecar stays empty.

    The ``resolution_id`` guard and the head-truncation stay in ``Memory`` above
    the backend call, so they still apply on the SQLite path.
    """
    from maestro.state_store import SqliteQuestionResolutionLedger, StateStore

    sqlite = SqliteQuestionResolutionLedger(StateStore(":memory:"))
    mem = Memory(FAKE_DIR, question_resolutions=sqlite)
    r = _make_resolution(resolution_id="r9")
    mem.append_question_resolution(r)
    assert mem.get_recent_question_resolutions(5) == [r]
    assert mem._load_all_question_resolutions() == [r]
    # Routed through the injected backend, not the JSONL sidecar.
    assert sqlite.load_all() == [r]
    assert str(FAKE_DIR / "question_resolutions.jsonl") not in mem_fs
    # Normalisation stays in ``Memory``: the empty-id guard still raises.
    with pytest.raises(ValueError, match="resolution_id"):
        mem.append_question_resolution(_make_resolution(resolution_id=""))
    # And the head-truncation still applies before the backend sees the row.
    mem.append_question_resolution(
        _make_resolution(resolution_id="r10", finding="x" * 600),
    )
    assert len(sqlite.load_all()[-1].finding) == 500


def test_question_resolution_backend_parity(mem_fs: dict[str, str]) -> None:
    """JSONL and SQLite adapters return identical ``QuestionResolution`` lists.

    A shared corpus (including a ``refined`` row carrying ``refined_to``)
    round-trips identically through both substrates, so flipping
    ``[paths] sqlite_state_enabled`` never changes what the readers see.
    """
    from maestro.memory import JsonlQuestionResolutionLedger
    from maestro.state_store import SqliteQuestionResolutionLedger, StateStore

    corpus = [
        _make_resolution(resolution_id="r1"),
        _make_resolution(
            resolution_id="r2",
            disposition="refined",
            refined_to="A sharper successor question?",
        ),
    ]
    jsonl = JsonlQuestionResolutionLedger(FAKE_DIR)
    sqlite = SqliteQuestionResolutionLedger(StateStore(":memory:"))
    for record in corpus:
        jsonl.record(record)
        sqlite.record(record)
    assert jsonl.load_all() == corpus
    assert sqlite.load_all() == corpus
    assert jsonl.load_all() == sqlite.load_all()


# ---------------------------------------------------------------------------
# DocumentRecord (historical rows of the retired doc-proposals pipeline)
# ---------------------------------------------------------------------------


def _make_doc(
    doc_id: str = "d1",
    *,
    doc_type: str = "core",
    status: str = "pending",
    linked_task_ids: list[str] | None = None,
    target_path: str | None = None,
    pr_url: str | None = None,
    error: str | None = None,
) -> DocumentRecord:
    return DocumentRecord(
        doc_id=doc_id,
        doc_type=doc_type,  # type: ignore[arg-type]
        title="Doc",
        linked_task_ids=list(linked_task_ids or ["t1"]),
        target_path=target_path,
        doc_branch=None,
        pr_url=pr_url,
        bundle_file_names=[],
        reasoning="",
        status=status,  # type: ignore[arg-type]
        error=error,
        created_at="2026-04-25T00:00:00+00:00",
    )


def _seed_docs(mem_fs: dict[str, str], *records: DocumentRecord) -> None:
    """Seed historical document rows (the writer was retired, proposal 149)."""
    mem_fs[str(FAKE_DIR / "document_records.jsonl")] = "".join(
        json.dumps(asdict(r)) + "\n" for r in records
    )


def test_load_all_documents_missing_file(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    assert mem._load_all_documents() == []


def test_load_all_documents_skips_corrupt_and_blank(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    valid = json.dumps(asdict(_make_doc(doc_id="d-good")))
    mem_fs[str(FAKE_DIR / "document_records.jsonl")] = (
        valid + "\n" + "{not json\n" + "\n"
        + valid.replace("d-good", "d-2") + "\n"
    )
    records = mem._load_all_documents()
    assert {r.doc_id for r in records} == {"d-good", "d-2"}


def test_load_all_documents_drops_unknown_fields(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    raw = asdict(_make_doc(doc_id="d-x"))
    raw["future_field"] = "ignored"
    mem_fs[str(FAKE_DIR / "document_records.jsonl")] = json.dumps(raw) + "\n"
    [r] = mem._load_all_documents()
    assert r.doc_id == "d-x"


def test_load_all_documents_coerces_list_fields(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    raw = asdict(_make_doc(doc_id="d-coerce"))
    raw["linked_task_ids"] = None
    raw["bundle_file_names"] = "single-line-name"
    mem_fs[str(FAKE_DIR / "document_records.jsonl")] = json.dumps(raw) + "\n"
    [r] = mem._load_all_documents()
    assert r.linked_task_ids == []
    assert r.bundle_file_names == ["single-line-name"]


def test_load_all_documents_drops_empty_doc_id(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    raw = asdict(_make_doc(doc_id="ok"))
    raw["doc_id"] = ""
    mem_fs[str(FAKE_DIR / "document_records.jsonl")] = json.dumps(raw) + "\n"
    assert mem._load_all_documents() == []


def test_get_document_records_filters_compose(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    _seed_docs(
        mem_fs,
        _make_doc(
            doc_id="d-a", doc_type="core", status="pr_opened",
            linked_task_ids=["t-1"],
        ),
        _make_doc(
            doc_id="d-b", doc_type="research", status="failed",
            linked_task_ids=["t-2"], error="bad",
        ),
        _make_doc(
            doc_id="d-c", doc_type="core", status="failed",
            linked_task_ids=["t-1"], error="other",
        ),
    )
    out = mem.get_document_records(doc_type="core", status="failed")
    assert [r.doc_id for r in out] == ["d-c"]
    out = mem.get_document_records(task_id="t-1")
    assert {r.doc_id for r in out} == {"d-a", "d-c"}


def test_get_document_records_n_zero_and_negative(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    _seed_docs(mem_fs, _make_doc(doc_id="d1"))
    assert mem.get_document_records(n=0) == []
    assert mem.get_document_records(n=-3) == []


def test_get_document_records_n_tail(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    _seed_docs(mem_fs, *(_make_doc(doc_id=f"d-{i}") for i in range(4)))
    out = mem.get_document_records(n=2)
    assert [r.doc_id for r in out] == ["d-2", "d-3"]
