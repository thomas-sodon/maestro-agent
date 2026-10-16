"""Tests for the ``observe_pr_readiness`` agent chokepoint (proposal 089 step 2).

``MaestroAgent.observe_pr_readiness`` is the single door every face reads PR
readiness through: one provenance-stamped :class:`~maestro.pr_facts.PRReadiness`
aggregating each blocker dimension, memoized per iteration with the merge-truth
freshness TTL. These tests pin the wiring, the memo, and the TTL re-observation
with mocked git/host I/O, per Testing Principles.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from maestro.pr_facts import MergeTruth, PRReadiness
from maestro.remediation_tracker import (
    MERGE_BLOCK_BEHIND_MAIN,
    MERGE_BLOCK_CONFLICTING,
    MERGE_BLOCK_NONE,
)
from maestro.vcs import VCSError
from maestro.wip_primacy import Discretionary, TendOwnedPR


class _Clock:
    """A mutable zero-arg clock so a test can advance time between calls."""

    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _merge_truth(*, landed: bool = False, source: str = "host_api") -> MergeTruth:
    return MergeTruth(
        pr_number=42,
        settled=landed,
        state="MERGED" if landed else "OPEN",
        branch_landed=landed,
        source=source,  # type: ignore[arg-type]
        observed_at=0.0,
    )


def _wire_clean(agent, *, conclusion: str = "success") -> None:  # type: ignore[no-untyped-def]
    """Wire every readiness oracle to a clean (ready-and-waiting) verdict."""
    agent.is_pr_settled = MagicMock(return_value=_merge_truth())
    agent.evolution._host.get_pr_check_conclusion = MagicMock(
        return_value=conclusion,
    )
    agent.evolution._host.get_pr_review_threads_strict = MagicMock(
        return_value=[],
    )
    agent._observe_pr_merge_block_reason = MagicMock(
        return_value=MERGE_BLOCK_NONE,
    )


def test_observe_pr_readiness_aggregates_clean_pr(make_agent):  # type: ignore[no-untyped-def]
    """A PR with every dimension clear is ready-and-waiting, source host_api."""
    agent = make_agent()
    _wire_clean(agent)
    with patch(
        "maestro.agent._fetch_eligible_overflow_findings",
        return_value=([], True),
    ):
        readiness = agent.observe_pr_readiness(42, "b42")
    assert readiness.ready_and_waiting is True
    assert readiness.ci_state == "GREEN"
    assert readiness.unresolved_threads == 0
    assert readiness.unaddressed_overflow == 0
    assert readiness.source == "host_api"


def test_observe_pr_readiness_unresolved_thread_blocks(make_agent):  # type: ignore[no-untyped-def]
    """An eligible inline thread count > 0 flips ready_and_waiting False."""
    agent = make_agent()
    _wire_clean(agent)
    with (
        patch(
            "maestro.evolution_tend._eligible_bug_comments",
            return_value=[(1, "f.py", "bug", "bug")],
        ),
        patch(
            "maestro.agent._fetch_eligible_overflow_findings",
            return_value=([], True),
        ),
    ):
        readiness = agent.observe_pr_readiness(42, "b42")
    assert readiness.unresolved_threads == 1
    assert readiness.ready_and_waiting is False


def test_observe_pr_readiness_overflow_blocks(make_agent):  # type: ignore[no-untyped-def]
    """An unaddressed overflow finding flips ready_and_waiting False."""
    agent = make_agent()
    _wire_clean(agent)
    finding = SimpleNamespace(severity="bug")
    with (
        patch("maestro.evolution_tend._eligible_bug_comments", return_value=[]),
        patch(
            "maestro.agent._fetch_eligible_overflow_findings",
            return_value=([finding], True),
        ),
    ):
        readiness = agent.observe_pr_readiness(42, "b42")
    assert readiness.unaddressed_overflow == 1
    assert readiness.ready_and_waiting is False


def test_observe_pr_readiness_failed_overflow_scan_is_not_ready(make_agent):  # type: ignore[no-untyped-def]
    """A swallowed overflow-fetch error must never read as ready-and-waiting.

    ``_fetch_eligible_overflow_findings`` returns ``([], scan_ok=False)`` on a
    host-fetch error. Counting that as zero outstanding would let a transient
    outage flip an otherwise-clean PR to ready — the exact failure
    ``_confirm_ready_for_review`` guards against. The overflow oracle surfaces a
    blocker instead, so the PR stays not-ready until the scan completes.
    """
    agent = make_agent()
    _wire_clean(agent)
    with (
        patch("maestro.evolution_tend._eligible_bug_comments", return_value=[]),
        patch(
            "maestro.agent._fetch_eligible_overflow_findings",
            return_value=([], False),
        ),
    ):
        readiness = agent.observe_pr_readiness(42, "b42")
    assert readiness.unaddressed_overflow == 1
    assert readiness.ready_and_waiting is False


def test_observe_pr_readiness_failed_thread_scan_is_not_ready(make_agent):  # type: ignore[no-untyped-def]
    """A failed review-thread scan must never read as zero unresolved threads.

    The thread oracle reads through the fail-loud strict reader; a ``VCSError``
    surfaces one blocker so the PR stays not-ready until a scan completes.
    """
    agent = make_agent()
    _wire_clean(agent)
    agent.evolution._host.get_pr_review_threads_strict = MagicMock(
        side_effect=VCSError("boom"),
    )
    with patch(
        "maestro.agent._fetch_eligible_overflow_findings",
        return_value=([], True),
    ):
        readiness = agent.observe_pr_readiness(42, "b42")
    assert readiness.unresolved_threads == 1
    assert readiness.ready_and_waiting is False


def test_observe_pr_readiness_memoized_within_ttl(make_agent):  # type: ignore[no-untyped-def]
    """A second observation within the TTL reuses the memo — no second read."""
    agent = make_agent()
    _wire_clean(agent)
    clock = _Clock(1000.0)
    with (
        patch("maestro.agent.time.time", clock),
        patch(
            "maestro.agent._fetch_eligible_overflow_findings",
            return_value=([], True),
        ),
        patch("maestro.evolution_tend._eligible_bug_comments", return_value=[]),
    ):
        first = agent.observe_pr_readiness(42, "b42")
        second = agent.observe_pr_readiness(42, "b42")
    assert first is second
    assert agent.evolution._host.get_pr_check_conclusion.call_count == 1
    assert agent.is_pr_settled.call_count == 1


def test_observe_pr_readiness_reobserves_past_ttl(make_agent):  # type: ignore[no-untyped-def]
    """A memoized fact older than the TTL is re-observed, not reused."""
    agent = make_agent()
    _wire_clean(agent)
    agent.config.merge_truth_ttl_seconds = 300
    clock = _Clock(1000.0)
    with (
        patch("maestro.agent.time.time", clock),
        patch(
            "maestro.agent._fetch_eligible_overflow_findings",
            return_value=([], True),
        ),
        patch("maestro.evolution_tend._eligible_bug_comments", return_value=[]),
    ):
        agent.observe_pr_readiness(42, "b42")
        clock.value = 2000.0  # advance well past the 300s TTL
        agent.observe_pr_readiness(42, "b42")
    assert agent.evolution._host.get_pr_check_conclusion.call_count == 2


def test_merge_block_reason_seam_classifies_conflict(make_agent):  # type: ignore[no-untyped-def]
    """The merge-block seam reads VCS/host signals and applies the CONFLICTING
    host-field refinement to a behind/clean classification."""
    agent = make_agent()
    agent._vcs.commits_ahead_behind = MagicMock(return_value=(0, 2))
    agent.evolution._host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "b42": SimpleNamespace(
                merge_state_status="BEHIND", mergeable="CONFLICTING",
            ),
        },
    )
    reason = agent._observe_pr_merge_block_reason(42, "b42", "success")
    assert reason == MERGE_BLOCK_CONFLICTING


def test_merge_block_reason_seam_behind_main(make_agent):  # type: ignore[no-untyped-def]
    """A behind-but-mergeable branch classifies BEHIND_MAIN."""
    agent = make_agent()
    agent._vcs.commits_ahead_behind = MagicMock(return_value=(0, 3))
    agent.evolution._host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "b42": SimpleNamespace(
                merge_state_status="BEHIND", mergeable="MERGEABLE",
            ),
        },
    )
    reason = agent._observe_pr_merge_block_reason(42, "b42", "success")
    assert reason == MERGE_BLOCK_BEHIND_MAIN


def test_merge_block_reason_seam_behind_and_ci_failing(make_agent):  # type: ignore[no-untyped-def]
    """A behind red head stays BEHIND_MAIN at the observer: the #4096 red-head
    refinement is routing-only, so ``branch_behind`` stays a git fact and a
    PENDING->FAILING flip is never counted as "behind resolved" lap progress."""
    agent = make_agent()
    agent._vcs.commits_ahead_behind = MagicMock(return_value=(0, 3))
    agent.evolution._host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "b42": SimpleNamespace(
                merge_state_status="BEHIND", mergeable="MERGEABLE",
            ),
        },
    )
    reason = agent._observe_pr_merge_block_reason(42, "b42", "failure")
    assert reason == MERGE_BLOCK_BEHIND_MAIN


def _wire_behind(agent, *, conflicts: bool) -> None:  # type: ignore[no-untyped-def]
    """Wire a behind, host-mergeable branch under the merge-queue model."""
    agent.config.tend_main_merge_enabled = False
    agent._vcs.rev_parse = MagicMock(return_value="sha")
    agent._vcs.commits_ahead_behind = MagicMock(return_value=(0, 5))
    agent._vcs.merge_tree_conflicts = MagicMock(return_value=conflicts)
    agent.evolution._host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "b42": SimpleNamespace(
                merge_state_status="BEHIND", mergeable="MERGEABLE",
            ),
        },
    )


def test_behind_but_clean_is_ready_under_merge_queue(make_agent):  # type: ignore[no-untyped-def]
    """#4307: with the Tend catch-up off, a green, thread-free PR that merely
    trails main is ready-and-waiting — the merge queue owns the rebase."""
    agent = make_agent()
    _wire_behind(agent, conflicts=False)
    agent.is_pr_settled = MagicMock(return_value=_merge_truth())
    agent.evolution._host.get_pr_check_conclusion = MagicMock(
        return_value="success",
    )
    agent.evolution._host.get_pr_review_threads = MagicMock(return_value=[])
    with patch(
        "maestro.agent._fetch_eligible_overflow_findings",
        return_value=([], True),
    ):
        readiness = agent.observe_pr_readiness(42, "b42")
    assert readiness.branch_behind is False
    assert readiness.ready_and_waiting is True
    agent._vcs.merge_tree_conflicts.assert_called_once_with(
        "origin/b42", "origin/main",
    )


def test_stale_local_clean_origin_is_not_conflicting_under_merge_queue(make_agent):  # type: ignore[no-untyped-def]
    """#4371: a conflict resolved on the host leaves the local branch stale;
    readiness must measure ``origin/<branch>``, not the stale local ref."""
    agent = make_agent()
    _wire_behind(agent, conflicts=False)
    agent._vcs.commits_ahead_behind = MagicMock(
        return_value=(0, 5),
    )
    agent._vcs.merge_tree_conflicts = MagicMock(
        side_effect=lambda ref, _base: ref == "b42",
    )
    reason = agent._observe_pr_merge_block_reason(42, "b42", "success")
    assert reason == MERGE_BLOCK_NONE
    refs = [c.args[0] for c in agent._vcs.commits_ahead_behind.call_args_list]
    assert refs == ["origin/b42"]
    agent._vcs.rev_parse.assert_called_once_with("origin/b42")
    agent._vcs.merge_tree_conflicts.assert_called_once_with("origin/b42", "origin/main")


def test_readiness_probe_falls_back_to_local_when_origin_missing(make_agent):  # type: ignore[no-untyped-def]
    """A never-pushed branch has no origin ref — probe the local branch."""
    agent = make_agent()
    _wire_behind(agent, conflicts=True)
    agent._vcs.rev_parse = MagicMock(return_value=None)
    reason = agent._observe_pr_merge_block_reason(42, "b42", "success")
    assert reason == MERGE_BLOCK_CONFLICTING
    agent._vcs.commits_ahead_behind.assert_called_once_with("b42", "origin/main")
    agent._vcs.merge_tree_conflicts.assert_called_once_with("b42", "origin/main")


def test_behind_and_conflicting_is_not_ready_under_merge_queue(make_agent):  # type: ignore[no-untyped-def]
    """A behind branch that no longer merges cleanly reads CONFLICTING."""
    agent = make_agent()
    _wire_behind(agent, conflicts=True)
    reason = agent._observe_pr_merge_block_reason(42, "b42", "success")
    assert reason == MERGE_BLOCK_CONFLICTING


def test_flag_on_behind_never_probes_merge_tree(make_agent):  # type: ignore[no-untyped-def]
    """The flag-on path stays byte-identical: BEHIND_MAIN, no extra probe."""
    agent = make_agent()
    _wire_behind(agent, conflicts=True)
    agent.config.tend_main_merge_enabled = True
    reason = agent._observe_pr_merge_block_reason(42, "b42", "success")
    assert reason == MERGE_BLOCK_BEHIND_MAIN
    agent._vcs.merge_tree_conflicts.assert_not_called()


def test_merge_block_reason_seam_branchless_is_none(make_agent):  # type: ignore[no-untyped-def]
    """A branchless request cannot compute branch state — degrades to NONE."""
    agent = make_agent()
    reason = agent._observe_pr_merge_block_reason(42, None, "success")
    assert reason == MERGE_BLOCK_NONE


# ---------------------------------------------------------------------------
# _current_task_focus gatherer (proposal 092)
# ---------------------------------------------------------------------------


def _readiness(
    pr_number: int,
    *,
    branch_landed: bool = False,
    branch_behind: bool = False,
    branch_conflicting: bool = False,
    awaiting_approval: bool = False,
    unresolved_threads: int = 0,
) -> PRReadiness:
    return PRReadiness(
        pr_number=pr_number,
        branch_landed=branch_landed,
        branch_behind=branch_behind,
        branch_conflicting=branch_conflicting,
        ci_state="GREEN",
        unresolved_threads=unresolved_threads,
        unaddressed_overflow=0,
        awaiting_approval=awaiting_approval,
        ready_and_waiting=False,
        source="host_api",
        observed_at=0.0,
    )


def test_current_task_focus_finds_owned_wip(make_agent):  # type: ignore[no-untyped-def]
    """The gatherer enumerates open owned PRs, observes each, returns the owned one."""
    agent = make_agent()
    agent.evolution._host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "maestro/evolve/a": SimpleNamespace(
                number=7, created_at="2026-01-01T00:00:00Z",
            ),
        },
    )
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness(7, branch_conflicting=True),
    )

    focus = agent._current_task_focus()

    assert focus == TendOwnedPR(pr_number=7, owned_blocker="CONFLICTING")
    agent.observe_pr_readiness.assert_called_once_with(7, "maestro/evolve/a")


def test_current_task_focus_discretionary_when_external(make_agent):  # type: ignore[no-untyped-def]
    """A single external-only (BEHIND_MAIN) PR yields Discretionary."""
    agent = make_agent()
    agent.evolution._host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "maestro/evolve/a": SimpleNamespace(
                number=3, created_at="2026-01-01T00:00:00Z",
            ),
        },
    )
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness(3, branch_behind=True),
    )

    assert agent._current_task_focus() == Discretionary()


def test_current_task_focus_empty_when_no_open_prs(make_agent):  # type: ignore[no-untyped-def]
    """No open owned PRs → Discretionary, readiness never observed."""
    agent = make_agent()
    agent.evolution._host.get_open_evolution_pr_states = MagicMock(
        return_value={},
    )
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness(1),
    )

    assert agent._current_task_focus() == Discretionary()
    agent.observe_pr_readiness.assert_not_called()


def test_current_task_focus_builds_map_over_multiple_prs(make_agent):  # type: ignore[no-untyped-def]
    """Both PRs are observed; the owned one (by its readiness) is chosen."""
    agent = make_agent()
    agent.evolution._host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "maestro/evolve/a": SimpleNamespace(
                number=1, created_at="2026-01-01T00:00:00Z",
            ),
            "maestro/evolve/b": SimpleNamespace(
                number=2, created_at="2026-02-01T00:00:00Z",
            ),
        },
    )
    readiness_by = {
        1: _readiness(1, branch_behind=True),
        2: _readiness(2, unresolved_threads=1),
    }
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda num, branch: readiness_by[num],
    )

    focus = agent._current_task_focus()

    assert focus == TendOwnedPR(pr_number=2, owned_blocker="THREADS_UNRESOLVED")
    assert agent.observe_pr_readiness.call_count == 2


def test_current_task_focus_memoizes_open_pr_enumeration(make_agent):  # type: ignore[no-untyped-def]
    """Two consults in one iteration share one ``get_open_evolution_pr_states``.

    The head-of-chain ``wip_primacy`` gate and the audit→directive bridge can
    both call ``_current_task_focus`` in a single iteration; the per-iteration
    enumeration snapshot (``_open_owned_pr_states_memo``) means the ``gh pr list``
    behind ``get_open_evolution_pr_states`` runs once, while a fresh iteration
    (memo cleared to ``None``) re-lists. Guards proposal-092 review finding r2.
    """
    agent = make_agent()
    enumerate_mock = MagicMock(
        return_value={
            "maestro/evolve/a": SimpleNamespace(
                number=7, created_at="2026-01-01T00:00:00Z",
            ),
        },
    )
    agent.evolution._host.get_open_evolution_pr_states = enumerate_mock
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness(7, branch_conflicting=True),
    )

    first = agent._current_task_focus()
    second = agent._current_task_focus()

    assert first == second
    assert first == TendOwnedPR(pr_number=7, owned_blocker="CONFLICTING")
    enumerate_mock.assert_called_once()

    # A fresh iteration clears the snapshot; the next consult re-enumerates.
    agent._open_owned_pr_states_memo = None
    agent._current_task_focus()
    assert enumerate_mock.call_count == 2


def test_current_task_focus_threads_the_owed_act_binding(make_agent):  # type: ignore[no-untyped-def]
    """The shim hands the flag-aware binding to the collaborator (137, step 11).

    A PR that is 0-behind, 0-conflict and thread-free while its CI is a
    concluded red carries no owned merge-block flag, so today it reads
    ``Discretionary``. With ``[loop] owed_act_binding_enabled`` on, the owed act
    the collaborator derives promotes it to owned WIP under ``CI_FAILING``;
    with the flag off the verdict is unchanged.
    """
    agent = make_agent()
    agent.evolution._host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "maestro/evolve/a": SimpleNamespace(
                number=7, created_at="2026-01-01T00:00:00Z",
            ),
        },
    )
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=dataclasses.replace(_readiness(7), ci_state="FAILING"),
    )

    agent.config.owed_act_binding_enabled = False
    assert agent._current_task_focus() == Discretionary()

    agent._open_owned_pr_states_memo = None
    agent.config.owed_act_binding_enabled = True
    assert agent._current_task_focus() == TendOwnedPR(
        pr_number=7, owned_blocker="CI_FAILING",
    )
