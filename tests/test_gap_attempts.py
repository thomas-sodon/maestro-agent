"""Gap-plan attempt accounting and the cooldown deferral (proposal 155 step 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.audit_checks import DEFERRED_COOLDOWN_TAG, classify_door_refusal_reason
from maestro.evolution_submission import (
    _REJECTION_COOLDOWN_DAYS,
    _defer_gap_for_cooldown,
    count_gap_attempts,
    gap_cooldown_until,
    gap_has_live_plan,
    gap_plan_disposition,
    submit_for_review,
)
from maestro.memory import EvolutionPlan


def _plan(plan_id: str, status: str = "pending_review", **over: object) -> EvolutionPlan:
    fields: dict[str, object] = {
        "plan_id": plan_id, "title": f"Plan {plan_id}", "category": "maintenance",
        "rationale": "r", "proposed_changes": ["c"], "files_affected": [],
        "expected_impact": "e", "status": status, "issue_number": 7,
        "created_at": "2026-10-01T00:00:00+00:00",
    }
    fields.update(over)
    return EvolutionPlan(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", ["pending_review", "approved", "implemented", "sliced", "blocked"])
def test_live_statuses(status: str) -> None:
    assert gap_plan_disposition(_plan("a", status)) == "live"


@pytest.mark.parametrize("status", ["merged", "rejected", "failed"])
def test_attempt_statuses(status: str) -> None:
    assert gap_plan_disposition(_plan("a", status)) == "attempt"


@pytest.mark.parametrize(("failures", "disposition"), [(0, "live"), (2, "live"), (3, "attempt")])
def test_failed_plan_is_live_while_retry_eligible(failures: int, disposition: str) -> None:
    """A failed plan implementation still retries holds its gap; a spent one is an attempt."""
    plan = _plan("a", "failed", failure_count=failures)
    assert gap_plan_disposition(plan, retry_budget=3) == disposition
    assert gap_has_live_plan([plan], 7, retry_budget=3) is (disposition == "live")
    assert count_gap_attempts([plan], 7, retry_budget=3) == int(disposition == "attempt")


def test_content_refusal_is_an_attempt() -> None:
    """A7: a restatement refusal spends an attempt."""
    plan = _plan("a", "obsolete", obsolete_reason="Near-duplicate of b (exact title)")
    assert gap_plan_disposition(plan) == "attempt"


def test_saturation_refusal_is_deferred() -> None:
    """A7: a saturation refusal is deferred and does not count."""
    plan = _plan(
        "a", "obsolete",
        obsolete_reason="Admission gate refused net-new mint: backlog saturated: 9",
    )
    assert classify_door_refusal_reason(plan.obsolete_reason or "") == "saturation"
    assert gap_plan_disposition(plan) == "deferred"
    assert count_gap_attempts([plan], 7) == 0


def test_cooldown_reason_classifies_as_cooldown() -> None:
    assert classify_door_refusal_reason(f"{DEFERRED_COOLDOWN_TAG}x] y") == "cooldown"


def test_obsolete_without_reason_is_an_attempt() -> None:
    assert gap_plan_disposition(_plan("a", "obsolete")) == "attempt"


def test_count_and_live_scope_to_issue() -> None:
    plans = [
        _plan("a", "rejected"), _plan("b", "failed"),
        _plan("c", "rejected", issue_number=8), _plan("d", "approved", issue_number=8),
    ]
    assert count_gap_attempts(plans, 7) == 2
    assert gap_has_live_plan(plans, 7) is False
    assert gap_has_live_plan(plans, 8) is True


def test_cooldown_until_cites_the_rejection() -> None:
    """A6: the deferral holds until the cited rejection is the cooldown old."""
    rejected = _plan("r1", "rejected", reviewed_at="2026-10-10T00:00:00+00:00")
    deferred = _plan(
        "d1", "obsolete", obsolete_reason=f"{DEFERRED_COOLDOWN_TAG}r1] restatement",
    )
    until = gap_cooldown_until([rejected, deferred], 7)
    expected = datetime(2026, 10, 10, tzinfo=UTC) + timedelta(days=_REJECTION_COOLDOWN_DAYS)
    assert until == expected


def test_cooldown_until_falls_back_to_created_at_and_naive_stamp() -> None:
    rejected = _plan("r1", "rejected", reviewed_at=None, created_at="2026-10-10T00:00:00")
    deferred = _plan("d1", "obsolete", obsolete_reason=f"{DEFERRED_COOLDOWN_TAG}r1] x")
    until = gap_cooldown_until([rejected, deferred], 7)
    assert until == datetime(2026, 10, 17, tzinfo=UTC)


def test_cooldown_until_none_without_deferral() -> None:
    assert gap_cooldown_until([_plan("a", "rejected")], 7) is None


def test_cooldown_until_unknown_citation_fails_open() -> None:
    deferred = _plan("d1", "obsolete", obsolete_reason=f"{DEFERRED_COOLDOWN_TAG}gone] x")
    assert gap_cooldown_until([deferred], 7) is None


def test_cooldown_until_empty_stamp_fails_open() -> None:
    rejected = _plan("r1", "rejected", reviewed_at=None, created_at="")
    deferred = _plan("d1", "obsolete", obsolete_reason=f"{DEFERRED_COOLDOWN_TAG}r1] x")
    assert gap_cooldown_until([rejected, deferred], 7) is None


def test_cooldown_until_unparseable_stamp_fails_open() -> None:
    rejected = _plan("r1", "rejected", reviewed_at="not-a-date")
    deferred = _plan("d1", "obsolete", obsolete_reason=f"{DEFERRED_COOLDOWN_TAG}r1] x")
    assert gap_cooldown_until([rejected, deferred], 7) is None


def _engine(plans: list[EvolutionPlan]) -> MagicMock:
    engine = MagicMock()
    engine._config.generator_reality_grounding_enabled = False
    engine._config.plan_grounding_enabled = False
    engine._config.multi_pr_proposals_enabled = False
    engine._memory.get_all_plans.return_value = plans
    return engine


def test_issue_less_plan_keeps_the_silent_return() -> None:
    engine = _engine([])
    _defer_gap_for_cooldown(engine, _plan("n", issue_number=None), _plan("r1"), "x")
    engine._memory.save_evolution_plan.assert_not_called()


@pytest.mark.asyncio
async def test_cooldown_records_issue_bearing_deferral() -> None:
    """A6: the cooldown arm records the gap plan deferred, citing the rejection."""
    recent = datetime.now(UTC).isoformat()
    rejected = _plan("r1", "rejected", title="Fix gate", reviewed_at=recent)
    engine = _engine([rejected])
    plan = _plan("new", title="Fix gate")
    await submit_for_review(engine, plan)
    reason = engine._memory.mark_plan_obsolete.call_args.args[1]
    assert reason.startswith(f"{DEFERRED_COOLDOWN_TAG}r1] exact title")
    assert len(reason) <= 280


@pytest.mark.asyncio
async def test_cooldown_restatement_records_deferral() -> None:
    recent = datetime.now(UTC).isoformat()
    rejected = _plan(
        "r1", "rejected", title="Repair the red quality gate on main",
        rationale="the gate on main is red because ruff fails", reviewed_at=recent,
    )
    engine = _engine([rejected])
    plan = _plan(
        "new", title="Repair red quality gate main",
        rationale="the gate on main is red because ruff fails",
    )
    await submit_for_review(engine, plan)
    reason = engine._memory.mark_plan_obsolete.call_args.args[1]
    assert reason.startswith(f"{DEFERRED_COOLDOWN_TAG}r1] restatement")


@pytest.mark.asyncio
async def test_issue_less_cooldown_records_nothing() -> None:
    recent = datetime.now(UTC).isoformat()
    engine = _engine([_plan("r1", "rejected", title="T", reviewed_at=recent)])
    await submit_for_review(engine, _plan("new", title="T", issue_number=None))
    engine._memory.save_evolution_plan.assert_not_called()


@pytest.mark.asyncio
async def test_exact_title_duplicate_of_live_plan_is_an_attempt() -> None:
    """A7: an issue-bearing exact-title duplicate lands obsolete as a restatement."""
    engine = _engine([_plan("live1", "approved", title="T")])
    plan = _plan("new", title="T")
    await submit_for_review(engine, plan)
    reason = engine._memory.mark_plan_obsolete.call_args.args[1]
    assert reason == "Near-duplicate of live1 (exact title)"
    plan.status = "obsolete"
    plan.obsolete_reason = reason
    assert gap_plan_disposition(plan) == "attempt"


@pytest.mark.asyncio
async def test_issue_less_exact_title_duplicate_records_nothing() -> None:
    engine = _engine([_plan("live1", "approved", title="T")])
    engine._notify = AsyncMock()
    await submit_for_review(engine, _plan("new", title="T", issue_number=None))
    engine._memory.save_evolution_plan.assert_not_called()


def test_chain_in_flight_spends_no_attempt() -> None:
    """A merged early slice of a still-live chain is progress, not an attempt."""
    plans = [
        _plan("s0", "merged", parent_plan_id="prop", phase_index=0),
        _plan("s1", "pending_review", parent_plan_id="prop", phase_index=1),
        _plan("s2", "sliced", parent_plan_id="prop", phase_index=2),
    ]
    assert count_gap_attempts(plans, 7) == 0


def test_finished_chain_is_one_attempt() -> None:
    plans = [
        _plan("s0", "merged", parent_plan_id="prop", phase_index=0),
        _plan("s1", "rejected", parent_plan_id="prop", phase_index=1),
        _plan("lone", "rejected"),
    ]
    assert count_gap_attempts(plans, 7) == 2


def test_deferred_chain_is_no_attempt() -> None:
    plans = [_plan(
        "s0", "obsolete", parent_plan_id="prop",
        obsolete_reason=f"{DEFERRED_COOLDOWN_TAG}x] y",
    )]
    assert count_gap_attempts(plans, 7) == 0


def test_superseded_original_folds_into_its_chain_attempt() -> None:
    """A scope-sliced original plus its finished chain spend one attempt, not two."""
    plans = [
        _plan("orig", "obsolete",
              obsolete_reason="Superseded by multi-PR proposal prop-1 (2 slices): too large"),
        _plan("s1", "merged", parent_plan_id="prop-1"),
        _plan("s2", "merged", parent_plan_id="prop-1"),
    ]
    assert gap_plan_disposition(plans[0]) == "deferred"
    assert count_gap_attempts(plans, 7) == 1


def test_count_since_reset_counts_only_newer_plans() -> None:
    """Proposal 155 step 5: a ``needs-decision`` reset restarts the count."""
    since = datetime(2026, 10, 5, tzinfo=UTC)
    plans = [
        _plan("old", "rejected", created_at="2026-10-01T00:00:00+00:00"),
        _plan("new", "rejected", created_at="2026-10-06T00:00:00Z"),
        _plan("naive", "rejected", created_at="2026-10-07T00:00:00"),
        _plan("naive-old", "rejected", created_at="2026-10-02T00:00:00"),
    ]
    assert count_gap_attempts(plans, 7) == 4
    assert count_gap_attempts(plans, 7, since=since) == 2


def test_count_since_unparseable_stamp_still_counts() -> None:
    since = datetime(2026, 10, 5, tzinfo=UTC)
    plans = [_plan("bad", "rejected", created_at="not a date"), _plan("e", "rejected", created_at="")]
    assert count_gap_attempts(plans, 7, since=since) == 2
