"""Owner of the source-file size budget (proposal 147).

The Agent SDK's subprocess transport frames the CLI's stream-json output and
refuses any single message above 1 MiB. A tool result carries the file it
touched, so a ``.py`` file whose numbered, JSON-escaped Read result exceeds that
frame makes every tool call on it fail deterministically, not transiently
(#3875). This module owns the frame-derived ceiling, the encoding measurement,
and one budget: every scanned file stays below :data:`SPLIT_BUDGET` (held by a
single assertion in ``tests/test_source_file_size.py``, no per-file pins), set
well under :data:`HARD_CEILING`, and is flagged at the earlier
:data:`SPLIT_DUE` line so the cure — a concern split into
``<parent>_<concern>`` siblings — lands before the budget is reached.

At runtime the split-due projection feeds three read-only consumers: the
``SPLIT-DUE`` contemplation sensor (:func:`split_due_readback`, behind
``[audit] split_due_sensor_enabled``), the split-due directive bridge
(:func:`split_due_directive_readback`, behind
``[audit] split_due_directive_enabled``), and the ``maestro source-budget`` CLI
read, which adds a git-derived growth rate (:func:`estimate_growth_per_day`)
and the AST profile (:func:`largest_definitions`) per file.
"""

from __future__ import annotations

import ast
import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from maestro.config import Config

# The transport's default frame ceiling.
TRANSPORT_FRAME_LIMIT = 1024 * 1024

# A file's raw bytes are not what crosses the wire. A Read result prefixes
# every line with a line number (~6% on this tree's line lengths) and the whole
# payload is then JSON-escaped into the stream-json frame (~3-5% more). The
# worst case measured over this tree's largest files is ~1.24x; 1.30 leaves
# margin for the message envelope and for denser (more escape-heavy) content.
# ``test_expansion_allowance_covers_the_measured_worst_case`` re-measures it,
# so the constant cannot silently drift away from the tree it describes.
ENCODING_EXPANSION = 1.30

# Derived, not chosen: the largest raw file that still fits the frame once
# expanded. ~806 KiB.
HARD_CEILING = int(TRANSPORT_FRAME_LIMIT / ENCODING_EXPANSION)

SCANNED_DIRS = ("maestro", "tests")

# The target regime: every file below the budget, flagged for a split once it
# reaches the split-due line.
SPLIT_BUDGET = 384 * 1024
SPLIT_DUE = 320 * 1024

# Most files the SPLIT-DUE contemplation line names; the rest collapse into a
# ``+K more`` suffix so the line stays bounded however many files qualify.
SPLIT_DUE_NAME_CAP = 5

# How many of a file's largest definitions the split directive names.
SPLIT_DIRECTIVE_CLUSTERS = 2


class SourceBand(StrEnum):
    """Where a file's raw size sits against the budget thresholds."""

    OK = "ok"
    SPLIT_DUE = "split_due"
    OVER_BUDGET = "over_budget"
    OVER_CEILING = "over_ceiling"


@dataclass(frozen=True)
class SourceBudgetReading:
    """One scanned file's size and band, path relative to the scan root."""

    relative: str
    size: int
    band: SourceBand


def classify_band(size: int) -> SourceBand:
    """Band for a raw byte *size*; every threshold is lower-inclusive."""
    if size >= HARD_CEILING:
        return SourceBand.OVER_CEILING
    if size >= SPLIT_BUDGET:
        return SourceBand.OVER_BUDGET
    if size >= SPLIT_DUE:
        return SourceBand.SPLIT_DUE
    return SourceBand.OK


def encoded_tool_result_size(path: Path) -> int:
    """Bytes a numbered, JSON-escaped Read result on ``path`` would occupy."""
    text = path.read_text(encoding="utf-8")
    numbered = "".join(
        f"{index:6d}\t{line}"
        for index, line in enumerate(text.splitlines(keepends=True), 1)
    )
    return len(json.dumps(numbered).encode("utf-8"))


def python_files_on_disk(
    root: Path, scanned_dirs: tuple[str, ...] = SCANNED_DIRS,
) -> list[Path]:
    """Every ``.py`` file on disk under *root*'s scanned source trees."""
    found: list[Path] = []
    for directory in scanned_dirs:
        found.extend(sorted((root / directory).rglob("*.py")))
    return found


def read_source_budget(
    root: Path, scanned_dirs: tuple[str, ...] = SCANNED_DIRS,
) -> list[SourceBudgetReading]:
    """Size and band of every scanned ``.py`` file under *root*."""
    readings: list[SourceBudgetReading] = []
    for path in python_files_on_disk(root, scanned_dirs):
        size = path.stat().st_size
        readings.append(SourceBudgetReading(
            relative=path.relative_to(root).as_posix(),
            size=size,
            band=classify_band(size),
        ))
    return readings


# Worst band first: the order the split-due projection ranks readings by.
_BAND_SEVERITY = {
    SourceBand.OVER_CEILING: 0,
    SourceBand.OVER_BUDGET: 1,
    SourceBand.SPLIT_DUE: 2,
}


def split_due_readings(
    readings: list[SourceBudgetReading],
) -> list[SourceBudgetReading]:
    """Readings at or above :data:`SPLIT_DUE`, worst band first, then largest."""
    return sorted(
        (r for r in readings if r.band is not SourceBand.OK),
        key=lambda r: (_BAND_SEVERITY[r.band], -r.size),
    )


def bytes_to_budget(size: int) -> int:
    """Headroom below :data:`SPLIT_BUDGET`; negative once a file is over it."""
    return SPLIT_BUDGET - size


