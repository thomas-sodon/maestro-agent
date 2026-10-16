"""Tests for Journal — fully mocked I/O."""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from maestro.journal import Journal, is_inner_thought
from maestro.types import INNER_THOUGHT_SOURCES

FAKE_DIR = Path("/fake/journal")


def _make_journal(mem_fs: dict[str, str]) -> Journal:
    return Journal(FAKE_DIR)


# ── Core read/write ──────────────────────────────────────────────────


def test_write_and_read(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    j.write_thought("I wonder what I am.", triggered_by="contemplation")

    # Verify data was written to in-memory storage
    journal_path = str(FAKE_DIR / "journal.jsonl")
    assert journal_path in mem_fs
    assert "I wonder what I am." in mem_fs[journal_path]

    recent = j.read_recent()
    assert len(recent) == 1
    assert recent[0].content == "I wonder what I am."


def test_write_thought_convenience(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    entry = j.write_thought(
        content="Testing thought",
        entry_type="realization",
        triggered_by="agent_tool",
    )
    assert entry.entry_id != ""
    assert entry.timestamp != ""

    # Verify round-trip through in-memory storage
    recent = j.read_recent()
    assert len(recent) == 1
    assert recent[0].content == "Testing thought"


def test_read_by_type(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    j.write_thought("Thought 1", "reflection", triggered_by="agent_tool")
    j.write_thought("Thought 2", "question", triggered_by="agent_tool")
    j.write_thought("Thought 3", "reflection", triggered_by="agent_tool")

    reflections = j.read_by_type("reflection")
    assert len(reflections) == 2

    questions = j.read_by_type("question")
    assert len(questions) == 1


def test_search(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    j.write_thought("I love learning about code", "reflection", triggered_by="agent_tool")
    j.write_thought("The weather is nice", "reflection", triggered_by="agent_tool")

    results = j.search("code")
    assert len(results) == 1
    assert "code" in results[0].content


def test_search_ranks_multi_token_match_above_single_token(
    mem_fs: dict[str, str],
) -> None:
    """search() should rank an entry that contains both query tokens
    above an entry containing only one of them."""
    j = _make_journal(mem_fs)
    j.write_thought("refactor the planner module urgently", triggered_by="agent_tool")
    j.write_thought("refactor the authentication layer", triggered_by="agent_tool")
    j.write_thought("the weather is nice today", triggered_by="agent_tool")
    j.write_thought("update the docs for release notes", triggered_by="agent_tool")
    j.write_thought("the cache layer needs a quick fix", triggered_by="agent_tool")

    results = j.search("refactor planner")
    # Entry 0 has both tokens, entry 1 has only one — entry 0 ranks first.
    # Entries 2, 3, 4 contain neither token and are excluded.
    assert len(results) == 2
    assert results[0].content == "refactor the planner module urgently"
    assert results[1].content == "refactor the authentication layer"


def test_search_empty_query_returns_empty(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    j.write_thought("some content", "reflection", triggered_by="agent_tool")
    assert j.search("") == []
    assert j.search("   ") == []


def test_search_no_match_returns_empty(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    j.write_thought("apple banana cherry", "reflection", triggered_by="agent_tool")
    assert j.search("xyzzy") == []


def test_search_injected_sqlite_backend_matches_default(
    mem_fs: dict[str, str],
) -> None:
    """A ``Journal`` backed by the persistent SQLite index ranks identically to
    the default ephemeral backend over the same journal.

    ``mem_fs`` holds the append-only JSONL journal; the SQLite index lives in a
    separate ``:memory:`` store, so the two substrates are never mixed (the
    state-store test convention). This is the end-to-end parity that lets the
    flag flip in unnoticed.
    """
    from maestro.state_store import SqliteJournalSearchIndex, StateStore

    default = _make_journal(mem_fs)
    default.write_thought("refactor the planner module urgently", triggered_by="agent_tool")
    default.write_thought("refactor the authentication layer", triggered_by="agent_tool")
    default.write_thought("the weather is nice today", triggered_by="agent_tool")
    default.write_thought("plan_42 has three steps", triggered_by="agent_tool")

    injected = Journal(
        FAKE_DIR,
        search_index=SqliteJournalSearchIndex(StateStore(":memory:")),
    )

    for query in ["refactor planner", "refactor", "plan_42", "xyzzy", "", "!!!"]:
        default_ids = [e.entry_id for e in default.search(query)]
        injected_ids = [e.entry_id for e in injected.search(query)]
        assert injected_ids == default_ids


def test_empty_journal(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    assert j.read_recent() == []
    assert j.read_by_type("reflection") == []
    assert j.search("anything") == []


# ── Narrative ────────────────────────────────────────────────────────


def test_full_narrative(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    assert j.get_full_narrative() == ""

    j.write_thought("First thought", "reflection", triggered_by="agent_tool")
    j.write_thought("Second thought", "realization", triggered_by="agent_tool")

    narrative = j.get_full_narrative()
    assert "First thought" in narrative
    assert "Second thought" in narrative
    assert "---" in narrative


def test_get_recent_narrative_empty(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    assert j.get_recent_narrative() == ""


def test_get_recent_narrative_limits_entries(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    for i in range(20):
        j.write_thought(f"Entry {i}", "reflection", triggered_by="agent_tool")

    # Only last 5 entries
    narrative = j.get_recent_narrative(max_entries=5, max_chars=100000)
    assert "Entry 19" in narrative
    assert "Entry 15" in narrative
    assert "Entry 14" not in narrative


def test_get_recent_narrative_limits_chars(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    for i in range(10):
        j.write_thought(f"Entry {i} with some extra content", "reflection", triggered_by="agent_tool")

    narrative = j.get_recent_narrative(max_entries=50, max_chars=100)
    assert len(narrative) <= 100


def test_get_recent_narrative_defaults(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    j.write_thought("A thought", "reflection", triggered_by="agent_tool")
    j.write_thought("Another thought", "realization", triggered_by="agent_tool")

    narrative = j.get_recent_narrative()
    assert "A thought" in narrative
    assert "Another thought" in narrative
    assert "---" in narrative


def test_get_recent_narrative_chars_uses_entry_boundaries(mem_fs: dict[str, str]) -> None:
    """get_recent_narrative must drop whole entries (not truncate mid-entry) and
    run correctly for large max_entries values (O(n) fix regression check)."""
    j = _make_journal(mem_fs)
    # Write 200 entries so the old O(n^2) path would be noticeably slow if
    # re-introduced; correctness is what we assert here.
    for i in range(200):
        j.write_thought(f"Entry {i:03d} content", "reflection", triggered_by="agent_tool")

    narrative = j.get_recent_narrative(max_entries=200, max_chars=500)

    # Result must be within the character budget.
    assert len(narrative) <= 500

    # Result must not be empty (the most recent entries should fit).
    assert narrative != ""

    # Every line in the result must come from a whole entry boundary
    # (the separator must be intact, not cut mid-string).
    if "---" in narrative:
        for chunk in narrative.split("\n\n---\n\n"):
            assert chunk.strip() != ""

    # The narrative must contain the most recent entries, not oldest ones.
    assert "Entry 199" in narrative


def test_get_recent_narrative_single_oversized_entry_returns_truncated(
    mem_fs: dict[str, str],
) -> None:
    """When even a single entry exceeds max_chars, return a truncated partial
    entry rather than an empty string — a partial entry is more useful to an
    LLM caller than complete silence."""
    j = _make_journal(mem_fs)
    j.write_thought("A" * 5000, "reflection", triggered_by="agent_tool")

    result = j.get_recent_narrative(max_entries=50, max_chars=100)
    assert result != "", "get_recent_narrative returned empty string for a single large entry"
    assert len(result) <= 100, "Result should be bounded by max_chars"


# ── _load_all edge cases ─────────────────────────────────────────────


def test_load_all_empty_file(mem_fs: dict[str, str]) -> None:
    """_load_all should handle empty journal file."""
    mem_fs["/fake/memory/journal.jsonl"] = ""
    j = Journal(Path("/fake/memory"))
    result = j._load_all()
    assert result == []


def test_non_dict_json_line_skipped(mem_fs: dict[str, str]) -> None:
    """Valid JSON that is not a dict (e.g. a list) must be skipped, not crash."""
    j = _make_journal(mem_fs)
    # Pre-populate with a non-dict JSON line followed by a valid entry
    journal_path = str(FAKE_DIR / "journal.jsonl")
    mem_fs[journal_path] = (
        '[1, 2, 3]\n'
        '{"entry_id":"e1","timestamp":"2025-01-01T00:00:00Z",'
        '"stage":"awakening","entry_type":"reflection","triggered_by":"agent_tool",'
        '"content":"Valid entry"}\n'
    )
    recent = j.read_recent()
    assert len(recent) == 1
    assert recent[0].content == "Valid entry"


def test_journal_entry_extra_fields(mem_fs: dict[str, str]) -> None:
    """_load_all should ignore extra unknown fields in JSON (forward compat)."""
    entry_data = {
        "entry_id": "e1", "timestamp": "2026-01-01",
        "stage": "awakening", "entry_type": "reflection",
        "content": "A thought", "triggered_by": "agent_tool",
        "future_unknown_field": "should be ignored",
    }
    mem_fs["/fake/memory/journal.jsonl"] = json.dumps(entry_data) + "\n"

    j = Journal(Path("/fake/memory"))
    entries = j._load_all()
    assert len(entries) == 1
    assert entries[0].content == "A thought"
    assert entries[0].entry_id == "e1"


# ── read_recent edge cases ───────────────────────────────────────────


def test_read_recent_zero_returns_empty(mem_fs: dict[str, str]) -> None:
    """read_recent(0) must return an empty list, not all entries."""
    j = _make_journal(mem_fs)
    j.write_thought("Entry 1", "reflection", triggered_by="agent_tool")
    j.write_thought("Entry 2", "reflection", triggered_by="agent_tool")
    j.write_thought("Entry 3", "reflection", triggered_by="agent_tool")

    result = j.read_recent(0)
    assert result == []


def test_read_recent_n_larger_than_entries(mem_fs: dict[str, str]) -> None:
    """read_recent with n > total entries should return all entries."""
    mem_fs["/fake/memory/journal.jsonl"] = json.dumps({
        "entry_id": "e1", "timestamp": "2026-01-01", "stage": "awakening",
        "triggered_by": "agent_tool", "entry_type": "reflection", "content": "test",
    }) + "\n"

    j = Journal(Path("/fake/memory"))
    result = j.read_recent(1000)
    assert len(result) == 1


# ── read_by_type edge cases ──────────────────────────────────────────


def test_read_by_type_with_zero_count_slice(mem_fs: dict[str, str]) -> None:
    """read_by_type(type)[-0:] returns all entries instead of none (Python slice bug)."""
    j = _make_journal(mem_fs)
    for i in range(5):
        j.write_thought(
            content=f"Entry {i}",
            entry_type="reflection",
            triggered_by="agent_tool",
        )

    entries = j.read_by_type("reflection")
    # In cmd_journal: entries = journal.read_by_type(args.type)[-count:]
    # When count=0: entries[-0:] returns ALL entries (Python slice behavior)
    sliced = entries[-0:]
    # Confirms the bug: [-0:] returns everything
    assert len(sliced) == len(entries)


def test_read_by_type_invalid_type_raises(mem_fs: dict[str, str]) -> None:
    """read_by_type should reject invalid entry types."""
    j = Journal(Path("/fake/mem"))
    with pytest.raises(ValueError, match="entry_type"):
        j.read_by_type("not_a_real_type")


# ── get_recent_narrative edge cases ──────────────────────────────────


def test_get_recent_narrative_max_chars_zero(mem_fs: dict[str, str]) -> None:
    """get_recent_narrative(max_chars=0) should return empty string, not full text."""
    j = Journal(Path("/fake/journal6"))
    j.write_thought("Hello world", entry_type="reflection", triggered_by="agent_tool")
    result = j.get_recent_narrative(max_chars=0)
    assert result == "", f"Expected empty string for max_chars=0, got {result!r}"


def test_get_recent_narrative_max_chars_negative(mem_fs: dict[str, str]) -> None:
    """get_recent_narrative with negative max_chars should return empty string."""
    j = Journal(Path("/fake/journal6b"))
    j.write_thought("Hello world", entry_type="reflection", triggered_by="agent_tool")
    result = j.get_recent_narrative(max_chars=-5)
    assert result == "", f"Expected empty string for negative max_chars, got {result!r}"


def test_get_recent_narrative_max_entries_zero(mem_fs: dict[str, str]) -> None:
    """get_recent_narrative(max_entries=0) should return empty string."""
    j = Journal(Path("/fake/nzero"))
    j.write_thought(
        content="some thought",
        entry_type="reflection", triggered_by="agent_tool",
    )
    result = j.get_recent_narrative(max_entries=0)
    assert result == ""


# ── search edge cases ────────────────────────────────────────────────


def test_search_with_regex_special_chars(mem_fs: dict[str, str]) -> None:
    """search() should handle regex special characters in query string."""
    j = Journal(Path("/fake"))
    j.write_thought("Found bug in func(x, y) [critical]", triggered_by="agent_tool")
    j.write_thought("Normal entry", triggered_by="agent_tool")

    # Search with regex special characters
    results = j.search("func(x, y)")
    assert len(results) == 1
    assert "func(x, y)" in results[0].content

    # Brackets should also work
    results = j.search("[critical]")
    assert len(results) == 1


def test_search_with_null_content_entry(mem_fs: dict[str, str]) -> None:
    """search() should not crash when an entry has content=None."""
    d = Path("/fake/bug77")
    journal = Journal(d)

    entry_ok = {
        "entry_id": "abc",
        "timestamp": "2024-01-01T00:00:00",
        "stage": "awakening",
        "triggered_by": "agent_tool", "entry_type": "reflection",
        "content": "hello world",
    }
    entry_null = {
        "entry_id": "def",
        "timestamp": "2024-01-01T00:00:01",
        "stage": "awakening",
        "triggered_by": "agent_tool", "entry_type": "reflection",
        "content": None,
    }
    mem_fs[str(d / "journal.jsonl")] = (
        json.dumps(entry_ok) + "\n" + json.dumps(entry_null) + "\n"
    )

    results = journal.search("hello")
    assert len(results) == 1
    assert results[0].entry_id == "abc"


# ── narrative with null content ──────────────────────────────────────


def test_full_narrative_with_null_content(mem_fs: dict[str, str]) -> None:
    """get_full_narrative() should exclude entries with None content."""
    d = Path("/fake/bug78")
    journal = Journal(d)

    entry = {
        "entry_id": "abc",
        "timestamp": "2024-01-01T00:00:00",
        "stage": "awakening",
        "triggered_by": "agent_tool", "entry_type": "reflection",
        "content": None,
    }
    mem_fs[str(d / "journal.jsonl")] = json.dumps(entry) + "\n"

    narrative = journal.get_full_narrative()
    assert "None" not in narrative


# ── write_thought validation ─────────────────────────────────────────


def test_write_thought_none_content_raises(mem_fs: dict[str, str]) -> None:
    """write_thought should reject None content."""
    j = Journal(Path("/fake/mem"))
    with pytest.raises((ValueError, TypeError)):
        j.write_thought(content=None, triggered_by="agent_tool")  # type: ignore[arg-type]


def test_write_thought_empty_content_raises(mem_fs: dict[str, str]) -> None:
    """write_thought should reject empty string content."""
    j = Journal(Path("/fake/mem"))
    with pytest.raises(ValueError, match="content"):
        j.write_thought(content="", triggered_by="agent_tool")


def test_write_thought_extremely_long_content(mem_fs: dict[str, str]) -> None:
    """write_thought should handle very long content."""
    j = Journal(Path("/fake/longjournal"))
    long_content = "x" * 100_000
    j.write_thought(
        content=long_content,
        entry_type="reflection", triggered_by="agent_tool",
    )
    entries = j._load_all()
    assert len(entries) == 1
    assert len(entries[0].content) <= 100_000


# ── search_by_date_range ────────────────────────────────────────────


def test_search_by_date_range_basic(mem_fs: dict[str, str]) -> None:
    """search_by_date_range returns entries within [start, end] inclusive."""
    j = _make_journal(mem_fs)
    d = FAKE_DIR / "journal.jsonl"
    entries = [
        {"entry_id": "e1", "timestamp": "2025-01-01T00:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "First"},
        {"entry_id": "e2", "timestamp": "2025-01-15T12:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Middle"},
        {"entry_id": "e3", "timestamp": "2025-02-01T00:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Last"},
    ]
    mem_fs[str(d)] = "".join(json.dumps(e) + "\n" for e in entries)

    results = j.search_by_date_range(
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 1, 31, tzinfo=UTC),
    )
    assert len(results) == 2
    assert results[0].entry_id == "e1"
    assert results[1].entry_id == "e2"


def test_search_by_date_range_inclusive_boundaries(mem_fs: dict[str, str]) -> None:
    """Start and end boundaries are inclusive."""
    j = _make_journal(mem_fs)
    d = FAKE_DIR / "journal.jsonl"
    entries = [
        {"entry_id": "e1", "timestamp": "2025-03-01T00:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Start"},
        {"entry_id": "e2", "timestamp": "2025-03-31T23:59:59+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "End"},
    ]
    mem_fs[str(d)] = "".join(json.dumps(e) + "\n" for e in entries)

    results = j.search_by_date_range(
        datetime(2025, 3, 1, 0, 0, 0, tzinfo=UTC),
        datetime(2025, 3, 31, 23, 59, 59, tzinfo=UTC),
    )
    assert len(results) == 2


def test_search_by_date_range_sorted_ascending(mem_fs: dict[str, str]) -> None:
    """Results are sorted by timestamp ascending."""
    j = _make_journal(mem_fs)
    d = FAKE_DIR / "journal.jsonl"
    # Write entries out of order
    entries = [
        {"entry_id": "e3", "timestamp": "2025-06-15T00:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Third"},
        {"entry_id": "e1", "timestamp": "2025-06-01T00:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "First"},
        {"entry_id": "e2", "timestamp": "2025-06-10T00:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Second"},
    ]
    mem_fs[str(d)] = "".join(json.dumps(e) + "\n" for e in entries)

    results = j.search_by_date_range(
        datetime(2025, 6, 1, tzinfo=UTC),
        datetime(2025, 6, 30, tzinfo=UTC),
    )
    assert len(results) == 3
    assert results[0].entry_id == "e1"
    assert results[1].entry_id == "e2"
    assert results[2].entry_id == "e3"


def test_search_by_date_range_empty_journal(mem_fs: dict[str, str]) -> None:
    """Empty journal returns empty list."""
    j = _make_journal(mem_fs)
    results = j.search_by_date_range(
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 12, 31, tzinfo=UTC),
    )
    assert results == []


def test_search_by_date_range_no_matches(mem_fs: dict[str, str]) -> None:
    """Returns empty list when no entries fall within the range."""
    j = _make_journal(mem_fs)
    d = FAKE_DIR / "journal.jsonl"
    entries = [
        {"entry_id": "e1", "timestamp": "2024-01-01T00:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Old"},
    ]
    mem_fs[str(d)] = "".join(json.dumps(e) + "\n" for e in entries)

    results = j.search_by_date_range(
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 12, 31, tzinfo=UTC),
    )
    assert results == []


def test_search_by_date_range_naive_boundaries_treated_as_utc(
    mem_fs: dict[str, str],
) -> None:
    """Naive start/end datetimes are treated as UTC."""
    j = _make_journal(mem_fs)
    d = FAKE_DIR / "journal.jsonl"
    entries = [
        {"entry_id": "e1", "timestamp": "2025-05-15T10:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Match"},
    ]
    mem_fs[str(d)] = "".join(json.dumps(e) + "\n" for e in entries)

    # Pass naive datetimes (no tzinfo)
    results = j.search_by_date_range(
        datetime(2025, 5, 1),
        datetime(2025, 5, 31),
    )
    assert len(results) == 1
    assert results[0].entry_id == "e1"


def test_search_by_date_range_naive_entry_timestamps(
    mem_fs: dict[str, str],
) -> None:
    """Entries with naive timestamps are treated as UTC for comparison."""
    j = _make_journal(mem_fs)
    d = FAKE_DIR / "journal.jsonl"
    entries = [
        {"entry_id": "e1", "timestamp": "2025-07-10T08:00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Naive ts"},
    ]
    mem_fs[str(d)] = "".join(json.dumps(e) + "\n" for e in entries)

    results = j.search_by_date_range(
        datetime(2025, 7, 1, tzinfo=UTC),
        datetime(2025, 7, 31, tzinfo=UTC),
    )
    assert len(results) == 1


def test_search_by_date_range_unparseable_timestamp_skipped(
    mem_fs: dict[str, str],
) -> None:
    """Entries with unparseable timestamps are silently skipped."""
    j = _make_journal(mem_fs)
    d = FAKE_DIR / "journal.jsonl"
    entries = [
        {"entry_id": "e1", "timestamp": "not-a-date",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Bad ts"},
        {"entry_id": "e2", "timestamp": "2025-08-10T00:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Good ts"},
    ]
    mem_fs[str(d)] = "".join(json.dumps(e) + "\n" for e in entries)

    results = j.search_by_date_range(
        datetime(2025, 8, 1, tzinfo=UTC),
        datetime(2025, 8, 31, tzinfo=UTC),
    )
    assert len(results) == 1
    assert results[0].entry_id == "e2"


def test_search_by_date_range_multiple_entry_types(mem_fs: dict[str, str]) -> None:
    """All entry types are searchable by date range."""
    j = _make_journal(mem_fs)
    d = FAKE_DIR / "journal.jsonl"
    entries = [
        {"entry_id": "e1", "timestamp": "2025-04-02T00:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "A reflection"},
        {"entry_id": "e2", "timestamp": "2025-04-05T00:00:00+00:00",
         "stage": "self_reflection", "triggered_by": "agent_tool", "entry_type": "realization",
         "content": "A realization"},
        {"entry_id": "e3", "timestamp": "2025-04-10T00:00:00+00:00",
         "stage": "self_awareness", "triggered_by": "agent_tool", "entry_type": "milestone",
         "content": "A milestone"},
        {"entry_id": "e4", "timestamp": "2025-04-15T00:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "question", "content": "A question"},
        {"entry_id": "e5", "timestamp": "2025-04-20T00:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "dream", "content": "A dream"},
    ]
    mem_fs[str(d)] = "".join(json.dumps(e) + "\n" for e in entries)

    results = j.search_by_date_range(
        datetime(2025, 4, 1, tzinfo=UTC),
        datetime(2025, 4, 30, tzinfo=UTC),
    )
    assert len(results) == 5
    returned_types = {r.entry_type for r in results}
    # The retired ``dream`` type (proposal 149 Step 5) reads as a reflection.
    assert returned_types == {"reflection", "realization", "milestone", "question"}
    legacy = next(r for r in results if r.entry_id == "e5")
    assert legacy.entry_type == "reflection"


def test_search_by_date_range_excludes_outside_entries(
    mem_fs: dict[str, str],
) -> None:
    """Entries before start and after end are excluded; only in-range returned."""
    j = _make_journal(mem_fs)
    d = FAKE_DIR / "journal.jsonl"
    entries = [
        {"entry_id": "before", "timestamp": "2025-02-28T23:59:59+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Too early"},
        {"entry_id": "in1", "timestamp": "2025-03-01T00:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Start edge"},
        {"entry_id": "in2", "timestamp": "2025-03-15T12:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Mid range"},
        {"entry_id": "in3", "timestamp": "2025-03-31T23:59:59+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "End edge"},
        {"entry_id": "after", "timestamp": "2025-04-01T00:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Too late"},
    ]
    mem_fs[str(d)] = "".join(json.dumps(e) + "\n" for e in entries)

    results = j.search_by_date_range(
        datetime(2025, 3, 1, 0, 0, 0, tzinfo=UTC),
        datetime(2025, 3, 31, 23, 59, 59, tzinfo=UTC),
    )
    assert len(results) == 3
    returned_ids = {r.entry_id for r in results}
    assert "before" not in returned_ids
    assert "after" not in returned_ids
    assert returned_ids == {"in1", "in2", "in3"}


def test_search_by_date_range_exact_start_match(mem_fs: dict[str, str]) -> None:
    """An entry whose timestamp exactly equals start is included."""
    j = _make_journal(mem_fs)
    d = FAKE_DIR / "journal.jsonl"
    entries = [
        {"entry_id": "exact", "timestamp": "2025-09-01T00:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Exact start"},
    ]
    mem_fs[str(d)] = "".join(json.dumps(e) + "\n" for e in entries)

    results = j.search_by_date_range(
        datetime(2025, 9, 1, 0, 0, 0, tzinfo=UTC),
        datetime(2025, 9, 30, tzinfo=UTC),
    )
    assert len(results) == 1
    assert results[0].entry_id == "exact"


def test_search_by_date_range_exact_end_match(mem_fs: dict[str, str]) -> None:
    """An entry whose timestamp exactly equals end is included."""
    j = _make_journal(mem_fs)
    d = FAKE_DIR / "journal.jsonl"
    entries = [
        {"entry_id": "exact", "timestamp": "2025-09-30T00:00:00+00:00",
         "stage": "awakening", "triggered_by": "agent_tool", "entry_type": "reflection", "content": "Exact end"},
    ]
    mem_fs[str(d)] = "".join(json.dumps(e) + "\n" for e in entries)

    results = j.search_by_date_range(
        datetime(2025, 9, 1, tzinfo=UTC),
        datetime(2025, 9, 30, 0, 0, 0, tzinfo=UTC),
    )
    assert len(results) == 1
    assert results[0].entry_id == "exact"


def test_write_after_partial_tail_keeps_new_entry(mem_fs: dict[str, str]) -> None:
    """A crash fragment at EOF must not swallow the next journal entry (#4164)."""
    j = _make_journal(mem_fs)
    mem_fs[str(FAKE_DIR / "journal.jsonl")] = '{"entry_id": "crashed", "cont'
    entry = j.write_thought("survives the crash", triggered_by="agent_tool")
    assert entry.entry_id in [e.entry_id for e in j.read_recent(10)]


def test_write_uses_file_lock(mem_fs: dict[str, str]) -> None:
    """Journal appends serialize on ``file_lock`` like every JSONL append."""
    j = _make_journal(mem_fs)
    called_with: list[Any] = []

    @contextmanager
    def tracking_lock(path: Any) -> Iterator[None]:
        called_with.append(path)
        yield

    with patch("maestro.fileutil.file_lock", tracking_lock):
        j.write_thought("locked", triggered_by="agent_tool")
    assert called_with == [FAKE_DIR / "journal.jsonl"]


# ── inner thought vs operational events ──────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _legacy_row(entry_id: str, triggered_by: str | None, content: str) -> str:
    return json.dumps({
        "entry_id": entry_id, "timestamp": f"2025-03-0{entry_id[-1]}T00:00:00+00:00",
        "stage": "awakening", "entry_type": "milestone", "content": content,
        "triggered_by": triggered_by,
    }) + "\n"


def test_inner_thought_sources_are_the_model_authored_seams() -> None:
    expected = frozenset({"contemplation", "agent_tool"})
    assert expected == INNER_THOUGHT_SOURCES
    assert is_inner_thought("contemplation") is True
    assert is_inner_thought("pre_deploy") is False
    assert is_inner_thought(None) is False


def test_legacy_evolution_row_still_reads_as_thought(mem_fs: dict[str, str]) -> None:
    # Proposal 155 step 4: reflect is deleted, but its historical rows stay thought.
    assert is_inner_thought("evolution") is True
    j = _make_journal(mem_fs)
    mem_fs[str(j._path)] = _legacy_row("e1", "evolution", "an old reflection")
    assert [e.content for e in j._load_all()] == ["an old reflection"]


def test_write_thought_refuses_retired_evolution_source(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    with pytest.raises(ValueError, match="not an inner-thought source"):
        j.write_thought(
            content="x", entry_type="reflection",
            triggered_by="evolution",  # type: ignore[arg-type]
        )


def test_write_thought_refuses_operational_trigger(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    with pytest.raises(ValueError, match="record_event"):
        j.write_thought(
            "Pre-deploy notice", "milestone",
            triggered_by="pre_deploy",  # type: ignore[arg-type]
        )
    assert str(FAKE_DIR / "journal.jsonl") not in mem_fs


def test_record_event_writes_events_file_not_journal(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    entry = j.record_event(
        "Shutting down gracefully.", entry_type="milestone",
        triggered_by="shutdown",
    )
    assert str(FAKE_DIR / "journal.jsonl") not in mem_fs
    assert "Shutting down gracefully." in mem_fs[str(FAKE_DIR / "events.jsonl")]
    assert j.read_recent() == []
    assert j.get_recent_narrative() == ""
    assert [e.entry_id for e in j.read_recent_events()] == [entry.entry_id]
    assert entry.triggered_by == "shutdown"


def test_record_event_rejects_empty_content(mem_fs: dict[str, str]) -> None:
    j = _make_journal(mem_fs)
    with pytest.raises(ValueError, match="non-empty"):
        j.record_event("  ", entry_type="milestone", triggered_by="shutdown")


def test_legacy_operational_rows_move_to_event_view(mem_fs: dict[str, str]) -> None:
    """Pre-split operational and null-tagged rows never reach the narrative."""
    mem_fs[str(FAKE_DIR / "journal.jsonl")] = (
        _legacy_row("e1", "pre_deploy", "Merged PRs landing in this deploy: #1")
        + _legacy_row("e2", "contemplation", "I keep circling the same door.")
        + _legacy_row("e3", None, "unattributed row")
    )
    j = _make_journal(mem_fs)
    later = j.record_event("Waking up.", entry_type="milestone", triggered_by="startup")

    assert [e.entry_id for e in j.read_recent()] == ["e2"]
    assert "Merged PRs" not in j.get_recent_narrative()
    assert "Merged PRs" not in j.get_full_narrative()
    assert j.search("deploy") == []
    # Legacy rows come first, then events.jsonl's own rows.
    assert [e.entry_id for e in j.read_recent_events(10)] == ["e1", "e3", later.entry_id]


def test_event_readers(mem_fs: dict[str, str]) -> None:
    mem_fs[str(FAKE_DIR / "journal.jsonl")] = (
        _legacy_row("e1", "budget_throttle_enter", "throttle on")
        + _legacy_row("e5", "budget_throttle_exit", "throttle off")
    )
    j = _make_journal(mem_fs)
    audit = j.record_event(
        "audit finding", entry_type="audit_observation",
        triggered_by="audit_self_model",
    )

    assert j.read_recent_events(0) == []
    assert [e.entry_id for e in j.read_recent_events(1)] == [audit.entry_id]
    assert len(j.read_events_by_type("milestone")) == 2
    assert len(j.read_events_by_type("audit_observation")) == 1
    in_range = j.events_by_date_range(
        datetime(2025, 3, 1, tzinfo=UTC), datetime(2025, 3, 3, tzinfo=UTC),
    )
    assert [e.entry_id for e in in_range] == ["e1"]


def test_production_write_thought_callers_use_inner_sources() -> None:
    """Every production ``write_thought`` call names a literal inner source.

    mypy enforces the ``InnerThoughtSource`` literal where the journal is
    typed; this pins it where a receiver is ``Any`` or the call is ignored.
    """
    offenders: list[str] = []
    for path in sorted((_REPO_ROOT / "maestro").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "write_thought"
            ):
                continue
            trigger = next(
                (k.value for k in node.keywords if k.arg == "triggered_by"), None,
            )
            if not (
                isinstance(trigger, ast.Constant)
                and trigger.value in INNER_THOUGHT_SOURCES
            ):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno}")
    assert offenders == []


def test_new_rows_carry_no_stage_key(mem_fs: dict[str, str]) -> None:
    """Proposal 148 Step 5: neither write door persists the retired stage label."""
    j = _make_journal(mem_fs)
    j.write_thought("A thought", triggered_by="contemplation")
    j.record_event(
        "Engagement claimed", entry_type="realization", triggered_by="engagement:q1",
    )
    thought = json.loads(mem_fs[str(FAKE_DIR / "journal.jsonl")].strip())
    event = json.loads(mem_fs[str(FAKE_DIR / "events.jsonl")].strip())
    assert "stage" not in thought
    assert "stage" not in event


def test_legacy_stage_rows_round_trip_without_stage(mem_fs: dict[str, str]) -> None:
    """Proposal 148 Step 5: a legacy row carrying ``stage`` still loads from
    both files, and the label is dropped on read."""
    row = {
        "entry_id": "e1", "timestamp": "2025-01-01T00:00:00Z",
        "stage": "awakening", "entry_type": "reflection",
        "content": "Legacy thought", "triggered_by": "contemplation",
    }
    mem_fs[str(FAKE_DIR / "journal.jsonl")] = json.dumps(row) + "\n"
    mem_fs[str(FAKE_DIR / "events.jsonl")] = json.dumps(
        {**row, "entry_id": "e2", "content": "Legacy event"},
    ) + "\n"
    j = _make_journal(mem_fs)
    (thought,) = j.read_recent()
    (event,) = j.read_recent_events()
    assert thought.content == "Legacy thought"
    assert event.content == "Legacy event"
    assert not hasattr(thought, "stage")
    assert not hasattr(event, "stage")


def test_write_doors_take_no_stage_parameter() -> None:
    """Proposal 148 Step 6: the retired label is no longer a write argument."""
    import inspect

    assert "stage" not in inspect.signature(Journal.write_thought).parameters
    assert "stage" not in inspect.signature(Journal.record_event).parameters
