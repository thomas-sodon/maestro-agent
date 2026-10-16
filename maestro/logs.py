"""Log file reading and listing utilities for the CLI."""

from __future__ import annotations

import contextlib
import re
import subprocess
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
from typing import Any, NamedTuple

from maestro import transcript
from maestro.types import VALID_TRANSCRIPT_TURN_KINDS

_APP_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"\[(\w+)\] "
    r"([\w.]+): "
    r"(.*)"
)


def tail_file(path: Path, lines: int = 50, follow: bool = False) -> None:
    """Print the last N lines of a file, optionally following new output."""
    if not path.exists():
        print(f"No log file found at {path}")
        return

    if lines <= 0:
        return

    if follow:
        with contextlib.suppress(KeyboardInterrupt):
            subprocess.run(
                ["tail", "-n", str(lines), "-f", str(path)],
                check=False,
            )
        return

    # Stream lines through a bounded deque — O(n) memory instead of O(file_size).
    try:
        with open(path, encoding="utf-8") as fh:
            tail_deque: deque[str] = deque(fh, maxlen=lines)
    except UnicodeDecodeError:
        print(f"Cannot read {path}: file contains binary/non-UTF-8 data")
        return
    except OSError as exc:
        print(f"Cannot read {path}: {exc}")
        return
    for line in tail_deque:
        print(line, end="")


def _or_dash(value: object) -> object:
    """Render a ``None`` provenance field as a ``-`` sentinel in a listing row."""
    if value is None:
        return "-"
    return value


def list_sdk_sessions(sdk_log_dir: Path, date: str | None = None, max_dates: int = 5) -> None:
    """List available SDK session logs, optionally filtered by date.

    Renders from :func:`read_transcript_summaries` so a session written with a
    provenance ``.jsonl`` sidecar (``[logs] structured_transcripts_enabled`` on)
    surfaces its phase / session / iteration / task / turn count on a compact
    ``key=value`` row, while a legacy ``.log``-only session still prints
    ``filename (bytes)`` exactly as before. Every row leads with its filename so
    it can be copied into ``--session`` to open the detail view.
    """
    if max_dates <= 0:
        return
    if not sdk_log_dir.exists():
        print("No SDK logs found.")
        return

    summaries = read_transcript_summaries(sdk_log_dir, max_dates, date=date)
    if not summaries:
        # An empty result is ambiguous: read_transcript_summaries swallows a
        # directory PermissionError to [] to keep its dashboard-facing contract,
        # so re-probe readability here and surface the actionable permission
        # error — identical wording to show_sdk_session's _resolve_session_group
        # — instead of misreporting an unreadable directory as "no data".
        try:
            list(sdk_log_dir.iterdir())
        except PermissionError:
            print("Permission denied reading SDK log directory.")
            return
        if date is not None:
            print(f"No SDK logs for date {date}.")
        else:
            print("No SDK session logs found.")
        return

    for date_name, group in groupby(summaries, key=lambda s: s["date"]):
        rows = list(group)
        print(f"\n--- {date_name} ({len(rows)} sessions) ---")
        for row in rows:
            size = row["size_bytes"]
            if row["phase"] is None:
                # Legacy ``.log``-only session — filename + size, as today.
                print(f"  {row['filename']}  ({size:,} bytes)")
                continue
            print(
                f"  {row['filename']}  phase={row['phase']} "
                f"session={_or_dash(row['session'])} "
                f"iter={_or_dash(row['iteration'])} "
                f"task={_or_dash(row['task'])} "
                f"turns={_or_dash(row['turn_count'])}  ({size:,} bytes)"
            )


class _SessionFiles(NamedTuple):
    """A session as a stem group: a required tail target plus optional sidecar.

    ``primary`` is the file to tail — the raw ``.log`` when present, otherwise
    the ``.jsonl`` sidecar itself (so a sidecar-only session is still viewable).
    ``sidecar`` is the ``.jsonl`` transcript when one exists, used to render
    typed turns.
    """

    primary: Path
    sidecar: Path | None


