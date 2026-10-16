#!/usr/bin/env python3.14
"""Quarantine the test telemetry already written into the production ledger.

Proposal 138 fenced test processes off from live state; this operator script
cleans up the pollution written *before* the fence. It sorts fixture rows out of
``cost_samples.jsonl`` and test-shaped session logs out of ``logs/sdk/`` —
reversibly: nothing is ever deleted, and after ``--apply`` the live ledger plus
``cost_samples.quarantine.jsonl`` hold exactly the original lines.

A cost row is test traffic when either rule matches:

* ``gate_window`` — its timestamp falls inside a recorded ``gate_runs.jsonl``
  run (the loop's in-process gate ran the suite then);
* ``test_caller`` — its ``caller`` is a literal passed as ``caller=`` somewhere
  under ``tests/`` and appearing as *no* string constant anywhere under
  ``maestro/`` (AST-derived at run time), so a name production could plausibly
  emit — the ``"unknown"`` fallback, a phase name — is never swept by it alone.

A gate-window row is nonetheless kept live when it is a *production call*: its
``caller`` is a ``caller=`` literal under ``maestro/`` that ``tests/`` never
passes (no fixture can emit it), or a production label ``tests/`` shares that
carries prompt-cache tokens (a real call caches the identity system prompt;
fixture rows under production names do not). Production ran concurrently with
some gate runs, and those rows are real spend.

``--until`` bounds rule ``gate_window`` to gate runs before that instant (pass
the fence deploy time): once the fence ships, a gate run no longer pollutes the
ledger, so later windows would only sweep concurrent production rows.

Usage::

    python3.14 scripts/quarantine_test_telemetry.py [--data-dir DIR]
        [--since 2026-09-17] [--until 2026-09-26T14:00] [--window-hours H]
        [--report | --apply] [--repo-root DIR]

``--report`` (the default) writes nothing and takes no lock. Stop the loop,
review the report, then re-run with ``--apply``.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import collections
import json
import os
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from maestro.fileutil import atomic_write, file_lock  # noqa: E402
from maestro.gate_capacity import read_gate_runs  # noqa: E402

LEDGER = "cost_samples.jsonl"
QUARANTINE = "cost_samples.quarantine.jsonl"
LOG_DIR = Path("logs") / "sdk"
LOG_QUARANTINE_DIR = Path("logs") / "sdk-quarantine"
# Only the first few lines of a session log are read — the header is where
# ``SessionLogger`` writes the session-start stamp and the prompt.
HEADER_LINES = 8
# The fixture-query shapes ``SessionLogger`` writes for tests.
TEST_HEADER_MARKERS = ("Prompt: test", "caller=None")

Window = tuple[datetime, datetime]


def parse_ts(text: object) -> datetime | None:
    """Parse an ISO stamp to an aware UTC datetime; naive stamps are UTC."""
    if not isinstance(text, str):
        return None
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp


def _iter_py(root: Path) -> list[Path]:
    """Every ``.py`` file under *root*, sorted (empty when *root* is missing)."""
    return sorted(root.rglob("*.py")) if root.is_dir() else []


def _caller_literals(source: str) -> set[str]:
    """String constants passed as ``caller=`` or stored under a ``"caller"`` key."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (kw.arg == "caller" and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)):
                    found.add(kw.value.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (isinstance(key, ast.Constant) and key.value == "caller"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)):
                    found.add(value.value)
    return found


