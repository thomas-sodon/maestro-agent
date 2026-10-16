"""Tests for the deed ledger — the record of fired terminal acts.

All I/O runs through the ``mem_fs`` fixture (no real filesystem), so every
append/read is asserted against the in-memory file map.
"""

from __future__ import annotations

from pathlib import Path

from maestro import deed_ledger
from maestro.deed_ledger import (
    DEED_LEDGER_FILENAME,
    DeedType,
    JsonlDeedLedger,
    build_deed,
    count_deeds,
    format_terminal_deeds_summary,
    load_all_deeds,
    recent_deeds,
    record_deed,
)
from maestro.state_store import SqliteDeedLedger, StateStore

_DATA = Path("/mem")
_LEDGER = str(_DATA / DEED_LEDGER_FILENAME)


class TestRecordDeed:
    def test_appends_exactly_one_row_per_call(
        self, mem_fs: dict[str, str],
    ) -> None:
        record_deed(_DATA, DeedType.PR_CLOSE, 12, "closed")
        record_deed(_DATA, DeedType.HANDOFF_DELIVERED, 34, "handed off")
        lines = [ln for ln in mem_fs[_LEDGER].splitlines() if ln.strip()]
        assert len(lines) == 2

    def test_returns_deed_with_coerced_type_and_stamp(
        self, mem_fs: dict[str, str],
    ) -> None:
        deed = record_deed(
            _DATA, DeedType.OWNED_PRUNE, None, "pruned 2", now="2026-07-31T00:00:00+00:00",
        )
        assert deed.deed_type == "owned_prune"
        assert deed.pr_number is None
        assert deed.detail == "pruned 2"
        assert deed.timestamp == "2026-07-31T00:00:00+00:00"

    def test_accepts_raw_string_deed_type(
        self, mem_fs: dict[str, str],
    ) -> None:
        deed = record_deed(_DATA, "merge", 5, "merged")
        assert deed.deed_type == "merge"

    def test_row_is_utf8_json_with_all_fields(
        self, mem_fs: dict[str, str],
    ) -> None:
        import json

        record_deed(
            _DATA, DeedType.PR_CLOSE, 7, "closé", now="2026-07-31T01:02:03+00:00",
        )
        row = json.loads(mem_fs[_LEDGER].splitlines()[0])
        assert row == {
            "deed_type": "pr_close",
            "pr_number": 7,
            "detail": "closé",
            "timestamp": "2026-07-31T01:02:03+00:00",
        }


class TestCountAndRecent:
    def test_round_trips_on_read(self, mem_fs: dict[str, str]) -> None:
        record_deed(_DATA, DeedType.PR_CLOSE, 1, "a", now="2026-07-31T00:00:01+00:00")
        record_deed(_DATA, DeedType.MERGE, 2, "b", now="2026-07-31T00:00:02+00:00")
        assert count_deeds(_DATA) == 2
        deeds = recent_deeds(_DATA, n=10)
        # Newest-first.
        assert [d.pr_number for d in deeds] == [2, 1]
        assert deeds[0].deed_type == "merge"
        assert deeds[1].detail == "a"

    def test_count_empty_ledger_is_zero(self, mem_fs: dict[str, str]) -> None:
        assert count_deeds(_DATA) == 0
        assert recent_deeds(_DATA, n=5) == []

    def test_recent_deeds_honours_n(self, mem_fs: dict[str, str]) -> None:
        for i in range(5):
            record_deed(
                _DATA, DeedType.MERGE, i, f"d{i}",
                now=f"2026-07-31T00:00:0{i}+00:00",
            )
        deeds = recent_deeds(_DATA, n=2)
        assert len(deeds) == 2
        assert [d.pr_number for d in deeds] == [4, 3]

    def test_recent_deeds_non_positive_n_is_empty(
        self, mem_fs: dict[str, str],
    ) -> None:
        record_deed(_DATA, DeedType.MERGE, 1, "d")
        assert recent_deeds(_DATA, n=0) == []
        assert recent_deeds(_DATA, n=-1) == []

    def test_count_windows_by_since(self, mem_fs: dict[str, str]) -> None:
        record_deed(_DATA, DeedType.MERGE, 1, "old", now="2026-07-30T00:00:00+00:00")
        record_deed(_DATA, DeedType.MERGE, 2, "mid", now="2026-07-31T00:00:00+00:00")
        record_deed(_DATA, DeedType.MERGE, 3, "new", now="2026-08-01T00:00:00+00:00")
        # since at the middle timestamp includes mid + new, excludes old.
        assert count_deeds(_DATA, since="2026-07-31T00:00:00+00:00") == 2
        # since after every deed → zero.
        assert count_deeds(_DATA, since="2026-09-01T00:00:00+00:00") == 0
        # since before every deed → all.
        assert count_deeds(_DATA, since="2026-01-01T00:00:00+00:00") == 3