def _session_groups(date_dir: Path) -> dict[tuple[str, str], _SessionFiles]:
    """Correlate a date dir's ``.log`` / ``.jsonl`` files into stem groups.

    A ``.log`` and its ``.jsonl`` sidecar share a session stem
    (:func:`_session_stem_key`), so each session lists once as a group carrying
    both. The ``.log`` is the group's ``primary`` (tail target); a sidecar-only
    group (no raw ``.log`` on disk) makes the sidecar its own ``primary``.
    """
    logs = {_session_stem_key(f.name): f for f in date_dir.glob("*.log")}
    sidecars = {_session_stem_key(f.name): f for f in date_dir.glob("*.jsonl")}
    groups: dict[tuple[str, str], _SessionFiles] = {}
    for key, log in logs.items():
        groups[key] = _SessionFiles(primary=log, sidecar=sidecars.get(key))
    for key, sidecar in sidecars.items():
        if key not in groups:
            groups[key] = _SessionFiles(primary=sidecar, sidecar=sidecar)
    return groups


def _resolve_session_group(
    sdk_log_dir: Path, session: str | None, date: str | None,
) -> tuple[str, _SessionFiles] | None:
    """Resolve the target session (by ``--session`` string, or the latest).

    Returns ``(date_name, files)`` for the matched session as a stem group
    considering both ``.log`` and ``.jsonl`` files, or ``None`` after printing
    the reason (permission, no match, empty). A ``session`` substring matches
    either the ``.log`` or the sidecar name — the caller-slug lives only in the
    sidecar, the hex in both — and the metacharacter-safe literal ``in`` test
    replaces the former escaped glob.
    """
    if session:
        try:
            dirs = list(sdk_log_dir.iterdir())
        except PermissionError:
            print("Permission denied reading SDK log directory.")
            return None
        candidates: list[tuple[str, _SessionFiles]] = []
        for date_dir in dirs:
            if not date_dir.is_dir():
                continue
            if date and date_dir.name != date:
                continue
            for files in _session_groups(date_dir).values():
                names = {files.primary.name}
                if files.sidecar is not None:
                    names.add(files.sidecar.name)
                if any(session in name for name in names):
                    candidates.append((date_dir.name, files))
        if not candidates:
            print(f"No SDK session matching '{session}'.")
            return None
        return max(candidates, key=lambda c: (c[0], c[1].primary.name))

    try:
        date_dirs = sorted(
            (d for d in sdk_log_dir.iterdir() if d.is_dir()),
            reverse=True,
        )
    except PermissionError:
        print("Permission denied reading SDK log directory.")
        return None
    if not date_dirs:
        print("No SDK session logs found.")
        return None
    if date:
        search_dirs = [d for d in date_dirs if d.name == date]
        if not search_dirs:
            print(f"No SDK sessions found for date '{date}'.")
            return None
    else:
        search_dirs = date_dirs
    for candidate_dir in search_dirs:
        groups = _session_groups(candidate_dir)
        if groups:
            files = max(groups.values(), key=lambda g: g.primary.name)
            return candidate_dir.name, files
    print("No SDK session logs found.")
    return None


def show_sdk_session(
    sdk_log_dir: Path,
    session: str | None = None,
    date: str | None = None,
    lines: int = 50,
    follow: bool = False,
) -> None:
    """Show a specific SDK session, or the most recent one.

    A session carrying a provenance ``.jsonl`` sidecar renders as typed turns
    (phase header + one block per turn) via :func:`read_transcript_detail`; a
    legacy ``.log``-only session — or a sidecar that degraded to a raw blob —
    tails the raw ``.log`` exactly as before. ``--follow`` always streams the
    live raw ``.log`` (a completed sidecar has no live tail).
    """
    if not sdk_log_dir.exists():
        print("No SDK logs found.")
        return

    resolved = _resolve_session_group(sdk_log_dir, session, date)
    if resolved is None:
        return
    date_name, files = resolved

    sidecar = files.sidecar
    if sidecar is not None and not follow:
        detail = read_transcript_detail(sdk_log_dir, date_name, sidecar.name)
        if detail is not None and not detail["legacy"]:
            print(f"=== SDK Session: {date_name}/{sidecar.name} ===\n")
            print(_format_transcript_turns(detail))
            return

    print(f"=== SDK Session: {date_name}/{files.primary.name} ===\n")
    tail_file(files.primary, lines=lines, follow=follow)


