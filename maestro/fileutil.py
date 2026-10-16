"""File utilities for safe persistent state management.

Provides atomic writes (write-to-temp-then-rename) and advisory file locking
(``fcntl.flock``) to protect against crash corruption and concurrent access.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via temp-file-then-rename.

    Writes to a temporary file in the same directory, then atomically
    renames it to the target path.  This prevents corruption from
    mid-write crashes or concurrent reads (e.g. ``tar`` backup).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, suffix=".tmp", prefix=f"{path.stem}_",
    )
    try:
        os.close(tmp_fd)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        Path(tmp_path).replace(path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def list_dir_safe(
    path: Path | str | None,
    *,
    max_files: int = 5,
    max_name_chars: int = 60,
) -> list[str]:
    """Return up to *max_files* entry names directly inside *path*.

    Names are truncated to *max_name_chars* and returned in sorted order
    for deterministic output.  Returns an empty list when *path* is
    ``None``, missing, not a directory, or unreadable.  Never raises.

    This is intended for bounded prompt-context enumeration of task
    artifact directories — filenames only, no recursion, no content.
    """
    if path is None:
        return []
    try:
        p = Path(path)
    except (TypeError, ValueError):
        return []
    try:
        if not p.is_dir():
            return []
        names = sorted(entry.name for entry in p.iterdir())
    except OSError:
        return []
    limited = names[:max_files]
    return [n[:max_name_chars] for n in limited]


# A single JSONL record is assumed to fit well within this many bytes.  A
# longer *undelimited* run (no ``\n``) is treated as an oversized/corrupt
# record: it is skipped with one warning rather than accumulated in full, so
# ``iter_jsonl_reverse`` keeps its bounded-memory guarantee even on a
# pathological single-line file.  The default clears any realistic Maestro
# record by a wide margin.
_MAX_JSONL_RECORD_BYTES = 8 * 1024 * 1024


@dataclass
class SkipBudget:
    """A shared count of how many rejected rows a reverse walk may skip.

    ``iter_jsonl_reverse`` drops parser-level failures (invalid JSON, a
    non-object value, invalid UTF-8) inside the generator, so a caller that
    also rejects rows at a *validation* level cannot see — and therefore
    cannot bound — the decode work the corrupt rows cost.  Passing one budget
    to both layers makes the bound end-to-end: whichever layer rejects a row
    charges the same counter, and the walk stops once it is spent.

    ``remaining`` must be positive at construction: a caller that computes the
    cap wrongly fails loudly here rather than silently disabling or inverting
    the bound.
    """

    remaining: int

    def __post_init__(self) -> None:
        if self.remaining <= 0:
            raise ValueError("SkipBudget remaining must be positive")

    def consume(self) -> bool:
        """Charge one skip; return ``True`` while budget is left."""
        self.remaining -= 1
        return self.remaining > 0


def iter_jsonl_reverse(
    path: Path,
    *,
    chunk_size: int = 8192,
    max_record_bytes: int = _MAX_JSONL_RECORD_BYTES,
    skip_budget: SkipBudget | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield JSONL records from *path* in reverse (newest-first) order.

    Walks the file backwards in ``chunk_size``-byte blocks and only decodes
    complete lines (those bounded on both sides by ``\\n`` or by file edges),
    so multi-byte UTF-8 characters straddling a chunk boundary are reassembled
    safely.  Blank lines are skipped silently; lines that fail JSON decode are
    skipped with a single logged warning, mirroring forward-scan callers.

    Individual records are assumed to be ``<= max_record_bytes``.  A longer
    *undelimited* run (no ``\\n`` for more than that many bytes) is treated as
    an oversized/corrupt record: it is logged once and skipped, so a
    pathological single-line file cannot accumulate O(file-size) bytes into
    the tail buffer and defeat the reverse walker's bounded-memory guarantee.

    Yields ``dict`` records only — non-dict JSON values (lists, scalars) are
    treated as malformed and skipped.  The caller decides when to stop (a
    timestamp predicate or a count); this generator never reads bytes past
    the caller's last ``next()`` call.

    When *skip_budget* is supplied, every non-blank line that fails to decode
    charges it, as does each discarded oversized record, and the walk ends
    once it is exhausted, so a tail of malformed rows cannot force a
    full-file scan.  Blank lines are free — they are legitimate padding, not
    corruption, and cost no decode.  The default ``None`` leaves the walk
    unbounded, preserving the behaviour of callers that promise skipping
    rather than a bounded walk.
    """
    if not path.exists():
        return

    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        offset = f.tell()
        tail_buffer = b""
        # Entered when the leftmost undelimited fragment exceeds
        # ``max_record_bytes``: the oversized record is discarded chunk-by-chunk
        # until a ``\n`` reappears further left, at which point normal parsing
        # resumes for the records beyond it.
        skipping = False

        while offset > 0:
            read_size = min(chunk_size, offset)
            offset -= read_size
            f.seek(offset)
            chunk = f.read(read_size)

            if skipping:
                # Discard bytes belonging to the oversized record.  A chunk with
                # no ``\n`` is entirely inside it (drop it, stay bounded); the
                # first chunk with a ``\n`` marks the record's left boundary, so
                # keep only the bytes left of that delimiter and resume.
                boundary = chunk.rfind(b"\n")
                if boundary == -1:
                    continue
                chunk = chunk[:boundary]
                skipping = False

            tail_buffer = chunk + tail_buffer

            parts = tail_buffer.split(b"\n")
            # parts[0] may continue further left (into bytes we have not yet
            # read); keep it as the new tail_buffer.  parts[1:] are complete
            # lines bounded on both sides by ``\n`` or EOF.
            tail_buffer = parts[0]
            for raw in reversed(parts[1:]):
                record = _decode_jsonl_line(raw, path)
                if record is not None:
                    yield record
                elif (
                    skip_budget is not None
                    and raw.strip()
                    and not skip_budget.consume()
                ):
                    return

            if len(tail_buffer) > max_record_bytes:
                # The leftmost fragment is an oversized, still-undelimited
                # record.  Drop it and skip its remaining bytes rather than
                # letting the tail buffer grow without bound.
                logger.warning(
                    "Skipping oversized JSONL record (> %d bytes) in %s",
                    max_record_bytes, path,
                )
                tail_buffer = b""
                skipping = True
                if skip_budget is not None and not skip_budget.consume():
                    return

        # Whatever remains in tail_buffer after offset hits 0 is the first line
        # of the file (no leading ``\n`` to delimit it).  A walk that ended
        # mid-skip left the file's first record oversized; ``skipping`` keeps
        # its already-emptied buffer from being decoded.
        if not skipping and tail_buffer:
            record = _decode_jsonl_line(tail_buffer, path)
            if record is not None:
                yield record
            elif (
                skip_budget is not None
                and tail_buffer.strip()
                and not skip_budget.consume()
            ):
                return


