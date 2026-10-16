"""Regression tests for ``scripts/wtf.py``, the read-only forensic digest.

The script is the whole implementation behind ``/wtf``: it answers "where did
the dollars and the iterations go, and what landed" purely from the persisted
ledgers.  Every test here pins a way an earlier revision reported a *wrong*
number rather than failing loudly:

* plan fates (merged / killed) were dated by ``created_at``, so a plan that
  landed inside the window but was minted before it reported ``merged in
  window: 0`` — the command doc's headline "what landed" figure;
* each ledger was read hot-tier only, so ``all time`` silently shrank to the
  post-rotation tail once ``Memory.archive()`` split it;
* the cheap prefilter matched the first timestamp-shaped key anywhere in the
  raw line rather than the field the caller asked for, so a nested stamp could
  drop an in-window row before ``json.loads`` ever saw it;
* ``--top 0`` printed no table rows while dumping *every* task in the window;
* an unrecognized argument hard-exited ``2`` with no report, contradicting the
  command doc's "warn and use the defaults";
* the SQLite-routed ledgers reported a confident ``$0.00`` when ``[paths]
  sqlite_state_enabled`` had moved them out of JSONL;
* a stamp persisted as a number crashed the whole digest through
  ``fromisoformat``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "wtf.py"
_DATA = "/fake/data"


def _load_wtf() -> Any:
    """Import ``scripts/wtf.py`` by path — ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("wtf_script", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def wtf() -> Any:
    return _load_wtf()


def _jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(row) + "\n" for row in rows)


def _iso(when: datetime) -> str:
    return when.isoformat()