def read_sdk_session_file(
    sdk_log_dir: Path, date: str, filename: str,
) -> str | None:
    """Read the contents of a specific SDK session log file.

    Returns ``None`` if the file does not exist.
    """
    # Reject path traversal attempts (check path components, not substrings)
    if ".." in Path(date).parts or ".." in Path(filename).parts:
        return None
    if "/" in date or "/" in filename or "\\" in date or "\\" in filename:
        return None
    target = sdk_log_dir / date / filename
    if not target.exists() or target.is_symlink():
        return None
    # Belt-and-suspenders: ensure the resolved path is within sdk_log_dir
    try:
        target.resolve().relative_to(sdk_log_dir.resolve())
    except ValueError:
        return None
    try:
        return target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, IsADirectoryError, OSError):
        return None


def _session_stem_key(filename: str) -> tuple[str, str]:
    """Correlate a ``.log`` and its ``.jsonl`` sidecar by shared session stem.

    A ``.log`` stem is ``HH-MM-SS_<hex>`` and its sidecar stem is
    ``HH-MM-SS_<caller>_<hex>``; both share the first (``HH-MM-SS``) and last
    (``<hex>``) underscore-split components, so a session is keyed by that pair
    and listed once regardless of which file carries it. ``str.split`` always
    yields at least one element, so the empty / underscore-less name is safe.
    """
    parts = Path(filename).stem.split("_")
    return (parts[0], parts[-1])


def read_transcript_summaries(
    sdk_log_dir: Path, max_dates: int = 10, date: str | None = None,
) -> list[dict[str, Any]]:
    """Return provenance-rich SDK session summaries grouped by date.

    A superset of the old flat ``{date, filename, size_bytes}`` session list:
    every session appears, and one
    carrying a ``.jsonl`` transcript sidecar (written when ``[logs]
    structured_transcripts_enabled`` is on) reports its parsed provenance —
    ``phase`` (``caller``), ``session`` (``session_label``), ``iteration``,
    ``task`` (``task_id``), ``turn_count``, ``started_at``, ``session_id``. A
    session with only a raw ``.log`` (pre-rollout, or written with the flag off)
    reports those fields as ``None``. This is the sole ``transcript.parse`` call
    site in the package; a sidecar that fails to stat, read, or parse is skipped.

    ``date``, when given, selects that one date dir *before* the ``max_dates``
    recency slice, so a specific ``--date`` still resolves a dir older than the
    most-recent ``max_dates`` window (matching the pre-record listing, which
    filtered by date before bounding, and ``show_sdk_session``'s unbounded date
    path).
    """
    if max_dates <= 0 or not sdk_log_dir.exists():
        return []

    summaries: list[dict[str, Any]] = []
    try:
        date_dirs = sorted(
            (d for d in sdk_log_dir.iterdir() if d.is_dir()),
            reverse=True,
        )
    except PermissionError:
        return []
    if date is not None:
        date_dirs = [d for d in date_dirs if d.name == date]

    for date_dir in date_dirs[:max_dates]:
        stamped: set[tuple[str, str]] = set()
        for sidecar in sorted(date_dir.glob("*.jsonl"), reverse=True):
            try:
                size = sidecar.stat().st_size
            except OSError:
                continue
            try:
                text = sidecar.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            record = transcript.parse(text)
            if record is None:
                continue
            stamped.add(_session_stem_key(sidecar.name))
            summaries.append({
                "date": date_dir.name,
                "filename": sidecar.name,
                "size_bytes": size,
                "phase": record.caller,
                "session": record.session_label,
                "iteration": record.iteration,
                "task": record.task_id,
                "turn_count": len(record.turns),
                "started_at": record.started_at,
                "session_id": record.session_id,
            })
        for log_file in sorted(date_dir.glob("*.log"), reverse=True):
            if _session_stem_key(log_file.name) in stamped:
                continue
            try:
                size = log_file.stat().st_size
            except OSError:
                continue
            summaries.append({
                "date": date_dir.name,
                "filename": log_file.name,
                "size_bytes": size,
                "phase": None,
                "session": None,
                "iteration": None,
                "task": None,
                "turn_count": None,
                "started_at": None,
                "session_id": None,
            })

    return summaries


