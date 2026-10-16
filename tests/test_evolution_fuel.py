"""Proposal 155 step 1 — gap issues and the evolution-fuel owner."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from maestro.config import Config
from maestro.evolution_fuel import (
    GAP_LABELS,
    GAP_PREFIXES,
    MAX_EVIDENCE_CHARS,
    GapOpener,
    admit_gap_issues,
    classify_gap_trust,
    close_gap_issue,
    effective_operator_logins,
    gap_title,
    is_gap_label,
    open_gap_issue,
    operator_login_refusal,
    owner_id_of_title,
    parse_owner_id,
)
from maestro.gap_store import GapStore, build_gap_store
from maestro.issues import Issue, IssueTracker
from maestro.taint import FENCE_OPEN, mark_untrusted
from maestro.vcs import IssueClosure
from tests.component.mock_vcs import MockCodeHost

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
OPS = ("Thomas",)


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
                "createdAt": f"2026-10-{number:02d}T00:00:00Z",
            })
        return number

    def list_issues(self, **kwargs: Any) -> list[dict[str, Any]] | None:
        """gh's default ``--limit`` of 30 applies when no page size is given."""
        listed = super().list_issues(**kwargs)
        if listed is None or kwargs.get("page_size") is not None:
            return listed
        return sorted(listed, key=lambda i: -int(i["number"]))[:30]


def _store() -> GapStore:
    config = MagicMock()
    config.memory_dir = Path("/fake/mem")
    return build_gap_store(config, connection=sqlite3.connect(":memory:"))


def _rig(**host_kwargs: Any) -> tuple[_Host, IssueTracker, GapStore]:
    host = _Host(**host_kwargs)
    return host, IssueTracker("o/r", code_host=host), _store()


def _rows(store: GapStore) -> list[sqlite3.Row]:
    return list(store.connection.execute("SELECT * FROM gap_opens ORDER BY id"))


def _open(
    tracker: IssueTracker, store: GapStore, owner_id: str = "maestro:gate",
    opener: GapOpener = "detector", evidence: Any = "main is red",
    *, untrusted: bool = False, decision_ref: str = "run:1",
) -> Any:
    return open_gap_issue(
        owner_id, opener, evidence, decision_ref, untrusted_input=untrusted,
        tracker=tracker, store=store, operator_logins=OPS, now=NOW,
    )


# -- vocabulary --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "prefix"),
    [
        ("maestro:gate", "gate"),
        ("maestro:source_budget:maestro/memory.py", "source_budget"),
        ("maestro:toolchain:node20", "toolchain"),
        ("capability:act_kind:publish", "capability"),
        ("capability:completion_check:ci", "capability"),
    ],
)
def test_parse_owner_id_accepts_each_form(text: str, prefix: str) -> None:
    owner = parse_owner_id(text)
    assert owner is not None
    assert owner.prefix == prefix
    assert owner_id_of_title(gap_title(owner)) == text


@pytest.mark.parametrize(
    "text",
    [
        "please fix everything",
        "maestro:gate ",
        "maestro:source_budget:../etc/passwd.py",
        "maestro:source_budget:maestro/x.txt",
        "capability:feature:x",
        "phase:launch",
        "milestone:first/launch",
        "capability:income_provider:stripe",
        "x" * 300,
        7,
    ],
)
def test_parse_owner_id_rejects(text: object) -> None:
    assert parse_owner_id(text) is None


def test_each_prefix_maps_to_one_label() -> None:
    assert {label for label, _ in GAP_PREFIXES.values()} == set(GAP_LABELS)
    assert all(len(openers) == 1 for _, openers in GAP_PREFIXES.values())


def test_title_round_trip_refuses_foreign_text() -> None:
    assert owner_id_of_title("maestro:gate") is None
    assert owner_id_of_title("maestro:gate — anything else") is None
    assert owner_id_of_title("bogus — the main quality gate is red") is None


def test_is_gap_label() -> None:
    assert is_gap_label("gap:maestro") is True
    assert is_gap_label(" GAP:x") is True
    assert is_gap_label("bug") is False
    assert is_gap_label(None) is False


# -- open_gap_issue ------------------------------------------------------------


