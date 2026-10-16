"""Unit tests for the shared :mod:`maestro.pr_facts` timestamped-observation base.

Exercises :meth:`TimestampedObservation.is_fresh` directly on a minimal frozen
subclass that attaches the mixin the same way :class:`maestro.pr_facts.MergeTruth`
and :class:`maestro.pr_facts.PRReadiness` do — declaring its own ``observed_at``
field while inheriting the freshness test. The ``test_is_fresh_*`` cases in
``tests/test_world_state.py`` and ``tests/test_pr_readiness.py`` remain as parity
guards proving the inherited method behaves identically through each real subclass.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from maestro import consciousness, remediation_tracker
from maestro.pr_facts import (
    _STALLED_REMEDIATION_LAP_CAP,
    MERGE_BLOCK_AWAITING_APPROVAL,
    MERGE_BLOCK_AWAITING_REVIEW,
    MERGE_BLOCK_BEHIND_MAIN,
    MERGE_BLOCK_CI_FAILING,
    MERGE_BLOCK_CI_RUNNING,
    MERGE_BLOCK_CONFLICTING,
    MERGE_BLOCK_NONE,
    RESTATEMENT_THRESHOLD,
    CIState,
    PRReadiness,
    TimestampedObservation,
    classify_merge_blocker_label,
    compute_owned_writable_branches,
    verify_push_advanced_remote_head,
)
from maestro.vcs import evolution_branch, task_branch


def _readiness(
    *, ci_state: CIState, awaiting_approval: bool = False,
) -> PRReadiness:
    """Build a minimal :class:`PRReadiness` fixture for the label classifier.

    Only ``ci_state`` and ``awaiting_approval`` steer
    :func:`classify_merge_blocker_label`; every other field is filled with a
    benign not-landed default so the frozen dataclass constructs.
    """
    return PRReadiness(
        pr_number=7,
        branch_landed=False,
        branch_behind=False,
        branch_conflicting=False,
        ci_state=ci_state,
        unresolved_threads=0,
        unaddressed_overflow=0,
        awaiting_approval=awaiting_approval,
        ready_and_waiting=False,
        source="host_api",
        observed_at=0.0,
    )


@dataclass(frozen=True)
class _Observation(TimestampedObservation):
    """A minimal frozen fact attaching the mixin exactly as the real facts do."""

    observed_at: float


def test_is_fresh_within_ttl() -> None:
    """An observation younger than the TTL is fresh and may author a verdict."""
    obs = _Observation(observed_at=100.0)
    assert obs.is_fresh(now=150.0, ttl_seconds=300) is True


def test_is_fresh_past_ttl_is_stale() -> None:
    """An observation older than the TTL is stale — the chokepoint re-observes."""
    obs = _Observation(observed_at=100.0)
    assert obs.is_fresh(now=500.0, ttl_seconds=300) is False


def test_is_fresh_exactly_at_ttl_boundary_is_stale() -> None:
    """The boundary is exclusive: an age equal to the TTL is already stale."""
    obs = _Observation(observed_at=100.0)
    assert obs.is_fresh(now=400.0, ttl_seconds=300) is False


def test_is_fresh_zero_ttl_always_stale() -> None:
    """``ttl_seconds == 0`` forces a fresh observation on every call."""
    obs = _Observation(observed_at=100.0)
    assert obs.is_fresh(now=100.0, ttl_seconds=0) is False


def test_is_fresh_clock_not_advanced_is_fresh() -> None:
    """Within-iteration reuse (clock unmoved) keeps the observation fresh."""
    obs = _Observation(observed_at=100.0)
    assert obs.is_fresh(now=100.0, ttl_seconds=300) is True


def test_merge_block_reason_codes_match_remediation_tracker() -> None:
    """The pr_facts-local ``MERGE_BLOCK_*`` copies equal remediation_tracker's canon.

    ``observe_pr_readiness`` derives its branch/approval flags by comparing an
    injected reason string against ``pr_facts``'s four ``MERGE_BLOCK_*`` codes.
    Those are independent string-literal copies because ``remediation_tracker``
    imports its blocker vocabulary *from* ``pr_facts`` — so ``pr_facts`` cannot
    import the canonical codes back without a cycle and must stay a leaf. This
    agreement test pins the two homes together so the copies can never silently
    drift from the canonical values.
    """
    assert MERGE_BLOCK_BEHIND_MAIN == remediation_tracker.MERGE_BLOCK_BEHIND_MAIN
    assert MERGE_BLOCK_CONFLICTING == remediation_tracker.MERGE_BLOCK_CONFLICTING
    assert (
        MERGE_BLOCK_AWAITING_APPROVAL
        == remediation_tracker.MERGE_BLOCK_AWAITING_APPROVAL
    )
    assert MERGE_BLOCK_NONE == remediation_tracker.MERGE_BLOCK_NONE
    assert MERGE_BLOCK_CI_FAILING == remediation_tracker.MERGE_BLOCK_CI_FAILING
    assert MERGE_BLOCK_CI_RUNNING == remediation_tracker.MERGE_BLOCK_CI_RUNNING
    assert (
        MERGE_BLOCK_AWAITING_REVIEW
        == remediation_tracker.MERGE_BLOCK_AWAITING_REVIEW
    )


# ---------------------------------------------------------------------------
# classify_merge_blocker_label — the CI/review split (proposal: honest label).
# Pure over an injected PRReadiness fact and a classify_ci_conclusions verdict:
# every case passes plain values and asserts the returned label, no I/O.
# ---------------------------------------------------------------------------


def test_label_ci_failing_from_per_check_failure() -> None:
    """A concretely-failing per-check verdict yields CI_FAILING.

    Even when the aggregate rollup ``ci_state`` still reads PENDING (a timing
    skew between the two reads), a ``FAILING`` per-check verdict names a real fix
    target and wins.
    """
    label = classify_merge_blocker_label(
        _readiness(ci_state="PENDING"), "FAILING",
    )
    assert label == MERGE_BLOCK_CI_FAILING


def test_label_ci_failing_from_rollup_state() -> None:
    """A FAILING CI rollup yields CI_FAILING even when no per-check names it."""
    label = classify_merge_blocker_label(
        _readiness(ci_state="FAILING"), "RUNNING",
    )
    assert label == MERGE_BLOCK_CI_FAILING


def test_label_ci_running_from_in_progress_checks() -> None:
    """>=1 in-progress check with 0 concluded-failing yields CI_RUNNING."""
    label = classify_merge_blocker_label(
        _readiness(ci_state="PENDING"), "RUNNING",
    )
    assert label == MERGE_BLOCK_CI_RUNNING


def test_label_awaiting_review_when_green_and_unapproved() -> None:
    """All checks green + review not approved yields AWAITING_REVIEW, not a CI wait."""
    label = classify_merge_blocker_label(
        _readiness(ci_state="GREEN", awaiting_approval=True), "GREEN",
    )
    assert label == MERGE_BLOCK_AWAITING_REVIEW


def test_label_no_blocker_when_green_and_approved() -> None:
    """The boundary: all checks green + review approved yields no blocker (NONE)."""
    label = classify_merge_blocker_label(
        _readiness(ci_state="GREEN", awaiting_approval=False), "GREEN",
    )
    assert label == MERGE_BLOCK_NONE


def test_label_awaiting_review_when_no_ci_and_unapproved() -> None:
    """No CI configured + review not approved yields AWAITING_REVIEW, not a CI wait.

    ``ci_state == "NONE"`` (no checks exist) can never be a CI wait: a review-only
    PR must not fall through to ``CI_RUNNING`` and assert a queued check that does
    not exist. ``ci_conclusions`` reads ``RUNNING`` because an empty check-run
    sequence classifies as running, yet the honest label is the review wait.
    """
    label = classify_merge_blocker_label(
        _readiness(ci_state="NONE", awaiting_approval=True), "RUNNING",
    )
    assert label == MERGE_BLOCK_AWAITING_REVIEW


def test_label_no_blocker_when_no_ci_and_approved() -> None:
    """No CI configured + review approved yields no blocker (NONE), not CI_RUNNING."""
    label = classify_merge_blocker_label(
        _readiness(ci_state="NONE", awaiting_approval=False), "RUNNING",
    )
    assert label == MERGE_BLOCK_NONE


def test_restatement_threshold_matches_consciousness() -> None:
    """The pr_facts-local ``RESTATEMENT_THRESHOLD`` copy equals consciousness's canon.

    ``classify_pr_disposability`` defaults its Jaccard ``threshold`` argument to
    ``pr_facts``'s copy-pinned ``RESTATEMENT_THRESHOLD``. Its canonical home is
    ``consciousness.RESTATEMENT_THRESHOLD``, but importing it (or ``consciousness``
    itself) at module-load time would drag that module's dependency tree into this
    leaf base and form a cycle — the same reason the ``MERGE_BLOCK_*`` codes are
    copied, not imported. This agreement test pins the two homes together so the
    copy can never silently drift from the canonical value.
    """
    assert RESTATEMENT_THRESHOLD == consciousness.RESTATEMENT_THRESHOLD


def test_stalled_remediation_lap_cap_matches_agent() -> None:
    """The pr_facts-local stall-cap copy equals ``agent``'s canonical value.

    ``assess_pr_salvage``'s behind-budget disengage arm no longer disposes on
    distance alone (proposal 099): it gates on the same-base failure streak
    reaching ``pr_facts``'s copy-pinned ``_STALLED_REMEDIATION_LAP_CAP``. Its
    canonical home is ``loop.pr_remediation.STALLED_REMEDIATION_LAP_CAP``, but importing
    ``agent`` at module-load would drag the loop's heavy dependency tree into this
    leaf base and form a cycle — the same reason ``RESTATEMENT_THRESHOLD`` and the
    ``MERGE_BLOCK_*`` codes are copied, not imported. This agreement test pins the
    two homes together so the copy can never silently drift.
    """
    from maestro.loop.pr_remediation import STALLED_REMEDIATION_LAP_CAP

    assert _STALLED_REMEDIATION_LAP_CAP == STALLED_REMEDIATION_LAP_CAP


# ---------------------------------------------------------------------------
# Owned-writable-branch oracle (proposal 093, step 1). The oracle is a pure
# function taking all facts as parameters, so every test passes plain
# strings/iterables and asserts on the returned ``frozenset`` (no ``mem_fs``, no
# async marker, no network). Branch names are built with the canonical
# :mod:`maestro.vcs` builders so the tests track the real branch shapes.
# Relocated unchanged from the retired ``tests/test_owned_branch_oracle.py``.
# ---------------------------------------------------------------------------


def test_confirmed_evolve_branch_is_writable() -> None:
    """An evolve branch in both the live open-PR set and the ledger is owned."""
    branch = evolution_branch("abc123")
    result = compute_owned_writable_branches(
        task_branch("run1"), [branch], [branch],
    )
    assert branch in result


def test_run_branch_is_writable() -> None:
    """The run's own current branch is always owned-writable."""
    result = compute_owned_writable_branches(task_branch("run1"), [], [])
    assert result == frozenset({task_branch("run1")})


