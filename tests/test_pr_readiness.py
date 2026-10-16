"""Tests for :mod:`maestro.pr_facts` — the aggregated PR-readiness fact.

Proposal 089, step 1. The observer is pure and fully injectable, so every test
wires plain callables (no real network, no real git, no real clock) and asserts
on the returned :class:`PRReadiness`. Modelled on ``tests/test_world_state.py``.
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from maestro.pr_facts import (
    CIState,
    MergeState,
    MergeTruth,
    PRReadiness,
    Salvage,
    assess_pr_salvage,
    ci_state_clears_ci_axis,
    ci_state_from_conclusion,
    observe_pr_readiness,
)
from maestro.pr_owed_act import OwedAct, owed_act_from_readiness_snapshot
from maestro.remediation_tracker import (
    MERGE_BLOCK_AWAITING_APPROVAL,
    MERGE_BLOCK_BEHIND_MAIN,
    MERGE_BLOCK_CI_PENDING,
    MERGE_BLOCK_CONFLICTING,
    MERGE_BLOCK_NONE,
)


def _merge_truth(
    *,
    branch_landed: bool = False,
    source: str = "host_api",
    settled: bool | None = None,
    state: MergeState | None = None,
) -> MergeTruth:
    """Build a :class:`MergeTruth` stub for the merge-truth oracle.

    *settled* and *state* default to the merged/open pair implied by
    *branch_landed*; passing them explicitly builds the third real combination
    the host produces — a closed-but-unmerged PR (``settled=True``,
    ``state="CLOSED"``, ``branch_landed=False``).
    """
    return MergeTruth(
        pr_number=42,
        settled=branch_landed if settled is None else settled,
        state=("MERGED" if branch_landed else "OPEN") if state is None else state,
        branch_landed=branch_landed,
        source=source,  # type: ignore[arg-type]
        observed_at=0.0,
    )


def _observe(
    *,
    branch_landed: bool = False,
    merge_source: str = "host_api",
    settled: bool | None = None,
    state: MergeState | None = None,
    ci_state: CIState = "GREEN",
    threads: int = 0,
    overflow: int = 0,
    merge_block: str = MERGE_BLOCK_NONE,
    now: float = 100.0,
) -> PRReadiness:
    """Observe a PR with default-clean (ready-and-waiting) oracles + overrides."""
    return observe_pr_readiness(
        42,
        merge_truth=lambda _pr: _merge_truth(
            branch_landed=branch_landed,
            source=merge_source,
            settled=settled,
            state=state,
        ),
        ci_oracle=lambda _pr: ci_state,
        thread_oracle=lambda _pr: threads,
        overflow_oracle=lambda _pr: overflow,
        merge_block_oracle=lambda _pr: merge_block,
        now=lambda: now,
    )


def test_clean_open_pr_is_ready_and_waiting() -> None:
    """An open PR with every code-clearable blocker clear is ready-and-waiting."""
    readiness = _observe()
    assert readiness.ready_and_waiting is True
    assert readiness.settled is False
    assert readiness.branch_landed is False
    assert readiness.branch_behind is False
    assert readiness.branch_conflicting is False
    assert readiness.ci_state == "GREEN"
    assert readiness.unresolved_threads == 0
    assert readiness.unaddressed_overflow == 0


def test_one_unresolved_thread_flips_false() -> None:
    """A single unresolved inline thread is a code-clearable blocker."""
    readiness = _observe(threads=1)
    assert readiness.unresolved_threads == 1
    assert readiness.ready_and_waiting is False


def test_one_overflow_finding_flips_false() -> None:
    """A single unaddressed overflow finding is a code-clearable blocker."""
    readiness = _observe(overflow=1)
    assert readiness.unaddressed_overflow == 1
    assert readiness.ready_and_waiting is False


@pytest.mark.parametrize("ci_state", ["FAILING", "PENDING"])
def test_non_green_ci_flips_false(ci_state: CIState) -> None:
    """A failing or still-running CI rollup blocks readiness."""
    readiness = _observe(ci_state=ci_state)
    assert readiness.ci_state == ci_state
    assert readiness.ready_and_waiting is False


def test_no_ci_clears_ci_dimension() -> None:
    """A PR with no CI configured is ready-and-waiting when nothing else blocks."""
    readiness = _observe(ci_state="NONE")
    assert readiness.ready_and_waiting is True


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("GREEN", True),
        ("NONE", True),
        (" green ", True),
        ("FAILING", False),
        ("PENDING", False),
        ("", False),
        ("bogus", False),
    ],
)
def test_ci_state_clears_ci_axis(state: str, expected: bool) -> None:
    """Only GREEN and NONE clear the CI axis; unknown input never does."""
    assert ci_state_clears_ci_axis(state) is expected


_ALL_CI_STATES: tuple[CIState, ...] = ("GREEN", "FAILING", "PENDING", "NONE")
_READINESS_BLOCKS = (
    MERGE_BLOCK_NONE,
    MERGE_BLOCK_AWAITING_APPROVAL,
    MERGE_BLOCK_BEHIND_MAIN,
    MERGE_BLOCK_CONFLICTING,
)


@pytest.mark.parametrize(
    ("ci_state", "merge_block", "threads", "overflow"),
    list(itertools.product(_ALL_CI_STATES, _READINESS_BLOCKS, (0, 1), (0, 1))),
)
def test_ready_and_waiting_agrees_with_owed_act_handoff(
    ci_state: CIState, merge_block: str, threads: int, overflow: int,
) -> None:
    """``ready_and_waiting`` holds iff the snapshot's owed act is HANDOFF_READY."""
    readiness = _observe(
        ci_state=ci_state, merge_block=merge_block,
        threads=threads, overflow=overflow,
    )
    act = owed_act_from_readiness_snapshot(dataclasses.asdict(readiness))
    handoff = act is OwedAct.HANDOFF_READY
    assert readiness.ready_and_waiting is handoff


