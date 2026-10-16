"""Unit tests for the pure live-work oracle (proposal 100, step 1)."""

from __future__ import annotations

from maestro.memory import EvolutionPlan
from maestro.pr_facts import LIVE_STATUSES, select_live_plans


def _plan(
    plan_id: str,
    status: str = "pending_review",
    *,
    title: str = "Title",
    rationale: str = "Rationale",
    pr_url: str | None = None,
) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category="capability",
        rationale=rationale,
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        status=status,  # type: ignore[arg-type]
        created_at="2025-01-01T00:00:00Z",
        pr_url=pr_url,
    )


def test_admits_three_live_statuses_and_stamps_status() -> None:
    """All seven statuses in; only the three live ones out, in order, status stamped."""
    plans = [
        _plan("p-pending", "pending_review"),
        _plan("p-approved", "approved"),
        _plan("p-implemented", "implemented"),
        _plan("p-merged", "merged"),
        _plan("p-rejected", "rejected"),
        _plan("p-failed", "failed"),
        _plan("p-obsolete", "obsolete"),
    ]
    live = select_live_plans(plans)
    assert [lp.plan_id for lp in live] == ["p-pending", "p-approved", "p-implemented"]
    assert [lp.status for lp in live] == [
        "pending_review",
        "approved",
        "implemented",
    ]


def test_empty_input_yields_empty_list() -> None:
    """An empty corpus yields an empty list."""
    assert select_live_plans([]) == []


def test_each_terminal_status_excluded_in_isolation() -> None:
    """Every terminal status is excluded even as the sole plan present."""
    for terminal in ("merged", "rejected", "failed", "obsolete"):
        assert select_live_plans([_plan("p1", terminal)]) == []


def test_pr_number_parsed_from_well_formed_url() -> None:
    """A well-formed pr_url yields the parsed positive pr_number; trailing slash ok."""
    live = select_live_plans([
        _plan("p1", "implemented", pr_url="https://github.com/o/r/pull/4213"),
        _plan("p2", "approved", pr_url="https://github.com/o/r/pull/99/"),
    ])
    assert live[0].pr_number == 4213
    assert live[0].pr_url == "https://github.com/o/r/pull/4213"
    assert live[1].pr_number == 99


def test_pr_number_none_when_absent() -> None:
    """An absent pr_url leaves pr_number None."""
    live = select_live_plans([_plan("p1", "approved", pr_url=None)])
    assert live[0].pr_number is None


def test_pr_number_none_when_malformed() -> None:
    """A malformed or non-positive pr_url leaves pr_number None rather than raising."""
    malformed = [
        "",
        "not a url",
        "https://github.com/o/r/pull/",
        "https://github.com/o/r/pull/-3",
        "https://github.com/o/r/pull/abc",
    ]
    for bad in malformed:
        live = select_live_plans([_plan("p1", "pending_review", pr_url=bad)])
        assert live[0].pr_number is None


def test_live_statuses_names_exactly_the_three_live_statuses() -> None:
    """LIVE_STATUSES is the single named owner of the three live statuses."""
    assert LIVE_STATUSES == ("pending_review", "approved", "implemented")


def test_live_statuses_drives_select_live_plans() -> None:
    """Every LIVE_STATUSES member is admitted; no other status is."""
    all_statuses = [
        "pending_review", "approved", "implemented",
        "merged", "rejected", "failed", "obsolete",
    ]
    live = select_live_plans([_plan(f"p-{s}", s) for s in all_statuses])
    assert {p.status for p in live} == set(LIVE_STATUSES)