@pytest.mark.parametrize("protected", ["main", "master"])
def test_protected_ref_as_current_branch_never_writable(protected: str) -> None:
    """``main``/``master`` passed as the run branch are subtracted out."""
    result = compute_owned_writable_branches(protected, [], [])
    assert result == frozenset()


@pytest.mark.parametrize("protected", ["main", "master"])
def test_protected_ref_confirmed_never_writable(protected: str) -> None:
    """``main``/``master`` confirmed by both inputs are subtracted out.

    They never carry the evolve prefix, so they are already excluded by
    provenance; the unconditional subtraction is defense in depth.
    """
    result = compute_owned_writable_branches(
        task_branch("run1"), [protected], [protected],
    )
    assert protected not in result


def test_ledger_only_branch_excluded() -> None:
    """A branch in the ledger but absent from the live open-PR set is excluded."""
    branch = evolution_branch("abc123")
    result = compute_owned_writable_branches(task_branch("run1"), [], [branch])
    assert branch not in result


def test_live_only_branch_excluded() -> None:
    """A branch in the live open-PR set but absent from the ledger is excluded."""
    branch = evolution_branch("abc123")
    result = compute_owned_writable_branches(task_branch("run1"), [branch], [])
    assert branch not in result


def test_non_evolve_branch_confirmed_by_both_excluded() -> None:
    """A confirmed branch lacking the evolve prefix is excluded by provenance."""
    foreign = task_branch("other")
    result = compute_owned_writable_branches(
        task_branch("run1"), [foreign], [foreign],
    )
    assert foreign not in result