def iter_transcript_records(
    sdk_log_dir: Path, max_dates: int | None = None,
) -> Iterator[tuple[str, transcript.TranscriptRecord]]:
    """Yield ``(date, record)`` for every parseable sidecar, newest dates first.

    Walks the ``max_dates`` newest date dirs (``None`` = every retained dir)
    with :func:`read_transcript_summaries`' ordering and skip tolerance: a
    sidecar that fails to read or parse is skipped, and a ``.log``-only session
    yields nothing. Keeps ``transcript.parse`` inside this module for readers
    that need whole records (the proposal 142 tool-surface sensor).
    """
    if (max_dates is not None and max_dates <= 0) or not sdk_log_dir.exists():
        return
    try:
        date_dirs = sorted(
            (d for d in sdk_log_dir.iterdir() if d.is_dir()),
            reverse=True,
        )
    except PermissionError:
        return
    for date_dir in date_dirs[:max_dates]:
        for sidecar in sorted(date_dir.glob("*.jsonl"), reverse=True):
            try:
                text = sidecar.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            record = transcript.parse(text)
            if record is not None:
                yield date_dir.name, record


def read_transcript_detail(
    sdk_log_dir: Path, date: str, filename: str,
) -> dict[str, Any] | None:
    """Return one SDK session's typed transcript, or a raw-blob fallback.

    Signature matches :func:`read_sdk_session_file`, whose read this delegates to
    so the path-traversal, symlink, and containment guards are reused verbatim
    (``None`` on a rejected or missing path). A ``.jsonl`` sidecar that parses
    returns the flattened record header plus typed ``turns`` (``legacy=False``);
    a ``.log``, or an unreadable / malformed sidecar, returns the raw text under
    ``content`` with null provenance (``legacy=True``) so a caller can always
    render something.
    """
    text = read_sdk_session_file(sdk_log_dir, date, filename)
    if text is None:
        return None
    if filename.endswith(".jsonl"):
        record = transcript.parse(text)
        if record is not None:
            detail = asdict(record)
            detail["legacy"] = False
            return detail
    return {
        "caller": None,
        "session_id": None,
        "started_at": None,
        "session_label": None,
        "iteration": None,
        "task_id": None,
        "tools_advertised": None,
        "mcp_servers_advertised": None,
        "content": text,
        "legacy": True,
    }


def _format_transcript_turns(detail: dict[str, Any]) -> str:
    """Render a non-legacy :func:`read_transcript_detail` result as typed turns.

    Mirrors the dashboard's ``renderSdkTranscript`` field set: a provenance
    header (phase / session / iteration / task / ``started_at``) followed by one
    block per turn, each labelled by its ``kind`` and ``role`` and carrying its
    full ``content``. A turn whose ``kind`` is not in
    :data:`VALID_TRANSCRIPT_TURN_KINDS` falls back to a generic ``turn`` label
    (parsed sidecars only carry valid kinds, but the fallback keeps the renderer
    total over any dict). Pure formatting — no I/O; the caller passes a
    ``legacy=False`` detail dict from :func:`read_transcript_detail`.
    """
    header: list[str] = []
    if detail.get("caller"):
        header.append(f"phase={detail['caller']}")
    if detail.get("session_label"):
        header.append(f"session={detail['session_label']}")
    iteration = detail.get("iteration")
    if iteration is not None:
        header.append(f"iter={iteration}")
    if detail.get("task_id"):
        header.append(f"task={detail['task_id']}")
    if detail.get("started_at"):
        header.append(f"started={detail['started_at']}")

    lines: list[str] = []
    if header:
        lines.append(" ".join(header))
        lines.append("")
    for turn in detail.get("turns") or []:
        kind = turn.get("kind")
        label = kind if kind in VALID_TRANSCRIPT_TURN_KINDS else "turn"
        lines.append(f"--- {label} ({turn.get('role', '')}) ---")
        lines.append(turn.get("content", ""))
        lines.append("")
    return "\n".join(lines)


# Initial tail-read window. ~256 KiB carries roughly 1-2 k log entries at
# Maestro's default cadence — enough to cover the dashboard's ``n=200``
# default in a single read on a multi-megabyte file.
_TAIL_READ_INITIAL_BYTES = 256 * 1024

