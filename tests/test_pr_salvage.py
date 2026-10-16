"""Unit tests for the pure salvageability oracle (proposal 094, step 1).

Covers :func:`maestro.pr_facts.assess_pr_salvage` and
:func:`maestro.pr_facts.classify_pr_disposability` in isolation — no agent, no
host, no SDK. Every case is deterministic given the injected
:class:`~maestro.pr_facts.PRReadiness` fact and the pre-computed disposability
inputs.
"""

from __future__ import annotations

from maestro.pr_facts import (
    _STALLED_REMEDIATION_LAP_CAP,
    RECONCILE_COMMITS_BEHIND_BUDGET,
    CIState,
    PRReadiness,
    Salvage,
    assess_pr_salvage,
    classify_pr_disposability,
)

# Restatement fixtures reused verbatim from tests/test_consciousness.py, where
# the first pair scores above the 0.5 Jaccard threshold (near-restatement) and
# the second pair scores below it (genuinely distinct).
_RESTATING_PLAN = (
    "restatement convergence — builds measurement instruments "
    "instead of acting on existing knowledge"
)
_RESTATED_SIBLING = (
    "instrument saturation — builds measurement instruments "
    "instead of acting on existing knowledge"
)
_NOVEL_PLAN = "Honest partial reporting: identifies when scope exceeds capacity"
_DISTINCT_SIBLING = "Semantic merge resolution: 30+ PRs with intent-chain tracing"


def _readiness(
    *,
    ready_and_waiting: bool,
    ci_state: CIState = "GREEN",
    branch_conflicting: bool = False,
    settled: bool = False,
    branch_landed: bool = False,
) -> PRReadiness:
    """Build a :class:`PRReadiness` exposing the fields a test needs to set.

    :func:`assess_pr_salvage` reads :attr:`PRReadiness.ready_and_waiting` plus
    :attr:`PRReadiness.branch_conflicting` (for the budget-aware disposal gate)
    plus :attr:`PRReadiness.settled` (for the settled short-circuit); ``ci_state``
    is overridable so a test can honestly model the blocker its name asserts (a
    CI-red PR) even though the terminal keys only on the aggregate.
    ``branch_landed`` is overridable for the same honesty reason — the oracle
    never reads it, but the settled short-circuit covers *merged or closed*, and
    only a ``branch_landed=True``/``settled=True`` fact models the merged half
    the way :func:`observe_pr_readiness` would actually stamp it. The rest stay
    neutral.
    """
    return PRReadiness(
        pr_number=1,
        branch_landed=branch_landed,
        branch_behind=False,
        branch_conflicting=branch_conflicting,
        ci_state=ci_state,
        unresolved_threads=0,
        unaddressed_overflow=0,
        awaiting_approval=False,
        ready_and_waiting=ready_and_waiting,
        source="host_api",
        observed_at=0.0,
        settled=settled,
    )


# --- assess_pr_salvage truth table -----------------------------------------


def test_not_ready_and_not_disposable_is_salvage() -> None:
    """Unfinished work worth keeping must be salvaged and landed."""
    verdict = assess_pr_salvage(_readiness(ready_and_waiting=False), disposable=False)
    assert verdict == Salvage.SALVAGE_AND_LAND


def test_ci_red_not_ready_is_salvage() -> None:
    """A CI-red PR is not ready-and-waiting → salvage, not disengage.

    Failing CI is a code-clearable blocker aggregated into
    ``ready_and_waiting=False``; with the work non-disposable the verdict is to
    fix-and-land, closing the historic ``CI_FAILING`` disengagement hole.
    """
    verdict = assess_pr_salvage(
        _readiness(ready_and_waiting=False, ci_state="FAILING"), disposable=False
    )
    assert verdict == Salvage.SALVAGE_AND_LAND


def test_ready_and_waiting_permits_disengage() -> None:
    """Done and only awaiting a human → disengagement is honest completion."""
    verdict = assess_pr_salvage(_readiness(ready_and_waiting=True), disposable=False)
    assert verdict == Salvage.DISENGAGE_PERMITTED


def test_not_ready_but_disposable_permits_disengage() -> None:
    """Unfinished but disposable work → disengagement is permitted."""
    verdict = assess_pr_salvage(_readiness(ready_and_waiting=False), disposable=True)
    assert verdict == Salvage.DISENGAGE_PERMITTED


def test_ready_and_disposable_permits_disengage() -> None:
    """Both ready and disposable → disengagement is permitted."""
    verdict = assess_pr_salvage(_readiness(ready_and_waiting=True), disposable=True)
    assert verdict == Salvage.DISENGAGE_PERMITTED