def test_empty_current_branch_adds_nothing() -> None:
    """An empty run branch contributes nothing to the writable set."""
    result = compute_owned_writable_branches("", [], [])
    assert result == frozenset()


def test_current_branch_matching_confirmed_evolve_deduplicated() -> None:
    """A run branch that is also a confirmed evolve branch appears once."""
    branch = evolution_branch("abc123")
    result = compute_owned_writable_branches(branch, [branch], [branch])
    assert result == frozenset({branch})


def test_return_value_is_frozenset() -> None:
    """The oracle always returns a ``frozenset``."""
    result = compute_owned_writable_branches(task_branch("run1"), [], [])
    assert isinstance(result, frozenset)


# ---------------------------------------------------------------------------
# Same-turn push-landing verifier — 'delivered' binds to the remote branch head
# physically advancing to the pushed SHA, read back *this turn* (the push-act
# analogue of ``verify_obsolete_persisted``), not the push subprocess's return.
# Relocated unchanged from the retired ``tests/test_owned_branch_oracle.py``.
# ---------------------------------------------------------------------------

_SHA_A = "a" * 40
_SHA_B = "b" * 40


def test_verifier_true_when_remote_head_advanced() -> None:
    """A fetched ``origin/<branch>`` head equal to the pushed SHA confirms True."""
    reads: list[str] = []
    result = verify_push_advanced_remote_head(
        "maestro/evolve/ep1",
        _SHA_A,
        fetch=lambda _b: True,
        read_remote_head=lambda b: reads.append(b) or _SHA_A,
    )
    assert result is True
    assert reads == ["maestro/evolve/ep1"]