@pytest.mark.parametrize("code_owed", [None, False])
def test_no_ci_salvage_matches_green(code_owed: bool | None) -> None:
    """A no-CI awaiting-approval PR is released for disengagement like GREEN."""
    verdicts = []
    for ci_state in ("GREEN", "NONE"):
        readiness = _observe(
            ci_state=ci_state, merge_block=MERGE_BLOCK_AWAITING_APPROVAL,
        )
        if code_owed is None:
            verdicts.append(assess_pr_salvage(readiness, False))
        else:
            verdicts.append(
                assess_pr_salvage(readiness, False, code_owed=code_owed),
            )
    assert verdicts == [Salvage.DISENGAGE_PERMITTED, Salvage.DISENGAGE_PERMITTED]


def test_branch_behind_flips_false() -> None:
    """A behind-main branch is a code-clearable blocker."""
    readiness = _observe(merge_block=MERGE_BLOCK_BEHIND_MAIN)
    assert readiness.branch_behind is True
    assert readiness.branch_conflicting is False
    assert readiness.ready_and_waiting is False


def test_branch_conflicting_flips_false() -> None:
    """A conflicting branch is a code-clearable blocker."""
    readiness = _observe(merge_block=MERGE_BLOCK_CONFLICTING)
    assert readiness.branch_conflicting is True
    assert readiness.branch_behind is False
    assert readiness.ready_and_waiting is False


def test_awaiting_approval_does_not_block_readiness() -> None:
    """An approval wait is the external gate readiness hands up — not a blocker.

    The ``AWAITING_APPROVAL`` reason sets :attr:`PRReadiness.awaiting_approval`
    for provenance but, with every code-clearable blocker clear, the PR is still
    ready-and-waiting (the approval is exactly what it is waiting for).
    """
    readiness = _observe(merge_block=MERGE_BLOCK_AWAITING_APPROVAL)
    assert readiness.awaiting_approval is True
    assert readiness.ready_and_waiting is True


def test_ci_pending_alias_is_non_green() -> None:
    """The CI_PENDING reason path still requires GREEN CI to be ready."""
    readiness = _observe(ci_state="PENDING", merge_block=MERGE_BLOCK_CI_PENDING)
    assert readiness.ready_and_waiting is False


def test_landed_pr_is_not_ready_and_waiting() -> None:
    """A landed PR has nothing left to wait for — never ready-and-waiting."""
    readiness = _observe(branch_landed=True)
    assert readiness.branch_landed is True
    assert readiness.settled is True
    assert readiness.ready_and_waiting is False