def test_closed_unmerged_pr_permits_disengage_though_not_ready() -> None:
    """A closed-but-unmerged PR — settled, never landed — is not salvageable.

    The compensating guard for the readiness fix: once a closed-unmerged PR
    correctly reads ``ready_and_waiting=False``, the not-ready/not-disposable
    test alone would return ``SALVAGE_AND_LAND`` and — as the AST-enforced sole
    veto over every give-up actuator — block its stand-down writers and the
    orphaned-cure sweep forever. The settled short-circuit keeps the fact
    coherent: settled work is done being worked, salvageable or not.
    """
    verdict = assess_pr_salvage(
        _readiness(
            ready_and_waiting=False, settled=True, branch_landed=False,
        ),
        disposable=False,
    )
    assert verdict == Salvage.DISENGAGE_PERMITTED


def test_merged_pr_permits_disengage_though_not_ready() -> None:
    """The merged half of "merged *or* closed" disengages on the same door.

    Sibling of the closed-unmerged case: a *landed* PR is settled too, and the
    short-circuit is what makes the documented "merged or closed" wording true
    rather than closed-only. Before it, a merged non-disposable PR also fell
    through to the not-ready/not-disposable test — ``ready_and_waiting`` is
    ``False`` on a landed PR — and read ``SALVAGE_AND_LAND``, vetoing give-up on
    work that had already landed.
    """
    verdict = assess_pr_salvage(
        _readiness(
            ready_and_waiting=False, settled=True, branch_landed=True,
        ),
        disposable=False,
    )
    assert verdict == Salvage.DISENGAGE_PERMITTED


def test_settled_short_circuit_outranks_the_conflicting_blocker() -> None:
    """Settled outranks a code-clearable blocker with no behind-count measured.

    An unmeasured ``commits_behind`` normally fails safe toward
    ``SALVAGE_AND_LAND`` even on a conflicting branch; being settled decides the
    verdict before that budget arm is reached.
    """
    verdict = assess_pr_salvage(
        _readiness(
            ready_and_waiting=False, branch_conflicting=True, settled=True,
        ),
        disposable=False,
    )
    assert verdict == Salvage.DISENGAGE_PERMITTED


# --- budget-aware disposal (commits_behind dimension) -----------------------


def test_conflicting_past_budget_permits_disengage() -> None:
    """A CONFLICTING branch measured far past the reconcile budget → DISPOSE.

    77 commits behind ``origin/main`` re-conflicts before an LLM reconcile can
    land it, so salvage is a treadmill and disengagement (``gh pr close``) is the
    honest move — the ``DISENGAGE_PERMITTED`` verdict lifts the close veto even on
    non-disposable, not-ready work.
    """
    verdict = assess_pr_salvage(
        _readiness(ready_and_waiting=False, branch_conflicting=True),
        disposable=False,
        commits_behind=77,
    )
    assert verdict == Salvage.DISENGAGE_PERMITTED


def test_conflicting_within_budget_is_salvage() -> None:
    """A CONFLICTING branch within (at) the reconcile budget → salvage-and-land."""
    verdict = assess_pr_salvage(
        _readiness(ready_and_waiting=False, branch_conflicting=True),
        disposable=False,
        commits_behind=RECONCILE_COMMITS_BEHIND_BUDGET,
    )
    assert verdict == Salvage.SALVAGE_AND_LAND


def test_conflicting_none_behind_count_is_salvage() -> None:
    """An unavailable behind-count (``None``) fails safe toward salvage.

    The oracle never disposes on a guess: with the measurement absent the
    budget-aware gate cannot fire, so a non-disposable CONFLICTING PR stays
    SALVAGE_AND_LAND.
    """
    verdict = assess_pr_salvage(
        _readiness(ready_and_waiting=False, branch_conflicting=True),
        disposable=False,
        commits_behind=None,
    )
    assert verdict == Salvage.SALVAGE_AND_LAND


def test_ci_failing_blocker_past_budget_is_salvage() -> None:
    """A CI_FAILING blocker (no conflict) is owned, clearable work — even 77 behind.

    A branch whose blocker is *not* CONFLICTING (``branch_conflicting=False``,
    modelled here as CI-red) stays SALVAGE_AND_LAND at any behind-count: the
    budget gate fires only for the conflict blocker, whose re-conflict-before-land
    treadmill is the failure mode it targets.
    """
    verdict = assess_pr_salvage(
        _readiness(
            ready_and_waiting=False, ci_state="FAILING", branch_conflicting=False,
        ),
        disposable=False,
        commits_behind=77,
    )
    assert verdict == Salvage.SALVAGE_AND_LAND


