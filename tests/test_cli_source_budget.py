"""Tests for the read-only ``maestro source-budget`` CLI (proposal 147)."""

from __future__ import annotations

import argparse
import json
from unittest.mock import patch

from maestro.cli import cmd_source_budget
from maestro.config import Config
from maestro.source_budget import (
    HARD_CEILING,
    SPLIT_BUDGET,
    SPLIT_DUE,
    DefinitionSize,
    SourceBand,
    SourceBudgetReading,
)
from tests.conftest import capture_stdout

_READINGS = [
    SourceBudgetReading("maestro/ok.py", 10, SourceBand.OK),
    SourceBudgetReading("maestro/due.py", SPLIT_DUE, SourceBand.SPLIT_DUE),
    SourceBudgetReading("tests/test_big.py", SPLIT_BUDGET + 1, SourceBand.OVER_BUDGET),
]


def _run(readings: list[SourceBudgetReading], *, as_json: bool) -> str:
    args = argparse.Namespace(json=as_json)
    growth = {"tests/test_big.py": 3.0, "maestro/due.py": None}
    with patch("maestro.source_budget.read_source_budget", return_value=readings), \
         patch("maestro.source_budget.estimate_growth_per_day",
               side_effect=lambda _root, rel: growth[rel]), \
         patch("maestro.source_budget.largest_definitions",
               return_value=[DefinitionSize("big", 2048)]):
        return capture_stdout(cmd_source_budget, args, Config())


def test_text_lists_qualifying_files_worst_first() -> None:
    lines = _run(_READINGS, as_json=False).splitlines()
    assert lines[0] == "Source Budget"
    assert lines[2] == f"  tests/test_big.py  {(SPLIT_BUDGET + 1) // 1024} KiB  over_budget  to budget: -1  +3 B/day"
    assert lines[3] == f"  maestro/due.py  {SPLIT_DUE // 1024} KiB  split_due  to budget: {SPLIT_BUDGET - SPLIT_DUE}  growth unknown"
    assert len(lines) == 4


def test_text_reports_none_qualifying() -> None:
    output = _run(_READINGS[:1], as_json=False)
    assert "No file at or above the split-due line (320 KiB)." in output


def test_json_carries_thresholds_and_files() -> None:
    data = json.loads(_run(_READINGS, as_json=True))
    assert data["split_due"] == SPLIT_DUE
    assert data["split_budget"] == SPLIT_BUDGET
    assert data["hard_ceiling"] == HARD_CEILING
    assert data["files"] == [
        {"relative": "tests/test_big.py", "size": SPLIT_BUDGET + 1,
         "band": "over_budget", "bytes_to_budget": -1, "growth_per_day": 3.0,
         "largest_definitions": [{"name": "big", "size": 2048}]},
        {"relative": "maestro/due.py", "size": SPLIT_DUE,
         "band": "split_due", "bytes_to_budget": SPLIT_BUDGET - SPLIT_DUE,
         "growth_per_day": None,
         "largest_definitions": [{"name": "big", "size": 2048}]},
    ]
