"""Tests for maestro.remediation_tracker — per-PR remediation repetition gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from maestro.audit_checks_phantom import (
    _ON_BRANCH_HANDOFF_MARKERS,
)
from maestro.pr_facts import MergeTruth
from maestro.pr_remediation_state import Marker, PrRemediationState
from maestro.remediation_tracker import (
    _FETCH_REQUIRED_NARRATIVES,
    _FORCED_QUIET_MARKERS,
    _UNDELIVERED_PARTIAL_MARKERS,
    BLOCKER_OWNERSHIP_EXTERNAL,
    BLOCKER_OWNERSHIP_OWNED,
    HOST_CLOSED_UNMERGED,
    MERGE_BLOCK_AWAITING_APPROVAL,
    MERGE_BLOCK_AWAITING_PUSH,
    MERGE_BLOCK_BEHIND_MAIN,
    MERGE_BLOCK_CI_FAILING,
    MERGE_BLOCK_CI_PENDING,
    MERGE_BLOCK_CONFLICTING,
    MERGE_BLOCK_NONE,
    MERGE_BLOCK_THREADS_UNRESOLVED,
    NOOP_INDICATORS,
    OWNED_BLOCKERS,
    OWNED_PUSH_BLOCKER,
    RemediationTracker,
    _strip_merge_block_outcomes,
    classify_blocker_ownership,
    classify_host_pr_terminal,
    classify_merge_block,
    classify_noop,
    classify_owned_blocker,
    classify_remediation_outcome_state,
    record_preflight_pass,
    refine_merge_block_reason,
    should_skip_routing,
)

# ──────────────────────────────────────────────────────────────────────
# push-marker set parity — PUSH_UNCONFIRMED tracks OWNED_PUSH_AVAILABLE
# ──────────────────────────────────────────────────────────────────────

# The parallel push-marker coverage sets. ``PUSH_UNCONFIRMED`` (a no-op push:
# exit 0 but origin/<branch> never advanced) is the non-delivery sibling of
# ``OWNED_PUSH_AVAILABLE`` — an owned resolution sitting un-pushed on the branch
# — so it must appear in every set the owned marker does, or the marker split
# silently drops audit/sensor coverage over exactly the false-delivery case it
# set out to close (PR #3165). The evolution ``_has_unconsumed_awaiting_push_
# verdict`` OR-chain is not an importable set; its parity is covered
# behaviourally in ``tests/test_evolution_tend.py``.
_PUSH_MARKER_SETS: list[tuple[str, tuple[str, ...] | frozenset[str]]] = [
    ("_ON_BRANCH_HANDOFF_MARKERS", _ON_BRANCH_HANDOFF_MARKERS),
    ("_FORCED_QUIET_MARKERS", _FORCED_QUIET_MARKERS),
    ("_FETCH_REQUIRED_NARRATIVES", _FETCH_REQUIRED_NARRATIVES),
    ("_UNDELIVERED_PARTIAL_MARKERS", _UNDELIVERED_PARTIAL_MARKERS),
]


class TestPushMarkerSetParity:
    """PUSH_UNCONFIRMED is registered wherever OWNED_PUSH_AVAILABLE is."""

    @pytest.mark.parametrize(
        ("name", "marker_set"),
        _PUSH_MARKER_SETS,
        ids=[name for name, _ in _PUSH_MARKER_SETS],
    )
    def test_push_unconfirmed_present_alongside_owned_push(
        self, name: str, marker_set: tuple[str, ...] | frozenset[str],
    ) -> None:
        # Guard: the owned marker anchors the set (its presence is the reason
        # PUSH_UNCONFIRMED must join). If a future edit drops OWNED here this
        # parity claim is meaningless, so assert both.
        assert Marker.OWNED_PUSH_AVAILABLE in marker_set, (
            f"{name} no longer carries OWNED_PUSH_AVAILABLE — parity anchor gone"
        )
        assert Marker.PUSH_UNCONFIRMED in marker_set, (
            f"{name} is missing PUSH_UNCONFIRMED; the marker split regresses "
            f"audit/sensor coverage over no-op-push records (PR #3165)"
        )


# ──────────────────────────────────────────────────────────────────────
# classify_noop tests
# ──────────────────────────────────────────────────────────────────────


class TestClassifyNoop:
    """Tests for the deterministic no-op substring classifier."""

    @pytest.mark.parametrize("indicator", NOOP_INDICATORS)
    def test_matches_each_indicator(self, indicator: str) -> None:
        summary = f"The PR {indicator} — nothing to do."
        assert classify_noop(summary) is True

    @pytest.mark.parametrize("indicator", NOOP_INDICATORS)
    def test_case_insensitive(self, indicator: str) -> None:
        summary = indicator.upper() + " confirmed."
        assert classify_noop(summary) is True

    def test_productive_summary_rejected(self) -> None:
        assert classify_noop("Fixed ruff lint errors in 3 files") is False

    def test_empty_string(self) -> None:
        assert classify_noop("") is False

    def test_partial_match_not_triggered(self) -> None:
        # "already" alone should not match — needs "already pass" or
        # "already resolved"
        assert classify_noop("Already working on it") is False

    def test_complex_productive_summary(self) -> None:
        summary = (
            "Resolved merge conflict in agent.py by keeping both additions. "
            "Fixed mypy error in test_agent.py."
        )
        # Contains "resolved" but not "already resolved"
        assert classify_noop(summary) is False

    def test_preflight_skip_finding_is_noop(self) -> None:
        # The zero-cost ``[preflight]`` skip finding records as ``success`` but
        # the repetition gate must count it as a no-op.
        finding = (
            "[preflight] Quality gates already pass on branch "
            "maestro/evolve/abc — remediation skipped at zero SDK cost"
        )
        assert classify_noop(finding) is True


# ──────────────────────────────────────────────────────────────────────
# classify_remediation_outcome_state tests
# ──────────────────────────────────────────────────────────────────────


class TestClassifyRemediationOutcomeState:
    """Tests for the typed post-task remediation classifier (proposal 070)."""

    def test_chosen_quiet_marker_in_summary_is_stood_down(self) -> None:
        state = classify_remediation_outcome_state(
            "[stood-down] chose silence at the fork",
            "",
            merge_block_reason=None,
        )
        assert state is PrRemediationState.STOOD_DOWN

    def test_chosen_quiet_marker_in_findings_is_stood_down(self) -> None:
        state = classify_remediation_outcome_state(
            "Resolved on branch.",
            "[stood-down] chose silence",
            merge_block_reason=None,
        )
        assert state is PrRemediationState.STOOD_DOWN

    def test_marker_match_is_case_insensitive(self) -> None:
        state = classify_remediation_outcome_state(
            "[STOOD-DOWN] chose silence",
            "",
            merge_block_reason=None,
        )
        assert state is PrRemediationState.STOOD_DOWN

    def test_merge_block_reason_without_marker_is_branch_resolved(self) -> None:
        state = classify_remediation_outcome_state(
            "Committed a real fix on the branch.",
            "Patched the failing check.",
            merge_block_reason="CI_FAILING",
        )
        assert state is PrRemediationState.BRANCH_RESOLVED

    def test_neither_signal_is_remediable(self) -> None:
        state = classify_remediation_outcome_state(
            "Committed a real fix.",
            "Patched the failing check.",
            merge_block_reason=None,
        )
        assert state is PrRemediationState.REMEDIABLE

    def test_both_signals_precedence_pins_stood_down(self) -> None:
        # Defensive: a stand-down stamps ``partial`` and never reaches the
        # success-only downgrade that sets ``merge_block_reason``, so this cannot
        # co-occur in production — the precedence ordering is pinned regardless.
        state = classify_remediation_outcome_state(
            "[stood-down] chose silence",
            "",
            merge_block_reason="CI_FAILING",
        )
        assert state is PrRemediationState.STOOD_DOWN


# ──────────────────────────────────────────────────────────────────────
# classify_merge_block tests
# ──────────────────────────────────────────────────────────────────────


class TestClassifyMergeBlock:
    """Tests for the deterministic merge-blocker classifier."""

    def test_behind_main_by_commit_count(self) -> None:
        assert (
            classify_merge_block(2, "success", "CLEAN")
            == MERGE_BLOCK_BEHIND_MAIN
        )

    def test_behind_main_by_state(self) -> None:
        # commits_behind unknown (0) but GitHub reports BEHIND.
        assert (
            classify_merge_block(0, "success", "BEHIND")
            == MERGE_BLOCK_BEHIND_MAIN
        )

    def test_behind_main_takes_precedence_over_ci(self) -> None:
        # A stale base must catch up before its CI verdict matters.
        assert (
            classify_merge_block(1, "failure", "BEHIND")
            == MERGE_BLOCK_BEHIND_MAIN
        )

    def test_conflicting_by_dirty_state(self) -> None:
        # A DIRTY base has merge conflicts; named even with green CI.
        assert (
            classify_merge_block(0, "success", "DIRTY")
            == MERGE_BLOCK_CONFLICTING
        )

    def test_conflicting_takes_precedence_over_ci(self) -> None:
        # The conflict must be resolved before any CI verdict is mergeable.
        assert (
            classify_merge_block(0, "failure", "DIRTY")
            == MERGE_BLOCK_CONFLICTING
        )

    def test_conflict_takes_precedence_over_behind(self) -> None:
        # A behind-AND-dirty branch must route to LLM conflict resolution:
        # catching up (merging main) is exactly what produces the conflict, and
        # the merge-block gate skips LLM remediation for BEHIND_MAIN (#2521).
        assert (
            classify_merge_block(1, "success", "DIRTY")
            == MERGE_BLOCK_CONFLICTING
        )

    def test_behind_main_when_behind_without_conflict(self) -> None:
        # Behind AND reported BEHIND but no DIRTY conflict → behind still wins.
        assert (
            classify_merge_block(2, "success", "BEHIND")
            == MERGE_BLOCK_BEHIND_MAIN
        )

    def test_ci_failing(self) -> None:
        assert (
            classify_merge_block(0, "failure", "CLEAN")
            == MERGE_BLOCK_CI_FAILING
        )

    def test_ci_pending_empty_conclusion(self) -> None:
        assert (
            classify_merge_block(0, "", "CLEAN") == MERGE_BLOCK_CI_PENDING
        )

    def test_ci_pending_explicit(self) -> None:
        assert (
            classify_merge_block(0, "pending", "CLEAN")
            == MERGE_BLOCK_CI_PENDING
        )

    def test_awaiting_approval(self) -> None:
        # Current and green but review still outstanding.
        assert (
            classify_merge_block(0, "success", "BLOCKED")
            == MERGE_BLOCK_AWAITING_APPROVAL
        )

    def test_none_when_clean_and_green(self) -> None:
        assert (
            classify_merge_block(0, "success", "CLEAN") == MERGE_BLOCK_NONE
        )

    def test_case_and_whitespace_insensitive(self) -> None:
        assert (
            classify_merge_block(0, "  SUCCESS ", " blocked ")
            == MERGE_BLOCK_AWAITING_APPROVAL
        )


# ──────────────────────────────────────────────────────────────────────
# classify_blocker_ownership tests
# ──────────────────────────────────────────────────────────────────────


class TestClassifyBlockerOwnership:
    """Owned (mine to reconcile/close) vs external (Tend/human) blockers."""

    def test_conflicting_is_owned(self) -> None:
        assert (
            classify_blocker_ownership(MERGE_BLOCK_CONFLICTING)
            == BLOCKER_OWNERSHIP_OWNED
        )

    def test_raw_dirty_state_is_owned(self) -> None:
        # The raw mergeable_state GitHub reports for a conflicting branch.
        assert classify_blocker_ownership("DIRTY") == BLOCKER_OWNERSHIP_OWNED

    def test_threads_unresolved_is_owned(self) -> None:
        # An un-answered AI review thread on my own PR is owned, code-clearable
        # work — answer or refute it — never an external gate (#2907).
        assert (
            classify_blocker_ownership(MERGE_BLOCK_THREADS_UNRESOLVED)
            == BLOCKER_OWNERSHIP_OWNED
        )

    def test_awaiting_approval_is_external(self) -> None:
        assert (
            classify_blocker_ownership(MERGE_BLOCK_AWAITING_APPROVAL)
            == BLOCKER_OWNERSHIP_EXTERNAL
        )

    def test_awaiting_push_is_external(self) -> None:
        assert (
            classify_blocker_ownership(MERGE_BLOCK_AWAITING_PUSH)
            == BLOCKER_OWNERSHIP_EXTERNAL
        )

    def test_behind_main_is_external(self) -> None:
        assert (
            classify_blocker_ownership(MERGE_BLOCK_BEHIND_MAIN)
            == BLOCKER_OWNERSHIP_EXTERNAL
        )

    def test_casing_and_whitespace_normalised(self) -> None:
        assert (
            classify_blocker_ownership("  conflicting  ")
            == BLOCKER_OWNERSHIP_OWNED
        )
        assert classify_blocker_ownership(" dirty ") == BLOCKER_OWNERSHIP_OWNED

    def test_unrecognised_blocker_defaults_external(self) -> None:
        assert (
            classify_blocker_ownership("SOMETHING_ELSE")
            == BLOCKER_OWNERSHIP_EXTERNAL
        )

    def test_empty_blocker_defaults_external(self) -> None:
        # The safe default preserves current routing (re-admission), so an
        # unparsed/empty blocker is never wrongly diverted out of remediation.
        assert classify_blocker_ownership("") == BLOCKER_OWNERSHIP_EXTERNAL

    def test_own_push_reclassifies_awaiting_push_as_owned(self) -> None:
        # Proposal 098: with the rollout flag on, an unpushed on-branch conflict
        # resolution is an owned next act (the agent pushes it), not external.
        assert OWNED_PUSH_BLOCKER == MERGE_BLOCK_AWAITING_PUSH
        assert (
            classify_blocker_ownership(MERGE_BLOCK_AWAITING_PUSH, own_push=True)
            == BLOCKER_OWNERSHIP_OWNED
        )

    def test_own_push_false_keeps_awaiting_push_external(self) -> None:
        # The explicit flag-off path matches the no-arg default: AWAITING_PUSH
        # stays external so the pre-098 routing is byte-identical.
        assert (
            classify_blocker_ownership(MERGE_BLOCK_AWAITING_PUSH, own_push=False)
            == BLOCKER_OWNERSHIP_EXTERNAL
        )

    def test_own_push_leaves_other_owned_codes_owned(self) -> None:
        # The flag reclassifies only AWAITING_PUSH; every code already in the
        # OWNED_BLOCKERS set stays owned under own_push=True.
        for blocker in OWNED_BLOCKERS:
            assert (
                classify_blocker_ownership(blocker, own_push=True)
                == BLOCKER_OWNERSHIP_OWNED
            )

    def test_own_push_leaves_other_external_codes_external(self) -> None:
        # AWAITING_APPROVAL (human approval) and BEHIND_MAIN (Tend's mechanical
        # catch-up) stay external even under own_push=True — only AWAITING_PUSH
        # moves.
        assert (
            classify_blocker_ownership(
                MERGE_BLOCK_AWAITING_APPROVAL, own_push=True,
            )
            == BLOCKER_OWNERSHIP_EXTERNAL
        )
        assert (
            classify_blocker_ownership(MERGE_BLOCK_BEHIND_MAIN, own_push=True)
            == BLOCKER_OWNERSHIP_EXTERNAL
        )


def _merge_truth(state: str, *, branch_landed: bool) -> MergeTruth:
    """Build a :class:`MergeTruth` for classify_host_pr_terminal branch cases."""
    return MergeTruth(
        pr_number=42,
        settled=state != "OPEN",
        state=state,  # type: ignore[arg-type]
        branch_landed=branch_landed,
        source="host_api",
        observed_at=1.0,
    )


class TestClassifyHostPrTerminal:
    """The pure human-closed-terminal reader over an already-fetched record."""

    def test_closed_unmerged_is_terminal(self) -> None:
        # CLOSED with the branch unmerged (mergedAt null) is the human's own
        # terminal — the sentinel the dequeue gate keys eviction on.
        truth = _merge_truth("CLOSED", branch_landed=False)
        assert classify_host_pr_terminal(truth) == HOST_CLOSED_UNMERGED

    def test_closed_merged_is_none(self) -> None:
        # Defensive CLOSED-and-merged shape (a host that records a mergedAt on a
        # closed PR): merged means it landed, so no terminal eviction is owed.
        truth = _merge_truth("CLOSED", branch_landed=True)
        assert classify_host_pr_terminal(truth) is None

    def test_merged_is_none(self) -> None:
        # A MERGED PR landed — the merged/landed settled arms own it, not this
        # gate.
        truth = _merge_truth("MERGED", branch_landed=True)
        assert classify_host_pr_terminal(truth) is None

    def test_open_is_none(self) -> None:
        # An OPEN PR is still owed remediation — never a terminal eviction.
        truth = _merge_truth("OPEN", branch_landed=False)
        assert classify_host_pr_terminal(truth) is None

    def test_missing_record_is_none(self) -> None:
        # Fail-safe passthrough: no observation → the PR is left remediable.
        assert classify_host_pr_terminal(None) is None


class TestClassifyOwnedBlocker:
    """A live unresolved-thread count promotes an approval/none reason to owned."""

    def test_awaiting_approval_with_open_threads_promotes(self) -> None:
        # Approval withheld while a review thread is open is owned, not external.
        assert (
            classify_owned_blocker(MERGE_BLOCK_AWAITING_APPROVAL, 1)
            == MERGE_BLOCK_THREADS_UNRESOLVED
        )

    def test_none_with_open_threads_promotes(self) -> None:
        # A clean merge state with an open thread still has owned work left.
        assert (
            classify_owned_blocker(MERGE_BLOCK_NONE, 2)
            == MERGE_BLOCK_THREADS_UNRESOLVED
        )

    def test_empty_reason_reads_as_none_and_promotes(self) -> None:
        # An empty reason is read as NONE, so an open thread promotes it too.
        assert (
            classify_owned_blocker("", 1) == MERGE_BLOCK_THREADS_UNRESOLVED
        )

    def test_awaiting_approval_zero_threads_unchanged(self) -> None:
        # No open thread: the approval wait is genuinely external, left as-is.
        assert (
            classify_owned_blocker(MERGE_BLOCK_AWAITING_APPROVAL, 0)
            == MERGE_BLOCK_AWAITING_APPROVAL
        )

    def test_none_zero_threads_unchanged(self) -> None:
        assert (
            classify_owned_blocker(MERGE_BLOCK_NONE, 0) == MERGE_BLOCK_NONE
        )

    def test_conflicting_with_open_threads_unchanged(self) -> None:
        # A conflict names a different, still-blocking gate — never promoted.
        assert (
            classify_owned_blocker(MERGE_BLOCK_CONFLICTING, 3)
            == MERGE_BLOCK_CONFLICTING
        )

    def test_behind_main_with_open_threads_unchanged(self) -> None:
        assert (
            classify_owned_blocker(MERGE_BLOCK_BEHIND_MAIN, 1)
            == MERGE_BLOCK_BEHIND_MAIN
        )

    def test_ci_failing_with_open_threads_unchanged(self) -> None:
        assert (
            classify_owned_blocker(MERGE_BLOCK_CI_FAILING, 1)
            == MERGE_BLOCK_CI_FAILING
        )

    def test_casing_and_whitespace_normalised(self) -> None:
        # A lower-case approval reason normalises before matching/promotion.
        assert (
            classify_owned_blocker("  awaiting_approval  ", 1)
            == MERGE_BLOCK_THREADS_UNRESOLVED
        )


# ──────────────────────────────────────────────────────────────────────
# RemediationTracker tests
# ──────────────────────────────────────────────────────────────────────


class TestRemediationTracker:
    """Unit tests for the tracker using mem_fs fixture."""

    def test_should_reroute_true_no_history(self, mem_fs: dict[str, str]) -> None:
        tracker = RemediationTracker(Path("/fake/data"))
        assert tracker.should_reroute(42) is True

    def test_should_reroute_true_after_one_noop_under_threshold(
        self, mem_fs: dict[str, str],
    ) -> None:
        tracker = RemediationTracker(Path("/fake/data"), threshold=2)
        tracker.record_outcome(42, noop=True)
        # Only 1 noop, threshold is 2
        assert tracker.should_reroute(42) is True

    def test_should_reroute_false_after_consecutive_noops(
        self, mem_fs: dict[str, str],
    ) -> None:
        tracker = RemediationTracker(Path("/fake/data"), threshold=2)
        tracker.record_outcome(42, noop=True)
        tracker.record_outcome(42, noop=True)
        assert tracker.should_reroute(42) is False

    def test_mixed_outcomes_reset_consecutive_counter(
        self, mem_fs: dict[str, str],
    ) -> None:
        tracker = RemediationTracker(Path("/fake/data"), threshold=2)
        tracker.record_outcome(42, noop=True)
        # A productive outcome breaks the streak
        tracker.record_outcome(42, noop=False)
        tracker.record_outcome(42, noop=True)
        # Only 1 consecutive noop (after the productive one)
        assert tracker.should_reroute(42) is True

    def test_reset_clears_history(self, mem_fs: dict[str, str]) -> None:
        tracker = RemediationTracker(Path("/fake/data"), threshold=2)
        tracker.record_outcome(42, noop=True)
        tracker.record_outcome(42, noop=True)
        assert tracker.should_reroute(42) is False
        tracker.reset(42)
        assert tracker.should_reroute(42) is True

    def test_persistence_round_trip(self, mem_fs: dict[str, str]) -> None:
        path = Path("/fake/data")
        tracker1 = RemediationTracker(path, threshold=2)
        tracker1.record_outcome(99, noop=True)
        tracker1.record_outcome(99, noop=True)

        # New tracker from same path should see persisted data
        tracker2 = RemediationTracker(path, threshold=2)
        assert tracker2.should_reroute(99) is False

    def test_configurable_threshold_3(self, mem_fs: dict[str, str]) -> None:
        tracker = RemediationTracker(Path("/fake/data"), threshold=3)
        tracker.record_outcome(10, noop=True)
        tracker.record_outcome(10, noop=True)
        # 2 noops under threshold 3 — still allowed
        assert tracker.should_reroute(10) is True
        tracker.record_outcome(10, noop=True)
        # 3 noops at threshold 3 — blocked
        assert tracker.should_reroute(10) is False

    def test_gate_disabled_always_returns_true(
        self, mem_fs: dict[str, str],
    ) -> None:
        tracker = RemediationTracker(
            Path("/fake/data"), threshold=2, enabled=False,
        )
        tracker.record_outcome(42, noop=True)
        tracker.record_outcome(42, noop=True)
        # Disabled gate should allow re-routing regardless
        assert tracker.should_reroute(42) is True

    def test_different_prs_independent(self, mem_fs: dict[str, str]) -> None:
        tracker = RemediationTracker(Path("/fake/data"), threshold=2)
        tracker.record_outcome(10, noop=True)
        tracker.record_outcome(10, noop=True)
        # PR 10 blocked, PR 20 unaffected
        assert tracker.should_reroute(10) is False
        assert tracker.should_reroute(20) is True

    def test_reset_nonexistent_pr_no_error(
        self, mem_fs: dict[str, str],
    ) -> None:
        tracker = RemediationTracker(Path("/fake/data"))
        # Should not raise
        tracker.reset(999)

    def test_corrupt_file_handled(self, mem_fs: dict[str, str]) -> None:
        path = Path("/fake/data")
        file_path = str(path / "remediation_tracker.json")
        mem_fs[file_path] = "not valid json {"
        tracker = RemediationTracker(path)
        # Should start fresh on corrupt data
        assert tracker.should_reroute(42) is True

    def test_non_dict_json_handled(self, mem_fs: dict[str, str]) -> None:
        # Valid JSON that is not an object (a list, here) starts fresh on both
        # the outcome and items maps rather than raising.
        path = Path("/fake/data")
        file_path = str(path / "remediation_tracker.json")
        mem_fs[file_path] = json.dumps([1, 2, 3])
        tracker = RemediationTracker(path)
        assert tracker.should_reroute(42) is True
        assert tracker.last_items_remaining(42) is None

    def test_preflight_skips_trip_the_gate(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Recording consecutive preflight-skip findings (each a no-op by
        # ``classify_noop``) trips the repetition gate just like any other
        # no-op streak.
        tracker = RemediationTracker(Path("/fake/data"), threshold=2)
        finding = (
            "[preflight] Quality gates already pass on branch "
            "maestro/evolve/abc — remediation skipped at zero SDK cost"
        )
        tracker.record_outcome(7, noop=classify_noop(finding))
        assert tracker.should_reroute(7) is True
        tracker.record_outcome(7, noop=classify_noop(finding))
        assert tracker.should_reroute(7) is False

    def test_persisted_format(self, mem_fs: dict[str, str]) -> None:
        path = Path("/fake/data")
        tracker = RemediationTracker(path, threshold=2)
        tracker.record_outcome(42, noop=True)
        tracker.record_outcome(42, noop=False, items_remaining=3)

        file_path = str(path / "remediation_tracker.json")
        data = json.loads(mem_fs[file_path])
        # Wrapper format: outcome lists, the per-PR items baseline, the per-PR
        # repetition-gate skip count, the per-PR merge-block deferral count, and
        # the schema version persist side by side (#2744, #2783, #2796).
        assert data == {
            "outcomes": {"42": [True, False]},
            "items_remaining": {"42": 3},
            "skips": {},
            "merge_block_skips": {},
            "schema_version": 2,
        }

    def test_record_outcome_persists_items_remaining(
        self, mem_fs: dict[str, str],
    ) -> None:
        # ``items_remaining`` is stored as the PR's baseline and read back by
        # ``last_items_remaining``; a later ``None`` leaves it untouched.
        tracker = RemediationTracker(Path("/fake/data"), threshold=2)
        assert tracker.last_items_remaining(42) is None
        tracker.record_outcome(42, noop=True, items_remaining=4)
        assert tracker.last_items_remaining(42) == 4
        tracker.record_outcome(42, noop=True)
        # No items_remaining this lap → baseline unchanged.
        assert tracker.last_items_remaining(42) == 4
        tracker.record_outcome(42, noop=False, items_remaining=1)
        assert tracker.last_items_remaining(42) == 1

    def test_items_baseline_persists_round_trip(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A fresh tracker from the same path sees the persisted items baseline.
        path = Path("/fake/data")
        tracker1 = RemediationTracker(path, threshold=2)
        tracker1.record_outcome(99, noop=True, items_remaining=5)
        tracker2 = RemediationTracker(path, threshold=2)
        assert tracker2.last_items_remaining(99) == 5

    def test_reset_clears_items_baseline(
        self, mem_fs: dict[str, str],
    ) -> None:
        # ``reset`` drops both the no-op history and the items baseline.
        tracker = RemediationTracker(Path("/fake/data"), threshold=2)
        tracker.record_outcome(42, noop=True, items_remaining=2)
        tracker.record_outcome(42, noop=True, items_remaining=2)
        assert tracker.should_reroute(42) is False
        assert tracker.last_items_remaining(42) == 2
        tracker.reset(42)
        assert tracker.should_reroute(42) is True
        assert tracker.last_items_remaining(42) is None

    def test_reset_persists_when_only_items_baseline_present(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A PR with an items baseline but no outcome list still resets (and
        # persists) — exercises the items-only branch of ``reset``.
        path = Path("/fake/data")
        tracker = RemediationTracker(path, threshold=2)
        tracker._last_items["42"] = 3
        tracker._persist()
        tracker.reset(42)
        assert tracker.last_items_remaining(42) is None
        reloaded = RemediationTracker(path, threshold=2)
        assert reloaded.last_items_remaining(42) is None

    def test_legacy_flat_format_loads(self, mem_fs: dict[str, str]) -> None:
        # A pre-#2744 flat file (PR → outcome list) still loads its outcomes;
        # the items baseline is simply absent.
        path = Path("/fake/data")
        file_path = str(path / "remediation_tracker.json")
        mem_fs[file_path] = json.dumps({"42": [True, True]})
        tracker = RemediationTracker(path, threshold=2)
        assert tracker.should_reroute(42) is False
        assert tracker.last_items_remaining(42) is None

    def test_load_ignores_non_int_items_baseline(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A wrapper file whose items map carries a bool or non-int value drops
        # that entry rather than treating it as a count.
        path = Path("/fake/data")
        file_path = str(path / "remediation_tracker.json")
        mem_fs[file_path] = json.dumps({
            "outcomes": {"42": [True]},
            "items_remaining": {"42": True, "43": "x", "44": 6},
        })
        tracker = RemediationTracker(path, threshold=2)
        assert tracker.last_items_remaining(42) is None
        assert tracker.last_items_remaining(43) is None
        assert tracker.last_items_remaining(44) == 6

    def test_interleaved_noops_accumulate_per_pr_independently(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Reproduction probe: interleaved FIFO rotation must not reset a
        single PR's no-op streak.

        Premise under investigation (plan "Per-PR consecutive-no-op counting"):
        that the accumulator is *global-consecutive*, so two PRs rotating
        through the FIFO interrupt each other and no PR ever reaches K
        consecutive no-ops. Under that buggy logic the Kth interleaved no-op
        on a given PR would NOT trip the gate.

        This test interleaves ``threshold`` preflight-skip no-ops across two
        distinct PRs and asserts each PR's streak accumulates independently
        and trips at its own Kth no-op. If it passes against current code,
        the accumulator already keys per-PR and the premise is false.
        """
        tracker = RemediationTracker(Path("/fake/data"), threshold=2)
        finding = (
            "[preflight] Quality gates already pass on branch "
            "maestro/evolve/abc — remediation skipped at zero SDK cost"
        )
        noop = classify_noop(finding)

        # Round 1: one no-op on each PR (interleaved). Neither has reached
        # the threshold of 2, so both still re-route.
        tracker.record_outcome(2362, noop=noop)
        tracker.record_outcome(2348, noop=noop)
        assert tracker.should_reroute(2362) is True
        assert tracker.should_reroute(2348) is True

        # Round 2: a second no-op on each PR (still interleaved). Under a
        # global-consecutive accumulator the alternation would keep resetting
        # each streak to length 1; under per-PR keying each independently
        # reaches 2 consecutive no-ops and trips.
        tracker.record_outcome(2362, noop=noop)
        tracker.record_outcome(2348, noop=noop)
        assert tracker.should_reroute(2362) is False
        assert tracker.should_reroute(2348) is False

    def test_noop_count_trailing_consecutive(
        self, mem_fs: dict[str, str],
    ) -> None:
        # ``noop_count`` reports the trailing consecutive no-op streak — the
        # "stuck" depth a durable hand-up records (#2783).
        tracker = RemediationTracker(Path("/fake/data"), threshold=2)
        assert tracker.noop_count(42) == 0
        tracker.record_outcome(42, noop=True)
        tracker.record_outcome(42, noop=True)
        assert tracker.noop_count(42) == 2
        # A productive lap resets the trailing streak to zero...
        tracker.record_outcome(42, noop=False)
        assert tracker.noop_count(42) == 0
        # ...and a fresh no-op counts only from there.
        tracker.record_outcome(42, noop=True)
        assert tracker.noop_count(42) == 1

    def test_record_skip_increments_and_persists(
        self, mem_fs: dict[str, str],
    ) -> None:
        # ``record_skip`` accrues the per-PR repetition-gate suppression count
        # (#2783) and persists it across tracker instances.
        path = Path("/fake/data")
        tracker = RemediationTracker(path, threshold=2)
        assert tracker.skip_count(42) == 0
        assert tracker.record_skip(42) == 1
        assert tracker.record_skip(42) == 2
        assert tracker.skip_count(42) == 2
        # Independent per PR.
        assert tracker.skip_count(43) == 0
        # Round-trips through a fresh instance from the same path.
        reloaded = RemediationTracker(path, threshold=2)
        assert reloaded.skip_count(42) == 2

    def test_reset_clears_skip_count(self, mem_fs: dict[str, str]) -> None:
        # A branch-state change resets the skip count alongside the no-op list
        # so delivered progress earns the lap budget back (#2783).
        tracker = RemediationTracker(Path("/fake/data"), threshold=2)
        tracker.record_skip(42)
        tracker.record_skip(42)
        assert tracker.skip_count(42) == 2
        tracker.reset(42)
        assert tracker.skip_count(42) == 0

    def test_reset_persists_when_only_skip_count_present(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A PR with only a skip count (no outcome list, no items baseline) still
        # resets and persists — exercises the skip-only branch of ``reset``.
        path = Path("/fake/data")
        tracker = RemediationTracker(path, threshold=2)
        tracker.record_skip(42)
        tracker.reset(42)
        assert tracker.skip_count(42) == 0
        reloaded = RemediationTracker(path, threshold=2)
        assert reloaded.skip_count(42) == 0

    def test_load_ignores_non_int_skip_count(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A wrapper file whose skips map carries a bool or non-int value drops
        # that entry rather than treating it as a count (mirrors the items map).
        path = Path("/fake/data")
        file_path = str(path / "remediation_tracker.json")
        mem_fs[file_path] = json.dumps({
            "outcomes": {"42": [True]},
            "skips": {"42": True, "43": "x", "44": 5},
        })
        tracker = RemediationTracker(path, threshold=2)
        assert tracker.skip_count(42) == 0
        assert tracker.skip_count(43) == 0
        assert tracker.skip_count(44) == 5

    def test_legacy_flat_format_has_zero_skip_count(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A pre-#2783 flat file carries no skips map; the count starts at zero.
        path = Path("/fake/data")
        file_path = str(path / "remediation_tracker.json")
        mem_fs[file_path] = json.dumps({"42": [True, True]})
        tracker = RemediationTracker(path, threshold=2)
        assert tracker.skip_count(42) == 0

    # ── #2796: merge-block skips split out of should_reroute ──────────────

    def test_merge_block_record_does_not_feed_should_reroute(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A merge-block deferral lands in its own counter, never the
        # ``should_reroute`` outcome list — two of them do not exile the PR.
        tracker = RemediationTracker(Path("/fake/data"), threshold=2)
        tracker.record_outcome(42, noop=True, merge_block=True)
        tracker.record_outcome(42, noop=True, merge_block=True)
        assert tracker.should_reroute(42) is True
        assert tracker.merge_block_skip_count(42) == 2
        # The outcome list the gate reads stays empty.
        assert tracker.noop_count(42) == 0

    def test_genuine_noop_still_feeds_should_reroute(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A genuinely attempted-but-fruitless lap still trips the gate; the
        # merge-block counter stays at zero (the #2744 guard is intact).
        tracker = RemediationTracker(Path("/fake/data"), threshold=2)
        tracker.record_outcome(42, noop=True)
        tracker.record_outcome(42, noop=True)
        assert tracker.should_reroute(42) is False
        assert tracker.merge_block_skip_count(42) == 0

    def test_reset_clears_merge_block_count(
        self, mem_fs: dict[str, str],
    ) -> None:
        tracker = RemediationTracker(Path("/fake/data"), threshold=2)
        tracker.record_outcome(42, noop=True, merge_block=True)
        tracker.record_outcome(42, noop=True, merge_block=True)
        assert tracker.merge_block_skip_count(42) == 2
        tracker.reset(42)
        assert tracker.merge_block_skip_count(42) == 0

    def test_reset_persists_when_only_merge_block_count_present(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A PR with only a merge-block count (no outcome list, items, or skips)
        # still resets and persists — exercises that branch of ``reset``.
        path = Path("/fake/data")
        tracker = RemediationTracker(path, threshold=2)
        tracker.record_outcome(7, noop=True, merge_block=True)
        tracker.reset(7)
        assert tracker.merge_block_skip_count(7) == 0
        reloaded = RemediationTracker(path, threshold=2)
        assert reloaded.merge_block_skip_count(7) == 0

    def test_merge_block_count_persists_round_trip(
        self, mem_fs: dict[str, str],
    ) -> None:
        path = Path("/fake/data")
        tracker1 = RemediationTracker(path, threshold=2)
        tracker1.record_outcome(99, noop=True, merge_block=True)
        tracker2 = RemediationTracker(path, threshold=2)
        assert tracker2.merge_block_skip_count(99) == 1
        # And it never poisoned the re-route gate.
        assert tracker2.should_reroute(99) is True

    def test_load_ignores_non_int_merge_block_count(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A wrapper file whose merge_block_skips map carries a bool/non-int
        # value drops that entry (mirrors the items and skips maps).
        path = Path("/fake/data")
        file_path = str(path / "remediation_tracker.json")
        mem_fs[file_path] = json.dumps({
            "outcomes": {"42": [False]},
            "merge_block_skips": {"42": True, "43": "x", "44": 5},
            "schema_version": 2,
        })
        tracker = RemediationTracker(path, threshold=2)
        assert tracker.merge_block_skip_count(42) == 0
        assert tracker.merge_block_skip_count(43) == 0
        assert tracker.merge_block_skip_count(44) == 5

    # ── proposal 103: evict_settled forgets a settled PR's records ────────

    def test_evict_settled_purges_all_four_axes(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A settlement "forget this PR" event clears the PR from every axis and
        # reports that it removed a trace, so the on-settlement reconciler can
        # tell a real clear from a no-op. The eviction is durable across a fresh
        # tracker from the same path.
        path = Path("/fake/data")
        tracker = RemediationTracker(path, threshold=2)
        tracker.record_outcome(42, noop=True, items_remaining=3)
        tracker.record_outcome(42, noop=True)
        tracker.record_skip(42)
        tracker.record_outcome(42, noop=True, merge_block=True)
        assert tracker.should_reroute(42) is False
        assert tracker.last_items_remaining(42) == 3
        assert tracker.skip_count(42) == 1
        assert tracker.merge_block_skip_count(42) == 1

        assert tracker.evict_settled(42) is True

        assert tracker.should_reroute(42) is True
        assert tracker.last_items_remaining(42) is None
        assert tracker.skip_count(42) == 0
        assert tracker.merge_block_skip_count(42) == 0
        # Durable: a fresh tracker from the same path sees the PR forgotten.
        reloaded = RemediationTracker(path, threshold=2)
        assert reloaded.should_reroute(42) is True
        assert reloaded.skip_count(42) == 0
        assert reloaded.merge_block_skip_count(42) == 0

    def test_evict_settled_absent_pr_returns_false_and_does_not_persist(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Evicting a PR the tracker never recorded removes nothing, returns
        # False, and writes no file — the persist-only-on-change guard that keeps
        # a recurring settlement drive over an absent PR from touching disk.
        path = Path("/fake/data")
        tracker = RemediationTracker(path, threshold=2)
        assert tracker.evict_settled(999) is False
        assert str(path / "remediation_tracker.json") not in mem_fs

    def test_evict_settled_leaves_other_prs_untouched(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Only the settled PR is forgotten; a sibling PR's records survive.
        tracker = RemediationTracker(Path("/fake/data"), threshold=2)
        tracker.record_outcome(42, noop=True)
        tracker.record_outcome(42, noop=True)
        tracker.record_outcome(43, noop=True)
        tracker.record_outcome(43, noop=True)
        assert tracker.evict_settled(42) is True
        assert tracker.should_reroute(42) is True
        # PR 43's history is intact.
        assert tracker.should_reroute(43) is False


def _mb_task(
    pr: int, *, outcome: str = "partial", marked: bool = True,
) -> dict[str, object]:
    """Build a task-history record shaped like a ``gate_merge_block`` lap."""
    summary = (
        f"{Marker.MERGE_BLOCK} PR #{pr} remediation skipped at zero SDK cost"
        if marked
        else f"PR #{pr} remediation produced a fix"
    )
    return {
        "goal": f"Remediate PR #{pr}: fix quality gates",
        "outcome": outcome,
        "result_summary": summary,
    }


class TestStripMergeBlockOutcomes:
    """Pure-function tests for the #2796 merge-block outcome reconciliation."""

    def test_removes_up_to_merge_block_count_trailing_trues(self) -> None:
        tasks = [_mb_task(2736), _mb_task(2736)]
        # Both no-ops are merge-block-origin → the list empties and is dropped.
        assert _strip_merge_block_outcomes({"2736": [True, True]}, tasks) == {}

    def test_preserves_history_when_no_merge_block_laps(self) -> None:
        # No merge-block laps → nothing removed; the PR stays blocked.
        assert _strip_merge_block_outcomes({"2736": [True, True]}, []) == {
            "2736": [True, True],
        }

    def test_caps_removal_and_keeps_genuine_entries(self) -> None:
        # One merge-block lap removes one trailing True; a productive False and
        # an earlier genuine True survive.
        tasks = [_mb_task(10)]
        assert _strip_merge_block_outcomes(
            {"10": [True, False, True]}, tasks,
        ) == {"10": [True, False]}

    def test_budget_skips_false_entries_to_reach_earlier_true(self) -> None:
        # Two merge-block laps consume the two Trues, stepping over the False.
        tasks = [_mb_task(10), _mb_task(10)]
        assert _strip_merge_block_outcomes(
            {"10": [True, False, True]}, tasks,
        ) == {"10": [False]}

    def test_non_partial_marked_record_not_counted(self) -> None:
        # A success record carrying the marker is not a deferral lap.
        tasks = [_mb_task(10, outcome="success")]
        assert _strip_merge_block_outcomes({"10": [True, True]}, tasks) == {
            "10": [True, True],
        }

    def test_unmarked_partial_record_not_counted(self) -> None:
        # A genuinely-attempted partial lacking the marker is preserved history.
        tasks = [_mb_task(10, marked=False)]
        assert _strip_merge_block_outcomes({"10": [True, True]}, tasks) == {
            "10": [True, True],
        }

    def test_title_fallback_and_non_string_summary(self) -> None:
        # ``goal`` missing → ``title`` fallback counts; a non-string summary is
        # skipped rather than crashing.
        tasks: list[dict[str, object]] = [
            {
                "title": "Remediate PR #10: fix",
                "outcome": "partial",
                "result_summary": f"{Marker.MERGE_BLOCK} skipped",
            },
            {
                "goal": "Remediate PR #10: fix",
                "outcome": "partial",
                "result_summary": 123,
            },
        ]
        assert _strip_merge_block_outcomes({"10": [True, True]}, tasks) == {
            "10": [True],
        }

    def test_non_remediation_goal_ignored(self) -> None:
        tasks: list[dict[str, object]] = [
            {"goal": "Build a thing", "outcome": "partial",
             "result_summary": f"{Marker.MERGE_BLOCK} x"},
            {"outcome": "partial", "result_summary": "no goal at all"},
        ]
        assert _strip_merge_block_outcomes({"10": [True, True]}, tasks) == {
            "10": [True, True],
        }


class TestMergeBlockReconciliation:
    """Load-time one-time #2796 reconciliation of conflated history."""

    def _seed(
        self,
        mem_fs: dict[str, str],
        *,
        outcomes: dict[str, list[bool]],
        tasks: list[dict[str, object]],
        version: int | None = None,
    ) -> Path:
        path = Path("/fake/data")
        wrapper: dict[str, object] = {"outcomes": outcomes}
        if version is not None:
            wrapper["schema_version"] = version
        mem_fs[str(path / "remediation_tracker.json")] = json.dumps(wrapper)
        mem_fs[str(path / "task_history.jsonl")] = "".join(
            json.dumps(t) + "\n" for t in tasks
        )
        return path

    def test_frees_pr_poisoned_only_by_merge_block_skips(
        self, mem_fs: dict[str, str],
    ) -> None:
        path = self._seed(
            mem_fs,
            outcomes={"2736": [True, True]},
            tasks=[_mb_task(2736), _mb_task(2736)],
        )
        tracker = RemediationTracker(path, threshold=2)
        assert tracker.should_reroute(2736) is True

    def test_leaves_genuine_noop_pr_blocked(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Poisoned outcomes but NO merge-block task history → stays blocked.
        path = self._seed(
            mem_fs, outcomes={"2736": [True, True]}, tasks=[],
        )
        tracker = RemediationTracker(path, threshold=2)
        assert tracker.should_reroute(2736) is False

    def test_migration_is_idempotent(self, mem_fs: dict[str, str]) -> None:
        path = self._seed(
            mem_fs,
            outcomes={"2736": [True, True]},
            tasks=[_mb_task(2736), _mb_task(2736)],
        )
        first = RemediationTracker(path, threshold=2)
        assert first.should_reroute(2736) is True
        # A fresh genuine no-op streak recorded *after* the migration must not
        # be clawed back: the merge-block laps still sit in task_history, so a
        # re-running migration would strip them — but the version bump prevents
        # the second construction from re-running it.
        first.record_outcome(2736, noop=True)
        first.record_outcome(2736, noop=True)
        assert first.should_reroute(2736) is False
        second = RemediationTracker(path, threshold=2)
        assert second.should_reroute(2736) is False

    def test_fresh_tracker_skips_migration_and_writes_nothing(
        self, mem_fs: dict[str, str],
    ) -> None:
        # No on-disk file → version 0 → migration skipped, nothing persisted
        # until the first real record.
        path = Path("/fake/data")
        RemediationTracker(path, threshold=2)
        assert str(path / "remediation_tracker.json") not in mem_fs

    def test_version_2_file_not_re_migrated(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An already-migrated file is left untouched even with poisoned-looking
        # outcomes and merge-block laps in task_history.
        path = self._seed(
            mem_fs,
            outcomes={"2736": [True, True]},
            tasks=[_mb_task(2736), _mb_task(2736)],
            version=2,
        )
        tracker = RemediationTracker(path, threshold=2)
        assert tracker.should_reroute(2736) is False


# ──────────────────────────────────────────────────────────────────────
# Preflight-pass SHA gate tests
# ──────────────────────────────────────────────────────────────────────


class TestPreflightPassRouting:
    """Tests for the preflight-pass SHA routing skip (#2348)."""

    def test_record_preflight_pass_and_skip(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Recording a PR's passing head SHA makes a same-SHA route a no-op.
        data_dir = Path("/fake/data")
        record_preflight_pass(2348, "abc123", data_dir)
        assert should_skip_routing(2348, "abc123", data_dir) is True

    def test_new_sha_bypasses_skip(self, mem_fs: dict[str, str]) -> None:
        # A head SHA that advanced past the recorded pass re-routes.
        data_dir = Path("/fake/data")
        record_preflight_pass(2348, "aaa111", data_dir)
        assert should_skip_routing(2348, "bbb222", data_dir) is False

    def test_preflight_passes_persistence_roundtrip(
        self, mem_fs: dict[str, str],
    ) -> None:
        # State written by one call survives to a later read (on-disk ledger).
        data_dir = Path("/fake/data")
        record_preflight_pass(2348, "deadbeef", data_dir)
        file_path = str(data_dir / "preflight_passes.json")
        assert json.loads(mem_fs[file_path]) == {"2348": "deadbeef"}
        # A fresh lookup with no in-memory state still reads the persisted SHA.
        assert should_skip_routing(2348, "deadbeef", data_dir) is True

    def test_record_overwrites_prior_sha(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Only the most recent passing SHA matters; the prior one stops
        # matching once the head advances and re-passes.
        data_dir = Path("/fake/data")
        record_preflight_pass(2348, "old", data_dir)
        record_preflight_pass(2348, "new", data_dir)
        assert should_skip_routing(2348, "old", data_dir) is False
        assert should_skip_routing(2348, "new", data_dir) is True

    def test_empty_head_sha_never_skips(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An unresolvable head ("" ) must never suppress remediation.
        data_dir = Path("/fake/data")
        record_preflight_pass(2348, "abc123", data_dir)
        assert should_skip_routing(2348, "", data_dir) is False

    def test_unknown_pr_does_not_skip(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A PR with no recorded pass always routes.
        assert should_skip_routing(999, "abc123", Path("/fake/data")) is False

    def test_distinct_prs_independent(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        record_preflight_pass(2348, "abc", data_dir)
        record_preflight_pass(2362, "def", data_dir)
        assert should_skip_routing(2348, "abc", data_dir) is True
        assert should_skip_routing(2362, "def", data_dir) is True
        assert should_skip_routing(2348, "def", data_dir) is False

    def test_corrupt_ledger_handled(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        mem_fs[str(data_dir / "preflight_passes.json")] = "not valid json {"
        # Corrupt ledger reads as empty — never skips, and a record recovers.
        assert should_skip_routing(2348, "abc", data_dir) is False
        record_preflight_pass(2348, "abc", data_dir)
        assert should_skip_routing(2348, "abc", data_dir) is True

    def test_non_dict_ledger_handled(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        mem_fs[str(data_dir / "preflight_passes.json")] = "[1, 2, 3]"
        assert should_skip_routing(2348, "abc", data_dir) is False

    def test_non_string_values_filtered(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        mem_fs[str(data_dir / "preflight_passes.json")] = json.dumps(
            {"2348": 123, "2362": "valid"},
        )
        # Non-str value is dropped on load; str value survives.
        assert should_skip_routing(2348, "123", data_dir) is False
        assert should_skip_routing(2362, "valid", data_dir) is True


# ──────────────────────────────────────────────────────────────────────
# Unlanded-remediation detector tests — git-merge-truth ground truth
# ──────────────────────────────────────────────────────────────────────


def _seed_history(
    mem_fs: dict[str, str],
    data_dir: Path,
    tasks: list[dict[str, object]],
) -> None:
    """Write *tasks* as ``task_history.jsonl`` lines into the in-memory fs."""
    mem_fs[str(data_dir / "task_history.jsonl")] = "".join(
        json.dumps(t) + "\n" for t in tasks
    )


# --- refine_merge_block_reason: host/VCS refinements (#2521, #4096, #2457) ---


@pytest.mark.parametrize(
    ("reason", "mergeable", "conclusion", "unpushed", "expected"),
    [
        (MERGE_BLOCK_NONE, "CONFLICTING", "success", True, MERGE_BLOCK_CONFLICTING),
        (MERGE_BLOCK_BEHIND_MAIN, "CONFLICTING", "failure", False, MERGE_BLOCK_CONFLICTING),
        (MERGE_BLOCK_BEHIND_MAIN, "MERGEABLE", "failure", False, MERGE_BLOCK_CI_FAILING),
        (MERGE_BLOCK_BEHIND_MAIN, "MERGEABLE", " FAILURE ", False, MERGE_BLOCK_CI_FAILING),
        (MERGE_BLOCK_BEHIND_MAIN, "MERGEABLE", "failure", True, MERGE_BLOCK_BEHIND_MAIN),
        (MERGE_BLOCK_BEHIND_MAIN, "MERGEABLE", "pending", False, MERGE_BLOCK_BEHIND_MAIN),
        (MERGE_BLOCK_BEHIND_MAIN, "MERGEABLE", "", False, MERGE_BLOCK_BEHIND_MAIN),
        (MERGE_BLOCK_NONE, "MERGEABLE", "failure", True, MERGE_BLOCK_AWAITING_PUSH),
        (MERGE_BLOCK_NONE, "MERGEABLE", "success", False, MERGE_BLOCK_NONE),
        (MERGE_BLOCK_BEHIND_MAIN, "MERGEABLE", "success", True, MERGE_BLOCK_BEHIND_MAIN),
    ],
)
def test_refine_merge_block_reason_table(
    reason: str, mergeable: str, conclusion: str, unpushed: bool, expected: str,
) -> None:
    """Conflict outranks the red-head refinement, which yields to an unpushed
    fix and outranks the push refinement; each fires only where it names."""
    refined = refine_merge_block_reason(
        reason, mergeable=mergeable, check_conclusion=conclusion,
        unpushed_or_missing=unpushed,
    )
    assert refined == expected


def test_refine_merge_block_reason_omitted_conclusion_never_ci_failing() -> None:
    """The readiness observer omits the conclusion; BEHIND_MAIN stands."""
    refined = refine_merge_block_reason(
        MERGE_BLOCK_BEHIND_MAIN, mergeable="MERGEABLE",
    )
    assert refined == MERGE_BLOCK_BEHIND_MAIN


def test_refine_merge_block_reason_leaves_other_gates() -> None:
    """A verdict outside NONE/BEHIND_MAIN is never refined."""
    refined = refine_merge_block_reason(
        MERGE_BLOCK_CI_PENDING, mergeable="CONFLICTING",
        check_conclusion="failure", unpushed_or_missing=True,
    )
    assert refined == MERGE_BLOCK_CI_PENDING
