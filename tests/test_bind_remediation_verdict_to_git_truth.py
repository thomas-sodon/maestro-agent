"""Parity/behaviour test for
``loop.remediation_stamps.bind_remediation_verdict_to_git_truth``.

The stage is the verbatim lift of ``run``'s ``# 4e.`` verdict-fetch gate and
``# 4f.`` record-time merge-truth binding, taken as one stage (proposal 106).
The two blocks carried a byte-identical guard
(``remediation_pr_num is not None and remediation_note is not None``) in two
adjacent ``if`` statements — the repetition never load-bearing, since
``gate_remediation_verdict`` returns a ``str`` verdict, so once the first guard
passes the second can never fail. The lift collapses them into one early return.

These tests call the stage directly, agent-first-positional, and pin every arm so it
holds 100 % coverage on its own — the full-``run`` parity harness
(``tests/test_agent_remediation.py``) exercises the same two blocks end-to-end
and must pass unchanged. ``is_pr_settled`` is a MagicMock (the coercion's only
merge read) and the fetch-truth / obligation-fork markers are seeded through the
agent's ``mem_fs`` backends, so nothing shells out.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from maestro.loop.remediation_stamps import (
    bind_remediation_verdict_to_git_truth,
)
from maestro.pr_facts import MergeState, MergeTruth
from maestro.remediation_tracker import (
    MERGE_BLOCK_AWAITING_PUSH,
    MERGE_BLOCK_BEHIND_MAIN,
)

_PR = 2647
# A note wearing both a fetch-required narrative and an external-deferral
# narrative (``Marker.AWAITING_TEND_PUSH``), so the fetch floor and — when the
# verdict is owned — the fork floor both see a gated phrase to act on.
_AWAITING_NOTE = "Resolved on branch — awaiting tend push."


def _settled(*, branch_landed: bool) -> MergeTruth:
    """Build a ``MergeTruth`` whose ``branch_landed`` drives the coercion arm.

    ``coerce_verdict_to_settled_on_merge`` reads only ``.branch_landed`` through
    the injected ``is_pr_settled(n).branch_landed`` predicate; ``state`` is set
    consistently but is not consulted by the stage under test.
    """
    state: MergeState = "MERGED" if branch_landed else "OPEN"
    return MergeTruth(
        pr_number=_PR,
        settled=branch_landed,
        state=state,
        branch_landed=branch_landed,
        source="host_api",
        observed_at=0.0,
    )


def test_no_pr_number_returns_pair_unchanged(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Collapsed-guard arm A: ``remediation_pr_num is None`` short-circuits the
    early return, returning ``(outcome, note)`` untouched without touching the
    merge oracle."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]

    outcome, note = bind_remediation_verdict_to_git_truth(
        agent,
        task_id="t-none-pr",
        remediation_pr_num=None,
        outcome="success",
        remediation_note=_AWAITING_NOTE,
    )

    assert outcome == "success"
    assert note == _AWAITING_NOTE
    agent.is_pr_settled.assert_not_called()


def test_no_note_returns_pair_unchanged(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Collapsed-guard arm B: ``remediation_note is None`` short-circuits the
    early return, returning ``(outcome, None)`` without consulting git."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]

    outcome, note = bind_remediation_verdict_to_git_truth(
        agent,
        task_id="t-none-note",
        remediation_pr_num=_PR,
        outcome="success",
        remediation_note=None,
    )

    assert outcome == "success"
    assert note is None
    agent.is_pr_settled.assert_not_called()


def test_fetch_floor_fires_without_marker(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """§4e fetch floor: an awaiting-push narrative with no recorded fetch-truth
    marker is downgraded to a ``[fetch-missing]`` partial. The default config
    leaves ``owned_awaiting_push`` False, so the fork floor is skipped and the
    plain fetch floor decides."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_settled(branch_landed=False),
    )

    outcome, note = bind_remediation_verdict_to_git_truth(
        agent,
        task_id="t-fetch-missing",
        remediation_pr_num=_PR,
        outcome="partial",
        remediation_note=_AWAITING_NOTE,
    )

    assert outcome == "partial"
    assert note is not None
    assert "[fetch-missing]" in note


