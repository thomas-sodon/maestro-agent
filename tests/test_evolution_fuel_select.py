"""Evolution-fuel selection and the capped gap block (proposal 155 step 4)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from maestro.audit_checks import DEFERRED_COOLDOWN_TAG
from maestro.evolution_fuel import (
    FUEL_PAGE_SIZE,
    GAP_CONTEXT_CAPS,
    GAP_LABELS,
    EvolutionFuel,
    GapContextInputs,
    _cap_list,
    _cap_text,
    admit_gap_issues,
    fuel_class_of,
    parse_owner_id,
    render_gap_block,
    select_evolution_fuel,
)
from maestro.issues import Issue
from maestro.memory import EvolutionPlan
from maestro.taint import FENCE_OPEN, UNTRUSTED_DATA_RULE

NOW = datetime(2026, 10, 14, tzinfo=UTC)
OPERATOR = "thomas"


def _issue(number: int, title: str, labels: list[str], created: str = "2026-10-01") -> Issue:
    return Issue(number=number, title=title, labels=labels, body=f"body {number}",
                 created_at=created)


def _gate(number: int = 1, created: str = "2026-10-05") -> Issue:
    return _issue(number, "maestro:gate — the main quality gate is red", ["gap:maestro"], created)


def _budget(number: int = 2, created: str = "2026-10-01") -> Issue:
    return _issue(
        number,
        "maestro:source_budget:maestro/agent.py — a source file is at its split-due size",
        ["gap:maestro"], created,
    )


def _capability(number: int = 3, created: str = "2026-10-09") -> Issue:
    return _issue(
        number, "capability:act_kind:deploy — a capability a stream needs is missing",
        ["gap:capability"], created,
    )


def _tracker(
    issues: list[Issue] | None, *, actor: str = OPERATOR, label_ok: bool = True,
) -> MagicMock:
    tracker = MagicMock()
    tracker.list_open_or_none.return_value = issues
    tracker.label_actor.return_value = actor
    tracker.label_removed_at.return_value = None
    tracker.add_label.return_value = label_ok
    return tracker


def _store() -> MagicMock:
    store = MagicMock()
    store.connection.execute.return_value.fetchone.return_value = None
    return store


def _journal(events: list[str] | None = None) -> MagicMock:
    journal = MagicMock()
    journal.read_events_by_type.return_value = [
        MagicMock(content=c) for c in (events or [])
    ]
    return journal


def _plan(plan_id: str, status: str, issue: int | None, **over: object) -> EvolutionPlan:
    fields: dict[str, object] = {
        "plan_id": plan_id, "title": plan_id, "category": "maintenance",
        "rationale": "r", "proposed_changes": ["c"], "files_affected": [],
        "expected_impact": "e", "status": status, "issue_number": issue,
        "created_at": "2026-10-02T00:00:00+00:00",
    }
    fields.update(over)
    return EvolutionPlan(**fields)  # type: ignore[arg-type]


def _select(
    issues: list[Issue] | None,
    plans: list[EvolutionPlan] | None = None,
    *,
    journal: MagicMock | None = None,
    actor: str = OPERATOR,
    excerpt: str = "",
    label_ok: bool = True,
):  # type: ignore[no-untyped-def]
    tracker = _tracker(issues, actor=actor, label_ok=label_ok)
    selection = select_evolution_fuel(
        NOW, tracker=tracker, store=_store(), operator_logins=[OPERATOR],
        journal=journal or _journal(), plans=plans or [], max_attempts=2,
        failed_excerpt=excerpt, own_login="maestro-bot",
    )
    return selection, tracker


def test_one_labelled_read_per_gap_label() -> None:
    _, tracker = _select([_budget()])
    calls = [c.kwargs for c in tracker.list_open_or_none.call_args_list]
    assert calls == [
        {"labels": [label], "page_size": FUEL_PAGE_SIZE} for label in GAP_LABELS
    ]


def test_old_gap_beyond_unfiltered_page_is_still_fuel() -> None:
    """A gap behind many newer non-gap issues stays reachable (server filter)."""
    old = _budget()
    tracker = MagicMock()
    tracker.label_actor.return_value = OPERATOR
    tracker.list_open_or_none.side_effect = lambda labels, page_size: (
        [old] if labels[0] in old.labels else []
    )
    selection = select_evolution_fuel(
        NOW, tracker=tracker, store=_store(), operator_logins=[OPERATOR],
        journal=_journal(), plans=[], max_attempts=2,
    )
    assert selection.selected is not None
    assert selection.selected.issue.number == old.number


def test_one_unreadable_label_fails_closed() -> None:
    tracker = MagicMock()
    tracker.label_actor.return_value = OPERATOR
    tracker.list_open_or_none.side_effect = [[_budget()], None, [], []]
    selection = select_evolution_fuel(
        NOW, tracker=tracker, store=_store(), operator_logins=[OPERATOR],
        journal=_journal(), plans=[], max_attempts=2,
    )
    assert selection.selected is None


def test_full_page_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    page = [_issue(n, f"x{n}", ["bug"]) for n in range(FUEL_PAGE_SIZE)]
    with caplog.at_level("WARNING", logger="maestro"):
        _select(page)
    assert "full page" in caplog.text


def test_unreadable_issue_list_is_no_fuel() -> None:
    selection, _ = _select(None)
    assert selection.selected is None


def test_non_gap_issues_are_not_fuel() -> None:
    selection, tracker = _select([_issue(9, "a bug", ["bug"])])
    assert selection.selected is None
    tracker.label_actor.assert_not_called()


def test_unverified_gap_is_not_fuel() -> None:
    selection, _ = _select([_budget()], actor="maestro-bot")
    assert selection.selected is None



def test_operator_capability_gap_beats_source_budget() -> None:
    """A5: with no stream ledger, an operator gap:capability issue wins."""
    capability = _issue(4, "need an income provider", ["gap:capability"], "2026-10-10")
    selection, _ = _select([_budget(), capability])
    assert selection.selected is not None
    assert selection.selected.issue.number == 4
    assert selection.selected.owner is None
    assert selection.selected.trust == "operator"


def test_gate_is_class_one_and_carries_excerpt() -> None:
    selection, _ = _select([_capability(), _gate()], excerpt="origin/main red on: ruff")
    fuel = selection.selected
    assert fuel is not None
    assert fuel.fuel_class == 1
    assert fuel.bypasses_gates is True
    assert fuel.failed_excerpt == "origin/main red on: ruff"


def test_oldest_first_within_a_class() -> None:
    older = _capability(5, "2026-09-01")
    selection, _ = _select([_capability(3, "2026-10-09"), older])
    assert selection.selected is not None
    assert selection.selected.issue.number == 5


def test_pending_gap_plan_keeps_only_class_one() -> None:
    """A8: while a gap-citing plan is pending_review, only a red gate is fuel."""
    plans = [_plan("p", "pending_review", 3)]
    selection, _ = _select([_budget(), _capability()], plans)
    assert selection.selected is None
    assert (2, "pending-review") in selection.skipped
    gate_selection, _ = _select([_budget(), _capability(), _gate()], plans)
    assert gate_selection.selected is not None
    assert gate_selection.selected.issue.number == 1


def test_live_plan_skips_its_gap() -> None:
    selection, _ = _select([_budget()], [_plan("p", "approved", 2)])
    assert selection.selected is None
    assert selection.skipped == ((2, "live-plan"),)


def test_capped_gap_trips_to_needs_decision() -> None:
    """Step 5: a gap at the cap is labelled and returned as tripped."""
    plans = [_plan("a", "rejected", 2), _plan("b", "failed", 2)]
    journal = _journal()
    selection, tracker = _select([_budget()], plans, journal=journal)
    assert selection.selected is None
    assert selection.skipped == ((2, "tripped"),)
    assert [t.issue_number for t in selection.tripped] == [2]
    tracker.add_label.assert_called_once_with(2, "needs-decision")
    assert journal.record_event.call_args.args[0].startswith("gap-tripwire #2:")


def test_exhausted_gap_is_recorded_once_when_label_write_fails() -> None:
    plans = [_plan("a", "rejected", 2), _plan("b", "failed", 2)]
    journal = _journal()
    selection, _ = _select([_budget()], plans, journal=journal, label_ok=False)
    assert selection.skipped == ((2, "exhausted"),)
    assert selection.tripped == ()
    assert journal.record_event.call_args.args[0].startswith("gap-exhausted #2:")
    seen = _journal(["gap-exhausted #2: 2 plan attempt(s) spent"])
    _select([_budget()], plans, journal=seen, label_ok=False)
    seen.record_event.assert_not_called()


def test_exhausted_gaps_read_events_once_per_selection() -> None:
    """Several exhausted gaps (label writes failing) share one events read."""
    plans = [
        _plan("a", "rejected", 2), _plan("b", "rejected", 2),
        _plan("c", "rejected", 6), _plan("d", "rejected", 6),
    ]
    journal = _journal()
    _select([_budget(), _budget(6)], plans, journal=journal, label_ok=False)
    assert journal.read_events_by_type.call_count == 1
    assert journal.record_event.call_count == 2


def test_cooldown_deferral_skips_until_the_rejection_ages() -> None:
    """A6: a cooldown-deferred gap is not re-selected inside the window."""
    rejected = _plan("r1", "rejected", 2, reviewed_at="2026-10-12T00:00:00+00:00")
    deferred = _plan("d1", "obsolete", 2, obsolete_reason=f"{DEFERRED_COOLDOWN_TAG}r1] x")
    selection, _ = _select([_budget()], [rejected, deferred])
    assert selection.skipped == ((2, "cooldown"),)
    rejected.reviewed_at = "2026-10-01T00:00:00+00:00"
    aged, _ = _select([_budget()], [rejected, deferred])
    assert aged.selected is not None


@pytest.mark.parametrize(
    ("title", "labels", "expected"),
    [
        ("maestro:toolchain:node — a stream toolchain is missing", ["gap:maestro"], 2),
        ("maestro:source_budget:maestro/agent.py — a source file is at its "
         "split-due size", ["gap:maestro"], 3),
    ],
)
def test_fuel_classes(title: str, labels: list[str], expected: int) -> None:
    from maestro.evolution_fuel import _gap_label_of, owner_id_of_title

    issue = _issue(1, title, labels)
    owner = parse_owner_id(owner_id_of_title(title))
    assert fuel_class_of(owner, _gap_label_of(issue, owner)) == expected


def test_label_falls_back_to_gap_maestro() -> None:
    from maestro.evolution_fuel import _gap_label_of

    assert _gap_label_of(_issue(1, "x", ["gap:bogus"]), None) == "gap:maestro"


def test_admit_gap_issues_keeps_its_contract() -> None:
    admitted = admit_gap_issues(
        [_budget()], tracker=_tracker([]), store=_store(),
        operator_logins=[OPERATOR], journal=_journal(),
    )
    assert [i.number for i in admitted] == [2]


def _fuel(issue: Issue, trust: str = "operator", excerpt: str = "") -> EvolutionFuel:
    from maestro.evolution_fuel import owner_id_of_title

    owner = parse_owner_id(owner_id_of_title(issue.title))
    return EvolutionFuel(
        issue=issue, owner=owner, label="gap:maestro", fuel_class=4,
        trust=trust, failed_excerpt=excerpt,  # type: ignore[arg-type]
    )


def test_cap_text_marks_truncation() -> None:
    assert _cap_text("abcdef", 4) == "abcd[truncated: 2 chars]"
    assert _cap_text("abc", 4) == "abc"


def test_cap_list_keeps_newest() -> None:
    from maestro.evolution_fuel import GapCap

    assert _cap_list(["a", "b", "c"], GapCap(2, 10)) == ["b", "c", "[+1 more]"]
    assert _cap_list(["a"], GapCap(0, 10)) == ["[+1 more]"]


def test_operator_body_is_fenced() -> None:
    block = render_gap_block(_fuel(_budget()), GapContextInputs())
    assert FENCE_OPEN in block
    assert UNTRUSTED_DATA_RULE in block
    assert "(none)" in block


def test_operator_title_is_fenced_and_capped() -> None:
    """An operator-filed title is the filer's words: fenced, under its own cap."""
    issue = _issue(4, "t" * (GAP_CONTEXT_CAPS["issue_title"].chars + 1), ["gap:capability"])
    block = render_gap_block(_fuel(issue), GapContextInputs())
    header = block.split("\n", 1)[0]
    assert header.endswith(f"{FENCE_OPEN} source=gap_issue ref=issue:4>>>")
    assert "[truncated: 1 chars]" in block