@pytest.fixture
def run_main(wtf: Any, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Drive ``main()`` against the in-memory ledgers under ``/fake/data``."""
    monkeypatch.setattr(Path, "is_dir", lambda self: str(self) == _DATA)

    def _run(*argv: str) -> int:
        monkeypatch.setattr(sys, "argv", ["wtf.py", *argv, "--data-dir", _DATA])
        return int(wtf.main())

    return _run


# --- The cheap prefilter must be bound to the requested field ---------------


def test_ts_prefilter_matches_only_the_requested_field(wtf: Any) -> None:
    """A nested ``timestamp`` cannot satisfy a prefilter asking for ``started_at``."""
    line = '{"timestamp": "2020-01-01T00:00:00+00:00", "started_at": "2026-09-17T00:00:00+00:00"}'
    assert wtf.ts_prefilter("started_at").search(line).group(1) == "2026-09-17T00:00:00+00:00"


def test_prefilter_keeps_in_window_row_despite_earlier_nested_stamp(
    wtf: Any, mem_fs: dict[str, str],
) -> None:
    """An in-window row is not dropped by an out-of-window stamp under another key."""
    now = datetime.now(UTC)
    mem_fs[f"{_DATA}/usage.jsonl"] = _jsonl([
        {"timestamp": _iso(now - timedelta(days=400)), "started_at": _iso(now), "iteration": 7},
    ])
    kept = list(wtf.rows(Path(f"{_DATA}/usage.jsonl"), now - timedelta(hours=24), field="started_at"))
    assert [row["iteration"] for row in kept] == [7]


def test_rows_skips_out_of_window_row_on_the_requested_field(
    wtf: Any, mem_fs: dict[str, str],
) -> None:
    """The window filter still excludes a row whose own field predates the cutoff."""
    now = datetime.now(UTC)
    mem_fs[f"{_DATA}/usage.jsonl"] = _jsonl([
        {"started_at": _iso(now - timedelta(days=9)), "iteration": 1},
        {"started_at": _iso(now), "iteration": 2},
    ])
    kept = list(wtf.rows(Path(f"{_DATA}/usage.jsonl"), now - timedelta(hours=24), field="started_at"))
    assert [row["iteration"] for row in kept] == [2]


def test_rows_skips_corrupted_line(wtf: Any, mem_fs: dict[str, str]) -> None:
    """A truncated JSONL line is skipped, not fatal."""
    mem_fs[f"{_DATA}/journal.jsonl"] = '{"entry_type": "a"}\n{not json\n'
    assert [row["entry_type"] for row in wtf.rows(Path(f"{_DATA}/journal.jsonl"), None)] == ["a"]


# --- Cold archive sibling ---------------------------------------------------


def test_ledger_files_reads_cold_sibling_first(wtf: Any, mem_fs: dict[str, str]) -> None:
    """``Memory.archive()``'s ``<name>.jsonl.archive.jsonl`` sibling is read too."""
    mem_fs[f"{_DATA}/task_history.jsonl"] = ""
    mem_fs[f"{_DATA}/task_history.jsonl.archive.jsonl"] = ""
    found = wtf.ledger_files(Path(f"{_DATA}/task_history.jsonl"))
    assert [path.name for path in found] == [
        "task_history.jsonl.archive.jsonl",
        "task_history.jsonl",
    ]


def test_ledger_files_omits_a_missing_sibling(wtf: Any, mem_fs: dict[str, str]) -> None:
    """A host that has never rotated yields the hot ledger alone."""
    mem_fs[f"{_DATA}/task_history.jsonl"] = ""
    found = wtf.ledger_files(Path(f"{_DATA}/task_history.jsonl"))
    assert [path.name for path in found] == ["task_history.jsonl"]


def test_ledger_files_omits_a_missing_hot_ledger(wtf: Any, mem_fs: dict[str, str]) -> None:
    """Nothing on disk yields nothing to read, and ``rows`` stays empty."""
    assert wtf.ledger_files(Path(f"{_DATA}/absent.jsonl")) == []
    assert list(wtf.rows(Path(f"{_DATA}/absent.jsonl"), None)) == []


def test_rows_spans_cold_and_hot_tiers(wtf: Any, mem_fs: dict[str, str]) -> None:
    """Rotation does not hide the cold tier from an ``all``-window read."""
    mem_fs[f"{_DATA}/evolution_plans.jsonl.archive.jsonl"] = _jsonl([{"plan_id": "cold"}])
    mem_fs[f"{_DATA}/evolution_plans.jsonl"] = _jsonl([{"plan_id": "hot"}])
    seen = [row["plan_id"] for row in wtf.rows(Path(f"{_DATA}/evolution_plans.jsonl"), None)]
    assert seen == ["cold", "hot"]


# --- Plan fates are dated by review, not by mint ----------------------------


def test_plan_settled_at_prefers_reviewed_at(wtf: Any) -> None:
    """A terminal plan is dated by when it reached that fate."""
    settled = wtf.plan_settled_at(
        {"created_at": "2026-09-01T00:00:00+00:00", "reviewed_at": "2026-09-17T00:00:00+00:00"},
    )
    assert settled == datetime(2026, 9, 17, tzinfo=UTC)


def test_plan_settled_at_falls_back_to_mint_date(wtf: Any) -> None:
    """A plan still in flight has no ``reviewed_at``; it is not dated 'now'."""
    settled = wtf.plan_settled_at({"created_at": "2026-09-01T00:00:00+00:00"})
    assert settled == datetime(2026, 9, 1, tzinfo=UTC)


def test_plan_settled_at_dates_a_merged_plan_by_the_version_archive(wtf: Any) -> None:
    """A plan approved before the window but merged inside it stays in ``settled``.

    ``reviewed_at`` holds the *approval* moment for a merged plan — the merge
    flip stamps nothing — so dating the fate by it dropped the landed plan the
    digest exists to report.
    """
    plan = {
        "plan_id": "p1",
        "status": "merged",
        "created_at": "2026-09-01T00:00:00+00:00",
        "reviewed_at": "2026-09-14T00:00:00+00:00",
    }
    settled = wtf.plan_settled_at(plan, {"p1": "2026-09-17T12:00:00+00:00"})
    assert settled == datetime(2026, 9, 17, 12, tzinfo=UTC)


def test_plan_settled_at_merged_without_archive_row_falls_back(wtf: Any) -> None:
    """No ``VersionRecord`` (or an unreadable stamp) still dates, never crashes."""
    plan = {
        "plan_id": "p1",
        "status": "merged",
        "created_at": "2026-09-01T00:00:00+00:00",
        "reviewed_at": "2026-09-14T00:00:00+00:00",
    }
    assert wtf.plan_settled_at(plan, {}) == datetime(2026, 9, 14, tzinfo=UTC)
    assert wtf.plan_settled_at(plan) == datetime(2026, 9, 14, tzinfo=UTC)


def test_plan_settled_at_dates_a_failed_plan_by_its_failure(wtf: Any) -> None:
    """``_demote_plan_to_failed`` stamps ``last_failure_at``, not ``reviewed_at``."""
    plan = {
        "plan_id": "p2",
        "status": "failed",
        "created_at": "2026-09-01T00:00:00+00:00",
        "reviewed_at": "2026-09-05T00:00:00+00:00",
        "last_failure_at": "2026-09-18T00:00:00+00:00",
    }
    assert wtf.plan_settled_at(plan) == datetime(2026, 9, 18, tzinfo=UTC)


def test_plan_settled_at_failed_without_failure_stamp_falls_back(wtf: Any) -> None:
    """A legacy ``failed`` row with no ``last_failure_at`` keeps its old dating."""
    plan = {
        "plan_id": "p2",
        "status": "failed",
        "created_at": "2026-09-01T00:00:00+00:00",
        "reviewed_at": "2026-09-05T00:00:00+00:00",
    }
    assert wtf.plan_settled_at(plan) == datetime(2026, 9, 5, tzinfo=UTC)


def test_merge_dates_maps_plan_ids_to_merge_stamps(wtf: Any, mem_fs: dict[str, str]) -> None:
    """Rows missing either key are skipped rather than mapping to ``None``."""
    mem_fs[f"{_DATA}/version_archive.jsonl"] = _jsonl([
        {"plan_id": "p1", "merged_at": "2026-09-17T12:00:00+00:00"},
        {"plan_id": "p2"},
        {"merged_at": "2026-09-17T13:00:00+00:00"},
    ])
    assert wtf.merge_dates(Path(_DATA)) == {"p1": "2026-09-17T12:00:00+00:00"}


def test_merge_dates_is_empty_without_the_ledger(wtf: Any, mem_fs: dict[str, str]) -> None:
    """A checkout that never merged a plan yields an empty map, not a crash."""
    assert wtf.merge_dates(Path(_DATA)) == {}


def test_plan_minted_at_reads_created_at(wtf: Any) -> None:
    """'Minted in window' keeps using the mint date."""
    minted = wtf.plan_minted_at(
        {"created_at": "2026-09-01T00:00:00+00:00", "reviewed_at": "2026-09-17T00:00:00+00:00"},
    )
    assert minted == datetime(2026, 9, 1, tzinfo=UTC)


def test_in_window_admits_everything_for_the_all_window(wtf: Any) -> None:
    now = datetime.now(UTC)
    assert wtf.in_window(now - timedelta(days=900), None, now) is True


def test_in_window_keeps_an_undated_row(wtf: Any) -> None:
    """An unparseable/absent stamp is kept rather than silently dropped."""
    now = datetime.now(UTC)
    assert wtf.in_window(None, now - timedelta(hours=1), now) is True


def test_in_window_excludes_a_stamp_before_the_cutoff(wtf: Any) -> None:
    now = datetime.now(UTC)
    assert wtf.in_window(now - timedelta(days=2), now - timedelta(hours=1), now) is False


def test_plan_merged_in_window_but_minted_earlier_is_reported(
    mem_fs: dict[str, str], run_main: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    """The regression: a plan minted last week and merged today counts as landed."""
    now = datetime.now(UTC)
    mem_fs[f"{_DATA}/evolution_plans.jsonl"] = _jsonl([
        {
            "plan_id": "old-merge",
            "title": "Landed today, minted last week",
            "status": "merged",
            "created_at": _iso(now - timedelta(days=7)),
            "reviewed_at": _iso(now - timedelta(hours=2)),
            "pr_url": "https://example.invalid/pr/1",
        },
        {
            "plan_id": "old-kill",
            "title": "Killed today, minted last week",
            "status": "obsolete",
            "created_at": _iso(now - timedelta(days=7)),
            "reviewed_at": _iso(now - timedelta(hours=3)),
            "obsolete_reason": "Relieves pending_review saturation",
        },
    ])
    assert run_main("24h") == 0
    out = capsys.readouterr().out
    assert "minted in window: 0" in out
    assert "merged in window: 1" in out
    assert "Landed today, minted last week" in out
    assert "killed in window: 1" in out
    assert "Relieves pending_review saturation" in out


def test_plan_killed_outside_the_window_is_excluded(
    mem_fs: dict[str, str], run_main: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    """A fate older than the window is not counted as in-window output."""
    now = datetime.now(UTC)
    mem_fs[f"{_DATA}/evolution_plans.jsonl"] = _jsonl([
        {
            "plan_id": "stale",
            "status": "rejected",
            "created_at": _iso(now - timedelta(days=40)),
            "reviewed_at": _iso(now - timedelta(days=30)),
        },
    ])
    assert run_main("24h") == 0
    out = capsys.readouterr().out
    assert "merged in window: 0" in out
    assert "killed in window: 0" in out
    assert "all time (1 plans)" in out


def test_kill_reason_keys_fit_the_table_column(
    wtf: Any, mem_fs: dict[str, str], run_main: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    """A long kill reason is truncated to the name column, keeping rows aligned."""
    now = datetime.now(UTC)
    mem_fs[f"{_DATA}/evolution_plans.jsonl"] = _jsonl([
        {
            "plan_id": "verbose",
            "status": "obsolete",
            "created_at": _iso(now),
            "reviewed_at": _iso(now),
            "obsolete_reason": "R" * (wtf.NAME_WIDTH + 60),
        },
    ])
    assert run_main("24h") == 0
    reason_line = next(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("  RRR")
    )
    assert reason_line.startswith("  " + "R" * wtf.NAME_WIDTH + " ")


# --- Argument handling ------------------------------------------------------


def test_unrecognized_argument_warns_and_still_reports(
    mem_fs: dict[str, str], run_main: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    """The command doc promises "warn and use the defaults", not a usage exit."""
    assert run_main("24h", "--bogus") == 0
    captured = capsys.readouterr()
    assert "ignoring unrecognized argument(s) --bogus" in captured.err
    assert "window: 24h" in captured.out


def test_top_zero_is_clamped_in_both_directions(
    mem_fs: dict[str, str], run_main: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    """``--top 0`` prints one ranked row, not zero rows and every task."""
    now = datetime.now(UTC)
    mem_fs[f"{_DATA}/task_history.jsonl"] = _jsonl([
        {"timestamp": _iso(now), "outcome": "success", "goal": f"goal-{index}", "duration_secs": 1}
        for index in range(3)
    ])
    assert run_main("24h", "--top", "0") == 0
    out = capsys.readouterr().out
    assert "  success" in out
    assert "- [success] goal-2" in out
    assert "goal-0" not in out


def test_unparseable_window_exits_with_one_line(wtf: Any) -> None:
    with pytest.raises(SystemExit, match="unparseable window"):
        wtf.parse_window("tomorrow")


def test_all_window_has_no_cutoff(wtf: Any) -> None:
    assert wtf.parse_window("all") is None
    assert wtf.parse_window("forever") is None


def test_window_units(wtf: Any) -> None:
    assert wtf.parse_window("90m") == timedelta(minutes=90)
    assert wtf.parse_window("7d") == timedelta(days=7)
    assert wtf.parse_window("2w") == timedelta(weeks=2)


def test_missing_data_dir_exits_with_one_line(
    wtf: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda self: False)
    monkeypatch.setattr(sys, "argv", ["wtf.py", "24h"])
    with pytest.raises(SystemExit, match="no data directory found"):
        wtf.main()


def test_data_dir_flag_overrides_the_defaults(
    wtf: Any, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--data-dir`` wins over ``DEFAULT_DATA_DIRS`` and the env default."""
    monkeypatch.setenv("MAESTRO_DATA_DIR", "/fake/env-data")
    monkeypatch.setattr(Path, "is_dir", lambda self: str(self) == "/fake/flag-data")
    monkeypatch.setattr(sys, "argv", ["wtf.py", "all", "--data-dir", "/fake/flag-data"])
    assert wtf.main() == 0
    assert "data: /fake/flag-data" in capsys.readouterr().out


# --- Pulse and self-model rendering ----------------------------------------


def test_stale_heartbeat_and_lifecycle_alarms_are_flagged(
    mem_fs: dict[str, str], run_main: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(UTC)
    mem_fs[f"{_DATA}/loop.heartbeat"] = _iso(now - timedelta(hours=3))
    mem_fs[f"{_DATA}/lifecycle.json"] = json.dumps(
        {"iteration": 42, "current_phase": "act", "next_phase": "reflect",
         "consecutive_rate_limits": 3},
    )
    mem_fs[f"{_DATA}/maintenance.json"] = json.dumps({"active": True})
    assert run_main("24h") == 0
    out = capsys.readouterr().out
    assert "-> STALE" in out
    assert "!! consecutive_rate_limits: 3" in out
    assert "!! maintenance throttle active" in out


def test_missing_heartbeat_is_reported_not_fatal(
    mem_fs: dict[str, str], run_main: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_main("all") == 0
    out = capsys.readouterr().out
    assert "heartbeat: (missing)" in out
    assert "(nothing recorded)" in out


def test_live_heartbeat_reads_alive(
    mem_fs: dict[str, str], run_main: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    mem_fs[f"{_DATA}/loop.heartbeat"] = _iso(datetime.now(UTC))
    assert run_main("24h") == 0
    assert "-> ALIVE" in capsys.readouterr().out


def test_unparseable_timestamp_is_ignored(wtf: Any) -> None:
    assert wtf.parse_ts("not-a-date") is None
    assert wtf.parse_ts(None) is None


def test_naive_timestamp_is_read_as_utc(wtf: Any) -> None:
    assert wtf.parse_ts("2026-09-17T00:00:00") == datetime(2026, 9, 17, tzinfo=UTC)


def test_load_json_tolerates_corruption(wtf: Any, mem_fs: dict[str, str]) -> None:
    mem_fs[f"{_DATA}/self_model.json"] = "{not json"
    assert wtf.load_json(Path(f"{_DATA}/self_model.json")) is None
    assert wtf.load_json(Path(f"{_DATA}/absent.json")) is None


def test_spend_and_iteration_sections_report_per_hour_rates(
    mem_fs: dict[str, str], run_main: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(UTC)
    mem_fs[f"{_DATA}/cost_samples.jsonl"] = _jsonl([
        {"timestamp": _iso(now), "caller": "auto_fix", "cost_usd": 3.0, "duration_ms": 60000},
        {"timestamp": _iso(now), "caller": "introspect", "cost_usd": 1.0, "duration_ms": 30000},
    ])
    mem_fs[f"{_DATA}/usage.jsonl"] = _jsonl([
        {"started_at": _iso(now), "total_cost_usd": 4.0, "total_turns": 11},
    ])
    assert run_main("24h") == 0
    out = capsys.readouterr().out
    assert "total $4.00 over 2 calls" in out
    assert "($0.17/hour)" in out
    assert "auto_fix" in out
    assert "1 iterations, 11 turns, $4.00 metered" in out
    assert "iterations/hour" in out


def test_gate_audit_deed_and_journal_sections_render(
    mem_fs: dict[str, str], run_main: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(UTC)
    mem_fs[f"{_DATA}/gate_activations.jsonl"] = _jsonl([
        {"timestamp": _iso(now), "gate": "wip_primacy", "action": "block"},
    ])
    mem_fs[f"{_DATA}/audit_history.jsonl"] = _jsonl([
        {"timestamp": _iso(now), "findings": [{"rule": "phantom_merge"}, "bare_rule"]},
    ])
    mem_fs[f"{_DATA}/deed_ledger.jsonl"] = _jsonl([
        {"timestamp": _iso(now), "deed_type": "stand_down"},
    ])
    mem_fs[f"{_DATA}/journal.jsonl"] = _jsonl([
        {"timestamp": _iso(now), "entry_type": "audit_observation", "content": "a finding"},
        {"timestamp": _iso(now), "entry_type": "reflection", "content": "a thought",
         "triggered_by": "contemplation"},
    ])
    mem_fs[f"{_DATA}/events.jsonl"] = _jsonl([
        {"timestamp": _iso(now), "entry_type": "milestone", "content": "merged PR"},
    ])
    assert run_main("24h") == 0
    out = capsys.readouterr().out
    assert "wip_primacy:block" in out
    assert "phantom_merge" in out
    assert "bare_rule" in out
    assert "stand_down" in out
    assert "- [audit_observation] a finding" in out
    journal, events = out.split("Events", 1)
    assert "- [reflection] a thought" in journal
    assert "a finding" not in journal
    assert "- [milestone] merged PR" in events
    assert "a thought" not in events


def test_inner_thought_sources_mirror_types(wtf: Any) -> None:
    from maestro.types import INNER_THOUGHT_SOURCES

    assert wtf.INNER_THOUGHT_SOURCES == INNER_THOUGHT_SOURCES


def test_legacy_thought_sources_mirror_journal(wtf: Any) -> None:
    from maestro.journal import _LEGACY_THOUGHT_SOURCES

    assert wtf.LEGACY_THOUGHT_SOURCES == _LEGACY_THOUGHT_SOURCES


def test_self_model_section_reports_directive_and_queues(
    mem_fs: dict[str, str], run_main: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    mem_fs[f"{_DATA}/self_model.json"] = json.dumps({
        "evolution_count": 5,
        "next_task_directive": "Finish PR #3903",
        "open_questions": ["[audit] why do plans die?"],
    })
    assert run_main("all") == 0
    out = capsys.readouterr().out
    assert "  evolutions: 5" in out
    assert "stage" not in out
    assert "next_task_directive: Finish PR #3903" in out
    assert "- [audit] why do plans die?" in out
    assert "pending_user_requests: 0" in out


def test_empty_directive_renders_as_empty(
    mem_fs: dict[str, str], run_main: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    mem_fs[f"{_DATA}/self_model.json"] = json.dumps({"evolution_count": 0})
    assert run_main("all") == 0
    assert "next_task_directive: (empty)" in capsys.readouterr().out


def test_bar_spans_empty_to_full(wtf: Any) -> None:
    assert wtf.bar(0.0, width=4) == "...."
    assert wtf.bar(1.0, width=4) == "####"


def test_table_with_zero_total_shows_zero_share(
    wtf: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    wtf.table({"only": 0.0}, 0.0, 5)
    assert "0.0%" in capsys.readouterr().out


# --- SQLite-routed ledgers must not be reported as a measured zero ----------


def test_sqlite_routed_empty_ledger_is_flagged_not_reported_as_zero(
    mem_fs: dict[str, str], run_main: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    """``[paths] sqlite_state_enabled`` moves these ledgers out of JSONL entirely.

    The sidecars stay behind as the flag-off rollback path and stop growing, so
    an empty read beside a ``state.db`` is a configuration artifact — reporting
    ``total $0.00`` as fact would be the digest's headline "what it spent"
    number, silently wrong.
    """
    mem_fs[f"{_DATA}/state.db"] = ""
    assert run_main("24h") == 0
    out = capsys.readouterr().out
    assert "!! cost_samples.jsonl is EMPTY" in out
    assert "!! gate_activations.jsonl is EMPTY" in out
    assert "!! audit_history.jsonl is EMPTY" in out
    assert "!! deed_ledger.jsonl is EMPTY" in out
    assert "UNMEASURED, not zero" in out


def test_no_sqlite_banner_without_a_state_db(
    mem_fs: dict[str, str], run_main: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    """A host that never flipped the flag reads an honest measured zero."""
    assert run_main("24h") == 0
    assert "UNMEASURED" not in capsys.readouterr().out


def test_no_sqlite_banner_when_the_sidecar_has_rows(
    mem_fs: dict[str, str], run_main: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    """A populated sidecar is a real measurement even beside a ``state.db``."""
    mem_fs[f"{_DATA}/state.db"] = ""
    mem_fs[f"{_DATA}/cost_samples.jsonl"] = _jsonl([
        {"timestamp": _iso(datetime.now(UTC)), "caller": "introspect", "cost_usd": 2.0},
    ])
    assert run_main("24h") == 0
    out = capsys.readouterr().out
    assert "!! cost_samples.jsonl is EMPTY" not in out
    assert "total $2.00 over 1 calls" in out


def test_sqlite_warning_ignores_a_ledger_that_is_not_routed(
    wtf: Any, mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str],
) -> None:
    """``journal.jsonl`` keeps its JSONL home under the flag; no banner for it."""
    mem_fs[f"{_DATA}/state.db"] = ""
    wtf.warn_if_sqlite_shadowed(Path(_DATA), "journal.jsonl", 0)
    assert capsys.readouterr().out == ""


def test_sqlite_store_is_none_when_absent(wtf: Any, mem_fs: dict[str, str]) -> None:
    assert wtf.sqlite_store(Path(_DATA)) is None


# --- Malformed stamps degrade to "undated" rather than crashing -------------


def test_numeric_timestamp_is_undated_not_a_crash(wtf: Any, mem_fs: dict[str, str]) -> None:
    """An epoch-int stamp reaches ``fromisoformat`` as an int (``TypeError``)."""
    assert wtf.parse_ts(1758000000) is None
    mem_fs[f"{_DATA}/journal.jsonl"] = _jsonl([{"timestamp": 1758000000, "entry_type": "odd"}])
    kept = list(wtf.rows(Path(f"{_DATA}/journal.jsonl"), datetime.now(UTC) - timedelta(hours=1)))
    assert [row["entry_type"] for row in kept] == ["odd"]


def test_post_parse_cutoff_catches_a_row_the_prefilter_missed(
    wtf: Any, mem_fs: dict[str, str],
) -> None:
    """The prefilter is a cheap skip, not the filter: odd spacing defeats it.

    A hand-edited (or differently-serialised) ledger writing ``"timestamp" :``
    never matches the regex, so the post-``json.loads`` re-check is the only
    thing keeping the out-of-window row out.
    """
    now = datetime.now(UTC)
    old = _iso(now - timedelta(days=30))
    mem_fs[f"{_DATA}/journal.jsonl"] = (
        '{"timestamp" : "' + old + '", "entry_type": "old"}\n'
        + json.dumps({"timestamp": _iso(now), "entry_type": "new"}) + "\n"
    )
    kept = list(wtf.rows(Path(f"{_DATA}/journal.jsonl"), now - timedelta(hours=24)))
    assert [row["entry_type"] for row in kept] == ["new"]