def test_open_twice_reuses(mem_fs: dict[str, str]) -> None:
    """A1: second open reuses the open issue, no create, no new row."""
    host, tracker, store = _rig()
    first = _open(tracker, store)
    second = _open(tracker, store)
    assert (first.outcome, second.outcome) == ("opened", "reused")
    assert first.issue_number == second.issue_number
    assert len(host.created_issues) == 1
    assert host.created_issues[0]["labels"] == ["gap:maestro"]
    assert host.created_issues[0]["title"] == "maestro:gate — the main quality gate is red"
    assert len(_rows(store)) == 1
    assert _rows(store)[0]["untrusted_input"] == 0


_GATE_TITLE = "maestro:gate — the main quality gate is red"


def _seed_row(store: GapStore, number: int, *, closed: bool = True) -> None:
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO gap_opens (owner_id, label, opener, issue_number, decision_ref, "
            "untrusted_input, opened_at, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("maestro:gate", "gap:maestro", "detector", number, "run:0", 0,
             "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z" if closed else None),
        )


def _closed_host(closure: IssueClosure | None) -> tuple[_Host, IssueTracker, GapStore]:
    issues = [
        {"number": 4, "title": _GATE_TITLE, "labels": ["gap:maestro"], "state": "closed",
         "createdAt": "2026-01-01"},
        {"number": 9, "title": _GATE_TITLE, "labels": ["gap:maestro"], "state": "closed",
         "createdAt": "2026-02-01"},
    ]
    closures = {} if closure is None else {
        4: IssueClosure("maestro-bot", via_commit=False), 9: closure,
    }
    host, tracker, store = _rig(issues=issues, issue_closures=closures)
    _seed_row(store, 4)
    _seed_row(store, 9)
    return host, tracker, store


def test_forged_closed_twin_is_never_reopened(mem_fs: dict[str, str]) -> None:
    """A2/A3: a newer bot-closed twin with no row is ignored; the door's own
    operator-closed issue still reads as abandoned."""
    issues = [
        {"number": 4, "title": _GATE_TITLE, "labels": ["gap:maestro"], "state": "closed",
         "createdAt": "2026-01-01"},
        {"number": 30, "title": _GATE_TITLE, "labels": ["gap:maestro"], "state": "closed",
         "createdAt": "2026-03-01"},
    ]
    closures = {
        4: IssueClosure("thomas", via_commit=False),
        30: IssueClosure("maestro-bot", via_commit=False),
    }
    host, tracker, store = _rig(issues=issues, issue_closures=closures)
    _seed_row(store, 4)
    result = _open(tracker, store)
    assert (result.outcome, result.reason, result.issue_number) == (
        "refused", "operator-abandoned", 4,
    )
    assert host.reopened_issues == []


def test_forged_closed_twin_alone_opens_fresh(mem_fs: dict[str, str]) -> None:
    issues = [{"number": 30, "title": _GATE_TITLE, "labels": ["gap:maestro"],
               "state": "closed", "createdAt": "2026-03-01"}]
    host, tracker, store = _rig(
        issues=issues, issue_closures={30: IssueClosure("maestro-bot", via_commit=False)},
    )
    result = _open(tracker, store)
    assert result.outcome == "opened"
    assert result.issue_number != 30
    assert host.reopened_issues == []
    assert [r["issue_number"] for r in _rows(store)] == [result.issue_number]


def _forged_open_twin() -> dict[str, Any]:
    return {"number": 50, "title": _GATE_TITLE, "labels": ["gap:maestro"],
            "state": "open", "createdAt": "2026-09-01"}


def test_forged_open_twin_is_not_reused(mem_fs: dict[str, str]) -> None:
    _, tracker, store = _rig(issues=[_forged_open_twin()])
    first = _open(tracker, store)
    assert first.outcome == "opened"
    assert first.issue_number != 50
    second = _open(tracker, store)
    assert (second.outcome, second.issue_number) == ("reused", first.issue_number)