def _string_constants(source: str) -> set[str]:
    """Every string constant in *source* (empty when it does not parse)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _tree_union(root: Path, extract: Callable[[str], set[str]]) -> set[str]:
    found: set[str] = set()
    for path in _iter_py(root):
        found |= extract(path.read_text(encoding="utf-8"))
    return found


def derive_test_only_callers(repo_root: Path) -> frozenset[str]:
    """Caller literals used under ``tests/`` and as no string anywhere in ``maestro/``."""
    test_callers = _tree_union(repo_root / "tests", _caller_literals)
    return frozenset(test_callers - _tree_union(repo_root / "maestro", _string_constants))


@dataclass(frozen=True)
class ProductionCallers:
    """The ``caller=`` labels ``maestro/`` passes, and those ``tests/`` never does."""

    labels: frozenset[str] = frozenset()
    exclusive: frozenset[str] = frozenset()

    def spares(self, row: dict[str, object]) -> bool:
        """Whether *row* is a real production call, not a fixture row."""
        caller = row.get("caller")
        if not isinstance(caller, str) or caller not in self.labels:
            return False
        if caller in self.exclusive:
            return True
        cache = (row.get("cache_read_tokens"), row.get("cache_creation_tokens"))
        return any(isinstance(n, int | float) and n > 0 for n in cache)


# No production labels: every gate-window row is test traffic.
NO_PRODUCTION = ProductionCallers()


def derive_production_callers(repo_root: Path) -> ProductionCallers:
    """``caller=`` literals under ``maestro/``, split by whether ``tests/`` uses them."""
    labels = _tree_union(repo_root / "maestro", _caller_literals)
    exclusive = labels - _tree_union(repo_root / "tests", _caller_literals)
    return ProductionCallers(frozenset(labels), frozenset(exclusive))


def load_gate_windows(data_dir: Path, until: datetime | None = None) -> list[Window]:
    """Merged, sorted ``[start, end]`` windows of every gate run that ran pytest.

    With *until*, runs starting at or after it are dropped and the rest clipped to it.
    """
    spans: list[Window] = []
    for record in read_gate_runs(data_dir / "gate_runs.jsonl"):
        if record.admission == "declined":
            continue
        start = parse_ts(record.started_at)
        if start is None:
            continue
        end = start + timedelta(seconds=max(0.0, record.duration_seconds))
        if until is not None:
            if start >= until:
                continue
            end = min(end, until)
        spans.append((start, end))
    spans.sort()
    merged: list[Window] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def in_windows(stamp: datetime, windows: Sequence[Window]) -> bool:
    """Whether *stamp* falls inside any of the merged, sorted *windows*."""
    index = bisect.bisect_right([start for start, _ in windows], stamp) - 1
    return index >= 0 and stamp <= windows[index][1]


def _parse_row(line: str) -> dict[str, object] | None:
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    return row if isinstance(row, dict) else None


def classify_cost_row(
    row: dict[str, object] | None, windows: Sequence[Window], test_callers: frozenset[str],
    production: ProductionCallers = NO_PRODUCTION,
) -> str | None:
    """The rule a cost row matches — ``gate_window``, ``test_caller`` — or ``None``.

    An unparseable row (``None``) is always real: it stays live, as does a
    gate-window row *production* identifies as a real call.
    """
    if row is None:
        return None
    stamp = parse_ts(row.get("timestamp"))
    if stamp is not None and in_windows(stamp, windows):
        return None if production.spares(row) else "gate_window"
    if row.get("caller") in test_callers:
        return "test_caller"
    return None


def _log_stem_parts(log: Path) -> tuple[str, str] | None:
    """``(HH-MM-SS, short_id)`` from a ``SessionLogger`` ``.log`` filename."""
    stem, sep, short_id = log.stem.rpartition("_")
    return (stem, short_id) if sep else None


def sidecars_for(log: Path, siblings: Iterable[Path]) -> list[Path]:
    """The ``{stem}_{caller}_{short_id}.jsonl`` transcript sidecars of *log*."""
    parts = _log_stem_parts(log)
    if parts is None:
        return []
    stem, short_id = parts
    return sorted(
        p for p in siblings
        if p.suffix == ".jsonl" and p.parent == log.parent
        and p.name.startswith(f"{stem}_") and p.stem.endswith(f"_{short_id}")
        and p.stem != f"{stem}_{short_id}"
    )


def _sidecar_caller(log: Path, sidecar: Path) -> str:
    parts = _log_stem_parts(log)
    assert parts is not None
    stem, short_id = parts
    return sidecar.stem[len(stem) + 1:-(len(short_id) + 1)]


def _is_production_label(label: str, test_callers: frozenset[str]) -> bool:
    return label not in {"", "unknown", "None"} and label not in test_callers


def classify_log_file(
    path: Path,
    header: Sequence[str],
    mtime: datetime,
    windows: Sequence[Window],
    test_callers: frozenset[str],
    sidecars: Sequence[Path] = (),
) -> bool:
    """Whether a session log is test-shaped (and so belongs in quarantine).

    A test signal (mtime inside a gate window, or a fixture-query header shape)
    is required, *and* neither the header nor a sidecar filename may carry a
    production caller label.
    """
    signal = in_windows(mtime, windows) or any(
        marker in line for line in header for marker in TEST_HEADER_MARKERS
    )
    if not signal:
        return False
    for sidecar in sidecars:
        if _is_production_label(_sidecar_caller(path, sidecar), test_callers):
            return False
    for line in header:
        _, found, rest = line.partition("caller=")
        if found and _is_production_label(rest.split()[0] if rest.split() else "", test_callers):
            return False
    return True


def _walk_files(root: Path) -> list[Path]:
    """Every regular file under *root*, sorted (empty when *root* is missing)."""
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(os.path.getmtime(path), tz=UTC)


def _read_header(path: Path) -> list[str]:
    lines: list[str] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            lines.append(line.rstrip("\n"))
            if len(lines) >= HEADER_LINES:
                break
    return lines


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dst)


@dataclass
class LogPlan:
    """The test-shaped logs (with their sidecars) under ``logs/sdk``."""

    moves: list[Path] = field(default_factory=list)
    by_date: collections.Counter[str] = field(default_factory=collections.Counter)
    bytes_by_date: collections.Counter[str] = field(default_factory=collections.Counter)


def plan_log_moves(
    data_dir: Path, windows: Sequence[Window], test_callers: frozenset[str],
    since: datetime | None,
) -> LogPlan:
    log_root = data_dir / LOG_DIR
    files = _walk_files(log_root)
    by_parent: collections.defaultdict[Path, list[Path]] = collections.defaultdict(list)
    for path in files:
        by_parent[path.parent].append(path)
    plan = LogPlan()
    for path in files:
        if path.suffix != ".log":
            continue
        mtime = _mtime(path)
        if since is not None and mtime < since:
            continue
        sidecars = sidecars_for(path, by_parent[path.parent])
        if not classify_log_file(path, _read_header(path), mtime, windows, test_callers, sidecars):
            continue
        date = path.parent.name
        for moved in (path, *sidecars):
            plan.moves.append(moved)
            plan.by_date[date] += 1
            plan.bytes_by_date[date] += os.path.getsize(moved)
    return plan


def _read_lines(path: Path) -> list[str]:
    """Ledger lines with their original line endings (empty when missing)."""
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as handle:
        return list(handle)


@dataclass
class Partition:
    live: list[str] = field(default_factory=list)
    quarantine: list[str] = field(default_factory=list)
    counts: collections.Counter[tuple[str, str]] = field(default_factory=collections.Counter)
    cost: collections.defaultdict[tuple[str, str], float] = field(
        default_factory=lambda: collections.defaultdict(float),
    )
    spend: list[tuple[datetime, float]] = field(default_factory=list)
    spared: collections.Counter[str] = field(default_factory=collections.Counter)


def max_window_spend(spend: Sequence[tuple[datetime, float]], hours: float) -> float:
    """The largest summed cost inside any rolling *hours*-wide window."""
    ordered = sorted(spend)
    width = timedelta(hours=hours)
    best = total = 0.0
    lo = 0
    for stamp, cost in ordered:
        total += cost
        while ordered[lo][0] < stamp - width:
            total -= ordered[lo][1]
            lo += 1
        best = max(best, total)
    return best


def partition_ledger(
    lines: Iterable[str], windows: Sequence[Window], test_callers: frozenset[str],
    since: datetime | None, production: ProductionCallers = NO_PRODUCTION,
) -> Partition:
    """Split ledger lines into live and quarantine, tallying by ``(rule, caller)``.

    Rows before *since* (or with no parseable stamp while *since* is set) stay live.
    """
    part = Partition()
    for line in lines:
        row = _parse_row(line)
        rule = classify_cost_row(row, windows, test_callers, production)
        if rule is None and row is not None and production.spares(row):
            stamp = parse_ts(row.get("timestamp"))
            if stamp is not None and in_windows(stamp, windows):
                part.spared[str(row.get("caller"))] += 1
        if rule is not None and since is not None:
            stamp = parse_ts(row.get("timestamp")) if row is not None else None
            if stamp is None or stamp < since:
                rule = None
        if rule is None or row is None:
            part.live.append(line)
            continue
        part.quarantine.append(line)
        key = (rule, str(row.get("caller")))
        part.counts[key] += 1
        cost = row.get("cost_usd")
        amount = float(cost) if isinstance(cost, int | float) else 0.0
        part.cost[key] += amount
        stamp = parse_ts(row.get("timestamp"))
        if stamp is not None:
            part.spend.append((stamp, amount))
    return part


def print_report(
    part: Partition, logs: LogPlan, test_callers: frozenset[str], windows: Sequence[Window],
    window_hours: float,
) -> None:
    total = len(part.live) + len(part.quarantine)
    print(f"test-only callers ({len(test_callers)}): {', '.join(sorted(test_callers)) or '(none)'}")
    print(f"gate windows: {len(windows)}")
    print(f"cost rows: {total} total, {len(part.quarantine)} to quarantine, "
          f"{len(part.live)} live")
    if part.counts:
        print(f"  {'rule':<12} {'caller':<40} {'rows':>8} {'cost_usd':>12}")
        for key, count in sorted(part.counts.items(), key=lambda kv: (-kv[1], kv[0])):
            rule, caller = key
            print(f"  {rule:<12} {caller:<40} {count:>8} {part.cost[key]:>12.4f}")
    if part.spared:
        spared = ", ".join(f"{caller} {n}" for caller, n in sorted(part.spared.items()))
        print(f"gate-window rows kept live as production calls: {spared}")
    print(f"max quarantined cost_usd in any {window_hours:g}h window: "
          f"{max_window_spend(part.spend, window_hours):.4f}")
    print(f"session logs: {len(logs.moves)} files to quarantine, "
          f"{sum(logs.bytes_by_date.values())} bytes")
    for date in sorted(logs.by_date):
        print(f"  {date}: {logs.by_date[date]} files, {logs.bytes_by_date[date]} bytes")


def pid_is_live(pid_file: Path) -> bool:
    """Whether *pid_file* names a live process (mirrors ``cli._read_pid``)."""
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def apply(
    data_dir: Path, windows: Sequence[Window], test_callers: frozenset[str],
    since: datetime | None, production: ProductionCallers = NO_PRODUCTION,
) -> int:
    ledger = data_dir / LEDGER
    with file_lock(ledger):
        before = _read_lines(ledger)
        part = partition_ledger(before, windows, test_callers, since, production)
        if part.quarantine:
            # Quarantine first, live second: a crash in between duplicates rows
            # across the two files but can never drop one.
            with open(data_dir / QUARANTINE, "a", encoding="utf-8", newline="") as handle:
                handle.write("".join(part.quarantine))
            atomic_write(ledger, "".join(part.live))
    logs = plan_log_moves(data_dir, windows, test_callers, since)
    for path in logs.moves:
        _move(path, data_dir / LOG_QUARANTINE_DIR / path.relative_to(data_dir / LOG_DIR))
    print(f"cost rows: {len(before)} before, {len(part.live)} live after, "
          f"{len(part.quarantine)} quarantined")
    print(f"session logs: {len(logs.moves)} files moved to {LOG_QUARANTINE_DIR}")
    return 0


def _default_data_dir() -> tuple[Path, bool, float]:
    from maestro.config import Config

    config = Config.from_env()
    return config.memory_dir, config.sqlite_state_enabled, config.budget_rate_window_hours


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--since", default=None, help="ISO date or datetime (UTC)")
    parser.add_argument("--until", default=None,
                        help="ignore gate windows from this instant on (the fence deploy time)")
    parser.add_argument("--window-hours", type=float, default=None,
                        help="rolling spend window (default: budget_rate_window_hours)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--report", action="store_true", help="print the report (default)")
    mode.add_argument("--apply", action="store_true", help="move classified telemetry")
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    args = parser.parse_args(argv)

    default_dir, sqlite_enabled, default_hours = _default_data_dir()
    if sqlite_enabled:
        print("quarantine: sqlite_state_enabled routes cost rows to state.db; "
              "only the JSONL ledger is handled", file=sys.stderr)
        return 2
    data_dir: Path = args.data_dir or default_dir

    bounds: dict[str, datetime | None] = {}
    for name in ("since", "until"):
        text = getattr(args, name)
        bounds[name] = None if text is None else parse_ts(text)
        if text is not None and bounds[name] is None:
            print(f"quarantine: unparseable --{name} {text!r}", file=sys.stderr)
            return 2
    since, until = bounds["since"], bounds["until"]
    window_hours: float = args.window_hours or default_hours

    archive = data_dir / f"{LEDGER}.archive.jsonl"
    if archive.exists():
        print(f"quarantine: warning: cold archive {archive} is not processed", file=sys.stderr)

    test_callers = derive_test_only_callers(args.repo_root)
    production = derive_production_callers(args.repo_root)
    windows = load_gate_windows(data_dir, until)

    if args.apply:
        if pid_is_live(data_dir / "maestro.pid"):
            print("quarantine: maestro.pid names a live process; stop the loop first",
                  file=sys.stderr)
            return 1
        return apply(data_dir, windows, test_callers, since, production)

    part = partition_ledger(_read_lines(data_dir / LEDGER), windows, test_callers, since,
                            production)
    print_report(part, plan_log_moves(data_dir, windows, test_callers, since),
                 test_callers, windows, window_hours)
    return 0


if __name__ == "__main__":
    sys.exit(main())
