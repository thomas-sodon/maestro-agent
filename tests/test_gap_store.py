"""The loop-owned gap-fuel SQLite store (``gaps.db``, proposal 155)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro import gap_store
from maestro.gap_store import (
    GAP_DB_FILENAME,
    GAP_SCHEMA_VERSION,
    GAP_TABLES,
    GapStore,
    GapStoreReadOnly,
    GapStoreSchemaError,
    LedgerTable,
    build_gap_store,
    parse_utc_stamp,
    utc_stamp,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)

JOBS = LedgerTable(
    name="jobs",
    columns=("id TEXT PRIMARY KEY", "status TEXT NOT NULL"),
    indexes=("status",),
)
PINS = LedgerTable(name="pins", columns=("id INTEGER PRIMARY KEY", "job_id TEXT"))
TABLES = (JOBS, PINS)


def _config(memory_dir: Path = Path("/fake/mem")) -> MagicMock:
    config = MagicMock()
    config.memory_dir = memory_dir
    return config


def _store(tables: tuple[LedgerTable, ...] = TABLES) -> GapStore:
    return build_gap_store(
        _config(), connection=sqlite3.connect(":memory:"), tables=tables,
    )


def _ids(store: GapStore, table: str) -> list[str]:
    return [
        str(r[0]) for r in store.connection.execute(f"SELECT id FROM {table} ORDER BY id")
    ]


def test_db_filename_is_gaps_db() -> None:
    assert GAP_DB_FILENAME == "gaps.db"


def test_transaction_rolls_back_both_tables() -> None:
    store = _store()
    with pytest.raises(RuntimeError), store.transaction() as conn:
        conn.execute("INSERT INTO jobs VALUES ('a', 'open')")
        conn.execute("INSERT INTO pins (job_id) VALUES ('a')")
        raise RuntimeError("boom")
    assert _ids(store, "jobs") == []
    assert _ids(store, "pins") == []


def test_transaction_commits() -> None:
    store = _store()
    with store.transaction() as conn:
        conn.execute("INSERT INTO jobs VALUES ('a', 'open')")
    assert _ids(store, "jobs") == ["a"]


def test_transaction_keeps_original_error_when_sqlite_rolled_back() -> None:
    store = _store((PINS,))
    with pytest.raises(RuntimeError, match="boom"), store.transaction() as conn:
        conn.execute("ROLLBACK")
        raise RuntimeError("boom")
    assert store.connection.in_transaction is False


@pytest.mark.parametrize(
    "column",
    [
        "untrusted_input INTEGER",
        "untrusted_input INTEGER NOT NULL DEFAULT 0",
        None,
    ],
)
def test_derived_content_table_needs_strict_untrusted_column(column: str | None) -> None:
    columns = ("id TEXT PRIMARY KEY",) + ((column,) if column else ())
    table = LedgerTable(name="derived", columns=columns, holds_derived_content=True)
    with pytest.raises(GapStoreSchemaError, match="untrusted_input"):
        _store((table,))


def test_derived_content_table_with_strict_column_builds() -> None:
    table = LedgerTable(
        name="derived",
        columns=("id TEXT PRIMARY KEY", "untrusted_input INTEGER NOT NULL"),
        holds_derived_content=True,
    )
    assert _ids(_store((table,)), "derived") == []


def test_newer_schema_version_refused() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(f"PRAGMA user_version = {GAP_SCHEMA_VERSION + 1}")
    with pytest.raises(GapStoreSchemaError, match="newer"):
        build_gap_store(_config(), connection=conn)


def test_writer_stamps_schema_version() -> None:
    store = _store()
    version = store.connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == GAP_SCHEMA_VERSION
    assert store.tables == TABLES


def test_read_only_store_refuses_writes() -> None:
    store = build_gap_store(
        _config(), read_only=True, connection=sqlite3.connect(":memory:"),
    )
    with pytest.raises(GapStoreReadOnly), store.transaction():
        pass  # pragma: no cover - the context manager refuses on entry


def test_writer_open_refuses_column_drift() -> None:
    conn = sqlite3.connect(":memory:")
    build_gap_store(_config(), connection=conn, tables=(PINS,))
    drifted = LedgerTable(
        name="pins", columns=("id INTEGER PRIMARY KEY", "job_id TEXT", "extra TEXT"),
    )
    with pytest.raises(GapStoreSchemaError, match="on disk has columns"):
        build_gap_store(_config(), connection=conn, tables=(drifted,))


def test_utc_stamp_round_trips_in_fixed_format() -> None:
    assert utc_stamp(NOW) == "2026-10-13T12:00:00.000000Z"
    assert parse_utc_stamp(utc_stamp(NOW)) == NOW


def test_registry_holds_only_the_gap_ledgers() -> None:
    assert [t.name for t in GAP_TABLES] == ["gap_opens", "red_main_watch"]


def test_real_registry_builds_in_memory() -> None:
    store = build_gap_store(_config(), connection=sqlite3.connect(":memory:"))
    names = {
        row[0]
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"gap_opens", "red_main_watch"} <= names


def test_gap_opens_table_registered_as_derived_content() -> None:
    gap = next(t for t in GAP_TABLES if t.name == "gap_opens")
    assert gap.holds_derived_content is True
    store = build_gap_store(_config(), connection=sqlite3.connect(":memory:"))
    info = {
        row["name"]: row
        for row in store.connection.execute("PRAGMA table_info(gap_opens)")
    }
    assert info["untrusted_input"]["notnull"] == 1
    assert info["untrusted_input"]["dflt_value"] is None


def test_red_main_watch_added_to_existing_file() -> None:
    red = next(t for t in GAP_TABLES if t.name == "red_main_watch")
    assert red.holds_derived_content is False
    conn = sqlite3.connect(":memory:")
    older = tuple(t for t in GAP_TABLES if t.name != "red_main_watch")
    build_gap_store(_config(), connection=conn, tables=older)
    store = build_gap_store(_config(), connection=conn)
    columns = [
        row["name"]
        for row in store.connection.execute("PRAGMA table_info(red_main_watch)")
    ]
    assert tuple(columns) == red.column_names
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_read_only_uri_absolutises_relative_path() -> None:
    config = _config(Path("rel-mem"))
    with patch.object(gap_store.sqlite3, "connect") as connect:
        connect.return_value = sqlite3.connect(":memory:")
        build_gap_store(config, read_only=True)
    expected = (Path.cwd() / "rel-mem" / "gaps.db").as_uri() + "?mode=ro"
    assert connect.call_args.args[0] == expected


def test_read_only_uri_percent_encodes_path() -> None:
    config = _config(Path("/fake/m?e#m%"))
    with patch.object(gap_store.sqlite3, "connect") as connect:
        connect.return_value = sqlite3.connect(":memory:")
        build_gap_store(config, read_only=True)
    assert connect.call_args.args[0] == "file:///fake/m%3Fe%23m%25/gaps.db?mode=ro"


def test_writer_connects_to_the_gaps_db_path() -> None:
    conn = sqlite3.connect(":memory:")
    with patch.object(gap_store.sqlite3, "connect", return_value=conn) as connect:
        store = build_gap_store(_config(Path("/fake/mem")))
    assert connect.call_args.args[0] == "/fake/mem/gaps.db"
    store.close()


@pytest.mark.component
def test_file_backed_store_wal_and_read_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    writer = build_gap_store(config, tables=TABLES)
    assert writer.journal_mode() == "wal"
    with writer.transaction() as conn:
        conn.execute("INSERT INTO jobs VALUES ('a', 'open')")
    reader = build_gap_store(config, read_only=True, tables=TABLES)
    with pytest.raises(sqlite3.OperationalError):
        reader.connection.execute("INSERT INTO jobs VALUES ('x', 'open')")
    writer.connection.execute("BEGIN IMMEDIATE")
    writer.connection.execute("INSERT INTO jobs VALUES ('pending', 'open')")
    assert _ids(reader, "jobs") == ["a"]
    writer.connection.execute("COMMIT")
    reader.close()
    writer.close()


def test_carry_without_legacy_file_is_a_noop(tmp_path: Path) -> None:
    assert gap_store.carry_legacy_gap_rows(tmp_path) == 0
    assert not (tmp_path / GAP_DB_FILENAME).exists()


@pytest.mark.component
def test_carry_moves_gap_rows_and_retires_legacy_file(tmp_path: Path) -> None:
    legacy = sqlite3.connect(str(tmp_path / gap_store.LEGACY_CHARTER_DB_FILENAME))
    legacy.execute(
        "CREATE TABLE gap_opens (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "owner_id TEXT, label TEXT, opener TEXT, issue_number INTEGER, "
        "decision_ref TEXT, untrusted_input INTEGER, opened_at TEXT, "
        "closed_at TEXT, close_ref TEXT)"
    )
    legacy.execute(
        "CREATE TABLE red_main_watch (ref TEXT PRIMARY KEY, first_red_sha TEXT, "
        "first_red_at TEXT, failing_checks TEXT, last_read_sha TEXT, "
        "last_read_at TEXT)"
    )
    legacy.execute(
        "INSERT INTO gap_opens VALUES "
        "(7, 'o1', 'gap:x', 'door', 42, 'd1', 0, 't0', NULL, NULL)"
    )
    legacy.execute(
        "INSERT INTO red_main_watch VALUES ('main', 's', 't', '[]', 's', 't')"
    )
    legacy.commit()
    legacy.close()
    assert gap_store.carry_legacy_gap_rows(tmp_path) == 2
    assert not (tmp_path / gap_store.LEGACY_CHARTER_DB_FILENAME).exists()
    assert (tmp_path / gap_store.LEGACY_CHARTER_CARRIED_FILENAME).exists()
    store = build_gap_store(_config(tmp_path))
    rows = store.connection.execute(
        "SELECT owner_id, issue_number FROM gap_opens"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("o1", 42)]
    refs = store.connection.execute("SELECT ref FROM red_main_watch").fetchall()
    assert [r["ref"] for r in refs] == ["main"]
    store.close()
    # The legacy file is retired, so a second run carries nothing.
    assert gap_store.carry_legacy_gap_rows(tmp_path) == 0


@pytest.mark.component
def test_carry_tolerates_legacy_file_without_gap_tables(tmp_path: Path) -> None:
    legacy = sqlite3.connect(str(tmp_path / gap_store.LEGACY_CHARTER_DB_FILENAME))
    legacy.execute("CREATE TABLE other (x INTEGER)")
    legacy.commit()
    legacy.close()
    assert gap_store.carry_legacy_gap_rows(tmp_path) == 0
    assert (tmp_path / GAP_DB_FILENAME).exists()


@pytest.mark.component
def test_carry_retried_after_crash_before_rename_does_not_duplicate(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / gap_store.LEGACY_CHARTER_DB_FILENAME
    legacy = sqlite3.connect(str(legacy_path))
    legacy.execute(
        "CREATE TABLE gap_opens (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "owner_id TEXT, label TEXT, opener TEXT, issue_number INTEGER, "
        "decision_ref TEXT, untrusted_input INTEGER, opened_at TEXT, "
        "closed_at TEXT, close_ref TEXT)"
    )
    legacy.execute(
        "INSERT INTO gap_opens VALUES "
        "(7, 'o1', 'gap:x', 'door', 42, 'd1', 0, 't0', NULL, NULL)"
    )
    legacy.commit()
    legacy.close()
    real_rename = Path.rename
    calls: list[int] = []

    def _crash_once(self: Path, target: Path) -> Path:
        if not calls:
            calls.append(1)
            raise OSError("killed before rename")
        return real_rename(self, target)

    with patch.object(Path, "rename", _crash_once), pytest.raises(OSError):
        gap_store.carry_legacy_gap_rows(tmp_path)
    assert legacy_path.exists()
    assert gap_store.carry_legacy_gap_rows(tmp_path) == 0
    store = build_gap_store(_config(tmp_path))
    count = store.connection.execute("SELECT COUNT(*) FROM gap_opens").fetchone()[0]
    assert count == 1
    store.close()
    assert not legacy_path.exists()
