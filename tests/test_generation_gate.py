"""Tests for the pre-mint generation authority (``may_mint_plan``).

Proposal 120, Phase B. The pure function decides whether a net-new evolution
plan may be minted this tick, computing the same two refusal arms the submission
door applies — backlog saturation and self-measurement theme concentration —
from injected inputs alone. No ``mem_fs``, no I/O, no SDK: synthetic inputs only,
mirroring ``tests/test_outcome_authority.py``.
"""

from __future__ import annotations

import dataclasses

import pytest

from maestro.generation_gate import (
    GENERATION_STANDDOWN_DIRECTIVE,
    GenerationVerdict,
    may_mint_plan,
)


def _verdict(
    *,
    pending_count: int = 0,
    block_threshold: int = 5,
    candidate_is_self_measurement: bool = True,
    themed_fraction: float = 0.0,
    theme_window_total: int = 0,
    theme_fraction_threshold: float = 0.75,
) -> GenerationVerdict:
    """Call ``may_mint_plan`` with admit-by-default inputs, overriding only the
    arm under test — so each test states just the fact it exercises."""
    return may_mint_plan(
        pending_count=pending_count,
        block_threshold=block_threshold,
        candidate_is_self_measurement=candidate_is_self_measurement,
        themed_fraction=themed_fraction,
        theme_window_total=theme_window_total,
        theme_fraction_threshold=theme_fraction_threshold,
    )


def test_refuses_saturated_backlog() -> None:
    """``pending_count >= block_threshold`` (and no theme concentration) refuses
    with the ``saturation`` reason."""
    verdict = _verdict(pending_count=5, block_threshold=5)
    assert verdict.admit is False
    assert verdict.reason.startswith("saturation:")
    assert "5 >= 5" in verdict.reason


def test_refuses_over_concentrated_theme() -> None:
    """A themed candidate over a non-empty, over-threshold window refuses with
    the ``theme`` reason, even when the raw backlog sits under the cap."""
    verdict = _verdict(
        pending_count=0,
        candidate_is_self_measurement=True,
        themed_fraction=0.8,
        theme_window_total=10,
        theme_fraction_threshold=0.75,
    )
    assert verdict.admit is False
    assert verdict.reason.startswith("theme:")


def test_refuses_both_arms_names_saturation_and_theme() -> None:
    """When both arms fire the reason names ``saturation+theme``."""
    verdict = _verdict(
        pending_count=6,
        block_threshold=5,
        candidate_is_self_measurement=True,
        themed_fraction=0.9,
        theme_window_total=10,
    )
    assert verdict.admit is False
    assert verdict.reason.startswith("saturation+theme:")


def test_admits_when_neither_arm_holds() -> None:
    """A sub-cap backlog and a non-over-concentrated window admit, with no
    refusal reason."""
    verdict = _verdict(
        pending_count=2,
        block_threshold=5,
        themed_fraction=0.1,
        theme_window_total=10,
    )
    assert verdict.admit is True
    assert verdict.reason == ""


def test_theme_arm_ignores_non_self_measurement_candidate() -> None:
    """An over-concentrated window does NOT refuse a candidate that is not a
    self-measurement plan — the theme arm is gated on the candidate's theme."""
    verdict = _verdict(
        pending_count=0,
        candidate_is_self_measurement=False,
        themed_fraction=1.0,
        theme_window_total=10,
    )
    assert verdict.admit is True
    assert verdict.reason == ""


def test_disabled_cap_admits_regardless_of_depth() -> None:
    """``block_threshold <= 0`` disables the saturation cap — a huge backlog
    still admits (mirrors the submit-block's ``<= 0`` disable)."""
    for threshold in (0, -1):
        verdict = _verdict(pending_count=1000, block_threshold=threshold)
        assert verdict.admit is True
        assert verdict.reason == ""


def test_empty_theme_window_admits() -> None:
    """An empty recent-obsolete window (``theme_window_total == 0``) never
    triggers the theme arm even for a themed candidate at full concentration."""
    verdict = _verdict(
        pending_count=0,
        candidate_is_self_measurement=True,
        themed_fraction=1.0,
        theme_window_total=0,
    )
    assert verdict.admit is True
    assert verdict.reason == ""


@pytest.mark.parametrize(
    ("themed_fraction", "expect_refuse"),
    [(0.74, False), (0.75, True), (0.76, True)],
)
def test_theme_threshold_is_inclusive(
    themed_fraction: float, expect_refuse: bool,
) -> None:
    """The theme arm fires at or above the threshold (``>=``), not strictly
    above it."""
    verdict = _verdict(
        candidate_is_self_measurement=True,
        themed_fraction=themed_fraction,
        theme_window_total=10,
        theme_fraction_threshold=0.75,
    )
    assert verdict.admit is (not expect_refuse)


@pytest.mark.parametrize(
    ("pending_count", "expect_refuse"),
    [(4, False), (5, True), (6, True)],
)
def test_saturation_threshold_is_inclusive(
    pending_count: int, expect_refuse: bool,
) -> None:
    """Backlog saturation fires at or above the threshold (``>=``)."""
    verdict = _verdict(pending_count=pending_count, block_threshold=5)
    assert verdict.admit is (not expect_refuse)


def test_verdict_is_frozen() -> None:
    """``GenerationVerdict`` is an immutable value object."""
    verdict = _verdict(pending_count=0, block_threshold=5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.admit = False  # type: ignore[misc]


def test_standdown_directive_is_bounded() -> None:
    """The relocated directive stays within the ``MAX_DIRECTIVE_CHARS`` budget
    the self-model slot enforces, so no truncation reshapes it."""
    from maestro.consciousness import Consciousness

    assert len(GENERATION_STANDDOWN_DIRECTIVE) <= Consciousness.MAX_DIRECTIVE_CHARS