def _decode_jsonl_line(raw: bytes, path: Path) -> dict[str, Any] | None:
    """Decode one JSONL line; return ``None`` for blank or malformed lines."""
    if not raw.strip():
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("Skipping non-UTF-8 line in %s", path)
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Skipping malformed JSONL line in %s", path)
        return None
    if not isinstance(data, dict):
        logger.warning("Skipping non-object JSONL line in %s", path)
        return None
    return data


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Advisory exclusive lock on a ``.lock`` file adjacent to *path*.

    Uses ``fcntl.flock(LOCK_EX)`` which blocks until the lock is available.
    The lock is released when the context manager exits.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


PARTIAL_TAIL_WARNING = (
    "Dropping trailing partial JSONL line in %s "
    "(likely partial write from prior crash)"
)


def append_jsonl_row(path: Path, row: Mapping[str, Any]) -> None:
    """Append *row* to the JSONL file at *path* under :func:`file_lock`.

    The append always starts at a record boundary: see
    :func:`append_jsonl_row_locked`, which this wraps with the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        append_jsonl_row_locked(path, row)


def append_jsonl_row_locked(path: Path, row: Mapping[str, Any]) -> None:
    """Append *row* to *path*; the caller must already hold :func:`file_lock`.

    A SIGKILL / OOM / ENOSPC mid-append can leave an unterminated fragment at
    EOF.  Appending blindly would glue the new row onto it, producing one
    undecodable line that every reader skips — losing the new row too.  So
    when the last byte is not ``\n`` the fragment (everything after the last
    newline) is dropped with a WARNING before the append.  The common case
    reads only the final byte; the newline is located by a bounded backward
    chunk scan, and the kept prefix is rewritten as raw bytes via a same-dir
    temp file + rename (safe because the lock is held), so an undecodable
    interior byte never makes the append raise.  A newline-terminated but
    undecodable tail is left in place: the new row still lands on its own
    line and decodes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        cut = partial_tail_offset(path)
        if cut is not None:
            logger.warning(PARTIAL_TAIL_WARNING, path)
            with open(path, "rb") as f:
                kept = f.read(cut)
            _atomic_write_bytes(path, kept)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(row)) + "\n")


_TAIL_SCAN_CHUNK = 64 * 1024


def partial_tail_offset(path: Path) -> int | None:
    """Return the byte length to keep if *path* ends unterminated, else None."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        if not pos:
            return None
        f.seek(pos - 1)
        if f.read(1) == b"\n":
            return None
        while pos > 0:
            step = min(_TAIL_SCAN_CHUNK, pos)
            pos -= step
            f.seek(pos)
            idx = f.read(step).rfind(b"\n")
            if idx != -1:
                return pos + idx + 1
        return 0


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Byte-exact sibling of :func:`atomic_write` (temp file then rename)."""
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, suffix=".tmp", prefix=f"{path.stem}_",
    )
    try:
        os.close(tmp_fd)
        with open(tmp_path, "wb") as f:
            f.write(content)
        Path(tmp_path).replace(path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
