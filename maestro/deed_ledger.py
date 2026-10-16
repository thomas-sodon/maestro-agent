"""Deed ledger — a first-class record of fired terminal acts.

Maestro's apparatus for measuring the *treadmill* is rich: ``pr-lap-ledger``
counts remediation laps, the settlement ledger tracks merges, the version
archive records landings. But nothing records the terminal acts that actually
*end* a lineage — the ``gh pr close`` disposal, the delivered handoff, the owned
prune. Their absence made "refuse-the-lap-is-mine has zero instances" an
abstraction rather than a watched fact.

This module is the missing column. :func:`record_deed` appends one durable row
per fired terminal act to ``deed_ledger.jsonl`` (UTF-8, append-only, atomic under
:func:`~maestro.fileutil.file_lock`); :func:`count_deeds` and
:func:`recent_deeds` read it back. The contemplation-facing
:func:`format_terminal_deeds_summary` surfaces a ``TERMINAL-DEEDS: N fired vs M
laps`` tag so the count of terminal acts is measured against the count of laps it
should be replacing.

Reads mirror :meth:`maestro.memory.Memory.get_recent_tasks`: the ledger is walked
newest-first via :func:`~maestro.fileutil.iter_jsonl_reverse` and corrupted
records are skipped within a bounded scan, so a corrupted tail never forces a
full-file walk. Pure reads, zero SDK cost.
"""

from __future__ import annotations

import enum
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from maestro.fileutil import file_lock, iter_jsonl_reverse

logger = logging.getLogger(__name__)

#: The append-only ledger file, a sibling of the other per-``data_dir`` JSONL
#: state (``task_history.jsonl``, ``version_archive.jsonl``, …).
DEED_LEDGER_FILENAME = "deed_ledger.jsonl"

#: Bound on the number of corrupted records a single scan will skip before it
#: terminates with a partial result, mirroring the ``get_recent_tasks`` cap so a
#: corrupted tail can never force a full-file walk.
MAX_CORRUPT_SKIP = 256


class DeedType(enum.Enum):
    """The terminal acts a deed row can record.

    Each ends a PR lineage rather than re-lapping it: a reversible disposal
    (:attr:`PR_CLOSE`), a delivered merge-or-close handoff
    (:attr:`HANDOFF_DELIVERED`), a merge (:attr:`MERGE`), or an owned prune of a
    redundant ``pending_review`` plan (:attr:`OWNED_PRUNE`).
    """

    PR_CLOSE = "pr_close"
    HANDOFF_DELIVERED = "handoff_delivered"
    MERGE = "merge"
    OWNED_PRUNE = "owned_prune"


@dataclass(frozen=True)
class Deed:
    """One fired terminal act.

    ``pr_number`` is ``None`` for a deed that ends no single PR (an owned prune
    targets ``pending_review`` plans, not a PR). ``timestamp`` is an ISO-8601
    UTC string, so lexical order equals chronological order for the ``since``
    window comparison.
    """

    deed_type: str
    pr_number: int | None
    detail: str
    timestamp: str


@runtime_checkable
class DeedLedgerBackend(Protocol):
    """The deed-ledger persistence surface (proposal 106).

    Three writers in the agent loop (the delivered handoff, the ``gh pr close``
    disposal, the owned prune) and two readers (the ``TERMINAL-DEEDS``
    contemplation tag and the ``deed-ledger`` CLI) go through this seam, so the
    ledger can live in ``deed_ledger.jsonl`` or in ``state.db`` behind ``[paths]
    sqlite_state_enabled`` with no call-site branching. Mirrors the
    :class:`~maestro.cost_tracking.CostSampleBackend` idiom;
    ``@runtime_checkable`` enables a structural ``isinstance`` check in the
    agent's backend-selection tests.
    """

    def record(
        self,
        deed_type: DeedType | str,
        pr_number: int | None,
        detail: str,
        *,
        now: str | None = None,
    ) -> Deed:
        """Append exactly one deed and return it."""
        ...

    def count(self, *, since: str | None = None) -> int:
        """Count recorded deeds, optionally windowed to *since* onward."""
        ...

    def recent(self, n: int = 20) -> list[Deed]:
        """Return the *n* most-recent deeds, newest-first."""
        ...


