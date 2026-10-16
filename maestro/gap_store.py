"""The loop-owned SQLite store for the gap-fuel ledgers (proposal 155).

One file, ``gaps.db`` under ``memory_dir``, in WAL mode so read-only readers
never block the loop's writer. Every table is declared in the closed
:data:`GAP_TABLES` registry: the ``gap_opens`` provenance ledger and the
``red_main_watch`` row. Neither declares terminal statuses, so nothing ages
out of the file.

:func:`build_gap_store` is the only constructor (the ``gap_store_owner``
guard in ``tests/structural_guards.py``). It is pure: no secrets, no code host,
and independent of ``[paths] sqlite_state_enabled``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from maestro.state_store import _BUSY_TIMEOUT_MS

if TYPE_CHECKING:
    from maestro.config import Config

GAP_DB_FILENAME = "gaps.db"
#: The retired family store these ledgers lived in before ``gaps.db``;
#: :func:`carry_legacy_gap_rows` moves its gap rows across once.
LEGACY_CHARTER_DB_FILENAME = "charter.db"
#: The name the legacy file is renamed to once its rows are carried.
LEGACY_CHARTER_CARRIED_FILENAME = "charter.carried.db"
#: Stored in ``PRAGMA user_version``; a newer file is refused.
GAP_SCHEMA_VERSION = 1

#: The column every derived-content table must declare NOT NULL, no default.
UNTRUSTED_INPUT_COLUMN = "untrusted_input"


class GapStoreSchemaError(RuntimeError):
    """The file's schema is newer than this code, or a table breaks a rule."""


class GapStoreReadOnly(RuntimeError):
    """A write was attempted on a read-only store."""


@dataclass(frozen=True)
class LedgerTable:
    """One ledger table's DDL.

    ``columns`` are ``"<name> <type> [constraints]"`` fragments; ``indexes``
    name single columns to index.
    """

    name: str
    columns: tuple[str, ...]
    indexes: tuple[str, ...] = ()
    holds_derived_content: bool = False

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.split()[0] for c in self.columns)


#: Every gap issue the :mod:`maestro.evolution_fuel` door opened or reopened:
#: the provenance a ``rederived`` trust verdict reads. Not one row per open: a
#: reused issue's new-evidence comment also inserts a row (fresh
#: ``decision_ref``, ``opened_at`` = comment time), and a reopen's own row
#: carries a ``reopen:``-prefixed ref, so count opens by that, not rows.
GAP_OPENS_TABLE = LedgerTable(
    name="gap_opens",
    columns=(
        "id INTEGER PRIMARY KEY AUTOINCREMENT",
        "owner_id TEXT NOT NULL",
        "label TEXT NOT NULL",
        "opener TEXT NOT NULL",
        "issue_number INTEGER NOT NULL",
        "decision_ref TEXT NOT NULL",
        f"{UNTRUSTED_INPUT_COLUMN} INTEGER NOT NULL",
        "opened_at TEXT NOT NULL",
        "closed_at TEXT",
        "close_ref TEXT",
    ),
    indexes=("owner_id", "issue_number"),
    holds_derived_content=True,
)

#: The origin/main red-watch row: when the current set of failing check names
#: first read red, so the red-main detector opens its gate issue only past
#: ``[evolution_fuel] red_main_grace_hours``. Read and written only through
#: :mod:`maestro.evolution_fuel`. A green read deletes the row.
RED_MAIN_TABLE = LedgerTable(
    name="red_main_watch",
    columns=(
        "ref TEXT PRIMARY KEY",
        "first_red_sha TEXT NOT NULL",
        "first_red_at TEXT NOT NULL",
        "failing_checks TEXT NOT NULL",
        "last_read_sha TEXT NOT NULL",
        "last_read_at TEXT NOT NULL",
    ),
)

#: The closed registry of gap-store tables.
GAP_TABLES: tuple[LedgerTable, ...] = (GAP_OPENS_TABLE, RED_MAIN_TABLE)


_UTC_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utc_stamp(dt: datetime) -> str:
    """Return the one fixed ISO form every ledger time column stores."""
    return dt.astimezone(UTC).strftime(_UTC_STAMP_FORMAT)


def parse_utc_stamp(stamp: str) -> datetime:
    """Parse a :func:`utc_stamp` value back to an aware UTC datetime."""
    return datetime.strptime(stamp, _UTC_STAMP_FORMAT).replace(tzinfo=UTC)


