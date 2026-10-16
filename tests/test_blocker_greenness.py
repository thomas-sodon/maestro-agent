"""Tests for the blocker-severity total order (``maestro.pr_facts``).

The total order ``GREEN < THREADS_UNRESOLVED < CI_FAILING < CONFLICTING/DIRTY``
is the mechanical spine of the release contract's "the new blocker must be strictly greener
than the old" clause: a strictly positive :func:`greenness_delta` *is* that
clause. These tests pin the order's monotonicity, the clearance / rotation /
same-class / regression cases the acceptance criteria name, the signed
:func:`greenness_delta`, and the normalisation + unknown-class defaults. Pure —
no filesystem, no network, per Testing Principles.
"""

from __future__ import annotations

from maestro.pr_facts import (
    CI_FAILING,
    CONFLICTING,
    DIRTY,
    GREEN,
    THREADS_UNRESOLVED,
    greenness_delta,
)

# The documented total order, greenest first. The index of each class equals its
# severity rank (GREEN=0 ... CONFLICTING=3), so a delta between two members is the
# exact difference of their indices.
_ORDER = [GREEN, THREADS_UNRESOLVED, CI_FAILING, CONFLICTING]


def test_total_order_is_strictly_monotone() -> None:
    # For every pair, moving redder->greener yields a strictly positive delta and
    # the reverse (greener->redder, a regression) yields its negation — the whole
    # total order at once. Severity == list index, so the delta is exactly i - j.
    for i, redder in enumerate(_ORDER):
        for j, greener in enumerate(_ORDER[:i]):
            assert greenness_delta(redder, greener) == i - j
            assert greenness_delta(greener, redder) == j - i


def test_same_class_is_zero_delta() -> None:
    for cls in _ORDER:
        assert greenness_delta(cls, cls) == 0


def test_clearance_delta() -> None:
    # CONFLICTING -> GREEN is full clearance: strictly greener, delta +3.
    assert greenness_delta(CONFLICTING, GREEN) == 3


def test_rotation_to_greener_delta() -> None:
    # Acceptance criterion 1: CONFLICTING -> CI_FAILING rotates one class greener.
    assert greenness_delta(CONFLICTING, CI_FAILING) == 1


def test_same_class_conflicting_is_zero_delta() -> None:
    # Acceptance criterion 1: CONFLICTING -> CONFLICTING is not greener (zero).
    assert greenness_delta(CONFLICTING, CONFLICTING) == 0


def test_regression_is_negative_delta() -> None:
    # Acceptance criterion 2: every severity regression yields a negative delta.
    assert greenness_delta(CI_FAILING, CONFLICTING) == -1
    assert greenness_delta(GREEN, THREADS_UNRESOLVED) == -1
    assert greenness_delta(THREADS_UNRESOLVED, CI_FAILING) == -1


def test_dirty_ranks_with_conflicting() -> None:
    # DIRTY (raw host state) shares CONFLICTING's max severity.
    assert greenness_delta(DIRTY, CONFLICTING) == 0
    assert greenness_delta(DIRTY, CI_FAILING) == 1
    assert greenness_delta(CI_FAILING, DIRTY) == -1


def test_casing_and_whitespace_normalised() -> None:
    # Raw lower-case / padded host states classify like the canonical codes.
    assert greenness_delta("  conflicting  ", "ci_failing") == 1
    assert greenness_delta(" dirty ", "green") == 3


def test_unknown_class_treated_as_reddest() -> None:
    # An unrecognised blocker ranks maximally red: never greener than a known
    # class, and moving away from it toward a known class is strictly greener.
    assert greenness_delta("BOGUS", GREEN) == 3
    assert greenness_delta(GREEN, "BOGUS") == -3
    # Unknown vs CONFLICTING: both max severity -> no movement either way.
    assert greenness_delta("BOGUS", CONFLICTING) == 0
