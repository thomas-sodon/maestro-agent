"""Tests for maestro.fileutil — atomic writes and advisory file locking."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from maestro.fileutil import (
    SkipBudget,
    append_jsonl_row,
    atomic_write,
    file_lock,
    iter_jsonl_reverse,
    list_dir_safe,
)

FAKE_DIR = Path("/fake/fileutil")


def test_atomic_write_creates_file(mem_fs: dict[str, str]) -> None:
    target = FAKE_DIR / "data.json"
    atomic_write(target, '{"hello": "world"}')
    assert str(target) in mem_fs
    assert mem_fs[str(target)] == '{"hello": "world"}'


def test_atomic_write_creates_parent_dirs(mem_fs: dict[str, str]) -> None:
    """atomic_write should work even when parent dir doesn't exist."""
    target = FAKE_DIR / "nested" / "deep" / "file.json"
    atomic_write(target, '{"key": "value"}')
    assert str(target) in mem_fs
    assert mem_fs[str(target)] == '{"key": "value"}'


def test_atomic_write_overwrites_existing(mem_fs: dict[str, str]) -> None:
    target = FAKE_DIR / "data.json"
    mem_fs[str(target)] = "old content"
    atomic_write(target, "new content")
    assert mem_fs[str(target)] == "new content"


def test_atomic_write_cleans_up_on_error(mem_fs: dict[str, str]) -> None:
    target = FAKE_DIR / "data.json"
    mem_fs[str(target)] = "original"

    with patch("builtins.open", side_effect=OSError("disk full")), \
            contextlib.suppress(OSError):
        atomic_write(target, "broken")

    # Original file should be untouched since the atomic rename never happened
    assert mem_fs[str(target)] == "original"


def test_file_lock_acquires_and_releases() -> None:
    """file_lock creates a .lock file, acquires an exclusive flock, and releases it."""
    target = Path("/fake/data.jsonl")
    expected_lock_path = str(target.with_suffix(".jsonl.lock"))

    mock_fd = 42
    mock_flock = MagicMock()
    mock_os_open = MagicMock(return_value=mock_fd)
    mock_os_close = MagicMock()

    with (
        patch("maestro.fileutil.os.open", mock_os_open),
        patch("maestro.fileutil.fcntl.flock", mock_flock),
        patch("maestro.fileutil.os.close", mock_os_close),
    ):
        with file_lock(target):
            # Inside context: lock should have been acquired
            mock_os_open.assert_called_once_with(
                expected_lock_path, os.O_CREAT | os.O_RDWR,
            )
            mock_flock.assert_called_once_with(mock_fd, fcntl.LOCK_EX)

        # After context exit: lock released, fd closed
        assert mock_flock.call_count == 2
        mock_flock.assert_any_call(mock_fd, fcntl.LOCK_UN)
        mock_os_close.assert_called_once_with(mock_fd)


def test_file_lock_releases_on_exception() -> None:
    """file_lock releases the lock even if the body raises."""
    target = Path("/fake/data.jsonl")

    mock_fd = 99
    mock_flock = MagicMock()
    mock_os_close = MagicMock()

    with (
        patch("maestro.fileutil.os.open", return_value=mock_fd),
        patch("maestro.fileutil.fcntl.flock", mock_flock),
        patch("maestro.fileutil.os.close", mock_os_close),
    ):
        try:
            with file_lock(target):
                raise ValueError("boom")
        except ValueError:
            pass

        # flock called twice (acquire + release), fd closed
        assert mock_flock.call_count == 2
        mock_os_close.assert_called_once_with(mock_fd)


# --- Bug 25: atomic_write must use explicit UTF-8 encoding ---