class TestCorruptedTail:
    def test_skips_malformed_json_and_missing_field_records(
        self, mem_fs: dict[str, str],
    ) -> None:
        record_deed(_DATA, DeedType.MERGE, 1, "a", now="2026-07-31T00:00:01+00:00")
        record_deed(_DATA, DeedType.MERGE, 2, "b", now="2026-07-31T00:00:02+00:00")
        # Append a malformed-JSON line and a dict-missing-required-field line to
        # the tail. Both must be skipped, leaving the two valid deeds visible.
        mem_fs[_LEDGER] += "{not json\n"
        mem_fs[_LEDGER] += '{"deed_type": "merge", "pr_number": 3}\n'
        assert count_deeds(_DATA) == 2
        assert [d.pr_number for d in recent_deeds(_DATA, n=10)] == [2, 1]

    def test_non_int_pr_number_is_corrupt(
        self, mem_fs: dict[str, str],
    ) -> None:
        record_deed(_DATA, DeedType.MERGE, 1, "a")
        mem_fs[_LEDGER] += (
            '{"deed_type": "merge", "pr_number": "x", "detail": "d", '
            '"timestamp": "2026-07-31T00:00:09+00:00"}\n'
        )
        assert count_deeds(_DATA) == 1

    def test_corrupted_tail_is_bounded(self, mem_fs: dict[str, str]) -> None:
        # A valid deed sits below a burst of record-level corruption (parseable
        # JSON dicts missing required fields). With the skip bound smaller than
        # the corrupted burst, the newest-first scan stops before it ever reaches
        # the valid deed — a partial result, never a full-file walk.
        record_deed(_DATA, DeedType.MERGE, 1, "buried")
        for _ in range(10):
            mem_fs[_LEDGER] += '{"deed_type": "merge"}\n'
        assert count_deeds(_DATA, max_corrupt=3) == 0
        assert recent_deeds(_DATA, n=3) == []

    def test_small_corrupt_run_still_reaches_valid(
        self, mem_fs: dict[str, str],
    ) -> None:
        record_deed(_DATA, DeedType.MERGE, 1, "reachable")
        mem_fs[_LEDGER] += '{"deed_type": "merge"}\n'
        assert count_deeds(_DATA, max_corrupt=3) == 1


class TestFormatTerminalDeeds:
    def test_reports_fired_vs_laps(self) -> None:
        assert (
            format_terminal_deeds_summary(2, 5)
            == "TERMINAL-DEEDS: 2 fired vs 5 laps"
        )

    def test_zero_deeds_with_laps_still_surfaces(self) -> None:
        assert (
            format_terminal_deeds_summary(0, 3)
            == "TERMINAL-DEEDS: 0 fired vs 3 laps"
        )

    def test_both_zero_self_suppresses(self) -> None:
        assert format_terminal_deeds_summary(0, 0) == ""

    def test_deeds_without_laps_surfaces(self) -> None:
        assert (
            format_terminal_deeds_summary(4, 0)
            == "TERMINAL-DEEDS: 4 fired vs 0 laps"
        )


def test_deed_type_values() -> None:
    assert DeedType.PR_CLOSE.value == "pr_close"
    assert DeedType.HANDOFF_DELIVERED.value == "handoff_delivered"
    assert DeedType.MERGE.value == "merge"
    assert DeedType.OWNED_PRUNE.value == "owned_prune"


def test_module_exposes_ledger_filename() -> None:
    assert deed_ledger.DEED_LEDGER_FILENAME == "deed_ledger.jsonl"


class TestBuildDeed:
    def test_coerces_enum_and_uses_now(self) -> None:
        deed = build_deed(
            DeedType.PR_CLOSE, 7, "closed", now="2026-07-31T00:00:00+00:00",
        )
        assert deed.deed_type == "pr_close"
        assert deed.pr_number == 7
        assert deed.timestamp == "2026-07-31T00:00:00+00:00"

    def test_default_now_is_iso_utc_and_raw_type_passes_through(self) -> None:
        deed = build_deed("merge", None, "d")
        assert deed.deed_type == "merge"
        assert deed.pr_number is None
        assert deed.timestamp  # non-empty ISO string (current UTC)


