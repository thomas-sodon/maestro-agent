"""The one-shot, idempotent move of leaked test cost rows (proposal 152 step 1)."""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from maestro import migration
from maestro.cost_tracking import LEAK_COLD_FILENAME, LEAK_FENCE, CostSample
from maestro.migration import ensure_schema, move_leak_cost_samples
from maestro.state_store import StateStore

_MEM = Path("/fake/mem")
_HOT = _MEM / "cost_samples.jsonl"
_COLD = _MEM / LEAK_COLD_FILENAME
_REPO = Path(__file__).resolve().parent.parent


def _row(ts: str, caller: str, provider: str | None = "anthropic") -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": ts, "cost_usd": 0.1, "input_tokens": 1, "output_tokens": 1,
        "cache_read_tokens": 0, "cache_creation_tokens": 0, "caller": caller,
        "duration_ms": 1,
    }
    if provider is not None:
        row["provider"] = provider
    return row


_LEAKS = [
    _row("2026-09-01T00:00:00+00:00", "test"),
    _row("2026-09-02T00:00:00+00:00", "reflect", provider=None),
    _row("2026-09-03T00:00:00+00:00", "contemplate"),
]
_REAL = _row("2026-09-04T00:00:00+00:00", "reflect")
_LATE_TEST = _row("2026-09-30T00:00:00+00:00", "test")
_CORRUPT = "{not json"


def _seed(mem_fs: dict[str, str]) -> list[str]:
    leak_lines = [json.dumps(r) for r in _LEAKS]
    lines = [
        leak_lines[0], json.dumps(_REAL), _CORRUPT, leak_lines[1], "[1, 2]",
        leak_lines[2], json.dumps(_LATE_TEST),
    ]
    mem_fs[str(_HOT)] = "\n".join(lines) + "\n"
    return leak_lines


def _hot_rows(mem_fs: dict[str, str]) -> list[str]:
    return [ln for ln in mem_fs[str(_HOT)].splitlines() if ln.strip()]


def test_move_clears_fenced_rows_and_is_idempotent(mem_fs: dict[str, str]) -> None:
    leak_lines = _seed(mem_fs)
    assert move_leak_cost_samples(_MEM) == 3
    hot = _hot_rows(mem_fs)
    for line in hot:
        with contextlib.suppress(json.JSONDecodeError):
            row = json.loads(line)
            assert not (isinstance(row, dict) and LEAK_FENCE.matches(row))
    assert hot == [json.dumps(_REAL), _CORRUPT, "[1, 2]", json.dumps(_LATE_TEST)]
    assert mem_fs[str(_COLD)].splitlines() == leak_lines
    cold_before = mem_fs[str(_COLD)]
    assert move_leak_cost_samples(_MEM) == 0
    assert mem_fs[str(_COLD)] == cold_before


def test_missing_hot_file_moves_nothing(mem_fs: dict[str, str]) -> None:
    assert move_leak_cost_samples(_MEM) == 0
    assert str(_COLD) not in mem_fs


def test_prescan_no_op_takes_no_lock_and_writes_nothing(mem_fs: dict[str, str]) -> None:
    mem_fs[str(_HOT)] = (
        "{bad\n"
        + json.dumps({"no": "ts"}) + "\n"
        + json.dumps({"timestamp": "garbage"}) + "\n"
        + json.dumps({"timestamp": "9999-12-31T23:59:59-23:59"}) + "\n"
        + json.dumps(_REAL) + "\n"
        + json.dumps(_LATE_TEST) + "\n"
        + json.dumps(_LEAKS[0]) + "\n"  # out-of-order: past the stop point
    )
    lock = MagicMock()
    with patch.object(migration, "file_lock", lock), \
            patch.object(migration, "atomic_write") as write:
        assert move_leak_cost_samples(_MEM) == 0
    lock.assert_not_called()
    write.assert_not_called()


def test_prescan_exhausts_file_without_match(mem_fs: dict[str, str]) -> None:
    mem_fs[str(_HOT)] = json.dumps(_REAL) + "\n"
    with patch.object(migration, "atomic_write") as write:
        assert move_leak_cost_samples(_MEM) == 0
    write.assert_not_called()


def test_prescan_hit_but_locked_reread_finds_nothing(mem_fs: dict[str, str]) -> None:
    mem_fs[str(_HOT)] = json.dumps(_LEAKS[0]) + "\n"
    real_lock = migration.file_lock

    @contextlib.contextmanager
    def _racing_lock(path: Path) -> Iterator[None]:
        # A concurrent mover cleared the file between pre-scan and lock.
        mem_fs[str(_HOT)] = json.dumps(_REAL) + "\n\n"
        with real_lock(path):
            yield

    with patch.object(migration, "file_lock", _racing_lock), \
            patch.object(migration, "atomic_write") as write:
        assert move_leak_cost_samples(_MEM) == 0
    write.assert_not_called()