def test_atomic_write_uses_utf8_encoding(mem_fs: dict[str, str]) -> None:
    """Bug 25: atomic_write must write with explicit UTF-8 encoding.

    On systems where locale.getpreferredencoding() returns 'ascii' (e.g. LANG=C),
    open(path, 'w') without an explicit encoding will use ASCII and raise
    UnicodeEncodeError for any non-ASCII content.  The fix is to always pass
    encoding='utf-8' so behaviour is locale-independent.

    This test simulates an ASCII-locale system by intercepting open() and forcing
    encoding='ascii' whenever no explicit encoding is given.
    """
    target = FAKE_DIR / "model.json"
    non_ascii_content = '{"name": "élan vital", "symbol": "\u221e"}'

    import io

    class _AsciiLocaleBuffer(io.StringIO):
        """StringIO that enforces ASCII encoding just like open(path,'w') on LANG=C."""

        def __init__(self, storage: dict, path: str, encoding: str) -> None:
            super().__init__()
            self._storage = storage
            self._path = path
            self._encoding = encoding

        def write(self, s: str) -> int:  # type: ignore[override]
            # Simulate the encode step that a real file would perform.
            s.encode(self._encoding)  # raises UnicodeEncodeError on bad chars
            return super().write(s)

        def close(self) -> None:
            if not self.closed:
                self._storage[self._path] = self.getvalue()
            super().close()

    def ascii_locale_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        """Simulate open() on an ASCII-locale system: default to ascii encoding."""
        if "w" in mode:
            encoding = kwargs.get("encoding") or "ascii"
            return _AsciiLocaleBuffer(mem_fs, str(file), encoding)
        # For reads (e.g. unlink-related), just return empty buffer.
        return io.StringIO("")

    with patch("builtins.open", side_effect=ascii_locale_open):
        # Without Bug 25 fix: open defaults to ascii, write of é/∞ raises UnicodeEncodeError
        # With Bug 25 fix: open is called with encoding='utf-8', write succeeds
        try:
            atomic_write(target, non_ascii_content)
            wrote_ok = True
        except UnicodeEncodeError:
            wrote_ok = False

    # The write must succeed regardless of locale.
    assert wrote_ok, (
        "atomic_write raised UnicodeEncodeError — it must pass encoding='utf-8' "
        "to open() so non-ASCII content is safe on ASCII-locale systems."
    )
    # Verify the file was actually written with the correct content.
    assert mem_fs.get(str(target)) == non_ascii_content


def test_atomic_write_open_called_with_utf8_encoding(mem_fs: dict[str, str]) -> None:
    """Bug 25 (direct): verify open() is called with encoding='utf-8' explicitly.

    Intercept the open() call inside atomic_write and assert that the
    encoding keyword argument is always set to 'utf-8', making the function
    immune to locale differences.
    """
    target = FAKE_DIR / "config.json"
    content = '{"key": "value"}'

    open_kwargs_seen: list[dict] = []

    def recording_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        if "w" in mode:
            open_kwargs_seen.append({"file": str(file), "mode": mode, **kwargs})
        # Simulate successful write so the rest of atomic_write runs normally.
        import io

        class _Buf(io.StringIO):
            def close(self) -> None:
                if not self.closed:
                    mem_fs[str(file)] = self.getvalue()
                super().close()

        return _Buf()

    with patch("builtins.open", side_effect=recording_open):
        atomic_write(target, content)

    assert open_kwargs_seen, "open() was never called in write mode"
    for kw in open_kwargs_seen:
        assert kw.get("encoding") == "utf-8", (
            f"open() called without encoding='utf-8': {kw!r}. "
            "atomic_write must explicitly pass encoding='utf-8'."
        )


# --- Bug 26: file_lock must not leak fd when flock(LOCK_UN) raises ---

def test_file_lock_closes_fd_even_if_unlock_raises() -> None:
    """Bug 26: os.close(lock_fd) must be called even if flock(LOCK_UN) raises.

    The current finally block runs flock(LOCK_UN) then os.close(lock_fd)
    sequentially.  If flock raises an OSError, os.close is never reached,
    leaking the file descriptor.  The fix wraps the unlock in try/finally
    so os.close always executes.
    """
    target = Path("/fake/data.jsonl")
    mock_fd = 77
    mock_os_close = MagicMock()

    def flock_side_effect(fd: int, op: int) -> None:
        if op == fcntl.LOCK_UN:
            raise OSError("simulated flock(LOCK_UN) failure")
        # LOCK_EX succeeds silently.

    with (
        patch("maestro.fileutil.os.open", return_value=mock_fd),
        patch("maestro.fileutil.fcntl.flock", side_effect=flock_side_effect),
        patch("maestro.fileutil.os.close", mock_os_close),
    ):
        try:
            with file_lock(target):
                pass  # normal body, no exception
        except OSError:
            pass  # the unlock error may propagate — that's acceptable

    # The critical assertion: os.close MUST have been called despite the
    # flock(LOCK_UN) failure, otherwise the fd is leaked.
    mock_os_close.assert_called_once_with(mock_fd)