def test_threads_unresolved_blocker_past_budget_is_salvage() -> None:
    """A THREADS_UNRESOLVED blocker (no conflict) stays salvage — even 77 behind.

    The oracle reads only ``branch_conflicting`` for the budget gate, so an
    unresolved-thread blocker (non-conflicting, not ready-and-waiting) is never
    disposed on the behind-count alone: answering the thread is owned, clearable
    work regardless of how far the branch trails ``origin/main``.
    """
    verdict = assess_pr_salvage(
        _readiness(ready_and_waiting=False, branch_conflicting=False),
        disposable=False,
        commits_behind=77,
    )
    assert verdict == Salvage.SALVAGE_AND_LAND


# --- base-advance re-entry (same_base_failures dimension, proposal 099) ------
#
# Distance behind alone no longer authorizes disposal: the behind-budget
# disengage arm now also requires the PR to have failed ``_STALLED_REMEDIATION_LAP_CAP``
# consecutive laps against a base that stood still. A moved base is a new race.


def test_conflicting_past_budget_no_same_base_streak_is_salvage() -> None:
    """59 behind but zero same-base failures → SALVAGE (the base moved).

    A conflicting branch far past the reconcile budget is no longer disposed on
    distance alone: with no proven same-base failure streak the moved base is a
    fresh race the branch has never been re-run against, so salvage is still owed.
    """
    verdict = assess_pr_salvage(
        _readiness(ready_and_waiting=False, branch_conflicting=True),
        disposable=False,
        commits_behind=59,
        same_base_failures=0,
    )
    assert verdict == Salvage.SALVAGE_AND_LAND


def test_conflicting_past_budget_below_stall_cap_is_salvage() -> None:
    """59 behind with a below-cap same-base streak still salvages."""
    verdict = assess_pr_salvage(
        _readiness(ready_and_waiting=False, branch_conflicting=True),
        disposable=False,
        commits_behind=59,
        same_base_failures=_STALLED_REMEDIATION_LAP_CAP - 1,
    )
    assert verdict == Salvage.SALVAGE_AND_LAND


def test_conflicting_past_budget_at_stall_cap_permits_disengage() -> None:
    """59 behind *and* failed cap laps against the same base → DISENGAGE.

    Only repeated failure against a base that stood still restores the honest
    give-up: the branch has re-conflicted ``cap`` times without the base moving.
    """
    verdict = assess_pr_salvage(
        _readiness(ready_and_waiting=False, branch_conflicting=True),
        disposable=False,
        commits_behind=59,
        same_base_failures=_STALLED_REMEDIATION_LAP_CAP,
    )
    assert verdict == Salvage.DISENGAGE_PERMITTED


def test_conflicting_past_budget_explicit_none_same_base_is_salvage() -> None:
    """An explicit ``None`` same-base streak fails safe toward salvage.

    An unmeasurable streak (``None``) can never authorize disposal — the oracle
    never disposes on a guess, mirroring the ``commits_behind=None`` fail-safe.
    """
    verdict = assess_pr_salvage(
        _readiness(ready_and_waiting=False, branch_conflicting=True),
        disposable=False,
        commits_behind=59,
        same_base_failures=None,
    )
    assert verdict == Salvage.SALVAGE_AND_LAND


def test_conflicting_past_budget_omitted_same_base_permits_disengage() -> None:
    """An omitted ``same_base_failures`` reproduces the legacy distance disengage.

    The flag-off path passes no ``same_base_failures``; the ``_UNSET`` default
    keeps the pre-099 behaviour byte-identical — 59 behind alone disposes.
    """
    verdict = assess_pr_salvage(
        _readiness(ready_and_waiting=False, branch_conflicting=True),
        disposable=False,
        commits_behind=59,
    )
    assert verdict == Salvage.DISENGAGE_PERMITTED


# --- classify_pr_disposability ----------------------------------------------


def test_restatement_of_sibling_is_disposable() -> None:
    """A plan that near-restates an open sibling's backing plan is disposable."""
    disposable = classify_pr_disposability(
        _RESTATING_PLAN,
        [_RESTATED_SIBLING],
        superseded_by_merged=None,
        has_substantive_diff=True,
    )
    assert disposable is True


def test_superseded_by_merged_is_disposable() -> None:
    """A novel plan whose work a merged sibling already carries is disposable."""
    disposable = classify_pr_disposability(
        _NOVEL_PLAN,
        [_DISTINCT_SIBLING],
        superseded_by_merged=True,
        has_substantive_diff=True,
    )
    assert disposable is True


def test_empty_diff_is_disposable() -> None:
    """A branch with no substantive change to land is disposable."""
    disposable = classify_pr_disposability(
        _NOVEL_PLAN,
        [_DISTINCT_SIBLING],
        superseded_by_merged=None,
        has_substantive_diff=False,
    )
    assert disposable is True


