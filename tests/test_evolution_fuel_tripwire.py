"""The stuck-gap tripwire and its build/buy/drop decision (proposal 155 step 5)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro.evolution_fuel import (
    NEEDS_DECISION_LABEL,
    FuelSelection,
    apply_gap_decision,
    close_gap_issue,
    format_gap_decision_ask,
    gap_decision_note,
    gap_issue_url,
    open_gap_issue,
    parse_gap_decision_question,
    parse_gap_decision_reply,
    select_evolution_fuel,
)
from maestro.gap_store import GapStore, build_gap_store
from maestro.issues import IssueTracker
from maestro.memory import EvolutionPlan
from maestro.vcs import IssueClosure, LabelRemoval
from tests.component.mock_vcs import MockCodeHost

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
OPS = ("Thomas",)
OWNER = "maestro:source_budget:maestro/agent.py"


class _Host(MockCodeHost):
    """A MockCodeHost whose created issues become listable open issues."""

    def create_issue(
        self, title: str, body: str, labels: list[str] | None = None,
    ) -> int | None:
        number = super().create_issue(title, body, labels)
        if number is not None:
            self.issues.append({
                "number": number, "title": title, "body": body,
                "labels": list(labels or []), "state": "open",
                "createdAt": "2026-10-01T00:00:00Z",
            })
        return number


_OPEN_STORES: list[GapStore] = []


@pytest.fixture(autouse=True)
def _close_stores() -> Iterator[None]:
    yield
    while _OPEN_STORES:
        _OPEN_STORES.pop().close()


def _store() -> GapStore:
    config = MagicMock()
    config.memory_dir = Path("/fake/mem")
    store = build_gap_store(config, connection=sqlite3.connect(":memory:"))
    _OPEN_STORES.append(store)
    return store


def _rig() -> tuple[_Host, IssueTracker, GapStore, int]:
    host = _Host()
    tracker = IssueTracker("o/r", code_host=host)
    store = _store()
    result = open_gap_issue(
        OWNER, "detector", "agent.py is over budget", "scan:1",
        untrusted_input=False, tracker=tracker, store=store,
        operator_logins=OPS, now=NOW,
    )
    assert result.issue_number is not None
    return host, tracker, store, result.issue_number


def _journal(events: list[str] | None = None) -> MagicMock:
    journal = MagicMock()
    journal.read_events_by_type.return_value = [
        MagicMock(content=c, timestamp=NOW.isoformat()) for c in (events or [])
    ]
    return journal


def _plan(plan_id: str, issue: int, created: str = "2026-10-02T00:00:00+00:00") -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id, title=plan_id, category="maintenance", rationale="r",
        proposed_changes=["c"], files_affected=[], expected_impact="e",
        status="rejected", issue_number=issue, created_at=created,
    )


def _select(
    tracker: IssueTracker, store: GapStore, plans: list[EvolutionPlan],
    journal: MagicMock | None = None,
) -> FuelSelection:
    return select_evolution_fuel(
        NOW, tracker=tracker, store=store, operator_logins=OPS,
        journal=journal or _journal(), plans=plans, max_attempts=2,
        own_login="maestro-bot",
    )


def _labels(host: _Host, number: int) -> list[str]:
    labels: list[str] = next(i for i in host.issues if i["number"] == number)["labels"]
    return labels


def test_trip_labels_comments_once_and_selects_nothing(mem_fs: dict[str, str]) -> None:
    host, tracker, store, n = _rig()
    plans = [_plan("a", n), _plan("b", n)]
    journal = _journal()
    selection = _select(tracker, store, plans, journal)
    assert selection.selected is None
    assert [(t.issue_number, t.attempts) for t in selection.tripped] == [(n, 2)]
    assert NEEDS_DECISION_LABEL in _labels(host, n)
    assert len(host.reply_calls) == 1
    assert "needs-decision" in host.reply_calls[0]["body"]
    assert journal.record_event.call_args.args[0].startswith(f"gap-tripwire #{n}:")
    # The next selection skips it on the label: no second write or ask.
    again = _select(tracker, store, plans)
    assert again.skipped == ((n, "needs-decision"),)
    assert again.tripped == ()
    assert host.label_edits == [("add", n, NEEDS_DECISION_LABEL)]
    assert len(host.reply_calls) == 1


def test_failed_label_write_falls_back_to_exhausted_and_retries(mem_fs: dict[str, str]) -> None:
    host, tracker, store, n = _rig()
    host.label_edit_fail = True
    plans = [_plan("a", n), _plan("b", n)]
    journal = _journal()
    selection = _select(tracker, store, plans, journal)
    assert selection.tripped == ()
    assert selection.skipped == ((n, "exhausted"),)
    assert host.reply_calls == []
    assert journal.record_event.call_args.args[0].startswith(f"gap-exhausted #{n}:")
    host.label_edit_fail = False
    retried = _select(tracker, store, plans)
    assert [t.issue_number for t in retried.tripped] == [n]


def test_build_resets_the_count_and_two_new_attempts_retrip(mem_fs: dict[str, str]) -> None:
    host, tracker, store, n = _rig()
    plans = [_plan("a", n), _plan("b", n)]
    _select(tracker, store, plans)
    host.label_removed_stamp = "2026-10-10T00:00:00Z"
    journal = _journal()
    assert apply_gap_decision(tracker, journal, n, "build") is True
    assert NEEDS_DECISION_LABEL not in _labels(host, n)
    built = journal.record_event.call_args.args[0]
    assert "build" in built
    plans.append(_plan("c", n, "2026-10-14T13:00:00+00:00"))
    fuel = _select(tracker, store, plans, _journal([built]))
    assert fuel.selected is not None
    assert fuel.selected.issue.number == n
    plans.append(_plan("d", n, "2026-10-14T14:00:00+00:00"))
    retrip = _select(tracker, store, plans, _journal([built]))
    assert [(t.issue_number, t.attempts) for t in retrip.tripped] == [(n, 2)]


def test_unparseable_reset_stamp_trips(mem_fs: dict[str, str]) -> None:
    host, tracker, store, n = _rig()
    host.issue_label_removed_at[(n, NEEDS_DECISION_LABEL)] = LabelRemoval("yesterday", "maestro-bot")
    selection = _select(tracker, store, [_plan("a", n), _plan("b", n)])
    assert [t.issue_number for t in selection.tripped] == [n]


def test_naive_reset_stamp_is_utc(mem_fs: dict[str, str]) -> None:
    host, tracker, store, n = _rig()
    host.issue_label_removed_at[(n, NEEDS_DECISION_LABEL)] = LabelRemoval(
        "2026-10-10T00:00:00", "Thomas",
    )
    selection = _select(tracker, store, [_plan("a", n), _plan("b", n)])
    assert selection.selected is not None


def test_build_label_removal_failure_is_reported(mem_fs: dict[str, str]) -> None:
    host, tracker, _, n = _rig()
    host.label_edit_fail = True
    journal = _journal()
    assert apply_gap_decision(tracker, journal, n, "build") is False
    assert "removal failed" in journal.record_event.call_args.args[0]


def test_buy_keeps_the_label_and_the_closer_still_closes(mem_fs: dict[str, str]) -> None:
    host, tracker, store, n = _rig()
    _select(tracker, store, [_plan("a", n), _plan("b", n)])
    journal = _journal()
    assert apply_gap_decision(tracker, journal, n, "buy") is True
    assert NEEDS_DECISION_LABEL in _labels(host, n)
    assert "buy" in journal.record_event.call_args.args[0]
    assert close_gap_issue(OWNER, "bought", tracker=tracker, store=store, now=NOW) == n


def test_drop_makes_no_host_write(mem_fs: dict[str, str]) -> None:
    host, tracker, store, n = _rig()
    _select(tracker, store, [_plan("a", n), _plan("b", n)])
    edits, comments, closed = list(host.label_edits), len(host.reply_calls), list(host.closed_issues)
    journal = _journal()
    assert apply_gap_decision(tracker, journal, n, "drop") is True
    assert host.label_edits == edits
    assert len(host.reply_calls) == comments
    assert host.closed_issues == closed
    assert "operator closes" in journal.record_event.call_args.args[0]


@pytest.mark.parametrize(
    ("actor", "outcome", "reason"),
    [("maestro-bot", "reopened", ""), ("thomas", "refused", "operator-abandoned")],
)
def test_only_an_operator_close_drops(
    mem_fs: dict[str, str], actor: str, outcome: str, reason: str,
) -> None:
    host, tracker, store, n = _rig()
    host.issues[0]["state"] = "closed"
    host.issue_closures[n] = IssueClosure(actor, via_commit=False)
    result = open_gap_issue(
        OWNER, "detector", "agent.py is still over budget", "scan:2",
        untrusted_input=False, tracker=tracker, store=store,
        operator_logins=OPS, now=NOW,
    )
    assert (result.outcome, result.reason) == (outcome, reason)


def test_unverified_gap_is_never_tripped(mem_fs: dict[str, str]) -> None:
    host = _Host(issues=[{
        "number": 5, "title": "forged", "body": "", "labels": ["gap:maestro"],
        "state": "open", "createdAt": "2026-10-01T00:00:00Z",
    }])
    tracker = IssueTracker("o/r", code_host=host)
    selection = _select(tracker, _store(), [_plan("a", 5), _plan("b", 5)])
    assert selection.tripped == ()
    assert host.label_edits == []


def test_tripwire_runs_no_subprocess(mem_fs: dict[str, str]) -> None:
    _host, tracker, store, n = _rig()
    with patch("subprocess.run", side_effect=AssertionError("subprocess")) as run:
        selection = _select(tracker, store, [_plan("a", n), _plan("b", n)])
    assert [t.issue_number for t in selection.tripped] == [n]
    run.assert_not_called()


# --- the rendered task and its ask --------------------------------------------------


def test_ask_lists_reply_words_and_drop_only_as_a_link() -> None:
    url = gap_issue_url("o/r", 12)
    text = format_gap_decision_ask(12, url)
    lines = text.splitlines()
    assert lines[0] == "[gap-decision #12] Decide gap issue 12"
    assert "spent its plan attempts" in lines[1]
    assert "  build — clear needs-decision and plan this gap differently" in lines
    assert "  buy — keep the gap parked while it is bought" in lines
    assert not any(line.startswith("  drop") for line in lines)
    assert lines[-1] == f"To drop it, close the issue yourself: {url}"


def test_parsers_round_trip() -> None:
    assert parse_gap_decision_question(format_gap_decision_ask(42, "u")) == 42
    assert parse_gap_decision_question("plain question") is None
    assert parse_gap_decision_question("[gap-decision #0] x") is None
    assert parse_gap_decision_reply("Build!") == "build"
    assert parse_gap_decision_reply("  buy it") == "buy"
    assert parse_gap_decision_reply("drop") == "drop"
    assert parse_gap_decision_reply("maybe later") is None
    assert parse_gap_decision_reply("42") is None


def test_untrusted_label_removal_buys_no_attempts(mem_fs: dict[str, str]) -> None:
    host, tracker, store, n = _rig()
    plans = [_plan("a", n), _plan("b", n)]
    _select(tracker, store, plans)
    host.label_removed_stamp = "2026-10-10T00:00:00Z"
    host.label_editor_login = "drive-by"
    assert tracker.remove_label(n, NEEDS_DECISION_LABEL) is True
    retrip = _select(tracker, store, plans)
    assert [(t.issue_number, t.attempts) for t in retrip.tripped] == [(n, 2)]
    assert NEEDS_DECISION_LABEL in _labels(host, n)


def test_closed_then_reopened_gap_is_fuel_again_and_asks_again(mem_fs: dict[str, str]) -> None:
    host, tracker, store, n = _rig()
    plans = [_plan("a", n), _plan("b", n)]
    _select(tracker, store, plans)
    assert apply_gap_decision(tracker, _journal(), n, "buy") is True
    host.label_removed_stamp = "2026-10-10T00:00:00Z"
    assert close_gap_issue(OWNER, "green", tracker=tracker, store=store, now=NOW) == n
    assert NEEDS_DECISION_LABEL not in _labels(host, n)
    host.issues[0]["state"] = "closed"
    host.issue_closures[n] = IssueClosure("maestro-bot", via_commit=False)
    reopened = open_gap_issue(
        OWNER, "detector", "agent.py is over budget again", "scan:2",
        untrusted_input=False, tracker=tracker, store=store,
        operator_logins=OPS, now=NOW,
    )
    assert reopened.outcome == "reopened"
    host.issues[0]["state"] = "open"
    fuel = _select(tracker, store, plans)
    assert fuel.selected is not None
    assert fuel.selected.issue.number == n
    plans += [
        _plan("c", n, "2026-10-14T13:00:00+00:00"),
        _plan("d", n, "2026-10-14T14:00:00+00:00"),
    ]
    asks_again = _select(tracker, store, plans)
    assert [(t.issue_number, t.attempts) for t in asks_again.tripped] == [(n, 2)]


def test_gap_decision_note_caps_and_strips() -> None:
    assert gap_decision_note("build") == ""
    assert gap_decision_note("build: try X.") == "try X"
    assert gap_decision_note("build " + "y" * 400).endswith("[truncated: 100 chars]")


def _own_removal(host: _Host, tracker: IssueTracker, n: int) -> None:
    host.label_removed_stamp = "2026-10-10T00:00:00Z"
    host.label_editor_login = "maestro-bot"
    assert tracker.remove_label(n, NEEDS_DECISION_LABEL) is True


def test_uncorroborated_own_login_removal_is_relabelled_without_a_new_ask(
    mem_fs: dict[str, str],
) -> None:
    host, tracker, store, n = _rig()
    plans = [_plan("a", n), _plan("b", n)]
    first = _journal()
    _select(tracker, store, plans, first)
    tripped_event = first.record_event.call_args.args[0]
    _own_removal(host, tracker, n)
    comments = len(host.reply_calls)
    again = _select(tracker, store, plans, _journal([tripped_event]))
    assert again.tripped == ()
    assert (n, "needs-decision") in again.skipped
    assert NEEDS_DECISION_LABEL in _labels(host, n)
    assert len(host.reply_calls) == comments


def test_threaded_build_reply_by_own_login_corroborates_the_removal(
    mem_fs: dict[str, str],
) -> None:
    host, tracker, store, n = _rig()
    plans = [_plan("a", n), _plan("b", n)]
    _select(tracker, store, plans)
    host.label_removed_stamp = "2026-10-10T00:00:00Z"
    host.label_editor_login = "maestro-bot"
    journal = _journal()
    assert apply_gap_decision(tracker, journal, n, "build", reply_id="abc123") is True
    built = journal.record_event.call_args.args[0]
    assert "(reply abc123)" in built
    fuel = _select(tracker, store, plans, _journal([built]))
    assert (n, "needs-decision") not in fuel.skipped
    assert NEEDS_DECISION_LABEL not in _labels(host, n)


def test_is_build_removal_row_reads_only_landed_builds() -> None:
    from maestro.evolution_fuel import is_build_removal_row

    ok = f"gap-decision #7: build — {NEEDS_DECISION_LABEL} removed; the attempt count restarts"
    assert is_build_removal_row(ok, 7) is True
    stamped = ok.replace("build —", "build (reply r1) —")
    assert is_build_removal_row(stamped, 7) is True
    assert is_build_removal_row(ok, 70) is False
    assert is_build_removal_row(ok.replace("build", "buildx"), 7) is False
    failed = (
        f"gap-decision #7: build — {NEEDS_DECISION_LABEL} removal failed; Thomas's note: "
        f"— {NEEDS_DECISION_LABEL} removed; the attempt count restarts"
    )
    assert is_build_removal_row(failed, 7) is False


def test_uncorroborated_own_login_removal_trips_when_never_asked(mem_fs: dict[str, str]) -> None:
    host, tracker, store, n = _rig()
    _own_removal(host, tracker, n)
    selection = _select(tracker, store, [_plan("a", n), _plan("b", n)])
    assert [t.issue_number for t in selection.tripped] == [n]


def test_build_record_older_than_the_removal_does_not_corroborate(
    mem_fs: dict[str, str],
) -> None:
    host, tracker, store, n = _rig()
    _own_removal(host, tracker, n)
    stale = MagicMock(
        content=f"gap-decision #{n}: build — {NEEDS_DECISION_LABEL} removed; x",
        timestamp="2026-10-01T00:00:00+00:00",
    )
    journal = _journal()
    journal.read_events_by_type.return_value = [stale]
    selection = _select(tracker, store, [_plan("a", n), _plan("b", n)], journal)
    assert [t.issue_number for t in selection.tripped] == [n]


def test_build_record_with_unparseable_stamp_does_not_corroborate(
    mem_fs: dict[str, str],
) -> None:
    host, tracker, store, n = _rig()
    _own_removal(host, tracker, n)
    bad = MagicMock(
        content=f"gap-decision #{n}: build — {NEEDS_DECISION_LABEL} removed",
        timestamp="whenever",
    )
    journal = _journal()
    journal.read_events_by_type.return_value = [bad]
    selection = _select(tracker, store, [_plan("a", n), _plan("b", n)], journal)
    assert [t.issue_number for t in selection.tripped] == [n]


def test_failed_trip_comment_is_journalled(mem_fs: dict[str, str]) -> None:
    _, tracker, store, n = _rig()
    journal = _journal()
    with patch.object(tracker, "comment", return_value=False):
        selection = _select(tracker, store, [_plan("a", n), _plan("b", n)], journal)
    assert [t.issue_number for t in selection.tripped] == [n]
    events = [c.args[0] for c in journal.record_event.call_args_list]
    assert events[0].startswith(f"gap-tripwire-comment-failed #{n}:")
    assert events[1].startswith(f"gap-tripwire #{n}:")


def test_trip_comment_mentions_nobody_without_an_operator_login(mem_fs: dict[str, str]) -> None:
    host, tracker, store, n = _rig()
    selection = select_evolution_fuel(
        NOW, tracker=tracker, store=store, operator_logins=(),
        journal=_journal(), plans=[_plan("a", n), _plan("b", n)], max_attempts=2,
        own_login="maestro-bot",
    )
    assert [t.issue_number for t in selection.tripped] == [n]
    assert "@" not in host.reply_calls[0]["body"]
    assert "The operator:" in host.reply_calls[0]["body"]


def test_trip_comment_mentions_the_operator_deterministically(mem_fs: dict[str, str]) -> None:
    host, tracker, store, n = _rig()
    select_evolution_fuel(
        NOW, tracker=tracker, store=store, operator_logins=frozenset({"zed", "Thomas", ""}),
        journal=_journal(), plans=[_plan("a", n), _plan("b", n)], max_attempts=2,
        own_login="maestro-bot",
    )
    assert "@Thomas:" in host.reply_calls[0]["body"]


def test_past_episode_trip_does_not_silence_a_reopened_gaps_new_ask(
    mem_fs: dict[str, str],
) -> None:
    host, tracker, store, n = _rig()
    host.issues[0]["state"] = "closed"
    host.issue_closures[n] = IssueClosure("maestro-bot", via_commit=False)
    reopened = open_gap_issue(
        OWNER, "detector", "over budget again", "scan:2",
        untrusted_input=False, tracker=tracker, store=store,
        operator_logins=OPS, now=NOW,
    )
    assert reopened.outcome == "reopened"
    host.issues[0]["state"] = "open"
    old_trip = MagicMock(
        content=f"gap-tripwire #{n}: 2 plan attempt(s) spent; x",
        timestamp="2026-10-05T00:00:00+00:00",
    )
    journal = _journal()
    journal.read_events_by_type.return_value = [old_trip]
    plans = [
        _plan("c", n, "2026-10-14T13:00:00+00:00"),
        _plan("d", n, "2026-10-14T14:00:00+00:00"),
    ]
    _own_removal(host, tracker, n)
    selection = _select(tracker, store, plans, journal)
    assert [t.issue_number for t in selection.tripped] == [n]


def test_capped_gap_is_deferred_while_own_login_is_unresolved(
    mem_fs: dict[str, str],
) -> None:
    host, tracker, store, n = _rig()
    selection = select_evolution_fuel(
        NOW, tracker=tracker, store=store, operator_logins=OPS,
        journal=_journal(), plans=[_plan("a", n), _plan("b", n)], max_attempts=2,
        own_login="",
    )
    assert selection.tripped == ()
    assert (n, "owner-unresolved") in selection.skipped
    assert NEEDS_DECISION_LABEL not in _labels(host, n)


def test_capped_gap_is_deferred_when_label_events_are_unreadable(
    mem_fs: dict[str, str],
) -> None:
    host, tracker, store, n = _rig()
    host.issue_label_removed_at[(n, NEEDS_DECISION_LABEL)] = LabelRemoval("", readable=False)
    journal = _journal([f"gap-tripwire #{n}: 2 plan attempt(s) spent; x"])
    selection = _select(tracker, store, [_plan("a", n), _plan("b", n)], journal)
    assert selection.tripped == ()
    assert (n, "reset-unread") in selection.skipped
    assert NEEDS_DECISION_LABEL not in _labels(host, n)
