"""Unit tests for the pure WIP-focus owner (proposal 092).

Covers :func:`maestro.wip_primacy.select_task_focus` in isolation — no agent, no
host, no SDK. Every case is deterministic given the injected
:class:`~maestro.pr_facts.PRReadiness` facts.
"""

from __future__ import annotations

import dataclasses

import pytest

from maestro.pr_facts import PRReadiness
from maestro.pr_owed_act import OwedAct
from maestro.wip_primacy import (
    Discretionary,
    OpenOwnedPR,
    TendOwnedPR,
    select_task_focus,
)


def _readiness(
    pr_number: int,
    *,
    branch_landed: bool = False,
    branch_behind: bool = False,
    branch_conflicting: bool = False,
    awaiting_approval: bool = False,
    unresolved_threads: int = 0,
    unaddressed_overflow: int = 0,
) -> PRReadiness:
    """Build a :class:`PRReadiness` exposing only the fields the owner reads.

    ``merge_block_reason`` derives its code from
    ``branch_conflicting`` / ``branch_behind`` / ``awaiting_approval`` (in that
    priority), and the owner also reads ``branch_landed`` and
    ``unresolved_threads`` / ``unaddressed_overflow``; the rest are fixed to
    neutral values.
    """
    return PRReadiness(
        pr_number=pr_number,
        branch_landed=branch_landed,
        branch_behind=branch_behind,
        branch_conflicting=branch_conflicting,
        ci_state="GREEN",
        unresolved_threads=unresolved_threads,
        unaddressed_overflow=unaddressed_overflow,
        awaiting_approval=awaiting_approval,
        ready_and_waiting=False,
        source="host_api",
        observed_at=0.0,
    )


def _pr(pr_number: int, created_at: str = "2026-01-01T00:00:00Z") -> OpenOwnedPR:
    return OpenOwnedPR(pr_number=pr_number, created_at=created_at)


def test_conflicting_pr_is_owned_wip() -> None:
    """A CONFLICTING branch is a self-clearable blocker → TendOwnedPR."""
    focus = select_task_focus(
        [_pr(1)], {1: _readiness(1, branch_conflicting=True)},
    )
    assert focus == TendOwnedPR(pr_number=1, owned_blocker="CONFLICTING")


def test_unresolved_threads_promote_clean_state_to_owned() -> None:
    """An open review thread on a clean-merge PR is owned THREADS_UNRESOLVED work."""
    focus = select_task_focus([_pr(7)], {7: _readiness(7, unresolved_threads=2)})
    assert focus == TendOwnedPR(pr_number=7, owned_blocker="THREADS_UNRESOLVED")


def test_unresolved_threads_promote_approval_wait_to_owned() -> None:
    """An open thread outranks an approval wait → owned THREADS_UNRESOLVED."""
    focus = select_task_focus(
        [_pr(8)],
        {8: _readiness(8, awaiting_approval=True, unresolved_threads=1)},
    )
    assert focus == TendOwnedPR(pr_number=8, owned_blocker="THREADS_UNRESOLVED")


def test_behind_main_is_external() -> None:
    """BEHIND_MAIN is Tend's mechanical catch-up, not owned → Discretionary."""
    focus = select_task_focus([_pr(3)], {3: _readiness(3, branch_behind=True)})
    assert focus == Discretionary()


def test_approval_wait_without_threads_is_external() -> None:
    """An approval wait with no open thread is the external gate → Discretionary."""
    focus = select_task_focus(
        [_pr(4)], {4: _readiness(4, awaiting_approval=True)},
    )
    assert focus == Discretionary()


def test_clean_pr_with_no_blocker_is_discretionary() -> None:
    """A PR with no code-clearable blocker (reason NONE) is external default."""
    focus = select_task_focus([_pr(5)], {5: _readiness(5)})
    assert focus == Discretionary()


def test_no_open_prs_is_discretionary() -> None:
    """No owned PRs at all → discretionary work is allowed."""
    assert select_task_focus([], {}) == Discretionary()


def test_landed_pr_is_never_owned_wip() -> None:
    """A landed branch is never owned WIP, even with a conflicting flag set."""
    focus = select_task_focus(
        [_pr(6)],
        {6: _readiness(6, branch_landed=True, branch_conflicting=True)},
    )
    assert focus == Discretionary()


def test_oldest_owned_pr_wins_by_created_at() -> None:
    """Among owned PRs the oldest (earliest ``created_at``) is chosen."""
    focus = select_task_focus(
        [
            _pr(5, "2026-02-01T00:00:00Z"),
            _pr(2, "2026-01-01T00:00:00Z"),
        ],
        {
            5: _readiness(5, branch_conflicting=True),
            2: _readiness(2, branch_conflicting=True),
        },
    )
    assert focus == TendOwnedPR(pr_number=2, owned_blocker="CONFLICTING")


def test_equal_created_at_tie_breaks_on_lower_pr_number() -> None:
    """Same ``created_at`` → the lower PR number wins (stable tie-break)."""
    same = "2026-01-01T00:00:00Z"
    focus = select_task_focus(
        [_pr(9, same), _pr(4, same)],
        {
            9: _readiness(9, branch_conflicting=True),
            4: _readiness(4, branch_conflicting=True),
        },
    )
    assert focus == TendOwnedPR(pr_number=4, owned_blocker="CONFLICTING")


def test_external_older_pr_does_not_block_owned_newer_pr() -> None:
    """An older external-only PR is skipped; a newer owned PR is still chosen."""
    focus = select_task_focus(
        [
            _pr(1, "2026-01-01T00:00:00Z"),
            _pr(2, "2026-02-01T00:00:00Z"),
        ],
        {
            1: _readiness(1, branch_behind=True),
            2: _readiness(2, branch_conflicting=True),
        },
    )
    assert focus == TendOwnedPR(pr_number=2, owned_blocker="CONFLICTING")


