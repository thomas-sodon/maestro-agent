"""Enforcing live-state write barrier for the test process (proposal 138).

A ``sys.addaudithook`` hook records every write-shaped filesystem event whose
resolved path falls under a live-state root (the ``memory_dir`` each
``maestro*.toml`` names, plus ``Config``'s ``~/.maestro`` default), attributed to
the running test. When ``enforcing``, the hook refuses the write with
``PermissionError`` before it reaches disk; ``tests/conftest.py`` then fails the
attempting test with its recorded stack (a best-effort handler may have
swallowed the refusal) and fails the session for writes outside any test. The
fix for a hit is ``mem_fs`` or ``tmp_path``, never an allowlist.

Known gaps: a ``dir_fd``-relative path is classified by its relative form
alone, so it is only caught when that form already resolves under a root;
and ``os.chmod``/``os.utime``-style metadata writes are not watched.
"""

from __future__ import annotations

import errno
import os
import sys
import traceback
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from maestro.toml_config import _read_toml

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_SINGLE_PATH_EVENTS = frozenset(
    {"os.mkdir", "os.rmdir", "os.remove", "os.truncate", "shutil.rmtree"},
)
# ``os.replace`` raises ``os.rename``, which mutates both operands; a link only
# creates its destination, so its source is merely referenced.
_RENAME_EVENTS = frozenset({"os.rename"})
_LINK_EVENTS = frozenset({"os.link", "os.symlink"})
_WATCHED_EVENTS = _SINGLE_PATH_EVENTS | _RENAME_EVENTS | _LINK_EVENTS | {"open", "sqlite3.connect"}
_STACK_LIMIT = 12


def resolve_live_state_roots(rootdir: Path) -> tuple[str, ...]:
    """Every live-state root, in both ``abspath`` and ``realpath`` forms."""
    candidates: list[Path] = []
    for toml_path in sorted(rootdir.glob("maestro*.toml")):
        memory_dir = _read_toml(toml_path).get("paths", {}).get("memory_dir")
        if isinstance(memory_dir, str) and memory_dir:
            path = Path(memory_dir).expanduser()
            candidates.append(path if path.is_absolute() else rootdir / path)
    candidates.append(Path.home() / ".maestro")
    roots: list[str] = []
    for candidate in candidates:
        for form in (os.path.abspath(candidate), os.path.realpath(candidate)):
            if form not in roots:
                roots.append(form)
    return tuple(roots)


def _under_roots(path: Any, roots: tuple[str, ...]) -> str | None:
    if not isinstance(path, (str, bytes, os.PathLike)):
        return None
    resolved = os.path.abspath(os.fsdecode(path))
    for root in roots:
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved
    return None


def classify_audit_event(
    event: str, args: tuple[Any, ...], roots: tuple[str, ...],
) -> str | None:
    """The live-state path a write-shaped audit event targets, else ``None``."""
    if event not in _WATCHED_EVENTS:
        return None
    if event == "open":
        # Both ``io.open`` and ``os.open`` raise ``(path, mode, flags)`` with
        # the OS flags populated; ``mode`` is ``None`` for ``os.open``.
        path, _mode, flags = args
        if not isinstance(flags, int) or not flags & _WRITE_FLAGS:
            return None
        return _under_roots(path, roots)
    if event in _RENAME_EVENTS:
        return _under_roots(args[0], roots) or _under_roots(args[1], roots)
    if event in _LINK_EVENTS:
        return _under_roots(args[1], roots)
    if event == "sqlite3.connect":
        database = args[0]
        if database in ("", ":memory:", b"", b":memory:"):
            return None
        return _under_roots(database, roots)
    return _under_roots(args[0], roots)


@dataclass(frozen=True)
class Violation:
    """Write-shaped events under a live-state root, one per ``(nodeid, path)``.

    ``stack`` is the first hit's; later hits only bump ``count``.
    """

    event: str
    path: str
    nodeid: str | None
    stack: tuple[str, ...]
    count: int = 1