def build_deed(
    deed_type: DeedType | str,
    pr_number: int | None,
    detail: str,
    *,
    now: str | None = None,
) -> Deed:
    """Construct a :class:`Deed`, stamping and coercing as the writers require.

    The shared construction path for both the JSONL :func:`record_deed` and the
    SQLite :class:`~maestro.state_store.SqliteDeedLedger` adapter, so the
    timestamp default and the :class:`DeedType`-to-``value`` coercion can never
    drift between substrates. *deed_type* accepts either a :class:`DeedType` (its
    ``value`` is stored) or a raw string. *now* is an injection seam for
    deterministic tests; it defaults to the current UTC time in ISO-8601.
    """
    stamp = now if now is not None else datetime.now(UTC).isoformat()
    type_value = deed_type.value if isinstance(deed_type, DeedType) else deed_type
    return Deed(
        deed_type=type_value,
        pr_number=pr_number,
        detail=detail,
        timestamp=stamp,
    )


def record_deed(
    data_dir: Path,
    deed_type: DeedType | str,
    pr_number: int | None,
    detail: str,
    *,
    now: str | None = None,
) -> Deed:
    """Append exactly one deed row to ``deed_ledger.jsonl`` and return it.

    Append-only and atomic: the write is guarded by
    :func:`~maestro.fileutil.file_lock` and opens the file in ``"a"`` mode with
    UTF-8 encoding, so a concurrent tend/loop writer can never interleave a
    half-line. *deed_type* accepts either a :class:`DeedType` (its ``value`` is
    stored) or a raw string. *now* is an injection seam for deterministic tests;
    it defaults to the current UTC time in ISO-8601.
    """
    deed = build_deed(deed_type, pr_number, detail, now=now)
    path = data_dir / DEED_LEDGER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps(
        {
            "deed_type": deed.deed_type,
            "pr_number": deed.pr_number,
            "detail": deed.detail,
            "timestamp": deed.timestamp,
        },
    )
    with file_lock(path), open(path, "a", encoding="utf-8") as f:
        f.write(record + "\n")
    return deed


def _parse_deed(data: dict[str, object]) -> Deed | None:
    """Return a :class:`Deed` from a decoded row, or ``None`` when malformed.

    A record missing ``deed_type``/``detail``/``timestamp``, or carrying a
    non-string in one of them (or a non-int/non-null ``pr_number``), is treated
    as corrupted and skipped by the reader's bounded scan.
    """
    deed_type = data.get("deed_type")
    detail = data.get("detail")
    timestamp = data.get("timestamp")
    pr_number = data.get("pr_number")
    if (
        not isinstance(deed_type, str)
        or not isinstance(detail, str)
        or not isinstance(timestamp, str)
    ):
        return None
    if pr_number is not None and not isinstance(pr_number, int):
        return None
    return Deed(
        deed_type=deed_type,
        pr_number=pr_number,
        detail=detail,
        timestamp=timestamp,
    )


def _iter_deeds(
    data_dir: Path, *, max_corrupt: int,
) -> Iterator[Deed]:
    """Yield deeds newest-first, skipping corrupted records within a bound.

    Walks ``deed_ledger.jsonl`` backwards via
    :func:`~maestro.fileutil.iter_jsonl_reverse`. Corrupted records are skipped
    up to *max_corrupt*; after that the walk terminates with a partial result so
    a burst of corrupted lines in the file tail never forces a full-file scan
    (mirroring :meth:`~maestro.memory.Memory.get_recent_tasks`).
    """
    path = data_dir / DEED_LEDGER_FILENAME
    if not path.exists():
        return
    skipped = 0
    for data in iter_jsonl_reverse(path):
        deed = _parse_deed(data)
        if deed is None:
            skipped += 1
            logger.warning("Skipping corrupted deed record in %s", path)
            if skipped >= max_corrupt:
                logger.warning(
                    "Stopped scanning %s after skipping %d corrupted records",
                    path, skipped,
                )
                return
            continue
        yield deed


