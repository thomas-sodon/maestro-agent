"""Maestro's persistent diary — thoughts, not logs.

The journal (``journal.jsonl``) holds only text the model authored in its own
voice, from the seams :data:`~maestro.types.INNER_THOUGHT_SOURCES` names; its
tail is read into the contemplation and planning prompts.
Every code-templated record — a deploy notice, a merge, a lifecycle milestone,
plan bookkeeping, an audit finding, a task outcome — goes through
:meth:`Journal.record_event` to ``events.jsonl``, which no prompt reads.

Rows written before that split live in ``journal.jsonl`` under an operational
``triggered_by``. They are normalised on read rather than rewritten (the file
is append-only): the journal readers skip them and the event readers serve them
ahead of ``events.jsonl``'s own rows, so event consumers keep their history.
A row with a null or unrecognised ``triggered_by`` is routed as an event too:
every genuine-thought seam has always passed one of the inner-thought sources
(a production journal of ~12.9k rows held no null-tagged row), so an
unattributed row is not model-authored thought.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

from maestro.fileutil import append_jsonl_row
from maestro.search import EphemeralJournalSearch, JournalSearchBackend
from maestro.types import (
    INNER_THOUGHT_SOURCES,
    VALID_JOURNAL_ENTRY_TYPES,
    InnerThoughtSource,
    JournalEntryType,
)


def _ensure_utc(dt: datetime) -> datetime:
    """Normalize a datetime to UTC — naive datetimes are treated as UTC.

    An *aware* value is returned unchanged, at its own offset: that is enough
    to make two values mutually comparable, which is all this helper's callers
    (here and in ``health_check``) ask for. ``cost_tracking._as_utc`` is the
    named sibling applying the same naive-is-UTC reading to stored cost-sample
    stamps; it parses from ISO and additionally ``astimezone``-converts,
    because it renders its result. The two are kept separate rather than folded
    — an infra module importing this state-and-memory one for two lines is the
    worse trade — so a change to how a naive value is read must change both.
    """
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


@dataclass
class JournalEntry:
    entry_id: str
    timestamp: str
    entry_type: JournalEntryType
    content: str
    triggered_by: str | None = None


logger = logging.getLogger("maestro")

#: Journal ``entry_type`` values retired with their producer, mapped to the
#: live type a legacy row now reads as. ``purpose_hypothesis`` rows were written
#: by the deleted purpose engine and ``dream`` rows by the deleted dream digest
#: (proposal 149); they read as reflections so history still loads and filters
#: under a valid type.
_LEGACY_ENTRY_TYPES: dict[str, str] = {
    "purpose_hypothesis": "reflection",
    "dream": "reflection",
}


#: Retired inner-thought sources whose historical rows still read as thought:
#: ``evolution`` was the deleted planning reflection (proposal 155 step 4).
#: Read only by :func:`is_inner_thought`; :meth:`Journal.write_thought` admits
#: just the live :data:`INNER_THOUGHT_SOURCES`.
_LEGACY_THOUGHT_SOURCES: frozenset[str] = frozenset({"evolution"})


def is_inner_thought(triggered_by: str | None) -> bool:
    """Return whether a row with *triggered_by* belongs in the journal."""
    return triggered_by in INNER_THOUGHT_SOURCES or triggered_by in _LEGACY_THOUGHT_SOURCES


def _in_date_range(
    entries: list[JournalEntry], start: datetime, end: datetime,
) -> list[JournalEntry]:
    """Return *entries* timestamped within [start, end], oldest first.

    Timezone-naive boundaries and timestamps are read as UTC; an unparseable
    timestamp is skipped.
    """
    start_utc = _ensure_utc(start)
    end_utc = _ensure_utc(end)
    matched: list[tuple[datetime, JournalEntry]] = []
    for entry in entries:
        try:
            ts = _ensure_utc(datetime.fromisoformat(entry.timestamp))
        except (ValueError, TypeError):
            continue
        if start_utc <= ts <= end_utc:
            matched.append((ts, entry))
    matched.sort(key=lambda pair: pair[0])
    return [entry for _, entry in matched]


class Journal:
    def __init__(
        self,
        memory_dir: Path,
        *,
        search_index: JournalSearchBackend | None = None,
    ) -> None:
        self._dir = memory_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "journal.jsonl"
        self._events_path = self._dir / "events.jsonl"
        # Default to the per-call ephemeral rebuild so every un-injected
        # construction site (the read-only CLI sites, every test) keeps today's
        # exact behaviour; the loop-owning build site injects the persistent
        # ``state.db`` backend under ``[paths] sqlite_state_enabled`` (proposal
        # 106).
        self._search_index: JournalSearchBackend = (
            search_index if search_index is not None else EphemeralJournalSearch()
        )

    def write_thought(
        self,
        content: str,
        entry_type: JournalEntryType = "reflection",
        *,
        triggered_by: InnerThoughtSource,
    ) -> JournalEntry:
        """Write a model-authored thought to the journal and return the entry.

        Refuses a *triggered_by* outside the inner-thought sources: an
        operational record belongs in :meth:`record_event`.
        """
        if triggered_by not in INNER_THOUGHT_SOURCES:
            raise ValueError(
                f"triggered_by {triggered_by!r} is not an inner-thought source "
                f"{sorted(INNER_THOUGHT_SOURCES)}; record operational entries "
                "with Journal.record_event"
            )
        entry = self._build_entry(content, entry_type, triggered_by)
        # Locked, tail-repairing append: a crash fragment at EOF would
        # otherwise swallow this entry into one undecodable line.
        append_jsonl_row(self._path, asdict(entry))
        return entry

    def record_event(
        self,
        content: str,
        *,
        entry_type: JournalEntryType,
        triggered_by: str,
    ) -> JournalEntry:
        """Append a code-templated operational record to ``events.jsonl``.

        Durable for the consumers that count or replay these records, and never
        read into a prompt.
        """
        entry = self._build_entry(content, entry_type, triggered_by)
        append_jsonl_row(self._events_path, asdict(entry))
        return entry

    @staticmethod
    def _build_entry(
        content: str,
        entry_type: JournalEntryType,
        triggered_by: str,
    ) -> JournalEntry:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        return JournalEntry(
            entry_id=uuid.uuid4().hex[:12],
            timestamp=datetime.now(UTC).isoformat(),
            entry_type=entry_type,
            content=content,
            triggered_by=triggered_by,
        )

    def _read_rows(self, path: Path) -> list[JournalEntry]:
        if not path.exists():
            return []
        entries = []
        for line in path.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                known = {f.name for f in fields(JournalEntry)}
                row = {k: v for k, v in raw.items() if k in known}
                legacy_type = row.get("entry_type")
                if legacy_type in _LEGACY_ENTRY_TYPES:
                    row["entry_type"] = _LEGACY_ENTRY_TYPES[legacy_type]
                entries.append(JournalEntry(**row))
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                logger.warning("Skipping corrupted journal entry in %s", path)
                continue
        return entries

    def _load_all(self) -> list[JournalEntry]:
        return [
            e for e in self._read_rows(self._path) if is_inner_thought(e.triggered_by)
        ]

    def _load_events(self) -> list[JournalEntry]:
        legacy = [
            e for e in self._read_rows(self._path)
            if not is_inner_thought(e.triggered_by)
        ]
        return legacy + self._read_rows(self._events_path)

    def read_recent_events(self, n: int = 20) -> list[JournalEntry]:
        if n <= 0:
            return []
        return self._load_events()[-n:]

    def read_events_by_type(self, entry_type: str) -> list[JournalEntry]:
        return [e for e in self._load_events() if e.entry_type == entry_type]

    def events_by_date_range(
        self, start: datetime, end: datetime,
    ) -> list[JournalEntry]:
        """Operational records within [start, end] inclusive, oldest first."""
        return _in_date_range(self._load_events(), start, end)

    def read_recent(self, n: int = 20) -> list[JournalEntry]:
        if n <= 0:
            return []
        return self._load_all()[-n:]

    def read_by_type(self, entry_type: str) -> list[JournalEntry]:
        if entry_type not in VALID_JOURNAL_ENTRY_TYPES:
            raise ValueError(
                f"Invalid entry_type {entry_type!r}. "
                f"Valid types: {sorted(VALID_JOURNAL_ENTRY_TYPES)}"
            )
        return [e for e in self._load_all() if e.entry_type == entry_type]

    def search_by_date_range(
        self, start: datetime, end: datetime,
    ) -> list[JournalEntry]:
        """Return entries whose timestamp falls within [start, end] inclusive.

        Results are sorted by timestamp ascending.  Timezone-naive
        boundaries and entry timestamps are treated as UTC for
        comparison purposes.  Unparseable timestamps are silently
        skipped.
        """
        return _in_date_range(self._load_all(), start, end)

    def search(self, query: str) -> list[JournalEntry]:
        """Return journal entries relevant to ``query``, ranked by BM25.

        Entries with non-string content are filtered out before ranking.
        Results are sorted descending by BM25 relevance score.  Returns
        an empty list for empty queries, empty journals, or when no
        entry contains any of the query's tokens.
        """
        entries = [e for e in self._load_all() if isinstance(e.content, str)]
        if not entries:
            return []
        corpus = [e.content for e in entries]
        return [entries[i] for i in self._search_index.rank(corpus, query)]

    def format_entry_header(self, entry: JournalEntry) -> str:
        """Render the ``[timestamp | entry_type]`` header of *entry*."""
        return f"[{entry.timestamp} | {entry.entry_type}]"

    def get_full_narrative(self) -> str:
        """Returns the complete journal as a readable narrative."""
        entries = self._load_all()
        if not entries:
            return ""
        parts = []
        for e in entries:
            if not isinstance(e.content, str):
                continue
            parts.append(f"{self.format_entry_header(e)}\n{e.content}")
        return "\n\n---\n\n".join(parts)

    def get_recent_narrative(
        self, max_entries: int = 50, max_chars: int = 3000,
    ) -> str:
        """Returns a bounded narrative from the most recent journal entries."""
        entries = self._load_all()[-max_entries:] if max_entries > 0 else []
        if not entries:
            return ""
        parts = []
        for e in entries:
            if not isinstance(e.content, str):
                continue
            parts.append(f"{self.format_entry_header(e)}\n{e.content}")
        if max_chars <= 0:
            return ""
        # Find the latest suffix of parts that fits within max_chars.
        # Use a backward pass with a start-index pointer to avoid O(n²)
        # behaviour from repeated join() + pop(0) calls.
        _SEP = "\n\n---\n\n"
        _sep_len = len(_SEP)
        total = 0
        start = len(parts)  # sentinel: nothing selected yet
        for i in range(len(parts) - 1, -1, -1):
            needed = len(parts[i]) + (_sep_len if start < len(parts) else 0)
            if total + needed > max_chars:
                break
            total += needed
            start = i
        if start >= len(parts):
            # Fall back: truncate the most recent entry rather than returning nothing
            return parts[-1][:max_chars] if parts else ""
        return _SEP.join(parts[start:])
