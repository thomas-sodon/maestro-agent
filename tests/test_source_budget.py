"""Unit tests for the source-budget owner (``maestro/source_budget.py``)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from maestro.audit_checks import _DIRECTIVE_MAX_CHARS, truncate_directive
from maestro.config import Config
from maestro.source_budget import (
    ENCODING_EXPANSION,
    HARD_CEILING,
    SPLIT_BUDGET,
    SPLIT_DUE,
    SPLIT_DUE_NAME_CAP,
    TRANSPORT_FRAME_LIMIT,
    DefinitionSize,
    SourceBand,
    SourceBudgetReading,
    bytes_to_budget,
    classify_band,
    definition_sizes,
    encoded_tool_result_size,
    estimate_growth_per_day,
    format_split_due_summary,
    largest_definitions,
    python_files_on_disk,
    read_source_budget,
    scan_target,
    split_due_directive,
    split_due_directive_readback,
    split_due_readback,
    split_due_readings,
)

_ROOT = Path("/repo")


@pytest.mark.parametrize(
    ("size", "band"),
    [
        (0, SourceBand.OK),
        (SPLIT_DUE - 1, SourceBand.OK),
        (SPLIT_DUE, SourceBand.SPLIT_DUE),
        (SPLIT_BUDGET - 1, SourceBand.SPLIT_DUE),
        (SPLIT_BUDGET, SourceBand.OVER_BUDGET),
        (HARD_CEILING - 1, SourceBand.OVER_BUDGET),
        (HARD_CEILING, SourceBand.OVER_CEILING),
    ],
)
def test_classify_band_is_lower_inclusive(size: int, band: SourceBand) -> None:
    assert classify_band(size) == band


def test_hard_ceiling_is_derived_from_frame_and_expansion() -> None:
    derived = int(TRANSPORT_FRAME_LIMIT / ENCODING_EXPANSION)
    assert derived == HARD_CEILING


def test_encoded_tool_result_size_counts_numbering_and_escapes(
    mem_fs: dict[str, str],
) -> None:
    text = 'a\t"quoted"\n\\back é\n'
    mem_fs["/repo/x.py"] = text
    numbered = f"{1:6d}\ta\t\"quoted\"\n{2:6d}\t\\back é\n"
    expected = len(json.dumps(numbered).encode("utf-8"))
    assert encoded_tool_result_size(Path("/repo/x.py")) == expected


_TREE = {
    "/repo/maestro": [Path("/repo/maestro/b.py"), Path("/repo/maestro/pkg/a.py")],
    "/repo/tests": [Path("/repo/tests/test_z.py")],
}
_SIZES = {
    "/repo/maestro/b.py": SPLIT_DUE,
    "/repo/maestro/pkg/a.py": 10,
    "/repo/tests/test_z.py": HARD_CEILING,
}


def _fake_rglob(self: Path, pattern: str) -> list[Path]:
    assert pattern == "*.py"
    return list(_TREE.get(str(self), []))


def _fake_stat(self: Path, **_: Any) -> SimpleNamespace:
    return SimpleNamespace(st_size=_SIZES[str(self)])


def test_python_files_on_disk_walks_scanned_roots_sorted() -> None:
    with patch.object(Path, "rglob", _fake_rglob):
        files = python_files_on_disk(_ROOT)
    assert files == [
        Path("/repo/maestro/b.py"),
        Path("/repo/maestro/pkg/a.py"),
        Path("/repo/tests/test_z.py"),
    ]


def test_python_files_on_disk_honours_scanned_dirs() -> None:
    with patch.object(Path, "rglob", _fake_rglob):
        files = python_files_on_disk(_ROOT, ("tests",))
    assert files == [Path("/repo/tests/test_z.py")]


def test_read_source_budget_stamps_relative_path_size_and_band() -> None:
    with patch.object(Path, "rglob", _fake_rglob), patch.object(Path, "stat", _fake_stat):
        readings = read_source_budget(_ROOT)
    assert readings == [
        SourceBudgetReading("maestro/b.py", SPLIT_DUE, SourceBand.SPLIT_DUE),
        SourceBudgetReading("maestro/pkg/a.py", 10, SourceBand.OK),
        SourceBudgetReading("tests/test_z.py", HARD_CEILING, SourceBand.OVER_CEILING),
    ]


def _reading(relative: str, size: int) -> SourceBudgetReading:
    return SourceBudgetReading(relative, size, classify_band(size))


def test_split_due_readings_drops_ok_and_ranks_worst_band_then_size() -> None:
    readings = [
        _reading("ok.py", 10),
        _reading("due_small.py", SPLIT_DUE),
        _reading("over.py", SPLIT_BUDGET),
        _reading("ceiling.py", HARD_CEILING),
        _reading("due_big.py", SPLIT_DUE + 100),
    ]
    ranked = [r.relative for r in split_due_readings(readings)]
    assert ranked == ["ceiling.py", "over.py", "due_big.py", "due_small.py"]


def test_bytes_to_budget_sign_flips_at_split_budget() -> None:
    assert bytes_to_budget(SPLIT_BUDGET - 5) == 5
    assert bytes_to_budget(SPLIT_BUDGET) == 0
    assert bytes_to_budget(SPLIT_BUDGET + 5) == -5


def test_format_split_due_summary_empty_is_suppressed() -> None:
    assert format_split_due_summary([]) == ""


def test_format_split_due_summary_names_a_single_file() -> None:
    line = format_split_due_summary([_reading("maestro/x.py", SPLIT_DUE)])
    assert line == f"SPLIT-DUE: maestro/x.py (split_due, {SPLIT_DUE // 1024} KiB)"


def test_format_split_due_summary_truncates_past_the_cap() -> None:
    readings = [_reading(f"f{i}.py", SPLIT_DUE) for i in range(SPLIT_DUE_NAME_CAP + 1)]
    line = format_split_due_summary(readings)
    assert line.count(".py") == SPLIT_DUE_NAME_CAP
    assert "f5.py" not in line
    assert line.endswith("; +1 more")


def test_scan_target_honours_work_retarget() -> None:
    config = Config(
        work_repo_workspace="/elsewhere",
        work_gate_source_path="src/",
        work_gate_tests_path="spec/",
    )
    assert scan_target(config) == (Path("/elsewhere"), ("src", "spec"))


def test_split_due_readback_off_reads_nothing() -> None:
    config = Config()
    assert config.audit_split_due_sensor_enabled is False
    with patch("maestro.source_budget.read_source_budget") as walk:
        assert split_due_readback(config) == ""
    walk.assert_not_called()


def test_split_due_readback_on_renders_the_scanned_tree() -> None:
    config = Config(
        work_repo_workspace="/repo",
        audit_split_due_sensor_enabled=True,
    )
    with patch.object(Path, "rglob", _fake_rglob), patch.object(Path, "stat", _fake_stat):
        line = split_due_readback(config)
    assert line == (
        f"SPLIT-DUE: tests/test_z.py (over_ceiling, {HARD_CEILING // 1024} KiB); "
        f"maestro/b.py (split_due, {SPLIT_DUE // 1024} KiB)"
    )


_PROFILE_SRC = (
    "import os\n"
    "\n"
    "def small():\n"
    "    return 1\n"
    "\n"
    "@decorator\n"
    "def big():\n"
    "    x = 1\n"
    "    return x\n"
    "\n"
    "class K:\n"
    "    async def m(self):\n"
    "        return 2\n"
    "    attr = 3\n"
)


def test_definition_sizes_qualifies_methods_counts_decorators_and_orders() -> None:
    assert definition_sizes(_PROFILE_SRC) == [
        DefinitionSize("big", len("@decorator\ndef big():\n    x = 1\n    return x\n")),
        DefinitionSize("K.m", len("    async def m(self):\n        return 2\n")),
        DefinitionSize("small", len("def small():\n    return 1\n")),
    ]


def test_definition_sizes_tie_breaks_by_name() -> None:
    sizes = definition_sizes("def b():\n    pass\ndef a():\n    pass\n")
    assert [d.name for d in sizes] == ["a", "b"]


def test_definition_sizes_unparseable_is_empty() -> None:
    assert definition_sizes("def (:\n") == []


def test_largest_definitions_reads_and_bounds(mem_fs: dict[str, str]) -> None:
    mem_fs["/repo/maestro/x.py"] = _PROFILE_SRC
    assert [d.name for d in largest_definitions(_ROOT, "maestro/x.py")] == ["big", "K.m"]
    assert [d.name for d in largest_definitions(_ROOT, "maestro/x.py", 1)] == ["big"]


def test_split_due_directive_names_file_and_clusters() -> None:
    reading = _reading("maestro/x.py", SPLIT_DUE)
    text = split_due_directive(
        reading, [DefinitionSize("a", 4096), DefinitionSize("b", 2048)],
    )
    assert text == (
        f"Split maestro/x.py ({SPLIT_DUE // 1024} KiB) into <parent>_<concern> "
        "siblings; largest definitions: a (4 KiB), b (2 KiB)"
    )


def test_split_due_directive_long_names_keep_both_clusters_within_cap() -> None:
    reading = _reading("tests/test_agent_failure_counters.py", SPLIT_DUE)
    first = "test_audit_self_model_record_reconciler_evicts_self_model_rows_" * 2
    second = "TestPendingRemediationLapBudget.test_escalates_once_past_budget_x" * 2
    text = split_due_directive(
        reading, [DefinitionSize(first, 4096), DefinitionSize(second, 3072)],
    )
    assert len(text) <= _DIRECTIVE_MAX_CHARS
    assert truncate_directive(text) == text
    assert "(4 KiB), " in text
    assert text.endswith("(3 KiB)")
    assert text.count("\u2026") == 2


def test_split_due_directive_omits_largest_without_clusters() -> None:
    text = split_due_directive(_reading("maestro/x.py", SPLIT_DUE), [])
    assert text.endswith("<parent>_<concern> siblings")
    assert "largest" not in text


def test_split_due_directive_readback_off_reads_nothing() -> None:
    config = Config()
    assert config.audit_split_due_directive_enabled is False
    with patch("maestro.source_budget.read_source_budget") as walk:
        assert split_due_directive_readback(config) is None
    walk.assert_not_called()


def test_split_due_directive_readback_none_when_nothing_due() -> None:
    config = Config(audit_split_due_directive_enabled=True)
    with patch("maestro.source_budget.read_source_budget",
               return_value=[_reading("ok.py", 10)]):
        assert split_due_directive_readback(config) is None


def test_split_due_directive_readback_names_worst_file_and_clusters() -> None:
    config = Config(work_repo_workspace="/repo", audit_split_due_directive_enabled=True)
    clusters = [DefinitionSize("a", 4096), DefinitionSize("b", 2048)]
    due = _reading("maestro/due.py", SPLIT_DUE)
    with patch("maestro.source_budget.read_source_budget",
               return_value=[_reading("ok.py", 10), due]), \
         patch("maestro.source_budget.largest_definitions",
               return_value=clusters) as profile:
        text = split_due_directive_readback(config)
    profile.assert_called_once_with(_ROOT, "maestro/due.py")
    assert text == split_due_directive(due, clusters)


def _run_stub(*results: SimpleNamespace) -> Any:
    queue = list(results)

    def _run(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return queue.pop(0)

    return _run


def test_estimate_growth_per_day_success() -> None:
    run = _run_stub(
        SimpleNamespace(returncode=0, stdout="abc123 1000\n"),
        SimpleNamespace(returncode=0, stdout="1000\n"),
    )
    with patch.object(Path, "stat", return_value=SimpleNamespace(st_size=4000)):
        rate = estimate_growth_per_day(
            _ROOT, "maestro/x.py", days=30, run=run, clock=lambda: 1000 + 30 * 86400,
        )
    assert rate == 100.0


def test_estimate_growth_per_day_divides_by_commit_age_not_window() -> None:
    """Sparse history: the found commit is 60 days old, so a 30-day window must
    not double the rate."""
    run = _run_stub(
        SimpleNamespace(returncode=0, stdout="abc123 1000\n"),
        SimpleNamespace(returncode=0, stdout="1000\n"),
    )
    with patch.object(Path, "stat", return_value=SimpleNamespace(st_size=7000)):
        rate = estimate_growth_per_day(
            _ROOT, "maestro/x.py", days=30, run=run, clock=lambda: 1000 + 60 * 86400,
        )
    assert rate == 100.0


def test_estimate_growth_per_day_non_positive_age() -> None:
    run = _run_stub(SimpleNamespace(returncode=0, stdout="abc123 5000"))
    rate = estimate_growth_per_day(_ROOT, "maestro/x.py", run=run, clock=lambda: 5000)
    assert rate is None


def test_estimate_growth_per_day_no_commit_that_old() -> None:
    run = _run_stub(SimpleNamespace(returncode=0, stdout=""))
    assert estimate_growth_per_day(_ROOT, "maestro/x.py", run=run) is None


def test_estimate_growth_per_day_rev_list_fails() -> None:
    run = _run_stub(SimpleNamespace(returncode=128, stdout="abc"))
    assert estimate_growth_per_day(_ROOT, "maestro/x.py", run=run) is None


def test_estimate_growth_per_day_missing_at_rev() -> None:
    run = _run_stub(
        SimpleNamespace(returncode=0, stdout="abc123 1000"),
        SimpleNamespace(returncode=128, stdout=""),
    )
    rate = estimate_growth_per_day(_ROOT, "maestro/x.py", run=run, clock=lambda: 1e9)
    assert rate is None


def test_estimate_growth_per_day_git_unavailable() -> None:
    def _run(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        raise FileNotFoundError("git")

    assert estimate_growth_per_day(_ROOT, "maestro/x.py", run=_run) is None