# Hard ceiling on the tail-read window. Past this we stop expanding even if
# we still don't have ``n`` matches (extremely sparse filter on a giant
# file). 16 MiB is well below the rotation threshold and bounds the worst
# case memory use of a single ``parse_app_log_tail`` call.
_TAIL_READ_MAX_BYTES = 16 * 1024 * 1024


def _parse_log_lines(
    lines: Iterable[str],
    n: int,
    level_upper: str | None,
    source_lower: str | None,
) -> list[dict[str, Any]]:
    """Parse ``lines`` into structured entries, bounded to the last *n* matches.

    Lifts the original forward-parser shape: a regex-matched line opens a new
    entry, subsequent lines append to the current entry as continuations
    (tracebacks), and orphan continuations before the first parsed header are
    silently skipped. This skip is what makes the seek-back tail read safe —
    if the seek lands mid-entry, the partial leader is discarded.
    """
    result: deque[dict[str, Any]] = deque(maxlen=n)
    pending: dict[str, Any] | None = None

    def _commit(entry: dict[str, Any] | None) -> None:
        if entry is None:
            return
        if level_upper and entry["level"] != level_upper:
            return
        if source_lower and (entry.get("source") or "").lower() != source_lower:
            return
        result.append(entry)

    for raw_line in lines:
        line = raw_line.rstrip("\n").rstrip("\r")
        m = _APP_LOG_RE.match(line)
        if m:
            _commit(pending)
            ts_str, lvl, logger_name, msg = m.groups()
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f").replace(
                tzinfo=UTC,
            )
            src = "sdk" if msg.startswith("SDK ") else "app"
            pending = {
                "timestamp": ts.isoformat(),
                "level": lvl,
                "message": msg,
                "logger": logger_name,
                "source": src,
            }
        elif pending is not None:
            pending["message"] += "\n" + line
        # else: orphan continuation before first header — silently skip.

    _commit(pending)
    return list(result)


def parse_app_log_tail(
    path: Path,
    n: int = 100,
    level: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """Read the tail of the app log file and return structured entries.

    Parses the format produced by the CLI file handler:
    ``%(asctime)s [%(levelname)s] %(name)s: %(message)s``

    Reads only the trailing window of the file (default 256 KiB, expanding up
    to 16 MiB if the filtered match count is short of *n*) instead of
    streaming from byte 0. Keeps the dashboard's polling cost constant in the
    log file size — critical because ``maestro.log`` grows unbounded between
    rotations. Continuation lines (e.g. tracebacks) are attached to their
    preceding header entry; orphan continuations before the first parsed
    header are silently skipped, which is what makes the seek-back safe when
    the read lands mid-entry.

    Returns an empty list if the file is missing or unreadable.
    """
    if not path.exists():
        return []

    level_upper = level.upper() if level else None
    source_lower = source.lower() if source else None

    tail_bytes = max(_TAIL_READ_INITIAL_BYTES, n * 512)

    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            file_size = fh.tell()
            if file_size == 0:
                return []
            while True:
                window = min(tail_bytes, file_size)
                offset = file_size - window
                fh.seek(offset)
                chunk = fh.read(window)
                text = chunk.decode("utf-8", errors="replace")
                lines = text.splitlines()
                if offset > 0 and lines:
                    # Drop everything up to (and including) the first complete
                    # line so a mid-line seek-back never feeds a truncated
                    # header into the parser. This loop is functionally redundant
                    # with `_parse_log_lines`, which already skips orphan
                    # continuations before the first header; it is kept for
                    # legibility — it makes the mid-seek discard explicit at the
                    # outer layer, and the explicit `lines = []` surfaces the "no
                    # header in this window → keep expanding" signal at the point
                    # the decision is made, so it is not deleted as dead code.
                    for idx, line in enumerate(lines):
                        if _APP_LOG_RE.match(line):
                            lines = lines[idx:]
                            break
                    else:
                        lines = []
                entries = _parse_log_lines(lines, n, level_upper, source_lower)
                if (
                    len(entries) >= n
                    or offset == 0
                    or tail_bytes >= _TAIL_READ_MAX_BYTES
                ):
                    return entries
                tail_bytes *= 2
    except (OSError, UnicodeDecodeError):
        return []