# --- Bug 27: file_lock must close fd when both flock(LOCK_UN) and body raise ---

class _FakeEntry:
    def __init__(self, name: str) -> None:
        self.name = name


def _patch_dir(is_dir: bool, entries: list[str]) -> Any:
    """Patch Path.is_dir and Path.iterdir for list_dir_safe tests."""
    return (
        patch.object(Path, "is_dir", lambda self: is_dir),
        patch.object(
            Path, "iterdir", lambda self: iter(_FakeEntry(n) for n in entries),
        ),
    )


def test_list_dir_safe_returns_none_for_none_path() -> None:
    assert list_dir_safe(None) == []


def test_list_dir_safe_returns_empty_for_missing_dir() -> None:
    with patch.object(Path, "is_dir", lambda self: False):
        assert list_dir_safe("/nope") == []


def test_list_dir_safe_lists_entries_sorted() -> None:
    p_is_dir, p_iter = _patch_dir(True, ["c.txt", "a.txt", "b.txt"])
    with p_is_dir, p_iter:
        assert list_dir_safe("/artifacts/abc") == ["a.txt", "b.txt", "c.txt"]


def test_list_dir_safe_caps_at_max_files() -> None:
    names = [f"f{i}.txt" for i in range(20)]
    p_is_dir, p_iter = _patch_dir(True, names)
    with p_is_dir, p_iter:
        result = list_dir_safe("/dir", max_files=3)
    assert len(result) == 3
    assert result == ["f0.txt", "f1.txt", "f10.txt"]  # sorted lexicographically


def test_list_dir_safe_truncates_long_names() -> None:
    long_name = "x" * 200
    p_is_dir, p_iter = _patch_dir(True, [long_name])
    with p_is_dir, p_iter:
        result = list_dir_safe("/dir", max_name_chars=10)
    assert result == ["x" * 10]


def test_list_dir_safe_swallows_os_error() -> None:
    def _raise(self: Path) -> bool:
        raise OSError("permission denied")

    with patch.object(Path, "is_dir", _raise):
        assert list_dir_safe("/denied") == []


def test_list_dir_safe_swallows_iterdir_error() -> None:
    def _raise(self: Path) -> Any:
        raise PermissionError("nope")

    with patch.object(Path, "is_dir", lambda self: True), \
            patch.object(Path, "iterdir", _raise):
        assert list_dir_safe("/denied") == []


def test_list_dir_safe_accepts_path_object() -> None:
    p_is_dir, p_iter = _patch_dir(True, ["only.txt"])
    with p_is_dir, p_iter:
        assert list_dir_safe(Path("/x")) == ["only.txt"]


def test_list_dir_safe_rejects_invalid_type() -> None:
    assert list_dir_safe(12345) == []  # type: ignore[arg-type]


def test_file_lock_closes_fd_when_body_and_unlock_both_raise() -> None:
    """Bug 27: os.close(lock_fd) must run even when the context body raises AND
    flock(LOCK_UN) also raises in the finally block.

    When both the body and flock(LOCK_UN) raise, Python chains the exceptions and
    only the unlock error propagates.  Without a nested try/finally for os.close,
    the fd is leaked in this scenario too.
    """
    target = Path("/fake/data.jsonl")
    mock_fd = 55
    mock_os_close = MagicMock()

    def flock_side_effect(fd: int, op: int) -> None:
        if op == fcntl.LOCK_UN:
            raise OSError("simulated flock(LOCK_UN) failure")

    with (
        patch("maestro.fileutil.os.open", return_value=mock_fd),
        patch("maestro.fileutil.fcntl.flock", side_effect=flock_side_effect),
        patch("maestro.fileutil.os.close", mock_os_close),
    ):
        try:
            with file_lock(target):
                raise RuntimeError("body raised")
        except (OSError, RuntimeError):
            pass  # either exception is acceptable

    # os.close must have been called regardless of which exception won.
    mock_os_close.assert_called_once_with(mock_fd)