def test_closed_unmerged_pr_is_not_ready_and_waiting() -> None:
    """A human-closed, unmerged PR is settled — nobody is going to approve it.

    Every code-clearable clause reads clean (green CI, zero threads, zero
    overflow, no merge block), so the derivation's openness test is the only
    thing standing between a closed PR and a ``ready_and_waiting`` snapshot.
    Those clean clauses are asserted rather than assumed: were ``_observe``'s
    defaults ever to drift red, the ``ready_and_waiting is False`` assertion
    would still pass — for the wrong reason — and stop guarding this bug.
    """
    readiness = _observe(settled=True, state="CLOSED", branch_landed=False)
    assert readiness.settled is True
    # The snapshot must keep reporting "not merged" — settled ≠ landed.
    assert readiness.branch_landed is False
    assert readiness.ci_state == "GREEN"
    assert readiness.unresolved_threads == 0
    assert readiness.unaddressed_overflow == 0
    assert readiness.merge_block_reason() == MERGE_BLOCK_NONE
    assert readiness.ready_and_waiting is False


def test_source_host_api_stamped_from_merge_truth() -> None:
    """A host-sourced merge truth stamps a host_api readiness source."""
    readiness = _observe(merge_source="host_api")
    assert readiness.source == "host_api"


@pytest.mark.parametrize("git_source", ["git_ancestry", "git_subject"])
def test_source_mixed_when_merge_truth_falls_back_to_git(
    git_source: str,
) -> None:
    """A git-fallback merge truth stamps a mixed readiness source."""
    readiness = _observe(merge_source=git_source)
    assert readiness.source == "mixed"


def test_observed_at_stamped_from_injected_clock() -> None:
    """``observed_at`` is stamped from the injected ``now`` clock."""
    readiness = _observe(now=1234.0)
    assert readiness.observed_at == 1234.0


@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [
        ("success", "GREEN"),
        ("SUCCESS", "GREEN"),
        ("failure", "FAILING"),
        ("none", "NONE"),
        ("", "PENDING"),
        ("pending", "PENDING"),
        ("weird", "PENDING"),
    ],
)
def test_ci_state_from_conclusion(conclusion: str, expected: CIState) -> None:
    """The host conclusion mapper covers every rollup string plus the fallback."""
    assert ci_state_from_conclusion(conclusion) == expected


def test_is_fresh_within_and_past_ttl() -> None:
    """``is_fresh`` mirrors MergeTruth: strictly-less-than-TTL is fresh."""
    readiness = _observe(now=100.0)
    assert readiness.is_fresh(100.0, 300) is True
    assert readiness.is_fresh(399.0, 300) is True
    assert readiness.is_fresh(400.0, 300) is False
    # ttl_seconds == 0 makes every observation immediately stale.
    assert readiness.is_fresh(100.0, 0) is False


@pytest.mark.parametrize(
    ("merge_block", "expected"),
    [
        (MERGE_BLOCK_CONFLICTING, MERGE_BLOCK_CONFLICTING),
        (MERGE_BLOCK_BEHIND_MAIN, MERGE_BLOCK_BEHIND_MAIN),
        (MERGE_BLOCK_AWAITING_APPROVAL, MERGE_BLOCK_AWAITING_APPROVAL),
        # A reason that sets none of the three branch flags recovers as NONE.
        (MERGE_BLOCK_NONE, MERGE_BLOCK_NONE),
        (MERGE_BLOCK_CI_PENDING, MERGE_BLOCK_NONE),
    ],
)
def test_merge_block_reason_round_trips_the_derivation(
    merge_block: str, expected: str,
) -> None:
    """``merge_block_reason`` inverts the branch-flag derivation observe runs."""
    assert _observe(merge_block=merge_block).merge_block_reason() == expected


def test_merge_block_reason_priority_order() -> None:
    """Directly-set flags resolve conflict > behind > approval-wait.

    ``observe_pr_readiness`` only ever sets one branch flag, but the accessor
    pins a deterministic order so a hand-built multi-flag fact is unambiguous.
    """
    readiness = PRReadiness(
        pr_number=42,
        branch_landed=False,
        branch_behind=True,
        branch_conflicting=True,
        ci_state="GREEN",
        unresolved_threads=0,
        unaddressed_overflow=0,
        awaiting_approval=True,
        ready_and_waiting=False,
        source="host_api",
        observed_at=0.0,
    )
    assert readiness.merge_block_reason() == MERGE_BLOCK_CONFLICTING
