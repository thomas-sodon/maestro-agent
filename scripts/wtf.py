#!/usr/bin/env python3.14
"""Print a blunt digest of where the running loop's time and money actually went.

Read-only: every number comes from the persisted ledgers under the live data
directory. Nothing here touches git, the SDK, or a code host.

Usage: scripts/wtf.py [window] [--data-dir DIR] [--top N]
  window: 24h (default), 7d, 90m, ... ; "all" for the whole ledger history.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import re
import sys
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_DATA_DIRS = ["/home/maestro/maestro/data", "data"]


def ts_prefilter(field: str) -> re.Pattern[str]:
    """Regex for the *requested* timestamp field only.

    Bound to ``field`` so a nested `"timestamp"` appearing earlier in the raw
    line cannot make the cheap prefilter drop an in-window row.
    """
    return re.compile(r'"' + re.escape(field) + r'":\s*"([^"]+)"')


def ledger_files(path: pathlib.Path) -> list[pathlib.Path]:
    """The cold archive sibling (oldest rows) then the hot ledger.

    ``Memory.archive()`` rotates `<name>.jsonl` into `<name>.jsonl.archive.jsonl`
    every dream cycle, so a forensic read that skipped the sibling would report
    only the hot tier as "all time".
    """
    cold = path.with_suffix(path.suffix + ".archive.jsonl")
    return [candidate for candidate in (cold, path) if candidate.exists()]


def parse_window(text: str) -> timedelta | None:
    if text in ("all", "forever"):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([mhdw])", text.strip().lower())
    if not m:
        raise SystemExit(f"wtf: unparseable window {text!r} (try 24h, 7d, 90m, all)")
    value = float(m.group(1))
    unit = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m.group(2)]
    return timedelta(**{unit: value})


def parse_ts(text: str | None) -> datetime | None:
    """Parse an ISO stamp, treating anything unreadable as "undated".

    ``TypeError`` is caught alongside ``ValueError``: rows come from arbitrary
    JSONL, so a stamp persisted as a number reaches ``fromisoformat`` as an int
    and would otherwise crash the whole digest.
    """
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def rows(
    path: pathlib.Path, cutoff: datetime | None, field: str = "timestamp"
) -> Iterator[dict[str, Any]]:
    """Yield parsed JSONL rows at or after cutoff, skipping the cheap way."""
    prefilter = ts_prefilter(field)
    for ledger in ledger_files(path):
        with open(ledger, encoding="utf-8") as handle:
            for line in handle:
                if cutoff is not None:
                    match = prefilter.search(line)
                    if match:
                        stamp = parse_ts(match.group(1))
                        if stamp is not None and stamp < cutoff:
                            continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cutoff is not None:
                    stamp = parse_ts(row.get(field))
                    if stamp is not None and stamp < cutoff:
                        continue
                yield row


def plan_minted_at(plan: Mapping[str, Any]) -> datetime | None:
    """When the plan was created."""
    return parse_ts(plan.get("created_at"))


def merge_dates(data: pathlib.Path) -> dict[str, str]:
    """``plan_id`` -> ``merged_at`` from the version archive.

    The ``merged`` status flip (``EvolutionEngine`` in ``maestro/evolution.py``)
    stamps no timestamp on the plan itself, so the only record of *when* a plan
    landed is the ``VersionRecord`` appended to ``version_archive.jsonl`` at
    merge time.  Without it a merged plan would be dated by its human approval.
    """
    return {
        str(row.get("plan_id")): str(row.get("merged_at"))
        for row in rows(data / "version_archive.jsonl", None)
        if row.get("plan_id") and row.get("merged_at")
    }


def plan_settled_at(
    plan: Mapping[str, Any],
    merged_at: Mapping[str, str] | None = None,
) -> datetime | None:
    """When the plan reached its current fate.

    ``reviewed_at`` is stamped only by the human approval/rejection surfaces and
    by ``mark_plan_obsolete`` — *not* by the ``merged`` flip nor by
    ``_demote_plan_to_failed`` — so for those two fates it holds the approval
    moment and would date a plan that landed hours ago by the day it was
    approved, dropping it out of the window the digest was built to report.
    Each fate is therefore dated by its own terminal stamp where one exists:
    the version archive's ``merged_at`` for a merged plan, ``last_failure_at``
    for a failed one.  A plan still in flight has no terminal stamp at all, so
    fall back to the mint date rather than silently dating it now.
    """
    status = str(plan.get("status") or "")
    if status == "merged":
        landed = parse_ts((merged_at or {}).get(str(plan.get("plan_id"))))
        if landed is not None:
            return landed
    elif status == "failed":
        failed = parse_ts(plan.get("last_failure_at"))
        if failed is not None:
            return failed
    return parse_ts(plan.get("reviewed_at")) or parse_ts(plan.get("created_at"))


def in_window(stamp: datetime | None, cutoff: datetime | None, now: datetime) -> bool:
    """Whether a row dated ``stamp`` falls inside the requested window."""
    return cutoff is None or (stamp or now) >= cutoff


#: Ledgers routed *exclusively* to ``memory_dir/state.db`` when
#: ``[paths] sqlite_state_enabled`` is on. Their JSONL sidecars are the
#: flag-off rollback path and simply stop growing, so an empty read is a
#: configuration artifact, not a measured zero.
SQLITE_ROUTED_LEDGERS = (
    "cost_samples.jsonl",
    "gate_activations.jsonl",
    "audit_history.jsonl",
    "deed_ledger.jsonl",
)


def sqlite_store(data: pathlib.Path) -> pathlib.Path | None:
    """The shared SQLite state store, when this host has one."""
    store = data / "state.db"
    return store if store.exists() else None


def warn_if_sqlite_shadowed(data: pathlib.Path, ledger: str, count: int) -> None:
    """Shout rather than report a silent zero for a SQLite-routed ledger.

    This script reads JSONL only. If the sidecar is empty *and* a ``state.db``
    sits beside it, the honest answer is "this number may be unmeasured", not
    ``$0.00``.
    """
    store = sqlite_store(data)
    if count or store is None or ledger not in SQLITE_ROUTED_LEDGERS:
        return
    print(f"  !! {ledger} is EMPTY and {store} exists — under"
          " [paths] sqlite_state_enabled this ledger lives in SQLite, which this"
          " read does not open. Treat the figures below as UNMEASURED, not zero.")


def load_json(path: pathlib.Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def money(value: float) -> str:
    return f"${value:,.2f}"


def bar(share: float, width: int = 24) -> str:
    filled = round(share * width)
    return "#" * filled + "." * (width - filled)


def section(title: str) -> None:
    print(f"\n## {title}")


#: Width of ``table()``'s name column; keys longer than this break alignment.
NAME_WIDTH = 34


def table(counter: Mapping[str, float], total: float, limit: int, unit: str = "") -> None:
    if not counter:
        print("  (nothing recorded)")
        return
    ranked = sorted(counter.items(), key=lambda item: item[1], reverse=True)[:limit]
    for name, value in ranked:
        share = (value / total) if total else 0.0
        shown = money(value) if unit == "$" else f"{value:g}{unit}"
        print(f"  {name:<{NAME_WIDTH}} {shown:>12}  {share*100:5.1f}%  {bar(share)}")


# Mirrors ``maestro.types.INNER_THOUGHT_SOURCES`` (pinned by
# ``tests/test_wtf_script.py``) so this script stays stdlib-only.
INNER_THOUGHT_SOURCES = frozenset({"contemplation", "agent_tool"})
# Mirrors ``maestro.journal._LEGACY_THOUGHT_SOURCES``: retired sources whose
# historical rows still read as thought.
LEGACY_THOUGHT_SOURCES = frozenset({"evolution"})


def journal_digest(entries: list[dict[str, Any]], top: int) -> None:
    """Print an entry-type table and the last few entries verbatim."""
    kinds: collections.Counter[str] = collections.Counter(
        str(row.get("entry_type")) for row in entries)
    print(f"  {sum(kinds.values()):,} entries")
    table(kinds, sum(kinds.values()) or 1, top)
    print("\n  last few, verbatim:")
    for row in entries[-5:]:
        text = " ".join(str(row.get("content") or "").split())[:160]
        print(f"  - [{row.get('entry_type')}] {text}")


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("window", nargs="?", default="24h")
    parser.add_argument("--data-dir", default=os.environ.get("MAESTRO_DATA_DIR"))
    parser.add_argument("--top", type=int, default=10)
    args, unknown = parser.parse_known_args()
    if unknown:
        print(f"wtf: ignoring unrecognized argument(s) {' '.join(unknown)}", file=sys.stderr)
    # ``--top 0`` would otherwise print no table rows at all while dumping every
    # task in the window through the ``tasks[-top:]`` slice.
    top = max(1, args.top)

    candidates = [args.data_dir] if args.data_dir else DEFAULT_DATA_DIRS
    data = next((pathlib.Path(c) for c in candidates if c and pathlib.Path(c).is_dir()), None)
    if data is None:
        raise SystemExit(f"wtf: no data directory found (looked at {candidates})")

    span = parse_window(args.window)
    now = datetime.now(UTC)
    cutoff = None if span is None else now - span
    hours = span.total_seconds() / 3600 if span else None

    print(f"# What has Maestro been doing — window: {args.window} (data: {data})")

    # --- Is it even alive -------------------------------------------------
    section("Pulse")
    beat = parse_ts((data / "loop.heartbeat").read_text(encoding="utf-8").strip()
                    if (data / "loop.heartbeat").exists() else None)
    if beat:
        age = (now - beat).total_seconds()
        state = "ALIVE" if age < 600 else "STALE"
        print(f"  heartbeat: {beat.isoformat()}  ({age/60:.1f} min ago) -> {state}")
    else:
        print("  heartbeat: (missing)")
    lifecycle = load_json(data / "lifecycle.json") or {}
    print(f"  iteration: {lifecycle.get('iteration')}   phase: {lifecycle.get('current_phase')}"
          f" -> {lifecycle.get('next_phase')}")
    for key in ("consecutive_rate_limits", "consecutive_cli_crashes",
                "consecutive_stream_stalls", "consecutive_stale_sessions"):
        value = lifecycle.get(key)
        if value:
            print(f"  !! {key}: {value}")
    maintenance = load_json(data / "maintenance.json") or {}
    if maintenance.get("active"):
        print(f"  !! maintenance throttle active: {maintenance}")

    # --- Money by caller --------------------------------------------------
    section("Where the money went (per-call cost samples)")
    by_caller: dict[str, float] = collections.defaultdict(float)
    calls: collections.Counter[str] = collections.Counter()
    seconds: dict[str, float] = collections.defaultdict(float)
    for row in rows(data / "cost_samples.jsonl", cutoff):
        caller = row.get("caller") or "unknown"
        by_caller[caller] += float(row.get("cost_usd") or 0.0)
        calls[caller] += 1
        seconds[caller] += float(row.get("duration_ms") or 0) / 1000.0
    spend = sum(by_caller.values())
    warn_if_sqlite_shadowed(data, "cost_samples.jsonl", sum(calls.values()))
    print(f"  total {money(spend)} over {sum(calls.values()):,} calls"
          + (f"  ({money(spend/hours)}/hour)" if hours else ""))
    table(by_caller, spend, top, unit="$")
    if calls:
        print("\n  call counts / wall-clock seconds, same window:")
        top_callers = sorted(by_caller.items(), key=lambda item: item[1], reverse=True)
        for caller, _ in top_callers[: top]:
            print(f"  {caller:<{NAME_WIDTH}} {calls[caller]:>6} calls  {seconds[caller]/60:8.1f} min")

    # --- Iterations -------------------------------------------------------
    section("Iterations")
    iterations = list(rows(data / "usage.jsonl", cutoff, field="started_at"))
    it_cost = sum(float(r.get("total_cost_usd") or 0.0) for r in iterations)
    turns = sum(int(r.get("total_turns") or 0) for r in iterations)
    print(f"  {len(iterations):,} iterations, {turns:,} turns, {money(it_cost)} metered")
    if iterations and hours:
        print(f"  {len(iterations)/hours:.1f} iterations/hour")

    # --- Tasks ------------------------------------------------------------
    section("Tasks actually run")
    tasks = list(rows(data / "task_history.jsonl", cutoff))
    outcomes: collections.Counter[str] = collections.Counter()
    task_seconds = 0.0
    for task in tasks:
        outcomes[str(task.get("outcome"))] += 1
        task_seconds += float(task.get("duration_secs") or 0.0)
    print(f"  {len(tasks)} tasks, {task_seconds/3600:.1f} h of task wall-clock")
    table(outcomes, len(tasks) or 1, top)
    for task in tasks[-top:]:
        goal = " ".join(str(task.get("goal") or "").split())[:100]
        print(f"  - [{task.get('outcome')}] {goal}")

    # --- Plans: the actual output ----------------------------------------
    section("Evolution plans (the thing it exists to produce)")
    plans_all = list(rows(data / "evolution_plans.jsonl", None))
    latest: dict[str, dict[str, Any]] = {}
    for plan in plans_all:
        latest[str(plan.get("plan_id"))] = plan
    created = [p for p in latest.values() if in_window(plan_minted_at(p), cutoff, now)]
    # A fate (merged / killed) is dated by when the plan reached it, not when it
    # was minted: filtering fates on created_at hides every plan that landed or
    # died in the window but was minted before it.
    merged_at = merge_dates(data)
    settled = [
        p for p in latest.values()
        if in_window(plan_settled_at(p, merged_at), cutoff, now)
    ]
    status_window: collections.Counter[str] = collections.Counter(
        str(p.get("status")) for p in created)
    status_all: collections.Counter[str] = collections.Counter(
        str(p.get("status")) for p in latest.values())
    print(f"  minted in window: {len(created)}")
    table(status_window, len(created) or 1, top)
    print(f"\n  all time ({len(latest)} plans):")
    table(status_all, len(latest) or 1, top)
    merged = [p for p in settled if p.get("status") == "merged"]
    print(f"\n  merged in window: {len(merged)}")
    for plan in merged[: top]:
        print(f"  + {str(plan.get('title'))[:90]}  {plan.get('pr_url') or ''}")
    dead = [p for p in settled if p.get("status") in ("obsolete", "rejected", "failed")]
    reasons: collections.Counter[str] = collections.Counter()
    for plan in dead:
        reason = plan.get("obsolete_reason") or plan.get("rejection_reason") or \
            plan.get("last_failure_reason") or "(no reason recorded)"
        reasons[" ".join(str(reason).split())[:NAME_WIDTH]] += 1
    print(f"\n  killed in window: {len(dead)} — why:")
    table(reasons, len(dead) or 1, top)

    # --- Self-policing machinery -----------------------------------------
    section("Gates, audits, deeds")
    gates: collections.Counter[str] = collections.Counter(
        f"{r.get('gate')}:{r.get('action')}" for r in rows(data / "gate_activations.jsonl", cutoff))
    warn_if_sqlite_shadowed(data, "gate_activations.jsonl", sum(gates.values()))
    print("  gate firings:")
    table(gates, sum(gates.values()) or 1, top)
    audits: collections.Counter[str] = collections.Counter()
    for row in rows(data / "audit_history.jsonl", cutoff):
        for finding in row.get("findings") or []:
            rule = finding.get("rule") if isinstance(finding, dict) else finding
            audits[str(rule)[:NAME_WIDTH]] += 1
    warn_if_sqlite_shadowed(data, "audit_history.jsonl", sum(audits.values()))
    print("\n  audit findings:")
    table(audits, sum(audits.values()) or 1, top)
    deeds: collections.Counter[str] = collections.Counter(
        str(r.get("deed_type")) for r in rows(data / "deed_ledger.jsonl", cutoff))
    warn_if_sqlite_shadowed(data, "deed_ledger.jsonl", sum(deeds.values()))
    print("\n  terminal deeds fired:")
    table(deeds, sum(deeds.values()) or 1, top)

    # --- What it was telling itself --------------------------------------
    # Legacy operational rows written to journal.jsonl before the
    # thoughts/events split are routed to the events section, as
    # ``Journal`` does on read.
    thoughts: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for row in rows(data / "journal.jsonl", cutoff):
        (thoughts if row.get("triggered_by") in INNER_THOUGHT_SOURCES | LEGACY_THOUGHT_SOURCES else events).append(row)
    events.extend(rows(data / "events.jsonl", cutoff))
    section("Journal")
    journal_digest(thoughts, top)
    section("Events")
    journal_digest(events, top)

    # --- Self-model -------------------------------------------------------
    section("Self-model right now")
    model = load_json(data / "self_model.json") or {}
    print(f"  evolutions: {model.get('evolution_count')}")
    directive = model.get("next_task_directive")
    print(f"  next_task_directive: {str(directive)[:200] if directive else '(empty)'}")
    for field in ("pending_user_requests", "pending_pr_requests", "open_questions"):
        items = model.get(field) or []
        print(f"  {field}: {len(items)}")
        for item in items[:3]:
            print(f"    - {' '.join(str(item).split())[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
