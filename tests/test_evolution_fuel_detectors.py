"""Proposal 155 step 2 — the zero-SDK self-health detectors."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from maestro import evolution_fuel
from maestro.ci_diagnosis import CheckRun
from maestro.evolution_fuel import (
    GATE_OWNER_ID,
    SOURCE_BUDGET_MAX_OPEN_ATTEMPTS,
    DetectorOutcome,
    RedMainReading,
    classify_gap_trust,
    detect_red_main,
    detect_source_budget,
    observe_red_main,
    open_gate_for_inherited_hold,
    record_detector_outcome,
    run_self_health_detectors,
)
from maestro.gap_store import GapStore, build_gap_store
from maestro.issues import Issue, IssueTracker
from maestro.source_budget import SPLIT_BUDGET, SPLIT_DUE, SourceBand, SourceBudgetReading
from maestro.vcs import IssueClosure
from tests.component.mock_vcs import MockCodeHost

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
OPS = ("thomas",)
GRACE = 12.0
RED = (CheckRun("tests", "failure"), CheckRun("lint", "success"))
RED_OTHER = (CheckRun("tests", "failure"), CheckRun("lint", "failure"))
GREEN = (CheckRun("tests", "success"), CheckRun("lint", "success"))
RUNNING = (CheckRun("tests", "in_progress"),)


class _Host(MockCodeHost):
    """Created issues become readable; a loop close is a bot close."""

    def create_issue(
        self, title: str, body: str, labels: list[str] | None = None,
    ) -> int | None:
        number = super().create_issue(title, body, labels)
        if number is not None:
            self.issues.append({
                "number": number, "title": title, "body": body,
                "labels": list(labels or []), "state": "open",
            })
        return number

    def close_issue(self, number: int) -> bool:
        closed = super().close_issue(number)
        if closed:
            self.issue_closures[number] = IssueClosure("maestro-bot", via_commit=False)
        return closed


def _store() -> GapStore:
    config = MagicMock()
    config.memory_dir = Path("/fake/mem")
    return build_gap_store(config, connection=sqlite3.connect(":memory:"))


def _rig() -> tuple[_Host, IssueTracker, GapStore]:
    host = _Host()
    return host, IssueTracker("o/r", code_host=host), _store()


def _observe(store: GapStore, runs: Any, now: datetime, sha: str = "a" * 40) -> RedMainReading:
    return observe_red_main(store, sha=sha, runs=runs, now=now, grace_hours=GRACE)


def _detect(
    reading: RedMainReading, tracker: IssueTracker, store: GapStore,
    now: datetime = NOW, *, hold: bool = False,
) -> DetectorOutcome | None:
    return detect_red_main(
        reading, inherited_hold_live=hold, tracker=tracker, store=store,
        operator_logins=OPS, now=now,
    )


def _watch(store: GapStore) -> list[sqlite3.Row]:
    return list(store.connection.execute("SELECT * FROM red_main_watch"))


# -- red main (A1) ------------------------------------------------------------


def test_red_then_green_inside_grace_opens_nothing(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    reading = _observe(store, RED, NOW)
    assert reading.state == "red_pending"
    assert reading.failing_checks == ("tests",)
    assert _detect(reading, tracker, store) is None
    green = _observe(store, GREEN, NOW + timedelta(hours=6))
    assert green.state == "green"
    assert _detect(green, tracker, store) is None
    assert host.created_issues == []
    assert _watch(store) == []


def test_red_past_grace_opens_exactly_one_gate_issue(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    _observe(store, RED, NOW, sha="a" * 40)
    later = NOW + timedelta(hours=GRACE)
    reading = _observe(store, RED, later, sha="b" * 40)
    assert reading.state == "red_due"
    assert reading.first_red_sha == "a" * 40
    outcome = _detect(reading, tracker, store, later)
    assert outcome == DetectorOutcome(GATE_OWNER_ID, "opened", 1)
    assert len(host.created_issues) == 1
    assert host.created_issues[0]["labels"] == ["gap:maestro"]
    assert "tests" in host.created_issues[0]["body"]
    row = store.connection.execute("SELECT decision_ref FROM gap_opens").fetchone()
    assert row[0] == f"red-main:{'a' * 12}:1-checks"
    watch = _watch(store)[0]
    assert watch["last_read_sha"] == "b" * 40
    third = _observe(store, RED, later + timedelta(hours=1), sha="c" * 40)
    assert _detect(third, tracker, store) == DetectorOutcome(GATE_OWNER_ID, "reused", 1)
    assert len(host.created_issues) == 1


def test_partial_read_subset_keeps_clock(mem_fs: dict[str, str]) -> None:
    _, _, store = _rig()
    _observe(store, RED_OTHER, NOW)
    later = NOW + timedelta(hours=GRACE)
    partial = (CheckRun("tests", "failure"), CheckRun("lint", "in_progress"))
    reading = _observe(store, partial, later, sha="b" * 40)
    assert reading.state == "red_due"
    assert reading.first_red_sha == "a" * 40
    assert reading.failing_checks == ("lint", "tests")
    assert _watch(store)[0]["last_read_sha"] == "b" * 40


def test_partial_read_with_new_failure_restarts_clock(mem_fs: dict[str, str]) -> None:
    _, _, store = _rig()
    _observe(store, RED, NOW)
    later = NOW + timedelta(hours=GRACE)
    partial = (CheckRun("lint", "failure"), CheckRun("browser", "queued"))
    reading = _observe(store, partial, later, sha="b" * 40)
    assert reading.state == "red_pending"
    assert reading.first_red_sha == "b" * 40
    assert reading.failing_checks == ("lint",)


def test_changed_failing_set_restarts_clock(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    _observe(store, RED, NOW)
    later = NOW + timedelta(hours=GRACE)
    reading = _observe(store, RED_OTHER, later, sha="b" * 40)
    assert reading.state == "red_pending"
    assert reading.first_red_sha == "b" * 40
    assert reading.failing_checks == ("lint", "tests")
    assert _detect(reading, tracker, store, later) is None
    assert host.created_issues == []


def test_running_keeps_row_and_due_state(mem_fs: dict[str, str]) -> None:
    _, _, store = _rig()
    assert _observe(store, RUNNING, NOW).state == "running"
    _observe(store, RED, NOW)
    assert _observe(store, RUNNING, NOW + timedelta(hours=1)).state == "running"
    due = _observe(store, RUNNING, NOW + timedelta(hours=GRACE))
    assert due.state == "red_due"
    assert due.failing_checks == ("tests",)
    assert len(_watch(store)) == 1


def test_unread_writes_nothing(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    assert _observe(store, None, NOW).state == "unread"
    assert _observe(store, RED, NOW, sha="").state == "unread"
    assert _watch(store) == []
    assert _detect(RedMainReading("unread"), tracker, store) is None
    assert host.closed_issues == []


# -- close paths (A6) ---------------------------------------------------------


def _opened_gate(tracker: IssueTracker, store: GapStore) -> None:
    _observe(store, RED, NOW - timedelta(hours=GRACE))
    reading = _observe(store, RED, NOW)
    assert _detect(reading, tracker, store) is not None


def test_green_closes_gate_issue(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    _opened_gate(tracker, store)
    green = _observe(store, GREEN, NOW, sha="d" * 40)
    assert _detect(green, tracker, store) == DetectorOutcome(
        GATE_OWNER_ID, "closed", 1, "green",
    )
    assert host.closed_issues == [1]
    row = store.connection.execute("SELECT close_ref FROM gap_opens").fetchone()
    assert row[0] == f"main-green:{'d' * 12}"
    assert _detect(green, tracker, store) is None


def test_green_read_closes_without_hold(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    _opened_gate(tracker, store)
    outcome = _detect(RedMainReading("green", "e" * 40), tracker, store)
    assert outcome is not None
    assert outcome.action == "closed"
    assert host.closed_issues == [1]


@pytest.mark.parametrize("state", ["red_pending", "running"])
def test_still_maybe_red_read_never_closes(mem_fs: dict[str, str], state: str) -> None:
    host, tracker, store = _rig()
    _opened_gate(tracker, store)
    assert _detect(RedMainReading(state, "e" * 40), tracker, store) is None  # type: ignore[arg-type]
    assert host.closed_issues == []


def test_live_inherited_hold_keeps_issue_open(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    _opened_gate(tracker, store)
    assert _detect(RedMainReading("green", "e" * 40), tracker, store, hold=True) is None
    assert host.closed_issues == []


# -- inherited hold (A2) ------------------------------------------------------


def _hold(tracker: IssueTracker, store: GapStore) -> DetectorOutcome:
    return open_gate_for_inherited_hold(
        "plan-1", "f" * 40, "tests/test_x.py::test_y", tracker=tracker,
        store=store, operator_logins=OPS, now=NOW,
    )


def test_hold_then_red_main_share_one_issue(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    assert _hold(tracker, store) == DetectorOutcome(GATE_OWNER_ID, "opened", 1)
    row = store.connection.execute("SELECT decision_ref FROM gap_opens").fetchone()
    assert row[0] == f"inherited-hold:plan-1@{'f' * 12}"
    _observe(store, RED, NOW - timedelta(hours=GRACE))
    reading = _observe(store, RED, NOW)
    assert _detect(reading, tracker, store) == DetectorOutcome(GATE_OWNER_ID, "reused", 1)
    assert len(host.created_issues) == 1
    assert len(host.reply_calls) == 1
    assert host.reply_calls[0]["issue_number"] == 1
    assert f"first red at {'a' * 40}" in host.reply_calls[0]["body"]
    assert "Failing checks: tests." in host.reply_calls[0]["body"]
    # The same episode's evidence is commented once, not every tick.
    assert _detect(reading, tracker, store) == DetectorOutcome(GATE_OWNER_ID, "reused", 1)
    assert len(host.reply_calls) == 1


def test_red_main_then_hold_share_one_issue(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    _opened_gate(tracker, store)
    assert _hold(tracker, store) == DetectorOutcome(GATE_OWNER_ID, "reused", 1)
    assert len(host.created_issues) == 1
    assert len(host.reply_calls) == 1
    assert "Plan plan-1 is held" in host.reply_calls[0]["body"]
    assert _hold(tracker, store) == DetectorOutcome(GATE_OWNER_ID, "reused", 1)
    assert len(host.reply_calls) == 1


def test_failed_evidence_comment_retries_next_tick(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    _opened_gate(tracker, store)
    with patch.object(tracker, "comment", return_value=False):
        assert _hold(tracker, store) == DetectorOutcome(GATE_OWNER_ID, "reused", 1)
    assert host.reply_calls == []
    _hold(tracker, store)
    assert len(host.reply_calls) == 1


def test_reopened_gate_issue_gets_new_episode_evidence(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    _opened_gate(tracker, store)
    _detect(_observe(store, GREEN, NOW, sha="d" * 40), tracker, store)
    later = NOW + timedelta(days=7)
    _observe(store, RED_OTHER, later - timedelta(hours=GRACE), sha="b" * 40)
    reading = _observe(store, RED_OTHER, later, sha="b" * 40)
    assert _detect(reading, tracker, store, later) == DetectorOutcome(
        GATE_OWNER_ID, "reopened", 1,
    )
    assert len(host.reply_calls) == 1
    body = host.reply_calls[0]["body"]
    assert f"first red at {'b' * 40}" in body
    assert "Failing checks: lint, tests." in body


def test_hold_without_head_names_unknown(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    open_gate_for_inherited_hold(
        "plan-2", None, "x", tracker=tracker, store=store, operator_logins=OPS, now=NOW,
    )
    assert "unknown" in host.created_issues[0]["body"]


# -- source budget (A3) -------------------------------------------------------


def test_reuse_with_changed_size_posts_no_new_comment(mem_fs: dict[str, str]) -> None:
    """A byte-size change inside one band keeps one decision_ref: no comment noise."""
    host, tracker, store = _rig()
    first = [_reading("maestro/big.py", SPLIT_BUDGET + 10, SourceBand.OVER_BUDGET)]
    edited = [_reading("maestro/big.py", SPLIT_BUDGET + 11, SourceBand.OVER_BUDGET)]
    _budget(first, tracker, store)
    _budget(edited, tracker, store)
    _budget(edited, tracker, store)
    assert host.reply_calls == []
    rows = list(store.connection.execute("SELECT decision_ref FROM gap_opens"))
    assert [r[0] for r in rows] == ["source-budget:over_budget"]


def _reading(path: str, size: int, band: SourceBand) -> SourceBudgetReading:
    return SourceBudgetReading(relative=path, size=size, band=band)


def _eleven() -> list[SourceBudgetReading]:
    over = [
        _reading(f"maestro/over{i}.py", SPLIT_BUDGET + 100 * i, SourceBand.OVER_BUDGET)
        for i in range(8)
    ]
    due = [
        _reading(f"maestro/due{i}.py", SPLIT_DUE + i, SourceBand.SPLIT_DUE)
        for i in range(3)
    ]
    return [*due, *over, _reading("maestro/ok.py", 10, SourceBand.OK)]


def _budget(
    readings: list[SourceBudgetReading], tracker: IssueTracker, store: GapStore,
) -> list[DetectorOutcome]:
    return detect_source_budget(
        readings, tracker=tracker, store=store, operator_logins=OPS, now=NOW,
    )


def test_empty_scan_closes_nothing(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    _budget(_eleven(), tracker, store)
    assert _budget([], tracker, store) == []
    assert host.issue_closures == {}


def test_one_issue_for_worst_over_budget_file(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    owner = "maestro:source_budget:maestro/over7.py"
    assert _budget(_eleven(), tracker, store) == [DetectorOutcome(owner, "opened", 1)]
    assert len(host.created_issues) == 1
    assert host.created_issues[0]["title"].startswith(owner)
    assert _budget(_eleven(), tracker, store) == [DetectorOutcome(owner, "reused", 1)]
    worse = [*_eleven(), _reading("maestro/huge.py", SPLIT_BUDGET * 2, SourceBand.OVER_CEILING)]
    assert _budget(worse, tracker, store) == [DetectorOutcome(owner, "reused", 1)]
    assert len(host.created_issues) == 1


def test_shrink_closes_then_next_tick_opens_new_worst(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    _budget(_eleven(), tracker, store)
    shrunk = [r for r in _eleven() if r.relative != "maestro/over7.py"]
    outcomes = _budget(shrunk, tracker, store)
    assert outcomes == [
        DetectorOutcome(
            "maestro:source_budget:maestro/over7.py", "closed", 1, "under-budget",
        ),
        DetectorOutcome("maestro:source_budget:maestro/over6.py", "opened", 2),
    ]
    assert host.closed_issues == [1]


def test_split_due_alone_opens_nothing(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    due = [r for r in _eleven() if r.band is not SourceBand.OVER_BUDGET]
    assert _budget(due, tracker, store) == []
    assert host.created_issues == []


def test_abandoned_worst_file_is_skipped(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    _budget(_eleven(), tracker, store)
    host.issues[0]["state"] = "closed"
    host.issue_closures[1] = IssueClosure("Thomas", via_commit=False)
    outcomes = _budget(_eleven(), tracker, store)
    assert outcomes == [
        DetectorOutcome(
            "maestro:source_budget:maestro/over7.py", "refused", 1, "operator-abandoned",
        ),
        DetectorOutcome("maestro:source_budget:maestro/over6.py", "opened", 2),
    ]


@pytest.mark.parametrize("reason", ["host-failed", "closure-unknown"])
def test_unsafe_refusal_tries_no_second_candidate(
    mem_fs: dict[str, str], reason: str,
) -> None:
    _, tracker, store = _rig()
    refused = evolution_fuel.GapOpenResult("refused", reason=reason)
    with patch.object(evolution_fuel, "open_gap_issue", return_value=refused) as door:
        outcomes = _budget(_eleven(), tracker, store)
    assert door.call_count == 1
    assert outcomes == [
        DetectorOutcome("maestro:source_budget:maestro/over7.py", "refused", reason=reason),
    ]


def test_failed_create_opens_no_second_file(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    with patch.object(host, "create_issue", return_value=None):
        outcomes = _budget(_eleven(), tracker, store)
    assert [o.reason for o in outcomes] == ["host-failed"]


def test_at_most_three_attempts(mem_fs: dict[str, str]) -> None:
    _, tracker, store = _rig()
    refused = evolution_fuel.GapOpenResult("refused", reason="operator-abandoned")
    with patch.object(evolution_fuel, "open_gap_issue", return_value=refused) as door:
        outcomes = _budget(_eleven(), tracker, store)
    assert door.call_count == SOURCE_BUDGET_MAX_OPEN_ATTEMPTS
    assert len(outcomes) == SOURCE_BUDGET_MAX_OPEN_ATTEMPTS


def test_unparseable_path_is_not_a_candidate(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    bad = [_reading("maestro/bad name!.py", SPLIT_BUDGET + 1, SourceBand.OVER_BUDGET)]
    assert _budget(bad, tracker, store) == []
    assert host.created_issues == []


def test_host_failure_opens_nothing(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    _budget(_eleven(), tracker, store)
    host.issues.clear()  # the door issue now reads None (unreadable)
    assert _budget(_eleven(), tracker, store) == [
        DetectorOutcome("maestro:source_budget:", "refused", reason="host-failed"),
    ]
    assert len(host.created_issues) == 1


def test_failed_close_stops_before_opening(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    _budget(_eleven(), tracker, store)
    shrunk = [r for r in _eleven() if r.relative != "maestro/over7.py"]
    with patch.object(evolution_fuel, "close_gap_issue", return_value=None):
        outcomes = _budget(shrunk, tracker, store)
    assert outcomes == [
        DetectorOutcome(
            "maestro:source_budget:maestro/over7.py", "refused", reason="host-failed",
        ),
    ]
    assert len(host.created_issues) == 1


# -- trust (A4) -----------------------------------------------------------------


@pytest.mark.parametrize(("actor", "trust"), [("Thomas", "operator"), ("maestro-bot", "unverified")])
def test_bug_shaped_issue_trust_follows_label_actor(
    mem_fs: dict[str, str], actor: str, trust: str,
) -> None:
    host, tracker, store = _rig()
    host.issue_label_actors[(5, "gap:maestro")] = actor
    issue = Issue(5, "Planner crashes on empty queue", ["bug", "gap:maestro"], "", "")
    assert classify_gap_trust(
        issue, tracker=tracker, store=store, operator_logins=OPS,
    ) == trust


# -- run_self_health_detectors (A5, journal) ------------------------------------


def _run(
    tracker: IssueTracker, store: GapStore, journal: Any, runs: Any,
    readings: list[SourceBudgetReading], now: datetime = NOW,
) -> Any:
    return run_self_health_detectors(
        sha="a" * 40, check_runs=runs, inherited_hold_live=False,
        readings=readings, grace_hours=GRACE, tracker=tracker, store=store,
        operator_logins=OPS, journal=journal, now=now,
    )


def test_detectors_start_no_subprocess_and_no_gate(mem_fs: dict[str, str]) -> None:
    _, tracker, store = _rig()
    boom = MagicMock(side_effect=AssertionError("subprocess started"))
    gates = MagicMock()
    with (
        patch.object(subprocess, "run", boom),
        patch.object(subprocess, "Popen", boom),
        patch.object(asyncio, "create_subprocess_exec", boom),
        patch("maestro.evolution.EvolutionEngine.run_quality_gates", gates),
    ):
        _run(tracker, store, MagicMock(), RED, _eleven(), NOW - timedelta(hours=GRACE))
        report = _run(tracker, store, MagicMock(), RED, _eleven())
    assert report.red_main.state == "red_due"
    boom.assert_not_called()
    gates.assert_not_called()


def test_journal_records_state_changes_not_reuse(mem_fs: dict[str, str]) -> None:
    _, tracker, store = _rig()
    journal = MagicMock()
    _run(tracker, store, journal, RED, [], NOW - timedelta(hours=GRACE))
    assert journal.record_event.call_count == 0
    report = _run(tracker, store, journal, RED, _eleven())
    assert [o.action for o in report.outcomes] == ["opened", "opened"]
    assert journal.record_event.call_count == 2
    first = journal.record_event.call_args_list[0]
    assert first.args[0] == "gap-opened maestro:gate #1"
    assert first.kwargs == {
        "entry_type": "audit_observation", "triggered_by": "evolution_fuel",
    }
    journal.reset_mock()
    report = _run(tracker, store, journal, RED, _eleven())
    assert [o.action for o in report.outcomes] == ["reused", "reused"]
    journal.record_event.assert_not_called()
    report = _run(tracker, store, journal, GREEN, _eleven())
    assert report.outcomes[0].action == "closed"
    assert journal.record_event.call_args_list[0].args[0] == (
        "gap-closed maestro:gate #1 (green)"
    )


def test_refusals_log_by_reason(caplog: pytest.LogCaptureFixture) -> None:
    journal = MagicMock()
    with caplog.at_level("INFO", logger="maestro"):
        record_detector_outcome(journal, DetectorOutcome("x", "refused", reason="operator-abandoned"))
        record_detector_outcome(journal, DetectorOutcome("y", "refused", reason="host-failed"))
    levels = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert levels == [
        ("INFO", "Self-health detector refused x: operator-abandoned"),
        ("WARNING", "Self-health detector refused y: host-failed"),
    ]
    journal.record_event.assert_not_called()


@pytest.fixture(autouse=True)
def _reset_abandon_memo() -> Any:
    evolution_fuel._SURFACED_GATE_ABANDONS.clear()
    yield
    evolution_fuel._SURFACED_GATE_ABANDONS.clear()


def test_abandoned_gate_is_surfaced_once(caplog: pytest.LogCaptureFixture) -> None:
    journal = MagicMock()
    events: list[Any] = []
    journal.read_events_by_type.side_effect = lambda _t: events
    journal.record_event.side_effect = lambda content, **_: events.append(
        MagicMock(content=content),
    )
    outcome = DetectorOutcome(GATE_OWNER_ID, "refused", 3, "operator-abandoned")
    with caplog.at_level("INFO", logger="maestro"):
        record_detector_outcome(journal, outcome)
        record_detector_outcome(journal, outcome)
    assert journal.record_event.call_count == 1
    assert journal.read_events_by_type.call_count == 1  # memo skips the 2nd walk
    content = journal.record_event.call_args.args[0]
    assert content.startswith("gap-abandoned maestro:gate #3:")
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


def test_abandoned_gate_without_number(mem_fs: dict[str, str]) -> None:
    journal = MagicMock()
    journal.read_events_by_type.return_value = []
    record_detector_outcome(
        journal, DetectorOutcome(GATE_OWNER_ID, "refused", reason="operator-abandoned"),
    )
    assert journal.record_event.call_args.args[0].startswith("gap-abandoned maestro:gate:")


def test_abandoned_gate_already_journalled_is_not_rerecorded(mem_fs: dict[str, str]) -> None:
    """A prior process's observation found in the journal is not re-recorded."""
    journal = MagicMock()
    outcome = DetectorOutcome(GATE_OWNER_ID, "refused", 3, "operator-abandoned")
    record_detector_outcome(MagicMock(read_events_by_type=MagicMock(return_value=[])), outcome)
    content = next(iter(evolution_fuel._SURFACED_GATE_ABANDONS))
    evolution_fuel._SURFACED_GATE_ABANDONS.clear()
    journal.read_events_by_type.return_value = [MagicMock(content=content)]
    record_detector_outcome(journal, outcome)
    record_detector_outcome(journal, outcome)
    journal.record_event.assert_not_called()
    assert journal.read_events_by_type.call_count == 1
