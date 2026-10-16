"""Proposal 155 step 2 — the loop seams onto the self-health detectors."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.ci_diagnosis import CheckRun
from maestro.evolution_fuel import DetectorOutcome
from maestro.gap_store import (
    GapStore,
    GapStoreSchemaError,
    build_gap_store,
)
from maestro.issues import IssueTracker
from maestro.loop import plan_holds, self_health
from maestro.vcs import RefCheckRuns
from tests.component.mock_vcs import MockCodeHost

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
SHA = "a" * 40


def _store() -> GapStore:
    config = MagicMock()
    config.memory_dir = Path("/fake/mem")
    return build_gap_store(config, connection=sqlite3.connect(":memory:"))


def _agent(*, repo: str | None = "o/r", held_sha: str | None = None) -> Any:
    agent = MagicMock()
    agent.config.work_target.return_value.github_repo = repo
    agent.config.evolution_fuel_operator_github_logins = ("Thomas", "test-owner")
    agent.config.evolution_fuel_red_main_grace_hours = 12.0
    agent.config.loop_evolve_every = 5
    host = MockCodeHost(
        ref_check_runs={SHA: RefCheckRuns(SHA, (CheckRun("tests", "failure"),))},
    )
    agent.evolution.code_host = host
    agent.issues = IssueTracker("o/r", code_host=host)
    agent._vcs.rev_parse.return_value = SHA
    plan = MagicMock(inherited_hold_main_sha=held_sha)
    agent.memory.get_live_plan_records.return_value = [plan]
    return agent


def _closed(store: GapStore) -> bool:
    try:
        store.connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def test_no_repo_reads_nothing() -> None:
    agent = _agent(repo=None)
    with patch.object(self_health, "build_gap_store") as build:
        assert self_health.run_evolve_prestep(agent, NOW) is None
        assert self_health.open_inherited_gate(agent, MagicMock(), "x", SHA) is None
    build.assert_not_called()
    agent._vcs.rev_parse.assert_not_called()
    assert agent.evolution.code_host.ref_check_run_reads == []


def test_prestep_feeds_detectors_and_closes_store() -> None:
    agent = _agent(held_sha=SHA)
    store = _store()
    with (
        patch.object(self_health, "build_gap_store", return_value=store),
        patch.object(self_health, "scan_target", return_value=(Path("/w"), ("maestro",))),
        patch.object(self_health, "read_source_budget", return_value=[]) as scan,
        patch.object(self_health, "run_self_health_detectors") as run,
    ):
        self_health.run_evolve_prestep(agent, NOW)
    agent._vcs.rev_parse.assert_called_once_with("origin/main")
    scan.assert_called_once_with(Path("/w"), ("maestro",))
    kwargs = run.call_args.kwargs
    assert kwargs["sha"] == SHA
    assert kwargs["check_runs"] == (CheckRun("tests", "failure"),)
    assert kwargs["inherited_hold_live"] is True
    assert kwargs["operator_logins"] == frozenset({"thomas"})
    assert kwargs["grace_hours"] == 12.0
    assert kwargs["now"] == NOW
    assert _closed(store)


def test_prestep_unread_head_and_no_hold() -> None:
    agent = _agent(held_sha="b" * 40)
    agent._vcs.rev_parse.return_value = None
    store = _store()
    with (
        patch.object(self_health, "build_gap_store", return_value=store),
        patch.object(self_health, "scan_target", return_value=(Path("/w"), ())),
        patch.object(self_health, "read_source_budget", return_value=[]),
    ):
        report = self_health.run_evolve_prestep(agent, NOW)
    assert report is not None
    assert report.red_main.state == "unread"
    assert agent.evolution.code_host.ref_check_run_reads == []


def test_prestep_closes_store_on_sqlite_error() -> None:
    agent = _agent()
    store = _store()
    with (
        patch.object(self_health, "build_gap_store", return_value=store),
        patch.object(self_health, "scan_target", return_value=(Path("/w"), ())),
        patch.object(self_health, "read_source_budget", return_value=[]),
        patch.object(
            self_health, "run_self_health_detectors",
            side_effect=sqlite3.OperationalError("locked"),
        ),
        pytest.raises(sqlite3.OperationalError),
    ):
        self_health.run_evolve_prestep(agent, NOW)
    assert _closed(store)


def test_open_inherited_gate_skips_while_evolve_cadence_is_off() -> None:
    """No closer runs with evolve off, so the open is skipped too."""
    agent = _agent(held_sha=SHA)
    agent.config.loop_evolve_every = 0
    with patch.object(self_health, "build_gap_store") as build:
        assert self_health.open_inherited_gate(agent, MagicMock(), "x", SHA) is None
    build.assert_not_called()


def test_open_inherited_gate_opens_and_journals() -> None:
    agent = _agent()
    store = _store()
    plan = MagicMock(plan_id="p1")
    with patch.object(self_health, "build_gap_store", return_value=store):
        outcome = self_health.open_inherited_gate(
            agent, plan, "tests/test_x.py", SHA,
        )
    assert outcome is not None
    assert outcome.action == "opened"
    body = agent.evolution.code_host.created_issues[0]["body"]
    assert SHA in body
    agent.journal.record_event.assert_called_once()
    assert _closed(store)


def test_open_inherited_gate_uses_the_stamped_head() -> None:
    agent = _agent()
    plan = MagicMock(plan_id="p1")
    with (
        patch.object(self_health, "build_gap_store", return_value=_store()),
        patch.object(self_health, "open_gate_for_inherited_hold") as door,
    ):
        door.return_value = DetectorOutcome("maestro:gate", "reused", 1)
        self_health.open_inherited_gate(agent, plan, "x", "c" * 40)
    assert door.call_args.args[1] == "c" * 40
    agent._vcs.rev_parse.assert_not_called()
    agent.journal.record_event.assert_not_called()


# -- record_inherited_hold wiring -------------------------------------------------


def _hold_agent() -> Any:
    agent = MagicMock()
    agent.consciousness.load_self_model.return_value = MagicMock(next_task_directive="")
    return agent


def test_record_inherited_hold_opens_gate_once() -> None:
    agent = _hold_agent()
    plan = MagicMock(plan_id="p1", title="t")
    with patch.object(self_health, "open_inherited_gate") as gate:
        plan_holds.record_inherited_hold(agent, plan, "ids", 1, SHA)
    gate.assert_called_once_with(agent, plan, "ids", SHA)
    agent.consciousness.save_self_model.assert_called_once()


def test_record_inherited_hold_survives_gate_store_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = _hold_agent()
    plan = MagicMock(plan_id="p1", title="t")
    with (
        patch.object(
            self_health, "open_inherited_gate",
            side_effect=sqlite3.OperationalError("locked"),
        ),
        caplog.at_level(logging.WARNING, logger="maestro"),
    ):
        plan_holds.record_inherited_hold(agent, plan, "ids", 1, SHA)
    assert "Could not open the maestro:gate issue" in caplog.text
    agent.journal.record_event.assert_called_once()
    model = agent.consciousness.save_self_model.call_args.args[0]
    assert model.next_task_directive.startswith("Base repair:")


def test_record_inherited_hold_survives_gap_store_schema_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = _hold_agent()
    plan = MagicMock(plan_id="p1", title="t")
    with (
        patch.object(
            self_health, "open_inherited_gate",
            side_effect=GapStoreSchemaError("newer schema"),
        ),
        caplog.at_level(logging.WARNING, logger="maestro"),
    ):
        plan_holds.record_inherited_hold(agent, plan, "ids", 1, SHA)
    assert "Could not open the maestro:gate issue" in caplog.text
    model = agent.consciousness.save_self_model.call_args.args[0]
    assert model.next_task_directive.startswith("Base repair:")


def test_prestep_starts_no_subprocess_and_no_gate() -> None:
    """A5 at the seam: the prestep's own reads start no subprocess or gate."""
    agent = _agent(held_sha=SHA)
    store = _store()
    boom = MagicMock(side_effect=AssertionError("subprocess started"))
    gates = MagicMock()
    with (
        patch.object(self_health, "build_gap_store", return_value=store),
        patch.object(
            self_health, "scan_target",
            return_value=(Path("/nonexistent-p155-root"), ("maestro",)),
        ),
        patch.object(subprocess, "run", boom),
        patch.object(subprocess, "Popen", boom),
        patch.object(asyncio, "create_subprocess_exec", boom),
        patch("maestro.evolution_quality.run_quality_gates", gates),
    ):
        report = self_health.run_evolve_prestep(agent, NOW)
    assert report is not None
    assert report.red_main.state == "red_pending"
    assert agent.evolution.code_host.ref_check_run_reads == [SHA]
    boom.assert_not_called()
    gates.assert_not_called()
    agent.evolution.run_quality_gates.assert_not_called()