def test_read_and_rewrite_happen_under_the_hot_file_lock(mem_fs: dict[str, str]) -> None:
    _seed(mem_fs)
    held: list[Path] = []
    events: list[tuple[str, bool]] = []
    real_lock = migration.file_lock
    real_write = migration.atomic_write

    @contextlib.contextmanager
    def _tracking_lock(path: Path) -> Iterator[None]:
        with real_lock(path):
            held.append(path)
            # An append landing just before the move's read survives it.
            mem_fs[str(_HOT)] += json.dumps(_row("2026-10-01T00:00:00+00:00", "x")) + "\n"
            try:
                yield
            finally:
                held.remove(path)

    def _tracking_write(path: Path, content: str) -> None:
        events.append(("write", _HOT in held))
        real_write(path, content)

    with patch.object(migration, "file_lock", _tracking_lock), \
            patch.object(migration, "atomic_write", _tracking_write):
        assert move_leak_cost_samples(_MEM) == 3
    assert events == [("write", True)]
    assert any('"caller": "x"' in ln for ln in _hot_rows(mem_fs))


def _sqlite_sample(ts: str, caller: str, provider: str | None) -> CostSample:
    return CostSample(
        timestamp=ts, cost_usd=0.1, input_tokens=1, output_tokens=1,
        cache_read_tokens=0, cache_creation_tokens=0, caller=caller,
        duration_ms=1, provider=provider,
    )


def test_sqlite_arm_moves_only_fenced_rows(mem_fs: dict[str, str]) -> None:
    store = StateStore(":memory:")
    fenced = [
        _sqlite_sample("2026-09-01T00:00:00+00:00", "test", "anthropic"),
        _sqlite_sample("2026-09-02T00:00:00+00:00", "reflect", None),
    ]
    kept = [
        _sqlite_sample("2026-09-03T00:00:00+00:00", "reflect", "anthropic"),
        _sqlite_sample("2026-09-30T00:00:00+00:00", "test", "anthropic"),
    ]
    for sample in [*fenced, *kept]:
        store.append_cost_sample(sample)
    mem_fs[str(_MEM / "state.db")] = ""
    with patch("maestro.state_store.StateStore", return_value=store):
        assert move_leak_cost_samples(_MEM, sqlite_state_enabled=True) == 2
        assert move_leak_cost_samples(_MEM, sqlite_state_enabled=True) == 0
    assert store.load_cost_samples_since(0.0) == kept
    cold = [json.loads(ln) for ln in mem_fs[str(_COLD)].splitlines()]
    assert cold == [asdict(s) for s in fenced]


def test_sqlite_arm_skipped_when_flag_off(mem_fs: dict[str, str]) -> None:
    mem_fs[str(_MEM / "state.db")] = ""
    with patch("maestro.state_store.StateStore") as cls:
        assert move_leak_cost_samples(_MEM) == 0
    cls.assert_not_called()


@pytest.mark.parametrize("exc", [OSError("disk"), sqlite3.OperationalError("locked")])
def test_narrow_failure_logs_and_returns_zero(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture, exc: Exception,
) -> None:
    with patch.object(migration, "_move_leak_jsonl", side_effect=exc), \
            caplog.at_level(logging.WARNING, logger="maestro"):
        assert move_leak_cost_samples(_MEM) == 0
    assert "Failed to move leaked cost samples cold" in caplog.text


def test_ensure_schema_passes_sqlite_flag(mem_fs: dict[str, str]) -> None:
    with patch.object(migration, "move_leak_cost_samples") as mover, \
            patch.object(migration, "migrate_backfill_hashes"), \
            patch.object(migration, "_seed_self_model"):
        assert ensure_schema(_MEM, sqlite_state_enabled=True) is True
    mover.assert_called_once_with(_MEM, sqlite_state_enabled=True)


# -- 1.2 provenance pin ------------------------------------------------------


def _load_script() -> Any:
    import importlib.util
    import sys

    path = _REPO / "scripts" / "quarantine_test_telemetry.py"
    spec = importlib.util.spec_from_file_location("quarantine_script_152", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _caller_union(
    script: Any, root: Path, needles: tuple[str, ...],
    stop_when: frozenset[str] | None = None,
) -> set[str]:
    """``caller=`` literals under *root*, parsing only files naming a *needle*.

    With *stop_when*, the walk ends once every name in it has been found —
    a presence proof needs no exhaustive scan.
    """
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if any(n in source for n in needles):
            found |= script._caller_literals(source)
            if stop_when is not None and stop_when <= found:
                break
    return found


def test_leak_fence_callers_have_test_provenance() -> None:
    """``contemplate`` (and every fenced caller but ``unknown``) is test-only."""
    script = _load_script()
    fenced = LEAK_FENCE.callers - {"unknown"}
    needles = tuple(
        q + c + q for c in sorted(fenced) for q in ('"', "'")
    )
    # The test side is a presence proof, so tight ``caller="x"`` needles are
    # safe there (a miss fails the assertion, never passes it falsely).
    tight = tuple(f'caller="{c}"' for c in sorted(fenced)) + tuple(
        f'"caller": "{c}"' for c in sorted(fenced)
    )
    test_callers = _caller_union(
        script, _REPO / "tests", tight, stop_when=frozenset(fenced),
    )
    # A ``caller=`` literal naming a fenced caller must contain one of these
    # quoted needles, so only the few files that name one are parsed.
    prod_callers = _caller_union(script, _REPO / "maestro", needles)
    assert "contemplate" in test_callers
    assert "contemplate" not in prod_callers
    for caller in fenced:
        assert caller in test_callers, caller
        assert caller not in prod_callers, caller
    # ``unknown`` is the SDK's own fallback for a caller-less call.
    assert "unknown" in script._string_constants(
        (_REPO / "maestro" / "sdk.py").read_text(encoding="utf-8"),
    )
