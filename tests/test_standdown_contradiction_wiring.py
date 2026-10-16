"""Contemplation wiring for the stand-down contradiction sensor (proposal 135).

The oracle's own truth table lives in ``tests/test_standdown_validity.py``; these
tests pin the *wiring*: contemplation reads each active standdown's claim, derives
the live owed act from the readiness snapshot already persisted on the task
record, and renders a contradicted latch as ``[standdown-refuted]`` rather than a
quiet ``[stood-down]`` — adding no host call to do it.

They live in this sibling file rather than ``tests/test_agent.py`` because that
file is held shrink-only by ``tests/test_source_file_size.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from maestro.loop.known_state import compose_pr_lap_context
from tests.conftest import MockQueryResult

# --- Stand-down contradiction sensor in contemplation (proposal 135) ---


def _refutable_history_record(pr: int, snapshot: dict[str, object]):  # type: ignore[no-untyped-def]
    """A remediation TaskRecord for *pr* carrying a persisted readiness snapshot."""
    from maestro.memory import PlanStep, TaskRecord

    return TaskRecord(
        task_id=f"t-{pr}",
        goal=f"Remediate PR #{pr}: clear the blocker",
        plan=[PlanStep(step_id="s1", description="step")],
        outcome="partial",
        result_summary="awaiting tend push",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00Z",
        pr_readiness_snapshot=snapshot,
    )


def _green_snapshot(**overrides: object) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "branch_landed": False,
        "branch_behind": False,
        "branch_conflicting": False,
        "ci_state": "GREEN",
        "unresolved_threads": 0,
        "unaddressed_overflow": 0,
        "awaiting_approval": False,
        "ready_and_waiting": True,
        "settled": False,
    }
    snapshot.update(overrides)
    return snapshot


async def _contemplate_pr_lap_context(agent, history, standdowns) -> str:  # type: ignore[no-untyped-def]
    """Run ``contemplate`` over seeded state; return the rendered PR-LAPS line.

    The line is captured off the ``format_known_state_summary`` call rather than
    re-derived, so the assertion is on what contemplation actually injects.
    """
    for record in history:
        agent.memory.record_task(record)
    agent.escalation_standdowns.mutate(lambda m: m.update(standdowns))
    agent.config.pr_remediation_lap_budget = 6

    with patch(
        "maestro.loop.contemplation.format_known_state_summary", return_value="",
    ) as mock_summary, patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_q:
        mock_q.side_effect = [
            MockQueryResult(response="reflection"),
            MockQueryResult(response=json.dumps({
                "identity_statements": ["I am"],
                "values": ["growth"],
            })),
        ]
        await agent.contemplate()

    return mock_summary.call_args.kwargs["pr_lap_context"]


@pytest.mark.asyncio
async def test_contemplate_names_a_refuted_standdown(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A handoff latch over a now-CONFLICTING PR renders ``[standdown-refuted]``.

    The latch claimed "merge-ready, only Thomas can act"; the PR's latest
    persisted readiness snapshot says the branch conflicts, i.e. owned,
    code-clearable work has reappeared. Contemplation must name that
    contradiction distinctly rather than reporting a quiet ``[stood-down]``.
    """
    agent = make_agent()
    line = await _contemplate_pr_lap_context(
        agent,
        [_refutable_history_record(770, _green_snapshot(branch_conflicting=True))],
        {
            770: {
                "head_sha": "h",
                "delivered_at": "2026-01-01T00:00:00+00:00",
                "serviced_comment_ids": [],
                "claim": "handoff-ready",
            },
        },
    )

    assert "#770 (1/6 laps) [standdown-refuted]" in line
    assert "[stood-down]" not in line