# --- Fuel selection seam (proposal 155 step 4) --------------------------------


def _report(*failing: str) -> Any:
    from maestro.evolution_fuel import RedMainReading, SelfHealthReport

    state = "red_due" if failing else "green"
    return SelfHealthReport(
        RedMainReading(state=state, sha=SHA, failing_checks=failing), (),
    )


def test_select_fuel_without_report_reads_nothing() -> None:
    from maestro.evolution_fuel import FuelSelection

    agent = _agent()
    with patch.object(self_health, "build_gap_store") as build:
        assert self_health.select_fuel(agent, None, NOW) == FuelSelection()
    build.assert_not_called()


def test_select_fuel_feeds_selector_and_closes_store() -> None:
    agent = _agent()
    agent.config.evolution_fuel_max_plans_per_gap = 2
    agent.memory.get_all_plans.return_value = ["plan"]
    store = _store()
    with (
        patch.object(self_health, "build_gap_store", return_value=store),
        patch.object(self_health, "select_evolution_fuel") as select,
    ):
        out = self_health.select_fuel(agent, _report("tests", "lint"), NOW)
    assert out is select.return_value
    kwargs = select.call_args.kwargs
    assert select.call_args.args == (NOW,)
    assert kwargs["plans"] == ["plan"]
    assert kwargs["max_attempts"] == 2
    assert kwargs["tracker"] is agent.issues
    assert kwargs["failed_excerpt"] == f"origin/main {SHA} is red on: tests, lint"
    assert _closed(store)


