"""Proposal 099 — base-advance re-entry for the stall give-up condition.

Covers the re-keying of :func:`maestro.loop.pr_remediation._is_stalled_remediation_lineage`
(and its pure counter :func:`maestro.loop.pr_remediation._same_base_partial_streak`) on
``origin/main`` advance, plus the agent seams that unconditionally thread the
current base into it: :meth:`maestro.agent.MaestroAgent._pending_pr_head_stalled`
and :meth:`maestro.agent.MaestroAgent._first_non_stalled_pr_index`.

A ``partial`` lap whose PR head did not advance against a base that *did* move is
a lost race, not unchanged externally-blocked work — so the streak breaks on base
advance exactly as it already breaks on head advance. Repeated failure against the
*same* base still stalls at the cap. All history I/O is via ``mem_fs``; git reads
are mocked at ``rev_parse``; no real subprocess or sleep.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from maestro.consciousness import PendingPRRequest
from maestro.loop.pr_remediation import (
    STALLED_REMEDIATION_LAP_CAP,
    _is_stalled_remediation_lineage,
    _same_base_partial_streak,
)
from maestro.pr_facts import PRReadiness, PrStaleness, Salvage

# Reuse the stall-detection lap factory (extended with ``base_head_sha`` for 099).
from tests.test_agent import _STALL_BASE, _STALL_HEAD, _remediation_lap

_PR = 2727
_GOAL = f"Remediate PR #{_PR}: fix the threads"
_BRANCH = f"maestro/evolve/ep{_PR}"
_MOVED_HEAD = "c" * 40
_MOVED_BASE = "d" * 40


def _rev_parse(head: str | None, base: str | None):  # type: ignore[no-untyped-def]
    """Return a ``rev_parse`` side-effect: ``origin/main`` -> *base*, else *head*."""

    def _fn(ref: str) -> str | None:
        if ref == "origin/main":
            return base
        return head

    return _fn


# --- Pure counter: _same_base_partial_streak --------------------------------


def test_same_base_streak_counts_matching_head_and_base() -> None:
    """``cap`` same-``(head, base)`` partial laps count to the full streak."""
    laps = [
        _remediation_lap(
            task_id=f"t{i}", pr_num=_PR, base_head_sha=_STALL_BASE,
        )
        for i in range(STALLED_REMEDIATION_LAP_CAP)
    ]
    streak = _same_base_partial_streak(_PR, _STALL_HEAD, _STALL_BASE, laps)
    assert streak == STALLED_REMEDIATION_LAP_CAP


def test_same_base_streak_moved_base_breaks() -> None:
    """A current base differing from the laps' recorded base yields no streak."""
    laps = [
        _remediation_lap(
            task_id=f"t{i}", pr_num=_PR, base_head_sha=_STALL_BASE,
        )
        for i in range(STALLED_REMEDIATION_LAP_CAP)
    ]
    streak = _same_base_partial_streak(_PR, _STALL_HEAD, _MOVED_BASE, laps)
    assert streak == 0


def test_same_base_streak_non_partial_lap_breaks_run() -> None:
    """A non-``partial`` most-recent lap ends the run at zero."""
    laps = [
        _remediation_lap(task_id="old", pr_num=_PR, base_head_sha=_STALL_BASE),
        _remediation_lap(
            task_id="new", pr_num=_PR, outcome="success",
            base_head_sha=_STALL_BASE,
        ),
    ]
    streak = _same_base_partial_streak(_PR, _STALL_HEAD, _STALL_BASE, laps)
    assert streak == 0


def test_same_base_streak_skips_interleaved_non_pr_laps() -> None:
    """Interleaved laps for other PRs are skipped, not treated as breaks."""
    laps = [
        _remediation_lap(task_id="a", pr_num=_PR, base_head_sha=_STALL_BASE),
        _remediation_lap(task_id="other", pr_num=9999, base_head_sha=_STALL_BASE),
        _remediation_lap(task_id="b", pr_num=_PR, base_head_sha=_STALL_BASE),
    ]
    streak = _same_base_partial_streak(_PR, _STALL_HEAD, _STALL_BASE, laps)
    assert streak == 2


def test_same_base_streak_missing_head_is_zero() -> None:
    """A missing current head can start no same-base streak (fail to re-entry)."""
    laps = [_remediation_lap(task_id="t", pr_num=_PR, base_head_sha=_STALL_BASE)]
    assert _same_base_partial_streak(_PR, None, _STALL_BASE, laps) == 0


def test_same_base_streak_missing_base_is_zero() -> None:
    """A missing current base can start no same-base streak (fail to re-entry)."""
    laps = [_remediation_lap(task_id="t", pr_num=_PR, base_head_sha=_STALL_BASE)]
    assert _same_base_partial_streak(_PR, _STALL_HEAD, None, laps) == 0