def test_fetch_floor_stands_down_with_marker(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """§4e fetch floor stands down when the fetch-truth marker is present: git
    has spoken, so the awaiting-push partial passes through unmarked."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_settled(branch_landed=False),
    )
    # Seed the fetch-truth marker through the agent's mem_fs backend — the same
    # seam gate_remediation_verdict reads via ``fetch_truth.has``.
    agent.fetch_truth.record("t-marker", _PR, True)

    outcome, note = bind_remediation_verdict_to_git_truth(
        agent,
        task_id="t-marker",
        remediation_pr_num=_PR,
        outcome="partial",
        remediation_note=_AWAITING_NOTE,
    )

    assert outcome == "partial"
    assert note == _AWAITING_NOTE


def test_fork_floor_fires_when_enabled_and_owned(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """§4e obligation-fork floor: with ``obligation_fork_floor_enabled`` on and
    an owned ``AWAITING_PUSH`` merge-block, ``owned_awaiting_push`` derives True,
    so an external-deferral verdict lacking a fork marker is forced to a
    ``[fork-missing]`` partial — the derivation reaches the fork floor."""
    agent = make_agent()
    agent.config.obligation_fork_floor_enabled = True
    agent._last_remediation_merge_block = MERGE_BLOCK_AWAITING_PUSH
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_settled(branch_landed=False),
    )

    outcome, note = bind_remediation_verdict_to_git_truth(
        agent,
        task_id="t-fork-missing",
        remediation_pr_num=_PR,
        outcome="partial",
        remediation_note=_AWAITING_NOTE,
    )

    assert outcome == "partial"
    assert note is not None
    assert "[fork-missing]" in note


def test_fork_floor_inert_for_external_block_when_enabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``owned_awaiting_push`` stays False when the flag is on but the recorded
    merge-block is *external* (``BEHIND_MAIN``): the fork floor is skipped, so no
    ``[fork-missing]``/``[push-owed]`` stamp appears. A seeded fetch marker keeps
    the fetch floor quiet, isolating the derivation."""
    agent = make_agent()
    agent.config.obligation_fork_floor_enabled = True
    agent._last_remediation_merge_block = MERGE_BLOCK_BEHIND_MAIN
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_settled(branch_landed=False),
    )
    agent.fetch_truth.record("t-external", _PR, True)

    outcome, note = bind_remediation_verdict_to_git_truth(
        agent,
        task_id="t-external",
        remediation_pr_num=_PR,
        outcome="partial",
        remediation_note=_AWAITING_NOTE,
    )

    assert outcome == "partial"
    assert note == _AWAITING_NOTE
    assert "[fork-missing]" not in note
    assert "[push-owed]" not in note


def test_fork_floor_inert_when_flag_disabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``owned_awaiting_push`` stays False on the default-off path even with an
    owned ``AWAITING_PUSH`` block, so the pre-wiring behaviour is unchanged: no
    fork stamp, and the fetch floor (marker present) lets the verdict stand."""
    agent = make_agent()
    assert agent.config.obligation_fork_floor_enabled is False
    agent._last_remediation_merge_block = MERGE_BLOCK_AWAITING_PUSH
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_settled(branch_landed=False),
    )
    agent.fetch_truth.record("t-flag-off", _PR, True)

    outcome, note = bind_remediation_verdict_to_git_truth(
        agent,
        task_id="t-flag-off",
        remediation_pr_num=_PR,
        outcome="partial",
        remediation_note=_AWAITING_NOTE,
    )

    assert outcome == "partial"
    assert note == _AWAITING_NOTE
    assert "[fork-missing]" not in note
    assert "[push-owed]" not in note


def test_merge_bind_coerces_phantom_to_settled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """§4f merge bind: when ``is_pr_settled(...).branch_landed`` is True, a
    phantom awaiting-push verdict is overwritten with a settled ``success``
    carrying the ``[merged]`` text — the stale stamp cannot outvote git. A
    seeded fetch marker keeps the fetch floor from acting first, isolating the
    coercion arm."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_settled(branch_landed=True),
    )
    agent.fetch_truth.record("t-merged", _PR, True)

    outcome, note = bind_remediation_verdict_to_git_truth(
        agent,
        task_id="t-merged",
        remediation_pr_num=_PR,
        outcome="partial",
        remediation_note=_AWAITING_NOTE,
    )

    assert outcome == "success"
    assert note is not None
    assert "[merged]" in note
    assert "awaiting tend push" not in note.lower()


def test_merge_bind_leaves_unmerged_pair_intact(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """§4f merge bind is inert when git reports the branch not landed: the pair
    the fetch floor produced (here an untouched, marker-backed partial) is
    returned unchanged rather than coerced to settled."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_settled(branch_landed=False),
    )
    agent.fetch_truth.record("t-unmerged", _PR, True)

    outcome, note = bind_remediation_verdict_to_git_truth(
        agent,
        task_id="t-unmerged",
        remediation_pr_num=_PR,
        outcome="partial",
        remediation_note=_AWAITING_NOTE,
    )

    assert outcome == "partial"
    assert note == _AWAITING_NOTE
    assert "[merged]" not in note


def test_ordering_merged_pr_without_marker_settles_not_fetch_missing(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Ordering pin: a merged PR with **no** fetch-truth marker comes back a
    settled ``[merged]`` success, not a ``[fetch-missing]`` partial. The fetch
    floor runs first and would downgrade, but the merge coercion runs after and
    overwrites the verdict, so the coercion has the last word — the load-bearing
    sequence a future reorder must not break."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_settled(branch_landed=True),
    )
    # Deliberately seed no fetch-truth marker: the fetch floor would fire.

    outcome, note = bind_remediation_verdict_to_git_truth(
        agent,
        task_id="t-order",
        remediation_pr_num=_PR,
        outcome="partial",
        remediation_note=_AWAITING_NOTE,
    )

    assert outcome == "success"
    assert note is not None
    assert "[merged]" in note
    assert "[fetch-missing]" not in note