def test_rederived_evidence_is_fenced() -> None:
    """A re-derived body is host-editable, so it is fenced too."""
    block = render_gap_block(_fuel(_budget(), "rederived"), GapContextInputs())
    assert "Evidence:\n" + FENCE_OPEN in block
    assert "maestro:source_budget:maestro/agent.py" in block.split("\n", 1)[0]


def test_operator_filed_gap_without_owner() -> None:
    block = render_gap_block(_fuel(_issue(4, "need x", ["gap:capability"])), GapContextInputs())
    assert "owner (operator-filed)" in block


def test_every_input_is_capped() -> None:
    """A11: each input one unit over its cap is truncated with its marker."""
    caps = GAP_CONTEXT_CAPS

    def over(key: str) -> list[str]:
        cap = caps[key]
        return ["x" * (cap.chars + 1)] * (cap.items + 1)

    issue = _budget()
    issue.body = "y" * (caps["issue_body"].chars + 1)
    fuel = _fuel(issue, excerpt="z" * (caps["failed_excerpt"].chars + 1))
    inputs = GapContextInputs(
        citing_reasons=over("citing_reasons"),
        recent_rejections=over("recent_rejections"),
        source_task_candidates=over("source_task_candidates"),
        version_tail=over("version_tail"),
        live_targets=over("live_targets"),
    )
    block = render_gap_block(fuel, inputs)
    assert block.count("[truncated: 1 chars]") == 2 + sum(
        caps[k].items for k in (
            "citing_reasons", "recent_rejections", "source_task_candidates",
            "version_tail", "live_targets",
        )
    )
    assert block.count("[+1 more]") == 5
    # The bound is computed, not hard-coded: the same block rendered from empty
    # inputs is the fixed overhead (headings, fences, the data rule, the
    # "[+n more]" markers); every rendered input then adds exactly its cap plus
    # one truncation marker. Only the operator body cap counts — the evidence
    # cap is never rendered for an operator-trust issue.
    list_keys = (
        "citing_reasons", "recent_rejections", "source_task_candidates",
        "version_tail", "live_targets",
    )
    empty_issue = _budget()
    empty_issue.body = ""
    empty = render_gap_block(
        _fuel(empty_issue, excerpt="z"),
        GapContextInputs(**{k: [""] * (caps[k].items + 1) for k in list_keys}),
    )
    rendered_caps = (
        caps["issue_body"].chars + caps["failed_excerpt"].chars
        + sum(caps[k].items * caps[k].chars for k in list_keys)
    )
    marker = len("[truncated: 1 chars]") * block.count("[truncated: 1 chars]")
    assert len(block) == len(empty) - 1 + rendered_caps + marker
    rederived = _budget()
    rederived.body = "e" * (caps["evidence"].chars + 1)
    assert "[truncated: 1 chars]" in render_gap_block(_fuel(rederived, "rederived"), inputs)