def count_deeds(
    data_dir: Path,
    *,
    since: str | None = None,
    max_corrupt: int = MAX_CORRUPT_SKIP,
) -> int:
    """Count recorded deeds, optionally windowed to *since* onward.

    *since* is an ISO-8601 UTC timestamp; only deeds stamped at or after it are
    counted. Because the ledger is append-only and walked newest-first, the scan
    stops as soon as it reaches a deed older than *since*. Corrupted records are
    skipped within *max_corrupt* (the file-tail bound). Pure read.
    """
    count = 0
    for deed in _iter_deeds(data_dir, max_corrupt=max_corrupt):
        if since is not None and deed.timestamp < since:
            break
        count += 1
    return count


def recent_deeds(data_dir: Path, n: int = 20) -> list[Deed]:
    """Return the *n* most-recent deeds, newest-first.

    Corrupted records are skipped up to a cap of *n*, so a corrupted tail yields
    a partial result instead of a full-file walk. Returns an empty list for a
    non-positive *n* or an absent ledger.
    """
    if n <= 0:
        return []
    deeds: list[Deed] = []
    for deed in _iter_deeds(data_dir, max_corrupt=n):
        deeds.append(deed)
        if len(deeds) >= n:
            break
    return deeds


def format_terminal_deeds_summary(deed_count: int, lap_count: int) -> str:
    """Return the ``TERMINAL-DEEDS`` contemplation tag, or ``""`` when empty.

    Surfaces ``TERMINAL-DEEDS: {deed_count} fired vs {lap_count} laps`` so the
    count of terminal acts fired is watched against the count of remediation
    laps they should be replacing. Both counts must cover the same window — the
    trailing ``_UNLANDED_REMEDIATION_CYCLES`` task records the lap measure reads
    — or a stale all-time deed total masks laps piling up. Self-suppresses
    (returns ``""``) only when both counts are zero — an empty window has
    nothing to report.
    """
    if deed_count == 0 and lap_count == 0:
        return ""
    return f"TERMINAL-DEEDS: {deed_count} fired vs {lap_count} laps"


def load_all_deeds(data_dir: Path) -> list[Deed]:
    """Return every recorded deed oldest-first — the SQLite seed source.

    :func:`_iter_deeds` walks the ledger newest-first (honouring the same
    :data:`MAX_CORRUPT_SKIP` file-tail bound as the other reads), so the result
    is reversed to oldest-first. Seeding the SQLite store in chronological order
    lets its ``AUTOINCREMENT`` ``id`` reproduce the append order, so a later
    ``ORDER BY id DESC`` read matches the JSONL newest-first walk. A real call
    site (:class:`~maestro.state_store.SqliteDeedLedger`'s one-time seed), so it
    does not trip the self-review uncalled-symbol gate.
    """
    deeds = list(_iter_deeds(data_dir, max_corrupt=MAX_CORRUPT_SKIP))
    deeds.reverse()
    return deeds


class JsonlDeedLedger:
    """JSONL-backed :class:`DeedLedgerBackend` — the default, flag-off store.

    Binds a ``data_dir`` and delegates the three backend methods to the existing
    module-level :func:`record_deed` / :func:`count_deeds` / :func:`recent_deeds`,
    which remain the JSONL implementation core so ``tests/test_deed_ledger.py``
    and this adapter share one code path (behaviour-preserving: the ledger stays
    on ``deed_ledger.jsonl``).
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def record(
        self,
        deed_type: DeedType | str,
        pr_number: int | None,
        detail: str,
        *,
        now: str | None = None,
    ) -> Deed:
        return record_deed(
            self._data_dir, deed_type, pr_number, detail, now=now,
        )

    def count(self, *, since: str | None = None) -> int:
        return count_deeds(self._data_dir, since=since)

    def recent(self, n: int = 20) -> list[Deed]:
        return recent_deeds(self._data_dir, n)