class GapStore:
    """A connection to ``gaps.db``. Build only via :func:`build_gap_store`."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        tables: tuple[LedgerTable, ...],
        read_only: bool,
    ) -> None:
        self._conn = connection
        self._tables = tables
        self.read_only = read_only
        self._conn.row_factory = sqlite3.Row
        # Explicit transactions only: ``transaction()`` issues BEGIN IMMEDIATE.
        self._conn.isolation_level = None
        self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > GAP_SCHEMA_VERSION:
            raise GapStoreSchemaError(
                f"gaps.db schema version {version} is newer than "
                f"{GAP_SCHEMA_VERSION}"
            )
        if read_only:
            return
        self._conn.execute("PRAGMA journal_mode=WAL")
        for table in tables:
            self._create(table)
            self._check_columns(table)
            self._check_derived_content(table)
        self._conn.execute(f"PRAGMA user_version = {GAP_SCHEMA_VERSION}")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def tables(self) -> tuple[LedgerTable, ...]:
        return self._tables

    def _create(self, table: LedgerTable) -> None:
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table.name} ({', '.join(table.columns)})"
        )
        for column in table.indexes:
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS ix_{table.name}_{column} "
                f"ON {table.name} ({column})"
            )

    def _check_columns(self, table: LedgerTable) -> None:
        """Refuse an on-disk table whose columns drifted from its declaration.

        ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so a
        column added to a ``LedgerTable`` would otherwise fail only at INSERT.
        """
        on_disk = tuple(
            row["name"]
            for row in self._conn.execute(f"PRAGMA table_info({table.name})")
        )
        if on_disk != table.column_names:
            raise GapStoreSchemaError(
                f"gap table {table.name!r} on disk has columns {on_disk}, "
                f"declared {table.column_names}; no migration path exists"
            )

    def _check_derived_content(self, table: LedgerTable) -> None:
        if not table.holds_derived_content:
            return
        info = {
            row["name"]: row
            for row in self._conn.execute(f"PRAGMA table_info({table.name})")
        }
        col = info.get(UNTRUSTED_INPUT_COLUMN)
        if col is None or col["notnull"] != 1 or col["dflt_value"] is not None:
            raise GapStoreSchemaError(
                f"derived-content table {table.name!r} must declare "
                f"{UNTRUSTED_INPUT_COLUMN} NOT NULL with no default"
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run the block in one BEGIN IMMEDIATE; roll back and re-raise on error."""
        if self.read_only:
            raise GapStoreReadOnly("gap store opened read-only")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            # SQLite may already have rolled back (SQLITE_FULL, IOERR, NOMEM);
            # a second ROLLBACK would mask the original error.
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def journal_mode(self) -> str:
        return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0])

    def close(self) -> None:
        self._conn.close()


def build_gap_store(
    config: Config,
    *,
    read_only: bool = False,
    connection: sqlite3.Connection | None = None,
    tables: tuple[LedgerTable, ...] = GAP_TABLES,
) -> GapStore:
    """Open the gap store: the only constructor of :class:`GapStore`."""
    if connection is None:
        path = config.memory_dir / GAP_DB_FILENAME
        if read_only:
            connection = sqlite3.connect(
                f"{path.absolute().as_uri()}?mode=ro", uri=True, check_same_thread=False,
            )
        else:
            connection = sqlite3.connect(str(path), check_same_thread=False)
    return GapStore(connection, tables=tables, read_only=read_only)


def carry_legacy_gap_rows(memory_dir: Path) -> int:
    """Carry the gap ledgers out of a retired ``charter.db``, once.

    The gap door reuses, reopens and closes only issues it finds in its own
    provenance rows (:data:`GAP_OPENS_TABLE`), so a ``gaps.db`` that started empty would re-open a
    fresh issue for every condition a detector still sees and never close
    the pre-switch ones. The provenance rows (renumbered) and the red-watch
    row (:data:`RED_MAIN_TABLE`) are copied in one transaction, then the legacy
    file is renamed to :data:`LEGACY_CHARTER_CARRIED_FILENAME` so the carry
    never repeats. Returns the number of rows carried; ``0`` when there is no
    legacy file. A failure raises (fail loud) and leaves the legacy file in
    place, so the next startup retries.
    """
    legacy = memory_dir / LEGACY_CHARTER_DB_FILENAME
    if not legacy.exists():
        return 0
    # A read-write connection: closing the last one checkpoints any WAL.
    source = sqlite3.connect(str(legacy))
    source.row_factory = sqlite3.Row
    try:
        present = {
            row["name"]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        opens_cols = tuple(
            c for c in GAP_OPENS_TABLE.column_names if c != "id"
        )
        opens = (
            [tuple(r) for r in source.execute(
                f"SELECT {', '.join(opens_cols)} FROM {GAP_OPENS_TABLE.name} ORDER BY id"
            )]
            if GAP_OPENS_TABLE.name in present else []
        )
        red_cols = RED_MAIN_TABLE.column_names
        red = (
            [tuple(r) for r in source.execute(
                f"SELECT {', '.join(red_cols)} FROM {RED_MAIN_TABLE.name}"
            )]
            if RED_MAIN_TABLE.name in present else []
        )
    finally:
        source.close()
    store = GapStore(
        sqlite3.connect(str(memory_dir / GAP_DB_FILENAME), check_same_thread=False),
        tables=GAP_TABLES, read_only=False,
    )
    try:
        with store.transaction() as conn:
            # A kill between this COMMIT and the rename below must not carry
            # the provenance rows twice on the retry: rows already present
            # mean an earlier carry committed, so only the rename is owed.
            if conn.execute(
                f"SELECT 1 FROM {GAP_OPENS_TABLE.name} LIMIT 1"
            ).fetchone() is not None:
                opens = []
            conn.executemany(
                f"INSERT INTO {GAP_OPENS_TABLE.name} ({', '.join(opens_cols)}) "
                f"VALUES ({', '.join('?' for _ in opens_cols)})",
                opens,
            )
            conn.executemany(
                f"INSERT OR IGNORE INTO {RED_MAIN_TABLE.name} ({', '.join(red_cols)}) "
                f"VALUES ({', '.join('?' for _ in red_cols)})",
                red,
            )
    finally:
        store.close()
    legacy.rename(memory_dir / LEGACY_CHARTER_CARRIED_FILENAME)
    return len(opens) + len(red)