class TestLoadAllDeeds:
    def test_returns_oldest_first(self, mem_fs: dict[str, str]) -> None:
        record_deed(_DATA, DeedType.MERGE, 1, "a", now="2026-07-31T00:00:01+00:00")
        record_deed(_DATA, DeedType.MERGE, 2, "b", now="2026-07-31T00:00:02+00:00")
        record_deed(_DATA, DeedType.MERGE, 3, "c", now="2026-07-31T00:00:03+00:00")
        deeds = load_all_deeds(_DATA)
        # Oldest-first — the seed order for the SQLite adapter.
        assert [d.pr_number for d in deeds] == [1, 2, 3]

    def test_absent_ledger_returns_empty(self, mem_fs: dict[str, str]) -> None:
        assert load_all_deeds(Path("/mem/other")) == []


class TestJsonlDeedLedgerAdapter:
    """The JSONL adapter delegates to the module functions (flag-off default)."""

    def test_record_appends_and_returns(self, mem_fs: dict[str, str]) -> None:
        ledger = JsonlDeedLedger(_DATA)
        deed = ledger.record(
            DeedType.PR_CLOSE, 12, "closed", now="2026-07-31T00:00:00+00:00",
        )
        assert deed.pr_number == 12
        lines = [ln for ln in mem_fs[_LEDGER].splitlines() if ln.strip()]
        assert len(lines) == 1

    def test_count_and_recent_delegate(self, mem_fs: dict[str, str]) -> None:
        ledger = JsonlDeedLedger(_DATA)
        ledger.record(DeedType.MERGE, 1, "a", now="2026-07-31T00:00:01+00:00")
        ledger.record(DeedType.MERGE, 2, "b", now="2026-07-31T00:00:02+00:00")
        assert ledger.count() == 2
        # Newest-first.
        assert [d.pr_number for d in ledger.recent(10)] == [2, 1]

    def test_count_since_delegates(self, mem_fs: dict[str, str]) -> None:
        ledger = JsonlDeedLedger(_DATA)
        ledger.record(DeedType.MERGE, 1, "old", now="2026-07-30T00:00:00+00:00")
        ledger.record(DeedType.MERGE, 2, "new", now="2026-08-01T00:00:00+00:00")
        assert ledger.count(since="2026-07-31T00:00:00+00:00") == 1

    def test_recent_default_n(self, mem_fs: dict[str, str]) -> None:
        ledger = JsonlDeedLedger(_DATA)
        ledger.record(DeedType.MERGE, 1, "a")
        assert len(ledger.recent()) == 1


class TestBackendParity:
    """JSONL and SQLite deed backends must be interchangeable.

    Proves the flag-flip is behaviour-preserving before the JSONL path is ever
    conceded: recording the same deed sequence through both yields equal
    ``count()`` (including ``since`` windowing) and equal ``recent()`` output.
    Deeds are wall-clock-appended, so append order equals chronological order —
    the JSONL reader's early-stop ``since`` scan and the SQL ``WHERE timestamp >=
    ?`` agree only for that monotonic case (the realistic one for deeds).
    """

    @staticmethod
    def _shared_deeds() -> list[tuple[DeedType, int | None, str, str]]:
        # Strictly ascending timestamps (the real append order), including a
        # None-pr owned prune so the nullable column round-trips through both.
        return [
            (DeedType.PR_CLOSE, 1, "closed", "2026-07-30T00:00:01+00:00"),
            (DeedType.HANDOFF_DELIVERED, 2, "handed", "2026-07-31T00:00:02+00:00"),
            (DeedType.MERGE, 3, "merged", "2026-08-01T00:00:03+00:00"),
            (DeedType.OWNED_PRUNE, None, "pruned", "2026-08-02T00:00:04+00:00"),
        ]

    def _both(self) -> tuple[JsonlDeedLedger, SqliteDeedLedger]:
        jsonl = JsonlDeedLedger(_DATA)
        sqlite = SqliteDeedLedger(StateStore(":memory:"))
        for deed_type, pr, detail, ts in self._shared_deeds():
            jsonl.record(deed_type, pr, detail, now=ts)
            sqlite.record(deed_type, pr, detail, now=ts)
        return jsonl, sqlite

    def test_count_parity(self, mem_fs: dict[str, str]) -> None:
        jsonl, sqlite = self._both()
        assert jsonl.count() == sqlite.count()

    def test_count_since_parity(self, mem_fs: dict[str, str]) -> None:
        jsonl, sqlite = self._both()
        since = "2026-07-31T00:00:02+00:00"
        assert jsonl.count(since=since) == sqlite.count(since=since)

    def test_recent_parity(self, mem_fs: dict[str, str]) -> None:
        jsonl, sqlite = self._both()
        assert jsonl.recent(10) == sqlite.recent(10)

    def test_recent_bounded_parity(self, mem_fs: dict[str, str]) -> None:
        jsonl, sqlite = self._both()
        assert jsonl.recent(2) == sqlite.recent(2)