def test_novel_plan_with_real_diff_is_not_disposable() -> None:
    """A distinct plan carrying a real diff is unique work — never disposable."""
    disposable = classify_pr_disposability(
        _NOVEL_PLAN,
        [_DISTINCT_SIBLING],
        superseded_by_merged=None,
        has_substantive_diff=True,
    )
    assert disposable is False


def test_unknown_signals_fail_toward_salvage() -> None:
    """Both I/O-derived signals ``None`` contribute nothing → not disposable.

    An unavailable detector must fail toward salvage; the oracle never abandons
    work on a guess.
    """
    disposable = classify_pr_disposability(
        _NOVEL_PLAN,
        [_DISTINCT_SIBLING],
        superseded_by_merged=None,
        has_substantive_diff=None,
    )
    assert disposable is False


# --- Owed-act hold (proposal 137, step 3) ---------------------------------
#
# ``code_owed`` is the caller's boolean reading of the single
# ``classify_pr_owed_act`` partition. ``True`` inside
# ``RECONCILE_COMMITS_BEHIND_BUDGET`` holds the veto over give-up; omitted, and
# every other shape, reproduces the pre-137 table.


def test_code_owed_inside_budget_refuses_disengage() -> None:
    """A code-owed, disposable PR inside the budget still salvages (137).

    Disposability alone resolved to ``DISENGAGE_PERMITTED`` before the binding:
    the fix is one commit away and owned, so the exit is not offered.
    """
    readiness = _readiness(ready_and_waiting=False, ci_state="FAILING")
    verdict = assess_pr_salvage(
        readiness, True,
        commits_behind=RECONCILE_COMMITS_BEHIND_BUDGET,
        code_owed=True,
    )
    assert verdict == Salvage.SALVAGE_AND_LAND


def test_code_owed_inside_budget_holds_the_conflicting_disposal_arm() -> None:
    """Code-owed work inside the budget outranks the behind-budget arm."""
    readiness = _readiness(
        ready_and_waiting=False, branch_conflicting=True, ci_state="FAILING",
    )
    verdict = assess_pr_salvage(
        readiness, False, commits_behind=1,
        same_base_failures=_STALLED_REMEDIATION_LAP_CAP,
        code_owed=True,
    )
    assert verdict == Salvage.SALVAGE_AND_LAND


def test_code_owed_past_budget_preserves_disengage() -> None:
    """Past the reconcile budget the binding changes nothing (137).

    The binding removes the *premature* close, not the exit itself: a branch
    that re-conflicts faster than a reconcile can land is still disposable.
    """
    readiness = _readiness(
        ready_and_waiting=False, branch_conflicting=True, ci_state="FAILING",
    )
    verdict = assess_pr_salvage(
        readiness, False,
        commits_behind=RECONCILE_COMMITS_BEHIND_BUDGET + 1,
        same_base_failures=_STALLED_REMEDIATION_LAP_CAP,
        code_owed=True,
    )
    assert verdict == Salvage.DISENGAGE_PERMITTED


def test_code_owed_on_settled_pr_preserves_disengage() -> None:
    """The settled short-circuit stays first — a landed PR owes no code."""
    readiness = _readiness(
        ready_and_waiting=False, settled=True, branch_landed=True,
    )
    verdict = assess_pr_salvage(
        readiness, True, commits_behind=0, code_owed=True,
    )
    assert verdict == Salvage.DISENGAGE_PERMITTED


def test_code_owed_with_unmeasurable_behind_count_preserves_table() -> None:
    """An unknown behind-count cannot trigger the hold — today's table stands."""
    readiness = _readiness(ready_and_waiting=False, ci_state="FAILING")
    verdict = assess_pr_salvage(
        readiness, True, commits_behind=None, code_owed=True,
    )
    assert verdict == Salvage.DISENGAGE_PERMITTED


def test_code_owed_none_reading_preserves_table() -> None:
    """A ``None`` reading (the flag-off path) reproduces today's verdict."""
    readiness = _readiness(ready_and_waiting=False, ci_state="FAILING")
    verdict = assess_pr_salvage(
        readiness, True, commits_behind=0, code_owed=None,
    )
    assert verdict == Salvage.DISENGAGE_PERMITTED


def test_code_owed_omitted_preserves_table() -> None:
    """The omitted argument reproduces the pre-137 disposable verdict."""
    readiness = _readiness(ready_and_waiting=False, ci_state="FAILING")
    verdict = assess_pr_salvage(readiness, True, commits_behind=0)
    assert verdict == Salvage.DISENGAGE_PERMITTED
