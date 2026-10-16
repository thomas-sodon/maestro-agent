"""Structural guard bounding the size of any Python source file on disk.

A tool result carries the file it touched, so once a source file's numbered,
JSON-escaped Read result passes the SDK transport's 1 MiB stream-json frame,
*every* Edit/Write/Read on it fails deterministically (#3875). The ceiling, the
encoding measurement and the single budget are owned by
``maestro/source_budget.py``; this meta-test walks every ``.py`` file on disk
under ``maestro/`` and ``tests/`` (a plain filesystem walk, not a git query, so
a generated or not-yet-committed file is in scope too) and asserts:

* the hard ceiling is *derived* from the frame limit and the measured
  worst-case encoding expansion, not picked, and ``SPLIT_BUDGET`` sits well
  under it; and
* every file stays below ``SPLIT_BUDGET`` — one assertion, no per-file pins.
  A file nearing it is flagged at ``SPLIT_DUE`` (``maestro source-budget``);
  the cure is a concern split into ``<parent>_<concern>`` siblings, never a
  trim of comments, logs or names, and never a budget raise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maestro.source_budget import (
    ENCODING_EXPANSION,
    HARD_CEILING,
    SPLIT_BUDGET,
    SPLIT_DUE,
    TRANSPORT_FRAME_LIMIT,
    encoded_tool_result_size,
    python_files_on_disk,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The expansion allowance is measured over the largest files on disk: large
# files dominate the ceiling, while tiny files inflate the ratio from fixed
# per-line overhead that never approaches the frame limit.
_EXPANSION_SAMPLE = 5


def _over_budget(sizes: dict[str, int]) -> list[str]:
    """Paths whose size is at or above :data:`SPLIT_BUDGET`."""
    return sorted(rel for rel, size in sizes.items() if size >= SPLIT_BUDGET)


def test_scan_finds_the_source_trees() -> None:
    """The walk must not pass vacuously on an empty or mislocated tree."""
    files = python_files_on_disk(_REPO_ROOT)
    assert len(files) > 100
    assert _REPO_ROOT / "maestro" / "agent.py" in files


def test_thresholds_are_ordered_below_the_transport_frame_limit() -> None:
    """The ceiling leaves real headroom and the budget sits well under it."""
    assert HARD_CEILING < TRANSPORT_FRAME_LIMIT
    assert SPLIT_DUE < SPLIT_BUDGET
    assert SPLIT_BUDGET < HARD_CEILING


def test_expansion_allowance_covers_the_measured_worst_case() -> None:
    """The allowance must still bound what the tree actually encodes to."""
    largest = sorted(
        python_files_on_disk(_REPO_ROOT),
        key=lambda path: path.stat().st_size,
        reverse=True,
    )[:_EXPANSION_SAMPLE]
    measured = max(
        encoded_tool_result_size(path) / path.stat().st_size for path in largest
    )
    assert measured < ENCODING_EXPANSION, (
        f"measured worst-case encoding expansion {measured:.3f} reached the "
        f"{ENCODING_EXPANSION} allowance the ceiling is derived from — raise "
        "the allowance (which lowers the ceiling) rather than ignoring it."
    )


@pytest.mark.parametrize("path", python_files_on_disk(_REPO_ROOT), ids=str)
def test_source_file_under_split_budget(path: Path) -> None:
    """Every source file stays below the single split budget."""
    relative = path.relative_to(_REPO_ROOT).as_posix()
    size = path.stat().st_size
    assert _over_budget({relative: size}) == [], (
        f"{relative} is {size} bytes — at or above the {SPLIT_BUDGET}-byte "
        "split budget. Run `maestro source-budget --json` for its band and largest "
        "definitions, then split it along its concern seams into "
        "<parent>_<concern> siblings. Never trim it to fit and never raise "
        "the budget."
    )


def test_budget_guard_fires_on_a_seeded_file() -> None:
    """Seeded control: a file at the budget is reported, one byte under is not."""
    assert _over_budget({"seed.py": SPLIT_BUDGET}) == ["seed.py"]
    assert _over_budget({"seed.py": SPLIT_BUDGET - 1}) == []