def format_split_due_summary(
    readings: list[SourceBudgetReading], cap: int = SPLIT_DUE_NAME_CAP,
) -> str:
    """One ``SPLIT-DUE:`` line over the first *cap* readings, ``""`` when none."""
    if not readings:
        return ""
    entries = "; ".join(
        f"{r.relative} ({r.band.value}, {r.size // 1024} KiB)"
        for r in readings[:cap]
    )
    overflow = len(readings) - cap
    suffix = f"; +{overflow} more" if overflow > 0 else ""
    return f"SPLIT-DUE: {entries}{suffix}"


def scan_target(config: Config) -> tuple[Path, tuple[str, ...]]:
    """The scan root and directories, resolved from the ``[work]`` gate atoms."""
    target = config.work_target()
    return target.repo_workspace, (
        target.gate_source_path.rstrip("/"),
        target.gate_tests_path.rstrip("/"),
    )


def split_due_readback(config: Config) -> str:
    """The ``SPLIT-DUE`` contemplation line, or ``""`` with the sensor off.

    Off, it returns before touching the filesystem. Zero SDK cost.
    """
    if not config.audit_split_due_sensor_enabled:
        return ""
    return format_split_due_summary(
        split_due_readings(read_source_budget(*scan_target(config))),
    )


@dataclass(frozen=True)
class DefinitionSize:
    """One top-level definition (or ``Class.method``) and its UTF-8 byte span."""

    name: str
    size: int


def definition_sizes(text: str) -> list[DefinitionSize]:
    """Byte spans of top-level functions and direct methods of top-level classes.

    A span runs from the first decorator (or the ``def`` line) to
    ``end_lineno``, summed as UTF-8 line bytes. Largest first, name tie-break;
    ``[]`` for source that does not parse.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines(keepends=True)

    def span(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
        start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        end = node.end_lineno or node.lineno
        return sum(len(line.encode("utf-8")) for line in lines[start - 1:end])

    found: list[DefinitionSize] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append(DefinitionSize(node.name, span(node)))
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.append(DefinitionSize(f"{node.name}.{child.name}", span(child)))
    return sorted(found, key=lambda d: (-d.size, d.name))


def largest_definitions(
    root: Path, relative: str, n: int = SPLIT_DIRECTIVE_CLUSTERS,
) -> list[DefinitionSize]:
    """The *n* largest definitions of the file at *root* / *relative*."""
    return definition_sizes((root / relative).read_text(encoding="utf-8"))[:n]


def split_due_directive(
    reading: SourceBudgetReading, clusters: list[DefinitionSize],
) -> str:
    """The split-due directive text for *reading*, naming its largest *clusters*.

    The cluster tail gets its own budget (``_DIRECTIVE_MAX_CHARS - len(prefix)``,
    the ``finding_to_directive`` shape): each name is shortened to an equal
    share so every cluster stays named instead of whole-directive truncation
    cutting the tail mid-name and dropping the rest.
    """
    from maestro import audit_checks

    text = (
        f"Split {reading.relative} ({reading.size // 1024} KiB) into "
        "<parent>_<concern> siblings"
    )
    if not clusters:
        return text
    text += "; largest definitions: "
    sizes = [f" ({c.size // 1024} KiB)" for c in clusters]
    fixed = len(text) + sum(len(s) for s in sizes) + 2 * (len(clusters) - 1)
    share = max(1, (audit_checks._DIRECTIVE_MAX_CHARS - fixed) // len(clusters))
    names = [
        c.name if len(c.name) <= share else c.name[:max(0, share - 1)] + "\u2026"
        for c in clusters
    ]
    return text + ", ".join(n + s for n, s in zip(names, sizes, strict=True))


def split_due_directive_readback(config: Config) -> str | None:
    """Directive for the worst split-due file, ``None`` when off or none qualify.

    Off, it returns before touching the filesystem; ``None`` once no file sits
    at or above :data:`SPLIT_DUE` is what makes the bridge self-extinguishing.
    """
    if not config.audit_split_due_directive_enabled:
        return None
    root, dirs = scan_target(config)
    due = split_due_readings(read_source_budget(root, dirs))
    if not due:
        return None
    top = due[0]
    return split_due_directive(top, largest_definitions(root, top.relative))


def estimate_growth_per_day(
    repo_root: Path,
    relative: str,
    *,
    days: int = 30,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    clock: Callable[[], float] = time.time,
) -> float | None:
    """Bytes per day *relative* grew since the newest commit older than *days*.

    The rate divides by that commit's real age (its committer time), not by
    *days*, so sparse history does not inflate it. ``None`` when git cannot
    answer (no commit that old, the file absent at that commit, a non-positive
    age, or git unavailable) — the caller reports growth as unknown.
    """
    try:
        rev = run(
            ["git", "log", "-1", "--format=%H %ct", f"--before={days} days ago", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, check=False,
        )
        fields = rev.stdout.split()
        if rev.returncode != 0 or len(fields) != 2:
            return None
        commit, committed_at = fields
        elapsed_days = (clock() - int(committed_at)) / 86400
        if elapsed_days <= 0:
            return None
        then = run(
            ["git", "cat-file", "-s", f"{commit}:{relative}"],
            cwd=repo_root, capture_output=True, text=True, check=False,
        )
        if then.returncode != 0:
            return None
        now = (repo_root / relative).stat().st_size
        return (now - int(then.stdout.strip())) / elapsed_days
    except (OSError, subprocess.SubprocessError):
        return None