@dataclass
class LiveStateFence:
    """Records live-state writes; ``armed`` is the off switch.

    ``enforcing`` additionally refuses each recorded write.
    """

    roots: tuple[str, ...]
    current_nodeid: str | None = None
    armed: bool = False
    enforcing: bool = False
    violations: list[Violation] = field(default_factory=list)
    _recording: bool = False
    _index: dict[tuple[str | None, str], int] = field(default_factory=dict)

    def record(self, violation: Violation) -> None:
        """Add *violation*, folding it into an existing ``(nodeid, path)`` row."""
        key = (violation.nodeid, violation.path)
        slot = self._index.get(key)
        if slot is None:
            self._index[key] = len(self.violations)
            self.violations.append(violation)
            return
        existing = self.violations[slot]
        self.violations[slot] = replace(existing, count=existing.count + violation.count)

    def on_audit(self, event: str, args: tuple[Any, ...]) -> None:
        if not self.armed or self._recording:
            return
        path = classify_audit_event(event, args, self.roots)
        if path is None:
            return
        slot = self._index.get((self.current_nodeid, path))
        if slot is not None:
            existing = self.violations[slot]
            self.violations[slot] = replace(existing, count=existing.count + 1)
        else:
            self._record_first_hit(event, path)
        if self.enforcing:
            raise PermissionError(errno.EACCES, "live-state fence: write refused", path)

    def _record_first_hit(self, event: str, path: str) -> None:
        self._recording = True
        try:
            summary = traceback.StackSummary.extract(
                traceback.walk_stack(sys._getframe(2)),
                limit=_STACK_LIMIT,
                lookup_lines=False,
            )
            stack = tuple(f"{frame.filename}:{frame.name}" for frame in summary)
            self.record(Violation(event, path, self.current_nodeid, stack))
        finally:
            self._recording = False


_installed: list[LiveStateFence] = []


def install_fence(fence: LiveStateFence) -> bool:
    """Install *fence*'s audit hook once per process; hooks cannot be removed."""
    if _installed:
        return False
    sys.addaudithook(fence.on_audit)
    _installed.append(fence)
    return True


def serialise_violations(violations: Iterable[Violation]) -> list[dict[str, Any]]:
    """Plain-data form for the xdist ``workeroutput`` relay."""
    return [
        {
            "event": v.event, "path": v.path, "nodeid": v.nodeid,
            "stack": list(v.stack), "count": v.count,
        }
        for v in violations
    ]


def merge_worker_violations(fence: LiveStateFence, workeroutput: dict[str, Any]) -> None:
    """Fold a worker's relayed violations into the controller's fence."""
    for row in workeroutput.get("live_state_violations", []):
        fence.record(
            Violation(
                row["event"], row["path"], row["nodeid"], tuple(row["stack"]), row["count"],
            ),
        )


def format_violation_report(violations: Iterable[Violation]) -> list[str]:
    """Summary lines grouped by ``(nodeid, path)``; empty when nothing was hit."""
    groups: dict[tuple[str | None, str], list[Violation]] = {}
    for violation in violations:
        groups.setdefault((violation.nodeid, violation.path), []).append(violation)
    lines: list[str] = []
    for (nodeid, path), hits in groups.items():
        total = sum(hit.count for hit in hits)
        lines.append(f"{nodeid or '<outside any test>'}: {path} ({total}x {hits[0].event})")
        lines.extend(f"    {frame}" for frame in hits[0].stack)
    return lines


def violations_for(fence: LiveStateFence, nodeid: str) -> list[Violation]:
    """The rows *fence* recorded against test *nodeid*."""
    return [v for v in fence.violations if v.nodeid == nodeid]


def format_test_failure(violations: Iterable[Violation]) -> str:
    """The per-test failure message: each refused path with its stack."""
    lines = ["live-state fence: write refused under a live-state root"]
    lines.extend(format_violation_report(violations))
    return "\n".join(lines)


def session_failed(violations: Iterable[Violation]) -> bool:
    """Whether any live-state write was recorded at all."""
    return any(True for _ in violations)