# ---------------------------------------------------------------------------
# iter_jsonl_reverse
# ---------------------------------------------------------------------------


JSONL_PATH = Path("/fake/jsonl/data.jsonl")


def test_iter_jsonl_reverse_missing_file(mem_fs: dict[str, str]) -> None:
    """Missing file yields nothing rather than raising."""
    assert list(iter_jsonl_reverse(JSONL_PATH)) == []


def test_iter_jsonl_reverse_empty_file(mem_fs: dict[str, str]) -> None:
    mem_fs[str(JSONL_PATH)] = ""
    assert list(iter_jsonl_reverse(JSONL_PATH)) == []


def test_iter_jsonl_reverse_single_line(mem_fs: dict[str, str]) -> None:
    mem_fs[str(JSONL_PATH)] = json.dumps({"i": 1}) + "\n"
    assert list(iter_jsonl_reverse(JSONL_PATH)) == [{"i": 1}]


def test_iter_jsonl_reverse_no_trailing_newline(mem_fs: dict[str, str]) -> None:
    """The last record lacks a trailing ``\\n`` — must still be yielded."""
    mem_fs[str(JSONL_PATH)] = json.dumps({"i": 1}) + "\n" + json.dumps({"i": 2})
    assert list(iter_jsonl_reverse(JSONL_PATH)) == [{"i": 2}, {"i": 1}]


def test_iter_jsonl_reverse_yields_newest_first(mem_fs: dict[str, str]) -> None:
    rows = [{"i": i} for i in range(10)]
    mem_fs[str(JSONL_PATH)] = "\n".join(json.dumps(r) for r in rows) + "\n"
    assert list(iter_jsonl_reverse(JSONL_PATH)) == list(reversed(rows))


def test_iter_jsonl_reverse_multi_chunk(mem_fs: dict[str, str]) -> None:
    """Records spanning many chunks must reassemble correctly."""
    # 50 rows of ~20 bytes each ~= 1 KB; force chunk_size=8 so every record
    # straddles chunk boundaries.
    rows = [{"idx": i, "value": f"row-{i:03d}"} for i in range(50)]
    mem_fs[str(JSONL_PATH)] = "\n".join(json.dumps(r) for r in rows) + "\n"
    assert list(iter_jsonl_reverse(JSONL_PATH, chunk_size=8)) == list(reversed(rows))