def test_verifier_false_on_sha_mismatch() -> None:
    """A fetched head that differs from the pushed SHA (a no-op push) is False."""
    result = verify_push_advanced_remote_head(
        "maestro/evolve/ep1",
        _SHA_A,
        fetch=lambda _b: True,
        read_remote_head=lambda _b: _SHA_B,
    )
    assert result is False


def test_verifier_false_on_fetch_failure_without_reading() -> None:
    """A fetch failure short-circuits to False and never reads the remote head."""
    reads: list[str] = []
    result = verify_push_advanced_remote_head(
        "maestro/evolve/ep1",
        _SHA_A,
        fetch=lambda _b: False,
        read_remote_head=lambda b: reads.append(b) or _SHA_A,
    )
    assert result is False
    assert reads == []


def test_verifier_false_on_read_failure() -> None:
    """A ``None`` remote-head read (read failure) is False even after a fetch."""
    result = verify_push_advanced_remote_head(
        "maestro/evolve/ep1",
        _SHA_A,
        fetch=lambda _b: True,
        read_remote_head=lambda _b: None,
    )
    assert result is False


def test_verifier_false_on_empty_expected_sha_without_fetching() -> None:
    """An empty/missing pushed SHA is False and never fetches — a null local
    head can never be narrated as a delivery."""
    fetched: list[str] = []
    result = verify_push_advanced_remote_head(
        "maestro/evolve/ep1",
        "",
        fetch=lambda b: fetched.append(b) or True,
        read_remote_head=lambda _b: _SHA_A,
    )
    assert result is False
    assert fetched == []