@pytest.mark.asyncio
async def test_contemplate_costs_no_extra_host_call_to_name_a_refutation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Naming a refutation reaches no readiness, CI or review-thread fetch.

    The owed act is derived from the ``pr_readiness_snapshot`` already persisted
    on the task record, so the contradiction is free. The only merge-truth read
    in the window belongs to the pre-existing unlanded-lap sensor, which asks
    about exactly the one PR in history — so a second PR's worth of reads would
    fail this, as would any CI / thread / PR-state fetch.
    """
    agent = make_agent()
    host = agent.evolution._host

    line = await _contemplate_pr_lap_context(
        agent,
        [_refutable_history_record(771, _green_snapshot(branch_conflicting=True))],
        {
            771: {
                "head_sha": "h",
                "delivered_at": "2026-01-01T00:00:00+00:00",
                "serviced_comment_ids": [],
                "claim": "handoff-ready",
            },
        },
    )

    assert "[standdown-refuted]" in line
    assert host.get_pr_check_conclusion.call_count == 0
    assert host.get_pr_review_threads.call_count == 0
    assert host.get_open_evolution_pr_states.call_count == 0
    assert host.is_pr_closed.call_count == 0
    # The one merge read is the lap sensor's, about the single PR in history.
    assert [c.args for c in host.is_pr_merged.call_args_list] == [(771,)]


@pytest.mark.asyncio
async def test_contemplate_leaves_an_unrefuted_standdown_alone(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A still-green PR's handoff latch keeps the plain ``[stood-down]`` tag."""
    agent = make_agent()
    line = await _contemplate_pr_lap_context(
        agent,
        [_refutable_history_record(772, _green_snapshot())],
        {
            772: {
                "head_sha": "h",
                "delivered_at": "2026-01-01T00:00:00+00:00",
                "serviced_comment_ids": [],
                "claim": "handoff-ready",
            },
        },
    )

    assert "#772 (1/6 laps) [stood-down]" in line
    assert "refuted" not in line


@pytest.mark.asyncio
async def test_contemplate_never_refutes_a_non_handoff_claim(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A retirement-disposition latch is not released by owned work reappearing."""
    agent = make_agent()
    line = await _contemplate_pr_lap_context(
        agent,
        [_refutable_history_record(773, _green_snapshot(branch_conflicting=True))],
        {
            773: {
                "head_sha": "h",
                "delivered_at": "2026-01-01T00:00:00+00:00",
                "serviced_comment_ids": [],
                "claim": "retirement-pending",
            },
        },
    )

    assert "#773 (1/6 laps) [stood-down]" in line
    assert "refuted" not in line


# --- compose_pr_lap_context, the three-store fold (proposal 135) ---


def _ledger_with(claims: dict[int, str]):  # type: ignore[no-untyped-def]
    """An in-memory escalation ledger backend holding one record per claim."""
    from maestro.escalation_bridge import JsonEscalationLedger

    backend = JsonEscalationLedger(Path("/fake/known_state"))
    backend.mutate(
        lambda m: m.update(
            {
                pr: {
                    "head_sha": "h",
                    "delivered_at": "2026-01-01T00:00:00+00:00",
                    "serviced_comment_ids": [],
                    "claim": claim,
                }
                for pr, claim in claims.items()
            },
        ),
    )
    return backend


def test_compose_pr_lap_context_annotates_a_refuted_latch(
    mem_fs: dict[str, str],
) -> None:
    line = compose_pr_lap_context(
        {55: 4},
        _ledger_with({55: "handoff-ready"}),
        [{
            "goal": "Remediate PR #55: clear the blocker",
            "timestamp": "2026-01-02T00:00:00+00:00",
            "pr_readiness_snapshot": _green_snapshot(branch_conflicting=True),
        }],
        6,
    )
    assert "#55 (4/6 laps) [standdown-refuted]" in line


def test_compose_pr_lap_context_ignores_a_pre_latch_snapshot(
    mem_fs: dict[str, str],
) -> None:
    """A snapshot observed before the latch cannot refute the claim it predates.

    The ledger read supplies each record's ``delivered_at`` as a staleness floor,
    so the stale pre-handoff conflicting lap below leaves the latch annotated
    ``[stood-down]`` rather than manufacturing a refutation out of an
    observation the handoff already superseded.
    """
    line = compose_pr_lap_context(
        {55: 4},
        _ledger_with({55: "handoff-ready"}),
        [{
            "goal": "Remediate PR #55: clear the blocker",
            "timestamp": "2025-12-31T00:00:00+00:00",
            "pr_readiness_snapshot": _green_snapshot(branch_conflicting=True),
        }],
        6,
    )
    assert "#55 (4/6 laps) [stood-down]" in line
    assert "refuted" not in line


def test_compose_pr_lap_context_keeps_a_holding_latch_quiet(
    mem_fs: dict[str, str],
) -> None:
    line = compose_pr_lap_context(
        {55: 4},
        _ledger_with({55: "handoff-ready"}),
        [{
            "goal": "Remediate PR #55: clear the blocker",
            "timestamp": "2026-01-02T00:00:00+00:00",
            "pr_readiness_snapshot": _green_snapshot(),
        }],
        6,
    )
    assert "#55 (4/6 laps) [stood-down]" in line


def test_compose_pr_lap_context_empty_when_nothing_qualifies(
    mem_fs: dict[str, str],
) -> None:
    """A non-positive budget disables the line, as the formatter does."""
    assert compose_pr_lap_context({55: 9}, _ledger_with({}), [], 0) == ""