def test_select_fuel_closes_store_on_error() -> None:
    agent = _agent()
    store = _store()
    with (
        patch.object(self_health, "build_gap_store", return_value=store),
        patch.object(self_health, "select_evolution_fuel", side_effect=sqlite3.Error("x")),
        pytest.raises(sqlite3.Error),
    ):
        self_health.select_fuel(agent, _report(), NOW)
    assert _closed(store)


def test_green_main_has_no_excerpt() -> None:
    assert self_health._red_main_excerpt(_report().red_main) == ""


def test_red_excerpt_without_head_names_it_unknown() -> None:
    from maestro.evolution_fuel import RedMainReading

    reading = RedMainReading(state="red_due", sha="", failing_checks=("tests",))
    assert self_health._red_main_excerpt(reading) == (
        "origin/main (unknown head) is red on: tests"
    )


# --- the stuck-gap tripwire (proposal 155 step 5) -------------------------------


def _gap_agent() -> Any:

    agent = _agent()
    agent._pending_gap_asks = []
    agent.ask_thomas = AsyncMock(return_value="q1")
    return agent


def test_select_fuel_queues_trips() -> None:
    from maestro.evolution_fuel import FuelSelection, TrippedGap

    agent = _gap_agent()
    agent.config.evolution_fuel_max_plans_per_gap = 2
    trip = TrippedGap(7, 2)
    with (
        patch.object(self_health, "build_gap_store", return_value=_store()),
        patch.object(
            self_health, "select_evolution_fuel",
            return_value=FuelSelection(tripped=(trip,)),
        ),
    ):
        self_health.select_fuel(agent, _report(), NOW)
    assert agent._pending_gap_asks == [trip]


def test_deliver_sends_once_per_trip_and_drains() -> None:
    from maestro.evolution_fuel import TrippedGap

    agent = _gap_agent()
    agent._pending_gap_asks = [TrippedGap(7, 2), TrippedGap(9, 3)]
    asyncio.run(self_health.deliver_gap_decision_asks(agent))
    texts = [c.args[0] for c in agent.ask_thomas.await_args_list]
    assert [t.splitlines()[0][:16] for t in texts] == ["[gap-decision #7", "[gap-decision #9"]
    assert "https://github.com/o/r/issues/9" in texts[1]
    assert agent._pending_gap_asks == []
    asyncio.run(self_health.deliver_gap_decision_asks(agent))
    assert agent.ask_thomas.await_count == 2


def test_deliver_without_repo_sends_nothing() -> None:
    from maestro.evolution_fuel import TrippedGap

    agent = _gap_agent()
    agent.config.work_target.return_value.github_repo = None
    agent._pending_gap_asks = [TrippedGap(7, 2)]
    asyncio.run(self_health.deliver_gap_decision_asks(agent))
    agent.ask_thomas.assert_not_awaited()
    assert agent._pending_gap_asks == []