# --- Re-keyed predicate matrix: (head moved?, base moved?) -------------------
#
# Laps recorded ``(head=_STALL_HEAD, base=_STALL_BASE)``. Each cell varies the
# *current* head/base and asserts the flag-on (base-aware) verdict, then the
# flag-off (legacy, ``current_base=None``) verdict for the same inputs.


def _stall_laps() -> list:  # type: ignore[type-arg]
    return [
        _remediation_lap(
            task_id=f"t{i}", pr_num=_PR, base_head_sha=_STALL_BASE,
        )
        for i in range(STALLED_REMEDIATION_LAP_CAP)
    ]


def test_matrix_unchanged_head_unchanged_base_stalls() -> None:
    """Both unchanged: repeated failure against the same base still stalls."""
    laps = _stall_laps()
    assert _is_stalled_remediation_lineage(
        _GOAL, _STALL_HEAD, laps, current_base=_STALL_BASE,
    ) is True
    # Legacy (flag off) agrees — this is genuinely unchanged work.
    assert _is_stalled_remediation_lineage(_GOAL, _STALL_HEAD, laps) is True


def test_matrix_unchanged_head_moved_base_re_enters() -> None:
    """Unchanged head but moved base re-enters under the flag; legacy still stalls."""
    laps = _stall_laps()
    # Flag on: the moved base breaks the streak -> not stalled (re-enter).
    assert _is_stalled_remediation_lineage(
        _GOAL, _STALL_HEAD, laps, current_base=_MOVED_BASE,
    ) is False
    # Flag off (legacy, base-blind): the head never moved -> still stalled.
    assert _is_stalled_remediation_lineage(_GOAL, _STALL_HEAD, laps) is True


def test_matrix_moved_head_unchanged_base_releases() -> None:
    """A moved head releases as it always has, base-aware or not."""
    laps = _stall_laps()
    assert _is_stalled_remediation_lineage(
        _GOAL, _MOVED_HEAD, laps, current_base=_STALL_BASE,
    ) is False
    assert _is_stalled_remediation_lineage(_GOAL, _MOVED_HEAD, laps) is False


def test_matrix_both_moved_re_enters() -> None:
    """Both head and base moved: re-enter (not stalled), flag on or off."""
    laps = _stall_laps()
    assert _is_stalled_remediation_lineage(
        _GOAL, _MOVED_HEAD, laps, current_base=_MOVED_BASE,
    ) is False
    assert _is_stalled_remediation_lineage(_GOAL, _MOVED_HEAD, laps) is False


# --- Agent seam: _pending_pr_head_stalled (flag-gated base resolution) -------