def test_iter_jsonl_reverse_skips_oversized_undelimited_record(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """A leading undelimited run past the cap is skipped, later rows survive."""
    # First "record" is one 200-byte unbroken run with no ``\n`` — far past the
    # 64-byte cap; chunk_size=8 forces it to span many chunks so the skip walks
    # chunk-by-chunk (bounded memory) rather than accumulating the whole run.
    giant = "x" * 200
    mem_fs[str(JSONL_PATH)] = (
        f"{giant}\n"
        f'{json.dumps({"i": 1})}\n'
        f'{json.dumps({"i": 2})}\n'
    )
    with caplog.at_level(logging.WARNING, logger="maestro.fileutil"):
        result = list(
            iter_jsonl_reverse(JSONL_PATH, chunk_size=8, max_record_bytes=64),
        )
    assert result == [{"i": 2}, {"i": 1}]
    assert sum("oversized JSONL record" in r.message for r in caplog.records) == 1


def test_iter_jsonl_reverse_single_giant_line_bounded(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """A file that is one giant undelimited line yields nothing, warns once."""
    # The oversized run is the only record and offset reaches 0 while skipping,
    # so the trailing decode must not resurrect the emptied buffer.
    mem_fs[str(JSONL_PATH)] = "x" * 200
    with caplog.at_level(logging.WARNING, logger="maestro.fileutil"):
        result = list(
            iter_jsonl_reverse(JSONL_PATH, chunk_size=8, max_record_bytes=64),
        )
    assert result == []
    assert sum("oversized JSONL record" in r.message for r in caplog.records) == 1


def test_iter_jsonl_reverse_resumes_after_oversized_record(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """A valid record left of the oversized run is still parsed on resume."""
    # left / right are small valid rows; the 200-byte middle run trips the cap.
    # When the skip walk finds the ``\n`` left of the run it resumes, so the
    # left row is recovered — exercising the boundary-truncation resume path.
    mem_fs[str(JSONL_PATH)] = (
        f'{json.dumps({"L": 1})}\n'
        f'{"x" * 200}\n'
        f'{json.dumps({"R": 1})}\n'
    )
    with caplog.at_level(logging.WARNING, logger="maestro.fileutil"):
        result = list(
            iter_jsonl_reverse(JSONL_PATH, chunk_size=8, max_record_bytes=64),
        )
    assert result == [{"R": 1}, {"L": 1}]
    assert sum("oversized JSONL record" in r.message for r in caplog.records) == 1


def test_iter_jsonl_reverse_exact_chunk_boundary(mem_fs: dict[str, str]) -> None:
    """File size that is an exact multiple of chunk_size must work."""
    # Two 8-byte lines: '{"a":1}\n{"b":2}\n' — 8 + 8 = 16 bytes; chunk_size=8.
    mem_fs[str(JSONL_PATH)] = '{"a":1}\n{"b":2}\n'
    assert list(iter_jsonl_reverse(JSONL_PATH, chunk_size=8)) == [
        {"b": 2}, {"a": 1},
    ]


def test_iter_jsonl_reverse_skips_blank_lines(mem_fs: dict[str, str]) -> None:
    mem_fs[str(JSONL_PATH)] = (
        f'\n{json.dumps({"i": 1})}\n\n   \n{json.dumps({"i": 2})}\n\n'
    )
    assert list(iter_jsonl_reverse(JSONL_PATH)) == [{"i": 2}, {"i": 1}]


def test_iter_jsonl_reverse_skips_malformed_with_warning(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    mem_fs[str(JSONL_PATH)] = (
        f'{json.dumps({"i": 1})}\n'
        "this is not json {{\n"
        f'{json.dumps({"i": 2})}\n'
    )
    with caplog.at_level(logging.WARNING, logger="maestro.fileutil"):
        result = list(iter_jsonl_reverse(JSONL_PATH))
    assert result == [{"i": 2}, {"i": 1}]
    assert any("malformed JSONL line" in r.message for r in caplog.records)


def test_iter_jsonl_reverse_skips_non_object(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """JSON arrays and scalars are skipped — only objects are yielded."""
    mem_fs[str(JSONL_PATH)] = (
        f'{json.dumps({"i": 1})}\n'
        "[1, 2, 3]\n"
        '"a string"\n'
        f'{json.dumps({"i": 2})}\n'
    )
    with caplog.at_level(logging.WARNING, logger="maestro.fileutil"):
        result = list(iter_jsonl_reverse(JSONL_PATH))
    assert result == [{"i": 2}, {"i": 1}]
    assert sum("non-object" in r.message for r in caplog.records) == 2


def test_iter_jsonl_reverse_unicode_across_chunk_boundary(
    mem_fs: dict[str, str],
) -> None:
    """Multi-byte UTF-8 characters straddling a chunk boundary must decode."""
    # 'é' is 2 bytes in UTF-8 (0xC3 0xA9).  Place a row whose JSON falls
    # exactly on a chunk boundary so 'é' bridges two chunks.
    record = {"name": "café"}  # encoded JSON ends with ...:"caf\u00e9"}
    line = json.dumps(record, ensure_ascii=False) + "\n"
    # Verify the construction actually contains a multi-byte char.
    assert any(ord(c) > 127 for c in line)
    raw_bytes = line.encode("utf-8")
    # Pick a chunk_size that splits the line in the middle of 'é'.
    multibyte_pos = raw_bytes.index(b"\xc3")
    chunk_size = multibyte_pos + 1  # cuts between 0xC3 and 0xA9
    mem_fs[str(JSONL_PATH)] = line
    assert list(iter_jsonl_reverse(JSONL_PATH, chunk_size=chunk_size)) == [record]


def test_iter_jsonl_reverse_lazy_consumes_only_what_caller_reads(
    mem_fs: dict[str, str],
) -> None:
    """Caller can stop after the first record without forcing a full scan."""
    rows = [{"i": i} for i in range(100)]
    mem_fs[str(JSONL_PATH)] = "\n".join(json.dumps(r) for r in rows) + "\n"
    gen = iter_jsonl_reverse(JSONL_PATH)
    first = next(gen)
    assert first == {"i": 99}
    gen.close()


def test_iter_jsonl_reverse_skips_invalid_utf8(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """A line with an invalid UTF-8 byte sequence is skipped with a warning.

    Bypasses ``mem_fs`` (which encodes via UTF-8) by patching ``open`` to
    return a ``BytesIO`` containing raw bytes that cannot decode.
    """
    import io
    from unittest.mock import patch as _patch

    raw = (
        b'{"i": 1}\n'
        b"\xff\xfe not utf-8 \xc3\x28\n"  # 0xC3 0x28 is invalid UTF-8
        b'{"i": 2}\n'
    )

    class _StubFile(io.BytesIO):
        def __enter__(self) -> io.BytesIO:
            return self

        def __exit__(self, *args: Any) -> None:
            self.close()

    def stub_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if "b" in mode:
            return _StubFile(raw)
        raise AssertionError(f"unexpected mode {mode!r}")

    with (
        _patch.object(Path, "exists", lambda self: True),
        _patch("builtins.open", side_effect=stub_open),
        caplog.at_level(logging.WARNING, logger="maestro.fileutil"),
    ):
        result = list(iter_jsonl_reverse(JSONL_PATH))

    assert result == [{"i": 2}, {"i": 1}]
    assert any("non-UTF-8 line" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# SkipBudget
# ---------------------------------------------------------------------------


def test_skip_budget_rejects_non_positive_remaining() -> None:
    """A non-positive budget fails loudly rather than disabling the bound."""
    with pytest.raises(ValueError, match="must be positive"):
        SkipBudget(remaining=0)
    with pytest.raises(ValueError, match="must be positive"):
        SkipBudget(remaining=-1)


def test_skip_budget_consume_reports_exhaustion() -> None:
    """``consume`` decrements and reports whether any budget is left."""
    budget = SkipBudget(remaining=2)
    assert budget.consume() is True
    assert budget.remaining == 1
    assert budget.consume() is False
    assert budget.remaining == 0


def test_iter_jsonl_reverse_stops_when_skip_budget_exhausted(
    mem_fs: dict[str, str],
) -> None:
    """Malformed rows charge the budget; the walk ends once it is spent."""
    rows = ['{"i": 1}'] + ["not-json"] * 10
    mem_fs[str(JSONL_PATH)] = "\n".join(rows) + "\n"

    budget = SkipBudget(remaining=3)
    result = list(iter_jsonl_reverse(JSONL_PATH, skip_budget=budget))

    # The valid row sits behind the corrupt tail and is never reached.
    assert result == []
    assert budget.remaining == 0


def test_iter_jsonl_reverse_skip_budget_charges_first_line(
    mem_fs: dict[str, str],
) -> None:
    """The trailing first-line decode site charges the budget too."""
    mem_fs[str(JSONL_PATH)] = "not-json\n" + '{"i": 1}\n'

    budget = SkipBudget(remaining=1)
    result = list(iter_jsonl_reverse(JSONL_PATH, skip_budget=budget))

    assert result == [{"i": 1}]
    assert budget.remaining == 0


def test_iter_jsonl_reverse_skip_budget_ignores_blank_lines(
    mem_fs: dict[str, str],
) -> None:
    """Blank lines are legitimate padding and cost no budget."""
    mem_fs[str(JSONL_PATH)] = "\n".join(['{"i": 1}', "", "", '{"i": 2}']) + "\n"

    budget = SkipBudget(remaining=1)
    result = list(iter_jsonl_reverse(JSONL_PATH, skip_budget=budget))

    assert result == [{"i": 2}, {"i": 1}]
    assert budget.remaining == 1


def test_iter_jsonl_reverse_without_budget_stays_unbounded(
    mem_fs: dict[str, str],
) -> None:
    """The default ``None`` budget preserves today's unbounded walk."""
    rows = ['{"i": 1}'] + ["not-json"] * 10
    mem_fs[str(JSONL_PATH)] = "\n".join(rows) + "\n"

    assert list(iter_jsonl_reverse(JSONL_PATH)) == [{"i": 1}]


def test_iter_jsonl_reverse_skip_budget_charges_oversized_records(
    mem_fs: dict[str, str],
) -> None:
    """A discarded oversized record charges the budget and can end the walk."""
    long_line = "x" * 50
    mem_fs[str(JSONL_PATH)] = "\n".join(['{"i": 1}'] + [long_line] * 5) + "\n"

    budget = SkipBudget(remaining=2)
    result = list(
        iter_jsonl_reverse(
            JSONL_PATH, chunk_size=8, max_record_bytes=16, skip_budget=budget,
        )
    )

    # The valid row sits behind the oversized tail and is never reached.
    assert result == []
    assert budget.remaining <= 0


# ---------------------------------------------------------------------------
# append_jsonl_row — locked append that starts at a record boundary (#4164)
# ---------------------------------------------------------------------------

_LEDGER = FAKE_DIR / "ledger.jsonl"


def test_append_jsonl_row_creates_missing_file(mem_fs: dict[str, str]) -> None:
    append_jsonl_row(_LEDGER, {"a": 1})
    assert mem_fs[str(_LEDGER)] == '{"a": 1}\n'


def test_append_jsonl_row_empty_file(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    mem_fs[str(_LEDGER)] = ""
    with caplog.at_level(logging.WARNING, logger="maestro.fileutil"):
        append_jsonl_row(_LEDGER, {"a": 1})
    assert mem_fs[str(_LEDGER)] == '{"a": 1}\n'
    assert caplog.records == []


def test_append_jsonl_row_well_formed_tail_untouched(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    mem_fs[str(_LEDGER)] = '{"a": 0}\n'
    with caplog.at_level(logging.WARNING, logger="maestro.fileutil"):
        append_jsonl_row(_LEDGER, {"a": 1})
    assert mem_fs[str(_LEDGER)] == '{"a": 0}\n{"a": 1}\n'
    assert caplog.records == []


def test_append_jsonl_row_drops_unterminated_tail(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    mem_fs[str(_LEDGER)] = '{"a": 0}\n{"a": "hal'
    with caplog.at_level(logging.WARNING, logger="maestro.fileutil"):
        append_jsonl_row(_LEDGER, {"a": 1})
    assert mem_fs[str(_LEDGER)] == '{"a": 0}\n{"a": 1}\n'
    assert len(caplog.records) == 1
    assert str(_LEDGER) in caplog.records[0].getMessage()


def test_append_jsonl_row_fragment_only_file_emptied(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    mem_fs[str(_LEDGER)] = '{"a": "hal'
    with caplog.at_level(logging.WARNING, logger="maestro.fileutil"):
        append_jsonl_row(_LEDGER, {"a": 1})
    assert mem_fs[str(_LEDGER)] == '{"a": 1}\n'
    assert len(caplog.records) == 1


def test_append_jsonl_row_terminated_corrupt_tail_left_in_place(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEDGER)] = '{"a": 0}\nnot json\n'
    append_jsonl_row(_LEDGER, {"a": 1})
    lines = mem_fs[str(_LEDGER)].splitlines()
    assert lines == ['{"a": 0}', "not json", '{"a": 1}']
    assert json.loads(lines[-1]) == {"a": 1}


def test_append_jsonl_row_tail_scan_spans_chunks(mem_fs: dict[str, str]) -> None:
    mem_fs[str(_LEDGER)] = '{"a": 0}\n' + "x" * 20
    with patch("maestro.fileutil._TAIL_SCAN_CHUNK", 4):
        append_jsonl_row(_LEDGER, {"a": 1})
    assert mem_fs[str(_LEDGER)] == '{"a": 0}\n{"a": 1}\n'


def test_append_jsonl_row_rewrite_failure_cleans_temp(mem_fs: dict[str, str]) -> None:
    mem_fs[str(_LEDGER)] = '{"a": 0}\n{"a": "hal'
    with (
        patch.object(Path, "replace", side_effect=OSError("disk")),
        pytest.raises(OSError, match="disk"),
    ):
        append_jsonl_row(_LEDGER, {"a": 1})
    assert mem_fs[str(_LEDGER)] == '{"a": 0}\n{"a": "hal'
    assert [k for k in mem_fs if k.endswith(".tmp")] == []