def test_plain_ask_round_trips_to_an_applied_reply() -> None:
    """The ask is a plain marked question, and a threaded reply still applies."""
    from maestro.evolution_fuel import TrippedGap

    agent = _gap_agent()
    agent._pending_gap_asks = [TrippedGap(7, 2)]
    asyncio.run(self_health.deliver_gap_decision_asks(agent))
    asked = agent.ask_thomas.await_args.args[0]
    assert asked.startswith("[gap-decision #7] ")
    question = _msg("q1", "maestro", asked)
    reply = _msg("r1", "thomas", "build", "q1")
    agent = _thread_agent(question, reply)
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, reply)) is True
    agent.issues.remove_label.assert_called_once_with(7, "needs-decision")


def _msg(mid: str, sender: str, content: str, reply_to: str | None = None) -> Any:
    from maestro.messaging import Message

    return Message(mid, "2026-10-14T00:00:00+00:00", sender, content,  # type: ignore[arg-type]
                   "answer" if reply_to else "question", reply_to=reply_to)


def _thread_agent(*msgs: Any) -> Any:
    agent = _gap_agent()
    agent.message_store.get_thread.return_value = list(msgs)
    agent.issues = MagicMock()
    agent.issues.remove_label.return_value = True
    agent.ask_thomas = AsyncMock(return_value="n1")
    return agent


def test_reply_build_applies_and_is_consumed() -> None:
    question = _msg("q1", "maestro", "[gap-decision #7] Gap decision: gap issue 7")
    reply = _msg("r1", "thomas", "build", "q1")
    agent = _thread_agent(question, reply)
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, reply)) is True
    agent.issues.remove_label.assert_called_once_with(7, "needs-decision")
    agent.message_store.get_thread.assert_called_once_with("q1")


def test_second_reply_is_ignored() -> None:
    question = _msg("q1", "maestro", "[gap-decision #7] x")
    first, second = _msg("r1", "thomas", "buy", "q1"), _msg("r2", "thomas", "build", "q1")
    agent = _thread_agent(question, first, second)
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, second)) is True
    agent.issues.remove_label.assert_not_called()
    agent.journal.record_event.assert_not_called()


def test_unparseable_reply_is_consumed_without_a_write() -> None:
    question = _msg("q1", "maestro", "[gap-decision #7] x")
    reply = _msg("r1", "thomas", "hmm, not sure", "q1")
    agent = _thread_agent(question, reply)
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, reply)) is True
    agent.issues.remove_label.assert_not_called()
    stamp = agent.journal.record_event.call_args.args[0]
    assert stamp == "gap-decision-noop #7: r1"
    told = agent.ask_thomas.await_args.args[0]
    assert told.startswith("[gap-decision #7] ")
    assert "build or buy" in told


def test_redrained_no_option_reply_is_not_nagged_twice() -> None:
    question = _msg("q1", "maestro", "[gap-decision #7] x")
    reply = _msg("r1", "thomas", "hmm", "q1")
    agent = _thread_agent(question, reply)
    agent.journal.read_events_by_type.return_value = [
        MagicMock(content="gap-decision-noop #7: r1"),
    ]
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, reply)) is True
    agent.ask_thomas.assert_not_awaited()
    agent.journal.record_event.assert_not_called()


def test_no_option_reply_on_an_answered_thread_is_not_nagged() -> None:
    question = _msg("q1", "maestro", "[gap-decision #7] x")
    build, thanks = _msg("r1", "thomas", "build", "q1"), _msg("r2", "thomas", "thanks", "q1")
    agent = _thread_agent(question, build, thanks)
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, thanks)) is True
    agent.ask_thomas.assert_not_awaited()
    agent.issues.remove_label.assert_not_called()


def test_ordinary_question_reply_is_not_consumed() -> None:
    question = _msg("q1", "maestro", "Should I refactor?")
    reply = _msg("r1", "thomas", "build", "q1")
    agent = _thread_agent(question, reply)
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, reply)) is False
    missing = _thread_agent(reply)
    assert asyncio.run(self_health.apply_gap_decision_reply(missing, reply)) is False