def test_pending_pr_head_stalled_flag_on_moved_base_re_enters(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A same-head-stalled head whose base advanced is not stalled."""
    agent = make_agent()
    for i in range(STALLED_REMEDIATION_LAP_CAP):
        agent.memory.record_task(
            _remediation_lap(
                task_id=f"lap{i}", pr_num=_PR, base_head_sha=_STALL_BASE,
            ),
        )
    agent._vcs.rev_parse = MagicMock(  # type: ignore[method-assign]
        side_effect=_rev_parse(_STALL_HEAD, _MOVED_BASE),
    )
    entry = PendingPRRequest(directive=_GOAL, pr_branch=_BRANCH)
    assert agent._pending_pr_head_stalled(entry) is False


def test_pending_pr_head_stalled_flag_on_same_base_stalls(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The same head against an unchanged base still stalls at the cap."""
    agent = make_agent()
    for i in range(STALLED_REMEDIATION_LAP_CAP):
        agent.memory.record_task(
            _remediation_lap(
                task_id=f"lap{i}", pr_num=_PR, base_head_sha=_STALL_BASE,
            ),
        )
    agent._vcs.rev_parse = MagicMock(  # type: ignore[method-assign]
        side_effect=_rev_parse(_STALL_HEAD, _STALL_BASE),
    )
    entry = PendingPRRequest(directive=_GOAL, pr_branch=_BRANCH)
    assert agent._pending_pr_head_stalled(entry) is True


def test_pending_pr_head_stalled_flag_on_unmeasurable_base_re_enters(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An unmeasurable ``origin/main`` fails toward re-entry (not stalled)."""
    agent = make_agent()
    for i in range(STALLED_REMEDIATION_LAP_CAP):
        agent.memory.record_task(
            _remediation_lap(
                task_id=f"lap{i}", pr_num=_PR, base_head_sha=_STALL_BASE,
            ),
        )
    agent._vcs.rev_parse = MagicMock(  # type: ignore[method-assign]
        side_effect=_rev_parse(_STALL_HEAD, None),
    )
    entry = PendingPRRequest(directive=_GOAL, pr_branch=_BRANCH)
    assert agent._pending_pr_head_stalled(entry) is False


# --- Deadlock regression: _first_non_stalled_pr_index ------------------------


def test_first_non_stalled_index_single_base_advanced_entry(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A lone same-head-stalled but base-advanced entry is serviceable.

    The #3112 wedge: a single ``pending_pr_requests`` member that is stalled on
    the head alone can never self-release, so the whole FIFO freezes. Its moved
    base makes it re-serviceable — index ``0`` — so the scan no longer returns
    ``None`` and the queue unblocks.
    """
    agent = make_agent()
    for i in range(STALLED_REMEDIATION_LAP_CAP):
        agent.memory.record_task(
            _remediation_lap(
                task_id=f"lap{i}", pr_num=_PR, base_head_sha=_STALL_BASE,
            ),
        )
    agent._vcs.rev_parse = MagicMock(  # type: ignore[method-assign]
        side_effect=_rev_parse(_STALL_HEAD, _MOVED_BASE),
    )
    entries = [PendingPRRequest(directive=_GOAL, pr_branch=_BRANCH)]

    assert agent._first_non_stalled_pr_index(entries) == 0


# --- Agent seam: _assess_pr_salvage threads same_base_failures --------------


def _conflicting_readiness() -> PRReadiness:
    """A not-ready, non-disposable CONFLICTING readiness (the disengage arm)."""
    return PRReadiness(
        pr_number=_PR,
        branch_landed=False,
        branch_behind=True,
        branch_conflicting=True,
        ci_state="GREEN",
        unresolved_threads=0,
        unaddressed_overflow=0,
        awaiting_approval=False,
        ready_and_waiting=False,
        source="host_api",
        observed_at=0.0,
    )


def _wire_salvage_agent(make_agent, *, current_base):  # type: ignore[no-untyped-def]
    """Build an agent whose salvage inputs are a far-behind CONFLICTING PR.

    ``commits_behind`` (59) is past the reconcile budget, disposability is
    pinned False, and ``rev_parse`` resolves the branch head to ``_STALL_HEAD``
    and ``origin/main`` to *current_base*. ``cap`` same-``(head, base)`` laps are
    seeded so the same-base streak reaches the cap only when *current_base* is the
    laps' recorded base.
    """
    agent = make_agent()
    agent._pr_disposability = MagicMock(return_value=False)  # type: ignore[method-assign]
    agent.evolution._observe_pr_staleness = MagicMock(  # type: ignore[method-assign]
        return_value=PrStaleness(
            branch=_BRANCH,
            commits_behind=59,
            cleanly_mergeable=False,
            needs_catchup=False,
            base_head_sha=current_base,
        ),
    )
    agent._vcs.rev_parse = MagicMock(  # type: ignore[method-assign]
        side_effect=_rev_parse(_STALL_HEAD, current_base),
    )
    for i in range(STALLED_REMEDIATION_LAP_CAP):
        agent.memory.record_task(
            _remediation_lap(
                task_id=f"lap{i}", pr_num=_PR, base_head_sha=_STALL_BASE,
            ),
        )
    return agent


def test_assess_pr_salvage_flag_on_same_base_at_cap_disengages(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Cap same-base failures on a far-behind conflict → DISENGAGE."""
    agent = _wire_salvage_agent(make_agent, current_base=_STALL_BASE)
    verdict = agent._assess_pr_salvage(
        _PR, _BRANCH, readiness=_conflicting_readiness(),
    )
    assert verdict == Salvage.DISENGAGE_PERMITTED


def test_assess_pr_salvage_flag_on_moved_base_salvages(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A moved base yields zero same-base failures → SALVAGE (re-race)."""
    agent = _wire_salvage_agent(make_agent, current_base=_MOVED_BASE)
    verdict = agent._assess_pr_salvage(
        _PR, _BRANCH, readiness=_conflicting_readiness(),
    )
    assert verdict == Salvage.SALVAGE_AND_LAND


def test_delivering_lap_then_noop_lap_is_not_stalled() -> None:
    """#4567: a lap that delivered A->B (stamped at its lap-start head A)
    followed by one no-op lap at B is a single same-head lap — not stalled.
    A second no-op lap at B reaches the cap and stalls."""
    head_a = "a" * 40
    head_b = "b" * 40
    laps = [
        _remediation_lap(
            task_id="t1", pr_num=_PR, pr_head_sha=head_a, base_head_sha=_STALL_BASE,
        ),
        _remediation_lap(
            task_id="t2", pr_num=_PR, pr_head_sha=head_b, base_head_sha=_STALL_BASE,
        ),
    ]
    assert _is_stalled_remediation_lineage(
        _GOAL, head_b, laps, current_base=_STALL_BASE,
    ) is False

    laps.append(_remediation_lap(
        task_id="t3", pr_num=_PR, pr_head_sha=head_b, base_head_sha=_STALL_BASE,
    ))
    assert _is_stalled_remediation_lineage(
        _GOAL, head_b, laps, current_base=_STALL_BASE,
    ) is True
