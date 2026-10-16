"""Tests for ``scripts/quarantine_test_telemetry.py`` (proposal 138, step 7).

The script moves pre-fence test telemetry out of the production cost ledger and
the SDK session-log tree. These tests pin its classification rules and its
reversibility guarantee: after ``--apply``, the live and quarantine files together equal the
original ledger lines byte-for-byte, and nothing is deleted.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "quarantine_test_telemetry.py"
_DATA = Path("/fake/data")
_REPO = Path("/fake/repo")
_T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _load() -> Any:
    """Import the script by path — ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("quarantine_script", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``@dataclass`` resolves its module through ``sys.modules`` at class creation.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def q() -> Any:
    return _load()


def _row(when: datetime, caller: str, cost: float = 0.1) -> str:
    return json.dumps({"timestamp": when.isoformat(), "caller": caller, "cost_usd": cost}) + "\n"


def _gate(start: datetime, seconds: float, admission: str = "slot") -> dict[str, Any]:
    return {
        "started_at": start.isoformat(), "duration_seconds": seconds,
        "workers": 4, "in_flight": 0, "admission": admission, "outcome": "UNKNOWN",
        "available_mib": 5247, "bounded_by": "host", "source": "wrapper",
        "deadline_seconds": 0, "tests_seconds": 0.0, "scope": "full",
    }


def _seed_gates(mem_fs: dict[str, str], gates: list[dict[str, Any]]) -> None:
    mem_fs[str(_DATA / "gate_runs.jsonl")] = "".join(json.dumps(g) + "\n" for g in gates)


def _seed_repo(mem_fs: dict[str, str]) -> dict[Path, list[Path]]:
    tests_file = _REPO / "tests" / "test_x.py"
    maestro_file = _REPO / "maestro" / "x.py"
    mem_fs[str(tests_file)] = (
        'f(caller="fixture_only")\n'
        'f(caller="introspect")\n'
        'row = {"caller": "dict_fixture"}\n'
        'f(caller=name)\n'
    )
    mem_fs[str(maestro_file)] = 'f(caller="introspect")\ng(caller="unknown")\n'
    return {_REPO / "tests": [tests_file], _REPO / "maestro": [maestro_file]}


def _run(q: Any, argv: list[str], *, files: list[Path] | None = None,
         mtimes: dict[Path, datetime] | None = None, repo: dict[Path, list[Path]] | None = None,
         sqlite: bool = False) -> int:
    repo_map = repo or {}
    with patch.object(q, "_default_data_dir", return_value=(_DATA, sqlite, 5.0)), \
         patch.object(q, "_iter_py", side_effect=lambda root: repo_map.get(root, [])), \
         patch.object(q, "_walk_files", return_value=list(files or [])), \
         patch.object(q, "_mtime", side_effect=lambda p: (mtimes or {})[p]), \
         patch.object(q.os.path, "getsize", return_value=10):
        return int(q.main([*argv, "--repo-root", str(_REPO)]))


def test_derive_test_only_callers_excludes_names_shared_with_maestro(q: Any, mem_fs: dict[str, str]) -> None:
    repo = _seed_repo(mem_fs)
    mem_fs[str(_REPO / "tests" / "broken.py")] = "def (:\n"
    repo[_REPO / "tests"].append(_REPO / "tests" / "broken.py")
    with patch.object(q, "_iter_py", side_effect=lambda root: repo.get(root, [])):
        callers = q.derive_test_only_callers(_REPO)
    assert callers == frozenset({"fixture_only", "dict_fixture"})


def test_derive_keeps_production_strings_against_the_real_tree(q: Any, mem_fs: dict[str, str]) -> None:
    """Regression: ``"unknown"`` (the sdk fallback) and ``"contemplate"`` are
    production strings that are never a ``caller=`` literal in ``maestro/``."""
    real = Path(__file__).resolve().parent.parent / "maestro"
    tests_file = _REPO / "tests" / "test_x.py"
    mem_fs[str(tests_file)] = 'f(caller="unknown")\nf(caller="contemplate")\nf(caller="zz_fixture")\n'
    repo = {_REPO / "tests": [tests_file], _REPO / "maestro": [real / "sdk.py", real / "loop" / "iteration.py"]}
    with patch.object(q, "_iter_py", side_effect=lambda root: repo.get(root, [])):
        callers = q.derive_test_only_callers(_REPO)
    assert callers == frozenset({"zz_fixture"})


def test_iter_py_and_walk_files_are_empty_for_missing_roots(q: Any) -> None:
    missing = Path("/definitely/not/here")
    with patch.object(Path, "is_dir", return_value=False):
        assert q._iter_py(missing) == []
        assert q._walk_files(missing) == []


def test_iter_py_and_walk_files_list_present_roots(q: Any) -> None:
    root = Path("/fake/root")
    found = [root / "b.py", root / "a.py", root / "sub"]
    with patch.object(Path, "is_dir", return_value=True), \
         patch.object(Path, "rglob", return_value=iter(found[:2])):
        assert q._iter_py(root) == [root / "a.py", root / "b.py"]
    with patch.object(Path, "is_dir", return_value=True), \
         patch.object(Path, "rglob", return_value=iter(found)), \
         patch.object(Path, "is_file", side_effect=lambda: True):
        assert q._walk_files(root) == sorted(found)


def test_mtime_is_utc(q: Any) -> None:
    with patch.object(q.os.path, "getmtime", return_value=0.0):
        assert q._mtime(Path("/fake/x")) == datetime(1970, 1, 1, tzinfo=UTC)


def test_default_data_dir_reads_config(q: Any) -> None:
    config = type("C", (), {"memory_dir": _DATA, "sqlite_state_enabled": False,
                            "budget_rate_window_hours": 5.0})
    with patch("maestro.config.Config.from_env", return_value=config):
        assert q._default_data_dir() == (_DATA, False, 5.0)


def test_gate_windows_merge_overlaps_and_skip_declined(q: Any, mem_fs: dict[str, str]) -> None:
    _seed_gates(mem_fs, [
        _gate(_T0 + timedelta(minutes=5), 600),
        _gate(_T0, 400),
        _gate(_T0 + timedelta(hours=2), 60, admission="declined"),
        _gate(_T0 + timedelta(hours=3), 60),
    ])
    windows = q.load_gate_windows(_DATA)
    assert windows == [
        (_T0, _T0 + timedelta(minutes=15)),
        (_T0 + timedelta(hours=3), _T0 + timedelta(hours=3, minutes=1)),
    ]


def test_gate_windows_until_drops_and_clips(q: Any, mem_fs: dict[str, str]) -> None:
    _seed_gates(mem_fs, [_gate(_T0, 600), _gate(_T0 + timedelta(hours=1), 60)])
    windows = q.load_gate_windows(_DATA, _T0 + timedelta(minutes=5))
    assert windows == [(_T0, _T0 + timedelta(minutes=5))]


def test_until_flag_stops_gate_window_sweeping(
    q: Any, mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_ledger(mem_fs)
    assert _run(q, ["--until", _T0.isoformat()]) == 0
    assert "gate windows: 0" in capsys.readouterr().out
    assert _run(q, ["--until", "garbage"]) == 2


def test_max_window_spend(q: Any) -> None:
    spend = [(_T0, 1.0), (_T0 + timedelta(hours=1), 2.0), (_T0 + timedelta(hours=7), 4.0)]
    assert q.max_window_spend(spend, 5.0) == 4.0
    assert q.max_window_spend(spend, 1.0) == 4.0
    assert q.max_window_spend(spend[:2], 5.0) == 3.0
    assert q.max_window_spend(spend[:2], 0.5) == 2.0
    assert q.max_window_spend([], 5.0) == 0.0


def test_gate_windows_empty_without_ledger(q: Any, mem_fs: dict[str, str]) -> None:
    assert q.load_gate_windows(_DATA) == []


def test_parse_ts_shapes(q: Any) -> None:
    assert q.parse_ts("2026-09-18") == datetime(2026, 9, 18, tzinfo=UTC)
    assert q.parse_ts("nope") is None
    assert q.parse_ts(123) is None


def test_classify_cost_row_outcomes(q: Any) -> None:
    windows = [(_T0, _T0 + timedelta(minutes=10))]
    callers = frozenset({"fixture_only"})
    later = _T0 + timedelta(hours=1)
    assert q.classify_cost_row({"timestamp": (_T0 + timedelta(minutes=1)).isoformat(),
                                "caller": "introspect"}, windows, callers) == "gate_window"
    assert q.classify_cost_row({"timestamp": later.isoformat(), "caller": "fixture_only"},
                               windows, callers) == "test_caller"
    assert q.classify_cost_row({"timestamp": later.isoformat(), "caller": "unknown"},
                               windows, callers) is None
    assert q.classify_cost_row({"timestamp": later.isoformat(), "caller": "introspect"},
                               windows, callers) is None
    assert q.classify_cost_row(None, windows, callers) is None
    before = _T0 - timedelta(minutes=1)
    assert q.classify_cost_row({"timestamp": before.isoformat(), "caller": "x"},
                               windows, callers) is None


def test_derive_production_callers_splits_shared_labels(q: Any, mem_fs: dict[str, str]) -> None:
    repo = _seed_repo(mem_fs)
    with patch.object(q, "_iter_py", side_effect=lambda root: repo.get(root, [])):
        production = q.derive_production_callers(_REPO)
    assert production.labels == frozenset({"introspect", "unknown"})
    assert production.exclusive == frozenset({"unknown"})


@pytest.mark.parametrize(("row", "expected"), [
    ({"caller": "impl_only"}, True),                                   # tests never pass it
    ({"caller": "auto_fix", "cache_read_tokens": 351925}, True),       # real call caches
    ({"caller": "auto_fix", "cache_creation_tokens": 3725}, True),
    ({"caller": "auto_fix", "cache_read_tokens": 0}, False),           # fixture shape
    ({"caller": "auto_fix", "cache_read_tokens": "9"}, False),
    ({"caller": "unknown", "cache_read_tokens": 800}, False),          # not a production label
    ({"caller": None}, False),
])
def test_production_callers_spares(q: Any, row: dict[str, Any], expected: bool) -> None:
    production = q.ProductionCallers(frozenset({"impl_only", "auto_fix"}), frozenset({"impl_only"}))
    assert production.spares(row) is expected


def test_classify_cost_row_keeps_production_calls_in_gate_windows(q: Any) -> None:
    """Regression: real ``auto_fix`` / ``implement_plan`` calls that ran while a
    gate ran were classified ``gate_window`` and would have been quarantined."""
    windows = [(_T0, _T0 + timedelta(minutes=10))]
    production = q.ProductionCallers(frozenset({"auto_fix"}), frozenset())
    inside = (_T0 + timedelta(minutes=1)).isoformat()
    real = {"timestamp": inside, "caller": "auto_fix", "cache_read_tokens": 3451760}
    fixture = {"timestamp": inside, "caller": "auto_fix", "cache_read_tokens": 0}
    assert q.classify_cost_row(real, windows, frozenset(), production) is None
    assert q.classify_cost_row(fixture, windows, frozenset(), production) == "gate_window"


def test_parse_row_rejects_non_objects(q: Any) -> None:
    assert q._parse_row("[1]") is None
    assert q._parse_row("{bad") is None


def _ledger() -> list[str]:
    return [
        _row(_T0 + timedelta(minutes=1), "introspect", 0.5),   # gate window
        _row(_T0 + timedelta(hours=1), "fixture_only", 0.2),   # test caller
        _row(_T0 + timedelta(hours=1), "unknown", 0.3),        # real
        _row(_T0 + timedelta(hours=1), "introspect", 0.4),     # real
        "{not json\n",                                         # unparseable → live
        json.dumps({"timestamp": (_T0 + timedelta(minutes=2)).isoformat(),
                    "caller": "introspect", "cost_usd": "x"}) + "\n",
    ]


def _seed_ledger(mem_fs: dict[str, str]) -> list[str]:
    lines = _ledger()
    mem_fs[str(_DATA / "cost_samples.jsonl")] = "".join(lines)
    _seed_gates(mem_fs, [_gate(_T0, 600)])
    return lines


def test_report_classifies_and_writes_nothing(
    q: Any, mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_ledger(mem_fs)
    repo = _seed_repo(mem_fs)
    before = dict(mem_fs)
    assert _run(q, ["--report"], repo=repo) == 0
    assert mem_fs == before
    out = capsys.readouterr().out
    assert "6 total, 3 to quarantine, 3 live" in out
    assert "gate_window  introspect" in out
    assert "test_caller  fixture_only" in out
    assert "test-only callers (2): dict_fixture, fixture_only" in out
    assert "max quarantined cost_usd in any 5h window: 0.7000" in out
    assert _run(q, ["--window-hours", "0.01"], repo=repo) == 0
    assert "any 0.01h window: 0.5000" in capsys.readouterr().out


def _production_lines() -> list[str]:
    inside = (_T0 + timedelta(minutes=1)).isoformat()
    return [
        json.dumps({"timestamp": inside, "caller": "unknown", "cost_usd": 0.4}) + "\n",
        json.dumps({"timestamp": inside, "caller": "introspect", "cost_usd": 11.5,
                    "cache_read_tokens": 3451760}) + "\n",
        _row(_T0 + timedelta(minutes=2), "introspect", 0.0),
    ]


def test_report_names_gate_window_rows_kept_as_production(
    q: Any, mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str],
) -> None:
    mem_fs[str(_DATA / "cost_samples.jsonl")] = "".join(_production_lines())
    _seed_gates(mem_fs, [_gate(_T0, 600)])
    assert _run(q, [], repo=_seed_repo(mem_fs)) == 0
    out = capsys.readouterr().out
    assert "3 total, 1 to quarantine, 2 live" in out
    assert "gate-window rows kept live as production calls: introspect 1, unknown 1" in out


def test_apply_keeps_production_calls_live(q: Any, mem_fs: dict[str, str]) -> None:
    lines = _production_lines()
    mem_fs[str(_DATA / "cost_samples.jsonl")] = "".join(lines)
    _seed_gates(mem_fs, [_gate(_T0, 600)])
    assert _run(q, ["--apply"], repo=_seed_repo(mem_fs)) == 0
    assert mem_fs[str(_DATA / "cost_samples.jsonl")] == lines[0] + lines[1]
    assert mem_fs[str(_DATA / "cost_samples.quarantine.jsonl")] == lines[2]


def test_report_on_empty_ledger_and_no_gate_runs(
    q: Any, mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run(q, []) == 0
    out = capsys.readouterr().out
    assert "0 total, 0 to quarantine, 0 live" in out
    assert "gate windows: 0" in out
    assert "test-only callers (0): (none)" in out


def test_since_keeps_older_rows_live(q: Any, mem_fs: dict[str, str]) -> None:
    lines = _ledger()
    part = q.partition_ledger(lines, [(_T0, _T0 + timedelta(minutes=10))],
                              frozenset({"fixture_only"}), _T0 + timedelta(minutes=30))
    assert part.quarantine == [lines[1]]


def test_since_rejects_unparseable(q: Any, mem_fs: dict[str, str]) -> None:
    assert _run(q, ["--since", "garbage"]) == 2


def test_sqlite_state_refuses(q: Any, mem_fs: dict[str, str]) -> None:
    assert _run(q, ["--apply"], sqlite=True) == 2


def test_archive_sibling_warns(
    q: Any, mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str],
) -> None:
    mem_fs[str(_DATA / "cost_samples.jsonl.archive.jsonl")] = ""
    assert _run(q, []) == 0
    assert "is not processed" in capsys.readouterr().err


def test_apply_preserves_union_byte_for_byte(q: Any, mem_fs: dict[str, str]) -> None:
    lines = _seed_ledger(mem_fs)
    repo = _seed_repo(mem_fs)
    with patch.object(q.os, "kill") as kill:
        assert _run(q, ["--apply", "--since", "2026-09-18"], repo=repo) == 0
    kill.assert_not_called()
    live = mem_fs[str(_DATA / "cost_samples.jsonl")]
    quarantine = mem_fs[str(_DATA / "cost_samples.quarantine.jsonl")]
    union = live.splitlines(keepends=True) + quarantine.splitlines(keepends=True)
    assert Counter(union) == Counter(lines)
    assert quarantine == lines[0] + lines[1] + lines[5]


def test_apply_with_nothing_to_quarantine_leaves_ledger(q: Any, mem_fs: dict[str, str]) -> None:
    line = _row(_T0, "introspect")
    mem_fs[str(_DATA / "cost_samples.jsonl")] = line
    assert _run(q, ["--apply"]) == 0
    assert mem_fs[str(_DATA / "cost_samples.jsonl")] == line
    assert str(_DATA / "cost_samples.quarantine.jsonl") not in mem_fs


def test_apply_refuses_live_pid(q: Any, mem_fs: dict[str, str]) -> None:
    lines = _seed_ledger(mem_fs)
    mem_fs[str(_DATA / "maestro.pid")] = "4242\n"
    with patch.object(q.os, "kill", return_value=None) as kill:
        assert _run(q, ["--apply"]) == 1
    kill.assert_called_once_with(4242, 0)
    assert mem_fs[str(_DATA / "cost_samples.jsonl")] == "".join(lines)


def test_apply_proceeds_on_stale_pid(q: Any, mem_fs: dict[str, str]) -> None:
    _seed_ledger(mem_fs)
    mem_fs[str(_DATA / "maestro.pid")] = "4242\n"
    with patch.object(q.os, "kill", side_effect=ProcessLookupError):
        assert _run(q, ["--apply"]) == 0
    assert str(_DATA / "cost_samples.quarantine.jsonl") in mem_fs


@pytest.mark.parametrize(("content", "kill_effect", "expected"), [
    ("garbage", None, False),
    ("0", None, False),
    ("7", PermissionError, True),
])
def test_pid_is_live_edges(
    q: Any, mem_fs: dict[str, str], content: str, kill_effect: Any, expected: bool,
) -> None:
    mem_fs["/fake/pid"] = content
    with patch.object(q.os, "kill", side_effect=kill_effect):
        assert q.pid_is_live(Path("/fake/pid")) is expected


def test_pid_is_live_missing_file(q: Any, mem_fs: dict[str, str]) -> None:
    assert q.pid_is_live(Path("/fake/none")) is False


def _logs(mem_fs: dict[str, str]) -> tuple[list[Path], dict[Path, datetime]]:
    day = _DATA / "logs" / "sdk" / "2026-09-18"
    in_window = day / "12-01-00_aaaaaa.log"
    in_window_side = day / "12-01-00_unknown_aaaaaa.jsonl"
    fixture_header = day / "15-00-00_bbbbbb.log"
    production = day / "12-02-00_cccccc.log"
    production_side = day / "12-02-00_introspect_cccccc.jsonl"
    real = day / "16-00-00_dddddd.log"
    header_labelled = day / "12-03-00_eeeeee.log"
    old = day / "01-00-00_ffffff.log"
    mem_fs[str(in_window)] = "[x] SDK session started\n[x] Prompt: reflect\n"
    mem_fs[str(in_window_side)] = "{}"
    mem_fs[str(fixture_header)] = "[x] SDK session started\n[x] Prompt: test\n"
    mem_fs[str(production)] = "[x] Prompt: reflect\n"
    mem_fs[str(production_side)] = "{}"
    mem_fs[str(real)] = "[x] Prompt: reflect\n"
    mem_fs[str(header_labelled)] = "[x] caller=planner tokens_in=1\n"
    mem_fs[str(old)] = "[x] Prompt: test\n"
    inside = _T0 + timedelta(minutes=1)
    outside = _T0 + timedelta(hours=4)
    mtimes = {
        in_window: inside, fixture_header: outside, production: inside,
        real: outside, header_labelled: inside, old: _T0 - timedelta(days=1),
    }
    files = [in_window, in_window_side, fixture_header, production, production_side,
             real, header_labelled, old]
    return files, mtimes


def test_apply_moves_test_shaped_logs_with_sidecars(q: Any, mem_fs: dict[str, str]) -> None:
    _seed_gates(mem_fs, [_gate(_T0, 600)])
    files, mtimes = _logs(mem_fs)
    assert _run(q, ["--apply", "--since", "2026-09-18"], files=files, mtimes=mtimes) == 0
    quarantine = _DATA / "logs" / "sdk-quarantine" / "2026-09-18"
    live = _DATA / "logs" / "sdk" / "2026-09-18"
    for name in ("12-01-00_aaaaaa.log", "12-01-00_unknown_aaaaaa.jsonl", "15-00-00_bbbbbb.log"):
        assert str(quarantine / name) in mem_fs
        assert str(live / name) not in mem_fs
    for name in ("12-02-00_cccccc.log", "12-02-00_introspect_cccccc.jsonl",
                 "16-00-00_dddddd.log", "12-03-00_eeeeee.log", "01-00-00_ffffff.log"):
        assert str(live / name) in mem_fs


def test_report_counts_logs_by_date(
    q: Any, mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_gates(mem_fs, [_gate(_T0, 600)])
    files, mtimes = _logs(mem_fs)
    assert _run(q, [], files=files, mtimes=mtimes) == 0
    out = capsys.readouterr().out
    assert "session logs: 4 files to quarantine, 40 bytes" in out
    assert "2026-09-18: 4 files, 40 bytes" in out


def test_sidecars_for_unstructured_name(q: Any) -> None:
    assert q.sidecars_for(Path("/d/plain.log"), [Path("/d/plain_x.jsonl")]) == []


def test_read_header_is_bounded(q: Any, mem_fs: dict[str, str]) -> None:
    mem_fs["/fake/log.log"] = "".join(f"line{i}\n" for i in range(50))
    assert len(q._read_header(Path("/fake/log.log"))) == q.HEADER_LINES


def test_script_shebangs_name_the_project_interpreter() -> None:
    """Every versioned ``scripts/*.py`` shebang names the ``pyproject.toml`` mypy target.

    A shebang left on an older minor fails with ``bad interpreter`` on a host that
    ships only the pinned one (PR #4179 review).
    """
    import tomllib

    root = _SCRIPT.parent.parent
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["mypy"]["python_version"]
    for script in sorted((root / "scripts").glob("*.py")):
        first = script.read_text(encoding="utf-8").splitlines()[0]
        if first.startswith("#!/usr/bin/env python3."):
            assert first == f"#!/usr/bin/env python{version}", script.name