def test_reply_without_store_or_thread_is_not_consumed() -> None:
    agent = _gap_agent()
    agent.message_store = None
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, _msg("r", "thomas", "build", "q"))) is False
    agent = _gap_agent()
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, _msg("r", "thomas", "build"))) is False


def test_corrective_reply_after_an_unparseable_one_applies() -> None:
    question = _msg("q1", "maestro", "[gap-decision #7] x")
    typo, fixed = _msg("r1", "thomas", "biuld", "q1"), _msg("r2", "thomas", "build", "q1")
    agent = _thread_agent(question, typo, fixed)
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, fixed)) is True
    agent.issues.remove_label.assert_called_once_with(7, "needs-decision")


def test_build_reply_guidance_is_journaled() -> None:
    question = _msg("q1", "maestro", "[gap-decision #7] x")
    reply = _msg("r1", "thomas", "build — try splitting the module instead", "q1")
    agent = _thread_agent(question, reply)
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, reply)) is True
    text = agent.journal.record_event.call_args.args[0]
    assert text.endswith("Thomas's note: try splitting the module instead")


def test_undelivered_ask_is_logged_and_journaled(caplog: pytest.LogCaptureFixture) -> None:
    from maestro.evolution_fuel import TrippedGap

    agent = _gap_agent()
    agent.ask_thomas = AsyncMock(return_value="")
    agent._pending_gap_asks = [TrippedGap(7, 2)]
    with caplog.at_level(logging.WARNING, logger="maestro"):
        asyncio.run(self_health.deliver_gap_decision_asks(agent))
    assert "undelivered" in caplog.text
    head = agent.journal.record_event.call_args.args[0]
    assert head.startswith("gap-decision-undelivered #7:")


def test_failed_build_is_reported_and_a_repeat_applies() -> None:
    question = _msg("q1", "maestro", "[gap-decision #7] x")
    first, second = _msg("r1", "thomas", "build", "q1"), _msg("r2", "thomas", "build", "q1")
    agent = _thread_agent(question, first, second)
    agent.journal.read_events_by_type.return_value = []
    agent.issues.remove_label.return_value = False
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, first)) is True
    unapplied = agent.journal.record_event.call_args.args[0]
    assert unapplied.startswith("gap-decision-unapplied #7: r1 ")
    told = agent.ask_thomas.await_args.args[0]
    assert told.startswith("[gap-decision #7] ")
    assert "Reply to this message" in told
    agent.journal.read_events_by_type.return_value = [MagicMock(content=unapplied)]
    agent.issues.remove_label.return_value = True
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, second)) is True
    assert agent.issues.remove_label.call_count == 2


def test_reply_to_a_failure_notice_routes_and_applies() -> None:
    from maestro.evolution_fuel import format_gap_decision_followup

    notice = _msg("n1", "maestro", format_gap_decision_followup(7, "did not apply"))
    reply = _msg("r3", "thomas", "build", "n1")
    agent = _thread_agent(notice, reply)
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, reply)) is True
    agent.issues.remove_label.assert_called_once_with(7, "needs-decision")


def test_drop_reply_points_to_the_issue_and_does_not_win_the_thread() -> None:
    question = _msg("q1", "maestro", "[gap-decision #7] x")
    drop, build = _msg("r1", "thomas", "drop", "q1"), _msg("r2", "thomas", "build", "q1")
    agent = _thread_agent(question, drop, build)
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, drop)) is True
    agent.issues.remove_label.assert_not_called()
    pointer = agent.ask_thomas.await_args.args[0]
    assert pointer.startswith("[gap-decision #7] ")
    assert "close issue #7 yourself" in pointer
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, build)) is True
    agent.issues.remove_label.assert_called_once_with(7, "needs-decision")


def test_redrained_applied_reply_is_not_reapplied() -> None:
    question = _msg("q1", "maestro", "[gap-decision #7] x")
    reply = _msg("r1", "thomas", "build", "q1")
    agent = _thread_agent(question, reply)
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, reply)) is True
    applied = agent.journal.record_event.call_args.args[0]
    assert "(reply r1)" in applied
    agent.journal.read_events_by_type.return_value = [
        MagicMock(content="gap-decision #7: buy (reply r0) — x"),
        MagicMock(content=applied),
    ]
    assert asyncio.run(self_health.apply_gap_decision_reply(agent, reply)) is True
    agent.issues.remove_label.assert_called_once_with(7, "needs-decision")
    agent.ask_thomas.assert_not_awaited()