def test_pr_missing_from_readiness_map_is_skipped() -> None:
    """A PR with no readiness observation is skipped, not guessed owned."""
    focus = select_task_focus(
        [
            _pr(1, "2026-01-01T00:00:00Z"),
            _pr(2, "2026-02-01T00:00:00Z"),
        ],
        {2: _readiness(2, branch_conflicting=True)},
    )
    assert focus == TendOwnedPR(pr_number=2, owned_blocker="CONFLICTING")


def test_all_prs_missing_readiness_is_discretionary() -> None:
    """When no listed PR has a readiness fact, no owned WIP is found."""
    assert select_task_focus([_pr(1), _pr(2)], {}) == Discretionary()


def _red_readiness(pr_number: int) -> PRReadiness:
    """A PR whose *only* blocker is a concluded CI red — no branch flag fires."""
    readiness = _readiness(pr_number)
    return dataclasses.replace(readiness, ci_state="FAILING")


def test_ci_red_only_pr_is_discretionary_without_the_map() -> None:
    """Today's verdict: a 0-behind, 0-conflict, thread-free red reads external."""
    focus = select_task_focus([_pr(11)], {11: _red_readiness(11)})
    assert focus == Discretionary()


def test_ci_red_only_pr_is_owned_wip_when_the_owed_act_says_code_owed() -> None:
    """A CODE_OWED entry promotes the bare red to owned WIP under CI_FAILING."""
    focus = select_task_focus(
        [_pr(11)],
        {11: _red_readiness(11)},
        owed_act_by_pr={11: OwedAct.CODE_OWED},
    )
    assert focus == TendOwnedPR(pr_number=11, owned_blocker="CI_FAILING")


@pytest.mark.parametrize(
    "act", [OwedAct.WAIT_CI, OwedAct.HANDOFF_READY, OwedAct.EXTERNAL_BLOCKED, None],
)
def test_non_code_owed_entries_change_nothing(act: OwedAct | None) -> None:
    """Only CODE_OWED fires the second arm; every other act stays discretionary."""
    focus = select_task_focus(
        [_pr(11)], {11: _red_readiness(11)}, owed_act_by_pr={11: act},
    )
    assert focus == Discretionary()


def test_owned_merge_blocker_keeps_its_own_code_over_the_map() -> None:
    """The branch-flag arm has precedence: CONFLICTING is not rewritten."""
    focus = select_task_focus(
        [_pr(11)],
        {11: _readiness(11, branch_conflicting=True)},
        owed_act_by_pr={11: OwedAct.CODE_OWED},
    )
    assert focus == TendOwnedPR(pr_number=11, owned_blocker="CONFLICTING")


def test_landed_pr_is_skipped_even_when_code_owed() -> None:
    """A landed PR has nothing left to finish, whatever the map says."""
    focus = select_task_focus(
        [_pr(11)],
        {11: _readiness(11, branch_landed=True)},
        owed_act_by_pr={11: OwedAct.CODE_OWED},
    )
    assert focus == Discretionary()


def test_oldest_first_ordering_holds_across_a_mixed_pair() -> None:
    """The older code-owed red wins over a newer conflicting PR."""
    focus = select_task_focus(
        [
            _pr(20, "2026-02-01T00:00:00Z"),
            _pr(10, "2026-01-01T00:00:00Z"),
        ],
        {
            10: _red_readiness(10),
            20: _readiness(20, branch_conflicting=True),
        },
        owed_act_by_pr={10: OwedAct.CODE_OWED},
    )
    assert focus == TendOwnedPR(pr_number=10, owned_blocker="CI_FAILING")


def test_code_owed_from_overflow_is_not_named_a_ci_red() -> None:
    """A CODE_OWED grounded in review work names THREADS_UNRESOLVED, not CI_FAILING."""
    focus = select_task_focus(
        [_pr(11)],
        {11: _readiness(11, unaddressed_overflow=2)},
        owed_act_by_pr={11: OwedAct.CODE_OWED},
    )
    assert focus == TendOwnedPR(
        pr_number=11, owned_blocker="THREADS_UNRESOLVED",
    )


def test_code_owed_from_a_branch_flag_keeps_the_measured_code() -> None:
    """With CI green and no review work, the measured branch flag is reported."""
    focus = select_task_focus(
        [_pr(11)],
        {11: _readiness(11, branch_behind=True)},
        owed_act_by_pr={11: OwedAct.CODE_OWED},
    )
    assert focus == TendOwnedPR(pr_number=11, owned_blocker="BEHIND_MAIN")


def test_code_owed_with_no_measured_blocker_reports_none() -> None:
    """Nothing measured on the readiness: the code is NONE, never an invented red."""
    focus = select_task_focus(
        [_pr(11)], {11: _readiness(11)}, owed_act_by_pr={11: OwedAct.CODE_OWED},
    )
    assert focus == TendOwnedPR(pr_number=11, owned_blocker="NONE")


def test_code_owed_red_outranks_review_work_in_the_name() -> None:
    """A measured red wins the name even when overflow review work is outstanding.

    An unaddressed overflow finding is review work the branch-flag arm does not
    count (only ``unresolved_threads`` promotes there), so this PR does reach
    the owed-act arm — and the red, being the stronger fact, names it.
    """
    focus = select_task_focus(
        [_pr(11)],
        {
            11: dataclasses.replace(
                _readiness(11, unaddressed_overflow=1), ci_state="FAILING",
            ),
        },
        owed_act_by_pr={11: OwedAct.CODE_OWED},
    )
    assert focus == TendOwnedPR(pr_number=11, owned_blocker="CI_FAILING")