def _reopened_store(stamp: str | None) -> MagicMock:
    store = MagicMock()
    store.connection.execute.return_value.fetchone.return_value = (stamp,)
    return store


def test_reopened_gap_counts_only_current_episode_attempts() -> None:
    """A door reopen resets the attempt count: past episodes' merges don't exhaust it."""
    plans = [
        _plan("a", "merged", 2, created_at="2026-09-01T00:00:00+00:00"),
        _plan("b", "merged", 2, created_at="2026-09-20T00:00:00+00:00"),
    ]
    selection = select_evolution_fuel(
        NOW, tracker=_tracker([_budget()]),
        store=_reopened_store("2026-10-01T00:00:00.000000Z"),
        operator_logins=[OPERATOR], journal=_journal(), plans=plans, max_attempts=2,
    )
    assert selection.skipped == ()
    assert selection.selected is not None


def test_current_episode_keeps_new_unparseable_and_naive_plans() -> None:
    """Plans after the reopen, or with unreadable stamps, stay counted."""
    from maestro.evolution_fuel import _current_episode_plans

    plans = [
        _plan("old", "merged", 2, created_at="2026-09-01T00:00:00+00:00"),
        _plan("new", "merged", 2, created_at="2026-10-02T00:00:00Z"),
        _plan("bad", "merged", 2, created_at="not-a-date"),
        _plan("naive", "merged", 2, created_at="2026-09-01T00:00:00"),
        _plan("other", "merged", 3),
    ]
    kept = _current_episode_plans(
        _reopened_store("2026-10-01T00:00:00.000000Z"), 2, plans,
    )
    assert [p.plan_id for p in kept] == ["new", "bad", "naive"]
    no_reopen = _current_episode_plans(_reopened_store(None), 2, plans)
    assert [p.plan_id for p in no_reopen] == ["old", "new", "bad", "naive"]