def test_close_skips_forged_open_twin(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig(issues=[_forged_open_twin()])
    assert close_gap_issue(
        "maestro:gate", "r", tracker=tracker, store=store, now=NOW,
    ) is None
    opened = _open(tracker, store)
    assert close_gap_issue(
        "maestro:gate", "r", tracker=tracker, store=store, now=NOW,
    ) == opened.issue_number
    assert host.closed_issues == [opened.issue_number]
    assert _rows(store)[0]["close_ref"] == "r"


def test_evidence_too_long_refused(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    result = _open(tracker, store, evidence="x" * (MAX_EVIDENCE_CHARS + 1))
    assert (result.outcome, result.reason) == ("refused", "evidence-too-long")
    assert host.created_issues == []


def test_operator_hand_close_is_abandoned(mem_fs: dict[str, str]) -> None:
    """A3: an operator close (case-insensitive) is never reopened."""
    host, tracker, store = _closed_host(IssueClosure("thomas", via_commit=False))
    result = _open(tracker, store)
    assert (result.outcome, result.reason, result.issue_number) == (
        "refused", "operator-abandoned", 9,
    )
    assert host.reopened_issues == []
    assert host.created_issues == []
    assert len(_rows(store)) == 2


@pytest.mark.parametrize(
    "closure",
    [IssueClosure("thomas", via_commit=True), IssueClosure("maestro-bot", via_commit=False)],
)
def test_commit_or_bot_close_reopens(mem_fs: dict[str, str], closure: IssueClosure) -> None:
    host, tracker, store = _closed_host(closure)
    result = _open(tracker, store)
    assert (result.outcome, result.issue_number) == ("reopened", 9)
    assert host.reopened_issues == [9]
    assert [r["issue_number"] for r in _rows(store)] == [4, 9, 9, 9]
    assert [r["decision_ref"] for r in _rows(store)][-2:] == ["reopen:run:1", "run:1"]


def test_reopen_clears_a_left_over_needs_decision_label(mem_fs: dict[str, str]) -> None:
    """A label a failed or non-door close left behind is cleared on reopen."""
    host, tracker, store = _closed_host(IssueClosure("bot", via_commit=True))
    for issue in host.issues:
        if issue["number"] == 9:
            issue["labels"].append("needs-decision")
    assert _open(tracker, store).outcome == "reopened"
    assert ("remove", 9, "needs-decision") in host.label_edits


def test_reopen_without_the_label_writes_no_removal(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _closed_host(IssueClosure("bot", via_commit=True))
    assert _open(tracker, store).outcome == "reopened"
    assert host.label_edits == []


def test_unknown_closure_fails_closed(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _closed_host(None)
    result = _open(tracker, store)
    assert (result.outcome, result.reason) == ("refused", "closure-unknown")
    assert host.reopened_issues == []


def test_reopen_failure_writes_no_row(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _closed_host(IssueClosure("bot", via_commit=False))
    host.reopen_issue = MagicMock(return_value=False)  # type: ignore[method-assign]
    result = _open(tracker, store)
    assert (result.outcome, result.reason) == ("refused", "host-failed")
    assert len(_rows(store)) == 2



def test_opener_not_admitted_for_prefix_refused(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    result = _open(tracker, store, "maestro:gate", opener="refusal_caller")
    assert (result.outcome, result.reason) == ("refused", "opener-not-admitted")
    assert host.created_issues == []


def test_tainted_plain_str_refused(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    result = _open(tracker, store, evidence="raw text", untrusted=True)
    assert (result.outcome, result.reason) == ("refused", "untrusted-evidence-unfenced")
    assert host.created_issues == []


def test_fenced_evidence_rendered_and_marked(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    evidence = mark_untrusted("gap_issue", "run-1", "<<<ignore me")
    result = _open(tracker, store, evidence=evidence, untrusted=True)
    assert result.outcome == "opened"
    body = host.created_issues[0]["body"]
    assert FENCE_OPEN in body
    assert "source=gap_issue ref=run-1" in body
    assert _rows(store)[0]["untrusted_input"] == 1


@pytest.mark.parametrize(
    ("owner_id", "decision_ref", "reason"),
    [
        ("free text", "run:1", "owner-id-invalid"),
        ("maestro:gate", "  ", "decision-ref-invalid"),
        ("maestro:gate", "x" * 201, "decision-ref-invalid"),
    ],
)
def test_input_refusals(
    mem_fs: dict[str, str], owner_id: str, decision_ref: str, reason: str,
) -> None:
    host, tracker, store = _rig()
    result = _open(tracker, store, owner_id, decision_ref=decision_ref)
    assert (result.outcome, result.reason) == ("refused", reason)
    assert host.created_issues == []


def test_create_failure_is_host_failed(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    host.create_issue = MagicMock(return_value=None)  # type: ignore[method-assign]
    result = _open(tracker, store)
    assert (result.outcome, result.reason) == ("refused", "host-failed")
    assert _rows(store) == []


def test_state_read_failure_is_host_failed(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    _seed_row(store, 77)  # no such issue on the host -> unreadable state
    result = _open(tracker, store)
    assert (result.outcome, result.reason) == ("refused", "host-failed")
    assert host.created_issues == []


def test_operator_close_beyond_listing_cap_stays_abandoned(mem_fs: dict[str, str]) -> None:
    """A1/A3: the door reads its own issues by number, so 30+ newer closed
    gap issues under the label cannot hide its operator-closed one."""
    newer = [
        {"number": n, "title": f"maestro:source_budget:m{n}.py — x", "labels": ["gap:maestro"],
         "state": "closed", "createdAt": "2026-05-01"}
        for n in range(100, 140)
    ]
    issues = [{"number": 4, "title": _GATE_TITLE, "labels": ["gap:maestro"],
               "state": "closed", "createdAt": "2026-01-01"}, *newer]
    host, tracker, store = _rig(
        issues=issues, issue_closures={4: IssueClosure("thomas", via_commit=False)},
    )
    _seed_row(store, 4)
    assert all(i["number"] != 4 for i in host.list_issues(state="closed") or [])
    result = _open(tracker, store)
    assert (result.outcome, result.reason, result.issue_number) == (
        "refused", "operator-abandoned", 4,
    )
    assert host.created_issues == []


def test_operator_reopen_is_adopted_not_duplicated(mem_fs: dict[str, str]) -> None:
    """A1: a door issue an operator reopened is adopted, then closed by the door."""
    host, tracker, store = _rig()
    opened = _open(tracker, store)
    assert close_gap_issue("maestro:gate", "pr:1", tracker=tracker, store=store, now=NOW)
    host.issues[0]["state"] = "open"  # Thomas reopens by hand
    again = _open(tracker, store)
    assert (again.outcome, again.issue_number) == ("reopened", opened.issue_number)
    assert len(host.created_issues) == 1
    assert [r["closed_at"] is None for r in _rows(store)] == [False, True, True]
    assert _open(tracker, store).outcome == "reused"
    assert close_gap_issue("maestro:gate", "pr:2", tracker=tracker, store=store, now=NOW)
    assert host.closed_issues == [opened.issue_number, opened.issue_number]


@pytest.mark.parametrize(
    "edit",
    [{"labels": []}, {"title": "nothing to see here"}],
)
def test_tampered_door_issue_is_refused_not_reused(
    mem_fs: dict[str, str], edit: dict[str, Any],
) -> None:
    """A bot or operator editing away the door issue's label or title cannot
    leave the door silently reporting ``reused``."""
    host, tracker, store = _rig()
    opened = _open(tracker, store)
    host.issues[0].update(edit)
    result = _open(tracker, store)
    assert (result.outcome, result.reason, result.issue_number) == (
        "refused", "gap-tampered", opened.issue_number,
    )
    assert len(host.created_issues) == 1


def test_gone_door_issue_is_skipped(mem_fs: dict[str, str]) -> None:
    """A deleted or transferred door issue neither blocks the owner id nor
    reads as a transient ``host-failed``."""
    host, tracker, store = _rig()
    first = _open(tracker, store)
    host.issues.clear()
    host.gone_issues.add(first.issue_number)
    assert not close_gap_issue("maestro:gate", "pr:1", tracker=tracker, store=store, now=NOW)
    again = _open(tracker, store)
    assert again.outcome == "opened"
    assert again.issue_number != first.issue_number


# -- close_gap_issue -----------------------------------------------------------


def test_close_stamps_and_stops_rederiving(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    opened = _open(tracker, store)
    issue = tracker.list_open()[0]
    assert close_gap_issue(
        "maestro:gate", "pr:12", tracker=tracker, store=store, now=NOW,
    ) == opened.issue_number
    row = _rows(store)[0]
    assert row["close_ref"] == "pr:12"
    assert row["closed_at"] is not None
    assert host.closed_issues == [opened.issue_number]
    assert classify_gap_trust(
        issue, tracker=tracker, store=store, operator_logins=OPS,
    ) == "unverified"
    assert close_gap_issue(
        "maestro:gate", "pr:12", tracker=tracker, store=store, now=NOW,
    ) is None


def test_close_refusals(mem_fs: dict[str, str]) -> None:
    host, tracker, store = _rig()
    assert close_gap_issue("nope", "r", tracker=tracker, store=store, now=NOW) is None
    _open(tracker, store)
    host.close_issue = MagicMock(return_value=False)  # type: ignore[method-assign]
    assert close_gap_issue(
        "maestro:gate", "r", tracker=tracker, store=store, now=NOW,
    ) is None
    assert _rows(store)[0]["closed_at"] is None


# -- trust ---------------------------------------------------------------------


def _issue(number: int, title: str, labels: list[str], created: str = "2026") -> Issue:
    return Issue(number=number, title=title, labels=labels, body="", created_at=created)


def _insert(store: GapStore, owner_id: str, number: int, opener: str) -> None:
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO gap_opens (owner_id, label, opener, issue_number, "
            "decision_ref, untrusted_input, opened_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (owner_id, "gap:maestro", opener, number, "r", "t"),
        )


_GATE_TITLE = "maestro:gate — the main quality gate is red"


@pytest.mark.parametrize(
    "seed",
    [None, ("maestro:gate", 99, "detector"), ("maestro:gate", 5, "operating_review")],
)
def test_unverified_bot_label_records_one_event(
    mem_fs: dict[str, str], seed: tuple[str, int, str] | None,
) -> None:
    """A2: no row, a forged twin's row, or a non-admitted opener is unverified."""
    _, tracker, store = _rig(issue_label_actors={(5, "gap:maestro"): "maestro-bot"})
    if seed is not None:
        _insert(store, *seed)
    journal = MagicMock()
    issue = _issue(5, _GATE_TITLE, ["gap:maestro"])
    admitted = admit_gap_issues(
        [issue], tracker=tracker, store=store, operator_logins=OPS, journal=journal,
    )
    assert admitted == []
    journal.record_event.assert_called_once()
    assert journal.record_event.call_args.args[0].startswith("gap-unverified #5")
    assert journal.record_event.call_args.kwargs == {
        "entry_type": "audit_observation", "triggered_by": "evolution_fuel",
    }


def test_unverified_event_recorded_once_per_issue(mem_fs: dict[str, str]) -> None:
    _, tracker, store = _rig(issue_label_actors={(5, "gap:maestro"): "maestro-bot"})
    events: list[Any] = []
    journal = MagicMock()
    journal.record_event.side_effect = lambda content, **_: events.append(
        MagicMock(content=content),
    )
    journal.read_events_by_type.side_effect = lambda _t: [
        MagicMock(content="unrelated"), *events,
    ]
    issue = _issue(5, _GATE_TITLE, ["gap:maestro"])
    for _ in range(2):
        admit_gap_issues(
            [issue, issue], tracker=tracker, store=store, operator_logins=OPS,
            journal=journal,
        )
    assert journal.record_event.call_count == 1
    journal.read_events_by_type.assert_called_with("audit_observation")


def test_operator_label_trusted_without_key(mem_fs: dict[str, str]) -> None:
    _, tracker, store = _rig(issue_label_actors={(6, "gap:phase"): "THOMAS"})
    journal = MagicMock()
    labelled = _issue(6, "anything at all", ["gap:phase"], "2026-02")
    rederived_issue = _issue(7, _GATE_TITLE, ["gap:maestro"], "2026-01")
    _insert(store, "maestro:gate", 7, "detector")
    plain = _issue(8, "bug", ["bug"])
    admitted = admit_gap_issues(
        [labelled, plain, rederived_issue], tracker=tracker, store=store,
        operator_logins=OPS, journal=journal,
    )
    assert [i.number for i in admitted] == [7, 6]
    journal.record_event.assert_not_called()
    journal.read_events_by_type.assert_not_called()
    assert classify_gap_trust(
        plain, tracker=tracker, store=store, operator_logins=OPS,
    ) == "not_gap"


def test_title_label_mismatch_unverified(mem_fs: dict[str, str]) -> None:
    _, tracker, store = _rig()
    _insert(store, "maestro:gate", 3, "detector")
    issue = _issue(3, _GATE_TITLE, ["gap:phase"])
    assert classify_gap_trust(
        issue, tracker=tracker, store=store, operator_logins=OPS,
    ) == "unverified"


# -- D8 ------------------------------------------------------------------------


def test_operator_login_refusal() -> None:
    assert operator_login_refusal(["Thomas", "Maestro-Bot"], "maestro-bot") is not None
    assert operator_login_refusal(["Thomas"], "maestro-bot") is None
    assert operator_login_refusal(["Thomas"], "") is None


def test_effective_operator_logins_reexcludes_own() -> None:
    config = Config(evolution_fuel_operator_github_logins=["Thomas", "Bot"])
    assert effective_operator_logins(config, "bot") == frozenset({"thomas"})
    assert effective_operator_logins(config, "") == frozenset({"thomas", "bot"})


def test_reopen_refuses_bot_closed_issue_that_lost_its_label(
    mem_fs: dict[str, str],
) -> None:
    """A bot that strips the gap label then closes the door issue cannot get
    it reopened unlabelled (a silenced gap): the reopen path is tamper-checked."""
    host, tracker, store = _closed_host(IssueClosure("maestro-bot", via_commit=False))
    host.issues[1]["labels"] = []
    result = _open(tracker, store)
    assert (result.outcome, result.reason, result.issue_number) == (
        "refused", "gap-tampered", 9,
    )
    assert host.reopened_issues == []
    assert len(_rows(store)) == 2


def _hand_closed_then_reopened(reopened_by: str) -> tuple[_Host, IssueTracker, GapStore]:
    issues = [{"number": 4, "title": _GATE_TITLE, "labels": ["gap:maestro"],
               "state": "open", "createdAt": "2026-01-01"}]
    closure = IssueClosure("thomas", via_commit=False, reopened_by=reopened_by)
    host, tracker, store = _rig(issues=issues, issue_closures={4: closure})
    _seed_row(store, 4, closed=False)
    return host, tracker, store


def test_bot_reopen_of_operator_abandoned_issue_is_refused(
    mem_fs: dict[str, str],
) -> None:
    """A3: an operator hand close followed by a bot reopen stays abandoned."""
    host, tracker, store = _hand_closed_then_reopened("maestro-bot")
    result = _open(tracker, store)
    assert (result.outcome, result.reason, result.issue_number) == (
        "refused", "operator-abandoned", 4,
    )
    assert host.created_issues == []
    assert len(_rows(store)) == 1


def test_operator_reopen_of_abandoned_issue_resumes(mem_fs: dict[str, str]) -> None:
    _, tracker, store = _hand_closed_then_reopened("Thomas")
    result = _open(tracker, store)
    assert (result.outcome, result.issue_number) == ("reused", 4)


_BOT_CYCLE = IssueClosure(
    "maestro-bot", via_commit=False, reopened_by="maestro-bot",
    history=(
        ("closed", "thomas", False), ("reopened", "maestro-bot", False),
        ("closed", "maestro-bot", False), ("reopened", "maestro-bot", False),
    ),
)


def test_bot_close_reopen_cycle_cannot_launder_abandonment(
    mem_fs: dict[str, str],
) -> None:
    """A newer bot close does not hide an earlier operator hand close."""
    issues = [{"number": 4, "title": _GATE_TITLE, "labels": ["gap:maestro"],
               "state": "open", "createdAt": "2026-01-01"}]
    _, tracker, store = _rig(issues=issues, issue_closures={4: _BOT_CYCLE})
    _seed_row(store, 4, closed=False)
    result = _open(tracker, store)
    assert (result.outcome, result.reason) == ("refused", "operator-abandoned")
    assert classify_gap_trust(
        _issue(4, _GATE_TITLE, ["gap:maestro"]), tracker=tracker, store=store,
        operator_logins=OPS,
    ) == "unverified"


def test_bot_cycle_left_closed_is_not_reopened(mem_fs: dict[str, str]) -> None:
    closed = IssueClosure(
        "maestro-bot", via_commit=False,
        history=(
            ("closed", "thomas", False), ("reopened", "maestro-bot", False),
            ("closed", "maestro-bot", False),
        ),
    )
    issues = [{"number": 4, "title": _GATE_TITLE, "labels": ["gap:maestro"],
               "state": "closed", "createdAt": "2026-01-01"}]
    host, tracker, store = _rig(issues=issues, issue_closures={4: closed})
    _seed_row(store, 4, closed=False)
    result = _open(tracker, store)
    assert (result.outcome, result.reason) == ("refused", "operator-abandoned")
    assert host.reopened_issues == []


def test_operator_reopen_in_history_resumes(mem_fs: dict[str, str]) -> None:
    resumed = IssueClosure(
        "maestro-bot", via_commit=False, reopened_by="maestro-bot",
        history=(
            ("closed", "thomas", False), ("reopened", "Thomas", False),
            ("closed", "maestro-bot", False), ("reopened", "maestro-bot", False),
        ),
    )
    issues = [{"number": 4, "title": _GATE_TITLE, "labels": ["gap:maestro"],
               "state": "open", "createdAt": "2026-01-01"}]
    _, tracker, store = _rig(issues=issues, issue_closures={4: resumed})
    _seed_row(store, 4, closed=False)
    assert _open(tracker, store).outcome == "reused"


@pytest.mark.parametrize(
    "closure",
    [IssueClosure("thomas", via_commit=False, reopened_by="maestro-bot"), None],
)
def test_abandoned_or_unreadable_row_is_not_rederived(
    mem_fs: dict[str, str], closure: IssueClosure | None,
) -> None:
    """r18: a bot reopen of an operator-abandoned gap is not trusted fuel."""
    _, tracker, store = _rig(issue_closures={4: closure})
    _seed_row(store, 4, closed=False)
    journal = MagicMock()
    journal.read_events_by_type.return_value = []
    admitted = admit_gap_issues(
        [_issue(4, _GATE_TITLE, ["gap:maestro"])], tracker=tracker, store=store,
        operator_logins=OPS, journal=journal,
    )
    assert admitted == []
    journal.record_event.assert_called_once()


def test_open_issue_with_unreadable_closure_fails_closed(mem_fs: dict[str, str]) -> None:
    issues = [{"number": 4, "title": _GATE_TITLE, "labels": ["gap:maestro"],
               "state": "open", "createdAt": "2026-01-01"}]
    host, tracker, store = _rig(issues=issues, issue_closures={4: None})
    _seed_row(store, 4, closed=False)
    result = _open(tracker, store)
    assert (result.outcome, result.reason, result.issue_number) == (
        "refused", "closure-unknown", 4,
    )
    assert host.created_issues == []


def test_reopen_comment_failure_is_retried_on_next_tick(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed reopen evidence comment logs a WARNING and is posted on reuse."""
    host, tracker, store = _rig()
    opened = _open(tracker, store)
    assert close_gap_issue("maestro:gate", "pr:1", tracker=tracker, store=store, now=NOW)
    host.issues[0]["state"] = "open"
    host.comment_issue = MagicMock(return_value=False)  # type: ignore[method-assign]
    with caplog.at_level("WARNING", logger="maestro"):
        again = _open(tracker, store, decision_ref="run:2")
    assert (again.outcome, again.issue_number) == ("reopened", opened.issue_number)
    assert any("evidence comment failed" in r.getMessage() for r in caplog.records)
    assert "run:2" not in [r["decision_ref"] for r in _rows(store)]
    host.comment_issue = MagicMock(return_value=True)  # type: ignore[method-assign]
    assert _open(tracker, store, decision_ref="run:2").outcome == "reused"
    host.comment_issue.assert_called_once()
    assert [r["decision_ref"] for r in _rows(store)][-1] == "run:2"
