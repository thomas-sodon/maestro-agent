"""Conflict routing, cooldown and awaiting-push bypass inside tend; split out of
``tests/test_evolution_tend.py``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import evolution_tend
from maestro.config import Config
from maestro.evolution import EvolutionEngine
from maestro.evolution_tend import TendPRsResult
from maestro.memory import EvolutionPlan, TaskRecord
from maestro.pr_owed_act import OwedAct
from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import (
    COOLDOWN_SKIP_GATE,
    format_cooldown_skip_detail,
)
from maestro.vcs import MergeResult, PrMergeState, VCSError
from tests.support.merge_truth import merge_oracle
from tests.test_evolution_tend import (
    FAKE_DIR,
    _failing_gates,
    _ground_vcs,
    _make_engine,
    _plan,
    _pr_state,
)

# ---------------------------------------------------------------------------
# Mechanical-only tend — conflict routing
# ---------------------------------------------------------------------------


def _conflicted_engine(
    mem_fs: dict[str, str],
    *,
    probe_files: list[str],
) -> EvolutionEngine:
    """Engine whose VCS reports a dry-run probe with the given conflicts."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    vcs.merge_from.return_value = MergeResult(
        success=False, conflicted_files=probe_files,
    )
    # Grounded staleness (095): a DIRTY/CONFLICTING PR is behind + conflicting,
    # so the retired host proxy is replaced by measured behind>0 + merge-tree
    # conflict. Every user of this helper drives a conflict/cooldown scenario.
    _ground_vcs(vcs, behind=1, merge_clean=False)
    return _make_engine(mem_fs, vcs=vcs)


@pytest.mark.asyncio
async def test_dirty_pr_routes_into_conflicts_routed(
    mem_fs: dict[str, str],
) -> None:
    """A DIRTY PR populates ``conflicts_routed`` and never calls the SDK."""
    engine = _conflicted_engine(mem_fs, probe_files=["a.py", "b.py"])
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    with patch("maestro.evolution.query", new=AsyncMock()) as sdk_mock:
        result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    sdk_mock.assert_not_called()
    assert result.conflicts_routed == [
        (
            1, "t", ["a.py", "b.py"],
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    assert result.merged_updated == 0
    # Cooldown recorded so the next iteration does not re-route.
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.last_conflict_resolution_attempt_at is not None


def _cooldown_engine(mem_fs: dict[str, str]) -> EvolutionEngine:
    """A plain engine for the base-advance conflict-cooldown tests (099)."""
    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
    )
    return _make_engine(mem_fs, config=cfg)


def test_conflict_cooldown_released_on_base_advance(
    mem_fs: dict[str, str],
) -> None:
    """A moved ``origin/main`` releases the conflict cooldown (099)."""
    engine = _cooldown_engine(mem_fs)
    engine._vcs.rev_parse.return_value = "newbase"
    now = datetime.now(UTC)
    plan = _plan(
        last_conflict_attempt=now.isoformat(),
        last_conflict_base_sha="oldbase",
    )
    assert evolution_tend._in_conflict_cooldown(engine, plan, now) is False


def test_conflict_cooldown_preserved_on_unchanged_base(
    mem_fs: dict[str, str],
) -> None:
    """An unchanged base keeps the (recent) cooldown intact (099)."""
    engine = _cooldown_engine(mem_fs)
    engine._vcs.rev_parse.return_value = "base"
    now = datetime.now(UTC)
    plan = _plan(
        last_conflict_attempt=now.isoformat(),
        last_conflict_base_sha="base",
    )
    assert evolution_tend._in_conflict_cooldown(engine, plan, now) is True


def test_conflict_cooldown_unmeasurable_base_preserves_cooldown(
    mem_fs: dict[str, str],
) -> None:
    """An unreadable ``origin/main`` does not force-release the cooldown.

    Mirroring the #2821 head-advance guard, only a *proven* base advance (both
    SHAs known and differing) invalidates — a ``None`` current base leaves the
    time-based cooldown intact.
    """
    engine = _cooldown_engine(mem_fs)
    engine._vcs.rev_parse.return_value = None
    now = datetime.now(UTC)
    plan = _plan(
        last_conflict_attempt=now.isoformat(),
        last_conflict_base_sha="base",
    )
    assert evolution_tend._in_conflict_cooldown(engine, plan, now) is True


def test_conflict_cooldown_legacy_none_base_preserves_cooldown(
    mem_fs: dict[str, str],
) -> None:
    """A legacy plan with no stamped base is not force-released.

    Without a recorded base SHA there is nothing to compare against, so the
    base-advance release cannot fire and the time-based cooldown still governs.
    """
    engine = _cooldown_engine(mem_fs)
    engine._vcs.rev_parse.return_value = "newbase"
    now = datetime.now(UTC)
    plan = _plan(
        last_conflict_attempt=now.isoformat(),
        last_conflict_base_sha=None,
    )
    assert evolution_tend._in_conflict_cooldown(engine, plan, now) is True


def _keyed_engine(mem_fs: dict[str, str]) -> EvolutionEngine:
    """An engine with proposal 137's cooldown signature keying switched on."""
    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        cooldown_signature_keying_enabled=True,
    )
    return _make_engine(mem_fs, config=cfg)


# Each row: (label, current head, current base, current CI, expected cooldown).
# The stamped signature is always ("head", "base", "failure"), and every engine
# resolves ``rev_parse("origin/main")`` to the stamped base — so the
# proposal-099 base-advance release can never fire and any release observed
# below is attributable to the signature keying alone.
_SIGNATURE_MATRIX: list[tuple[str, str | None, str | None, str | None, bool]] = [
    ("identical signature holds", "head", "base", "failure", True),
    ("changed head releases", "newhead", "base", "failure", False),
    ("changed base releases", "head", "newbase", "failure", False),
    ("changed CI releases", "head", "base", "success", False),
    ("unknown current head holds", None, "base", "failure", True),
    ("unknown current base holds", "head", None, "failure", True),
    ("unknown current CI holds", "head", "base", "", True),
]


@pytest.mark.parametrize(
    ("label", "head", "base", "ci", "expected"), _SIGNATURE_MATRIX,
)
def test_conflict_cooldown_signature_matrix(
    mem_fs: dict[str, str],
    label: str,
    head: str | None,
    base: str | None,
    ci: str | None,
    expected: bool,
) -> None:
    """Proposal 137: a provably-changed component releases the conflict
    cooldown; an unmeasurable one leaves the time window intact."""
    engine = _keyed_engine(mem_fs)
    engine._vcs.rev_parse.return_value = "base"
    now = datetime.now(UTC)
    plan = _plan(
        last_conflict_attempt=now.isoformat(),
        last_conflict_base_sha="base",
        last_conflict_head_sha="head",
        last_conflict_ci_conclusion="failure",
    )
    assert evolution_tend._in_conflict_cooldown(
        engine,
        plan, now, head_sha=head, base_sha=base, ci_conclusion=ci,
    ) is expected


#: The quality-fix cooldown keys on ``(head, CI rollup)`` only: an
#: ``origin/main`` advance says nothing about whether this PR's own red changed,
#: so it must *not* release the window (proposal 137, the one row that differs
#: from the conflict matrix above).
_QUALITY_FIX_SIGNATURE_MATRIX: list[
    tuple[str, str | None, str | None, str | None, bool]
] = [
    ("identical signature holds", "head", "base", "failure", True),
    ("changed head releases", "newhead", "base", "failure", False),
    ("changed base holds — base is not a release component", "head", "newbase", "failure", True),
    ("changed CI releases", "head", "base", "success", False),
    ("unknown current head holds", None, "base", "failure", True),
    ("unknown current base holds", "head", None, "failure", True),
    ("unknown current CI holds", "head", "base", "", True),
]


@pytest.mark.parametrize(
    ("label", "head", "base", "ci", "expected"), _QUALITY_FIX_SIGNATURE_MATRIX,
)
def test_quality_fix_cooldown_signature_matrix(
    mem_fs: dict[str, str],
    label: str,
    head: str | None,
    base: str | None,
    ci: str | None,
    expected: bool,
) -> None:
    """Proposal 137: the quality-fix cooldown — which carried none of the
    conflict cooldown's ad-hoc releases — gets the same keying."""
    engine = _keyed_engine(mem_fs)
    now = datetime.now(UTC)
    plan = _plan(
        last_quality_fix_attempt=now.isoformat(),
        last_quality_fix_head_sha="head",
        last_quality_fix_base_sha="base",
        last_quality_fix_ci_conclusion="failure",
    )
    assert evolution_tend._in_quality_fix_cooldown(
        engine,
        plan, now, head_sha=head, base_sha=base, ci_conclusion=ci,
    ) is expected


def test_conflict_cooldown_legacy_stamp_holds_under_keying(
    mem_fs: dict[str, str],
) -> None:
    """A legacy anchor carrying no signature is never force-released.

    Only a *proven* difference — both sides known and unequal — releases, so a
    pre-137 row (all three stamps ``None``) keeps today's time window even with
    the flag on.
    """
    engine = _keyed_engine(mem_fs)
    engine._vcs.rev_parse.return_value = "base"
    now = datetime.now(UTC)
    plan = _plan(last_conflict_attempt=now.isoformat())
    assert evolution_tend._in_conflict_cooldown(
        engine,
        plan, now,
        head_sha="newhead", base_sha="newbase", ci_conclusion="success",
    ) is True


@pytest.mark.parametrize(
    ("head", "base", "ci"),
    [(head, base, ci) for _label, head, base, ci, _expected in _SIGNATURE_MATRIX],
)
def test_cooldown_signature_keying_inert_with_flag_off(
    mem_fs: dict[str, str],
    head: str | None,
    base: str | None,
    ci: str | None,
) -> None:
    """With the flag at its committed default, every matrix row holds.

    The signature is passed exactly as the walk passes it, and both predicates
    fall through to the pure wall-clock window — the off-branch is byte-
    identical to the pre-137 behaviour regardless of what the triple says.
    """
    engine = _cooldown_engine(mem_fs)
    assert engine._config.cooldown_signature_keying_enabled is False
    engine._vcs.rev_parse.return_value = "base"
    now = datetime.now(UTC)
    conflict_plan = _plan(
        last_conflict_attempt=now.isoformat(),
        last_conflict_base_sha="base",
        last_conflict_head_sha="head",
        last_conflict_ci_conclusion="failure",
    )
    assert evolution_tend._in_conflict_cooldown(
        engine,
        conflict_plan, now, head_sha=head, base_sha=base, ci_conclusion=ci,
    ) is True
    quality_plan = _plan(
        last_quality_fix_attempt=now.isoformat(),
        last_quality_fix_head_sha="head",
        last_quality_fix_base_sha="base",
        last_quality_fix_ci_conclusion="failure",
    )
    assert evolution_tend._in_quality_fix_cooldown(
        engine,
        quality_plan, now, head_sha=head, base_sha=base, ci_conclusion=ci,
    ) is True


def _backoff_engine(
    mem_fs: dict[str, str],
    *,
    base_hours: float = 1.0,
    ceiling_hours: float = 24.0,
    keyed: bool = True,
) -> EvolutionEngine:
    """An engine whose cooldown backoff ladder is configured explicitly."""
    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        cooldown_signature_keying_enabled=keyed,
        cooldown_backoff_base_hours=base_hours,
        conflict_resolution_cooldown_hours=ceiling_hours,
        quality_fix_cooldown_hours=ceiling_hours,
    )
    return _make_engine(mem_fs, config=cfg)


# Each row: (attempts, elapsed hours, expected cooldown). With base 1h the
# ladder is 1/2/4/8h, so an anchor is held below its rung and released above it.
_BACKOFF_LADDER: list[tuple[int, float, bool]] = [
    (0, 0.5, True),
    (0, 1.5, False),
    (1, 1.5, True),
    (1, 2.5, False),
    (2, 3.5, True),
    (2, 4.5, False),
    (3, 7.5, True),
    (3, 8.5, False),
    # Past the ladder the window clamps at the 24h ceiling and stops growing.
    (10, 23.5, True),
    (10, 24.5, False),
    (200, 24.5, False),
]

# The same (attempts, elapsed) rows read against the flat pre-137 window: with
# the flag off the attempt count is ignored entirely, so the cooldown holds iff
# the elapsed hours are under the 24h ceiling.
_BACKOFF_LADDER_FLAG_OFF: list[tuple[int, float, bool]] = [
    (0, 0.5, True),
    (0, 1.5, True),
    (1, 1.5, True),
    (1, 2.5, True),
    (2, 3.5, True),
    (2, 4.5, True),
    (3, 7.5, True),
    (3, 8.5, True),
    (10, 23.5, True),
    (10, 24.5, False),
    (200, 24.5, False),
]


@pytest.mark.parametrize(("attempts", "elapsed", "expected"), _BACKOFF_LADDER)
def test_cooldown_backoff_ladder(
    mem_fs: dict[str, str],
    attempts: int,
    elapsed: float,
    expected: bool,
) -> None:
    """Proposal 137: on an UNCHANGED signature the window decays to a bounded
    backoff over the laps already spent against it, capped at the ceiling."""
    engine = _backoff_engine(mem_fs)
    engine._vcs.rev_parse.return_value = "base"
    now = datetime.now(UTC)
    last = (now - timedelta(hours=elapsed)).isoformat()
    conflict_plan = _plan(
        last_conflict_attempt=last,
        last_conflict_base_sha="base",
        last_conflict_head_sha="head",
        last_conflict_ci_conclusion="failure",
        conflict_attempts=attempts,
    )
    assert evolution_tend._in_conflict_cooldown(
        engine,
        conflict_plan, now,
        head_sha="head", base_sha="base", ci_conclusion="failure",
    ) is expected
    quality_plan = _plan(
        last_quality_fix_attempt=last,
        last_quality_fix_head_sha="head",
        last_quality_fix_base_sha="base",
        last_quality_fix_ci_conclusion="failure",
        quality_fix_attempts=attempts,
    )
    assert evolution_tend._in_quality_fix_cooldown(
        engine,
        quality_plan, now,
        head_sha="head", base_sha="base", ci_conclusion="failure",
    ) is expected


@pytest.mark.parametrize(("attempts", "elapsed", "expected"), _BACKOFF_LADDER_FLAG_OFF)
def test_cooldown_backoff_inert_with_flag_off(
    mem_fs: dict[str, str],
    attempts: int,
    elapsed: float,
    expected: bool,
) -> None:
    """With the flag off the whole ladder is inert: every row under 24h holds.

    The attempt counts are still stamped, but nothing reads them — the window
    is the flat configured ceiling, byte-identical to the pre-137 behaviour.
    The expectation is an explicit column rather than a comparison recomputed
    inside the assertion, so the flat window is pinned by the table itself.
    """
    engine = _backoff_engine(mem_fs, keyed=False)
    engine._vcs.rev_parse.return_value = "base"
    now = datetime.now(UTC)
    last = (now - timedelta(hours=elapsed)).isoformat()
    plan = _plan(
        last_quality_fix_attempt=last,
        last_quality_fix_head_sha="head",
        last_quality_fix_base_sha="base",
        last_quality_fix_ci_conclusion="failure",
        quality_fix_attempts=attempts,
    )
    assert evolution_tend._in_quality_fix_cooldown(
        engine,
        plan, now,
        head_sha="head", base_sha="base", ci_conclusion="failure",
    ) is expected


def test_cooldown_backoff_base_above_ceiling_degrades_to_ceiling(
    mem_fs: dict[str, str],
) -> None:
    """A base at or above the ceiling clamps to exactly today's flat window.

    ``min(...)`` is why no cross-field validator is needed: an operator who
    configures a base above the ceiling gets the pre-137 behaviour, not a
    startup failure.
    """
    engine = _backoff_engine(mem_fs, base_hours=48.0, ceiling_hours=6.0)
    now = datetime.now(UTC)
    plan = _plan(
        last_quality_fix_attempt=(now - timedelta(hours=5.0)).isoformat(),
        last_quality_fix_head_sha="head",
        last_quality_fix_ci_conclusion="failure",
    )
    assert evolution_tend._in_quality_fix_cooldown(
        engine,
        plan, now, head_sha="head", ci_conclusion="failure",
    ) is True
    plan = _plan(
        last_quality_fix_attempt=(now - timedelta(hours=7.0)).isoformat(),
        last_quality_fix_head_sha="head",
        last_quality_fix_ci_conclusion="failure",
    )
    assert evolution_tend._in_quality_fix_cooldown(
        engine,
        plan, now, head_sha="head", ci_conclusion="failure",
    ) is False


def test_cooldown_backoff_uncomparable_signature_holds_ceiling(
    mem_fs: dict[str, str],
) -> None:
    """A legacy anchor — no component known on both sides — keeps the ceiling.

    The attempt count describes nothing in that case, so the window must fail
    toward today's behaviour rather than releasing after one hour.
    """
    engine = _backoff_engine(mem_fs)
    now = datetime.now(UTC)
    plan = _plan(
        last_quality_fix_attempt=(now - timedelta(hours=2.0)).isoformat(),
        quality_fix_attempts=0,
    )
    assert evolution_tend._in_quality_fix_cooldown(
        engine,
        plan, now, head_sha="head", ci_conclusion="failure",
    ) is True


def test_cooldown_backoff_negative_count_treated_as_first_rung(
    mem_fs: dict[str, str],
) -> None:
    """A corrupted negative count cannot shrink the window below the base."""
    engine = _backoff_engine(mem_fs)
    now = datetime.now(UTC)
    plan = _plan(
        last_quality_fix_attempt=(now - timedelta(hours=0.5)).isoformat(),
        last_quality_fix_head_sha="head",
        last_quality_fix_ci_conclusion="failure",
        quality_fix_attempts=-5,
    )
    assert evolution_tend._in_quality_fix_cooldown(
        engine,
        plan, now, head_sha="head", ci_conclusion="failure",
    ) is True


@pytest.mark.parametrize(
    ("head_sha", "conclusion"),
    [
        # The head did not resolve this lap ...
        (None, "failure"),
        # ... or the rollup fetch degraded to "". Either way no rung could
        # have been consumed against this reading, so none may be read off
        # it. Both rows keep every OTHER component unchanged, so the
        # signature release never fires and the window is what decides.
        ("head", ""),
    ],
)
def test_cooldown_backoff_unmeasurable_lap_holds_ceiling(
    mem_fs: dict[str, str],
    head_sha: str | None,
    conclusion: str,
) -> None:
    """An unmeasurable lap reads the ceiling, not its frozen rung.

    Regression: the consumption gate leaves the count where it is on such a
    lap, so a PR whose head never resolves would otherwise sit at rung 0
    forever while a *sibling* signature component still made the anchor
    comparable — re-routing at the 1h first rung indefinitely instead of
    converging on the 24h ceiling. Comparability therefore requires the lap
    to be measurable too, the exact reciprocal of the consumption rule.
    """
    engine = _backoff_engine(mem_fs)
    engine._vcs.rev_parse.return_value = "base"
    now = datetime.now(UTC)
    last = (now - timedelta(hours=2.0)).isoformat()
    conflict_plan = _plan(
        last_conflict_attempt=last,
        last_conflict_base_sha="base",
        last_conflict_head_sha="head",
        last_conflict_ci_conclusion="failure",
        conflict_attempts=0,
    )
    # Rung 0 would be a 1h window, already elapsed; the ceiling still holds.
    assert evolution_tend._in_conflict_cooldown(
        engine,
        conflict_plan, now,
        head_sha=head_sha, base_sha="base", ci_conclusion=conclusion,
    ) is True
    quality_plan = _plan(
        last_quality_fix_attempt=last,
        last_quality_fix_head_sha="head",
        last_quality_fix_ci_conclusion="failure",
        quality_fix_attempts=0,
    )
    assert evolution_tend._in_quality_fix_cooldown(
        engine,
        quality_plan, now,
        head_sha=head_sha, ci_conclusion=conclusion,
    ) is True


@pytest.mark.parametrize(
    ("stamped_head", "prior", "anchored", "consumed", "expected"),
    [
        ("head", 2, True, True, 3),
        ("oldhead", 2, True, True, 0),
        (None, 2, True, True, 3),
        ("head", 2, False, True, 0),
        # A non-consuming lap leaves the rung where it was on an unchanged
        # signature — neither advanced (nothing was retried) ...
        ("head", 2, True, False, 2),
        # ... nor reset on a CHANGED one: the gate precedes the reset arm, so a
        # lap that never bounced cannot restart the ladder either.
        ("oldhead", 2, True, False, 2),
        # The first-stamp rule still outranks the gate: an unanchored write has
        # no prior rung to preserve.
        ("head", 2, False, False, 0),
    ],
)
def test_next_cooldown_attempts(
    mem_fs: dict[str, str],
    stamped_head: str | None,
    prior: int,
    anchored: bool,
    consumed: bool,
    expected: int,
) -> None:
    """The counter advances on an unchanged signature and resets on a changed
    one, regardless of the flag — the counting is unconditional bookkeeping.
    A lap that did not genuinely bounce consumes no rung at all."""
    engine = _backoff_engine(mem_fs, keyed=False)
    assert evolution_tend._next_cooldown_attempts(
        engine,
        current=("head", "failure"),
        stamped=(stamped_head, "failure"),
        prior=prior,
        anchored=anchored,
        consumed=consumed,
    ) == expected


@pytest.mark.parametrize(
    ("origin_head", "conclusion", "expected"),
    [
        ("h", "success", True),
        ("h", "failure", True),
        ("h", "none", True),
        # A rollup still in flight, or degraded to "" by a failed fetch, is not
        # a conclusion — the lap observed nothing to have bounced off.
        ("h", "pending", False),
        ("h", "", False),
        # An unresolvable branch head is not a measurement either.
        (None, "success", False),
        (None, "", False),
    ],
)
def test_lap_signature_measurable(
    mem_fs: dict[str, str],
    origin_head: str | None,
    conclusion: str,
    expected: bool,
) -> None:
    """A lap is usable as a bounce only with a measured head AND a concluded
    rollup, the latter decided by ``check_runs_from_conclusion``."""
    engine = _backoff_engine(mem_fs)
    assert engine._lap_signature_measurable(
        origin_head=origin_head, check_conclusion=conclusion,
    ) is expected


@pytest.mark.asyncio
async def test_conflict_route_counts_unchanged_signature(
    mem_fs: dict[str, str],
) -> None:
    """A second conflict route against the same signature advances the count.

    ``_conflicted_engine`` resolves every ``rev_parse`` to "h" and reads a
    "success" rollup, so the second lap faces the signature the first stamped.
    """
    engine = _conflicted_engine(mem_fs, probe_files=["a.py"])
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    first = engine._memory.get_plan("ep1")
    assert first is not None
    # First-ever stamp: no prior lap bounced off anything, so the ladder starts
    # at its first rung rather than counting the all-``None`` legacy signature.
    assert first.conflict_resolution_signature_attempts == 0
    # Age the anchor past the ceiling so the second lap routes again.
    engine._memory.update_plan_tend_fields(
        "ep1",
        last_conflict_resolution_attempt_at=(
            datetime.now(UTC) - timedelta(hours=48)
        ).isoformat(),
    )
    await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    second = engine._memory.get_plan("ep1")
    assert second is not None
    assert second.conflict_resolution_signature_attempts == 1


@pytest.mark.asyncio
async def test_conflict_route_resets_count_on_changed_signature(
    mem_fs: dict[str, str],
) -> None:
    """A moved head restarts the ladder: the stamped count returns to ``0``."""
    engine = _conflicted_engine(mem_fs, probe_files=["a.py"])
    engine._memory.save_evolution_plan(
        _plan(
            last_conflict_attempt=(
                datetime.now(UTC) - timedelta(hours=48)
            ).isoformat(),
            last_conflict_base_sha="h",
            last_conflict_head_sha="oldhead",
            last_conflict_ci_conclusion="success",
            conflict_attempts=4,
        ),
    )
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.conflict_resolution_signature_attempts == 0


def _stranded_conflict_state() -> dict[str, PrMergeState]:
    """The open-PR map every consuming-lap test below drives."""
    return {
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }


def _ahead_conflict_engine(
    mem_fs: dict[str, str],
    *,
    config: Config | None = None,
) -> EvolutionEngine:
    """A conflicting-PR engine whose branch is strictly AHEAD of its own remote.

    ``_conflicted_engine`` cannot express this: ``_ground_vcs`` captures the
    ``commits_ahead_behind`` return value at wiring time, so a later
    ``return_value`` assignment is ignored. Built here in the right order, the
    ``(2, 0)`` reading drives the ahead-of-origin arm that runs the gates,
    pushes, and then falls through to the conflict route.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (2, 0)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    vcs.merge_from.return_value = MergeResult(
        success=False, conflicted_files=["a.py"],
    )
    _ground_vcs(vcs, behind=1, merge_clean=False)
    return _make_engine(mem_fs, vcs=vcs, config=config)


def _aged_rung_plan(
    *, prior: int = 3, head: str = "h", plan_id: str = "ep1",
) -> EvolutionPlan:
    """A plan whose conflict anchor is aged past every window — so the cooldown
    is not holding and the lap is never a bypassed one — sitting on rung
    *prior* with a signature that matches the reading the engines produce."""
    return _plan(
        plan_id=plan_id,
        last_conflict_attempt=(
            datetime.now(UTC) - timedelta(hours=48)
        ).isoformat(),
        last_conflict_base_sha="h",
        last_conflict_head_sha=head,
        last_conflict_ci_conclusion="success",
        conflict_attempts=prior,
    )


def _seeded_rung_plan(*, prior: int = 3, head: str = "h") -> EvolutionPlan:
    """A plan sitting on rung *prior* whose stamped signature matches the
    reading ``_conflicted_engine`` produces, so an unchanged lap would
    otherwise advance the rung to ``prior + 1``."""
    return _plan(
        last_conflict_attempt=datetime.now(UTC).isoformat(),
        last_conflict_base_sha="h",
        last_conflict_head_sha=head,
        last_conflict_ci_conclusion="success",
        conflict_attempts=prior,
    )


@pytest.mark.asyncio
async def test_hatch_admitted_lap_consumes_no_rung(
    mem_fs: dict[str, str],
) -> None:
    """A lap the conflict cooldown was holding, admitted by the awaiting-push
    escape hatch, re-routes but leaves the rung untouched.

    The cooldown *was* governing this PR — the hatch is what let the walk past
    it — so counting the re-route as a bounce would open a later genuine
    cooldown part-way up the ladder on a lap it never governed.
    """
    engine = _conflicted_engine(mem_fs, probe_files=["a.py"])
    engine._memory.save_evolution_plan(_seeded_rung_plan(prior=3))
    # The unconsumed awaiting-tend-push verdict against the current head is the
    # sole reason the fresh anchor's cooldown does not skip this PR.
    engine._memory.record_task(_awaiting_push_task(pr_number=1, head="h"))
    engine._host.get_open_evolution_pr_states.return_value = (  # type: ignore[attr-defined]
        _stranded_conflict_state()
    )
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # The hatch fired: no cooldown skip, and the conflict was re-routed.
    assert result.conflicts_cooldown_skipped == 0
    assert len(result.conflicts_routed) == 1
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.conflict_resolution_signature_attempts == 3


@pytest.mark.asyncio
async def test_pushing_lap_consumes_no_rung(
    mem_fs: dict[str, str],
) -> None:
    """A lap whose ahead-of-origin push succeeded leaves the rung untouched.

    The push advanced ``origin/<branch>``, so the anchor this lap stamps names
    the *pre-push* head — a head the routed outcome was never measured against.
    """
    engine = _ahead_conflict_engine(mem_fs)
    engine._memory.save_evolution_plan(_aged_rung_plan(prior=3))
    engine._host.get_open_evolution_pr_states.return_value = (  # type: ignore[attr-defined]
        _stranded_conflict_state()
    )
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    engine._vcs.push.assert_any_call("maestro/evolve/ep1")
    assert len(result.conflicts_routed) == 1
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.conflict_resolution_signature_attempts == 3


@pytest.mark.asyncio
async def test_failed_push_still_consumes_its_rung(
    mem_fs: dict[str, str],
) -> None:
    """The push flag tracks the *outcome*: a rejected push moved no head, so
    that lap really did bounce off the head it is stamping."""
    engine = _ahead_conflict_engine(mem_fs)
    engine._vcs.push.side_effect = VCSError("rejected")
    engine._memory.save_evolution_plan(_aged_rung_plan(prior=3))
    engine._host.get_open_evolution_pr_states.return_value = (  # type: ignore[attr-defined]
        _stranded_conflict_state()
    )
    await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.conflict_resolution_signature_attempts == 4


@pytest.mark.asyncio
async def test_unmeasurable_head_consumes_no_rung(
    mem_fs: dict[str, str],
) -> None:
    """A lap whose ``origin/<branch>`` head does not resolve observed nothing,
    so it neither advances nor resets the ladder."""
    engine = _conflicted_engine(mem_fs, probe_files=["a.py"])

    def _rev_parse(ref: str) -> str | None:
        return None if ref == "origin/maestro/evolve/ep1" else "h"

    engine._vcs.rev_parse.side_effect = _rev_parse
    engine._memory.save_evolution_plan(_aged_rung_plan(prior=3))
    engine._host.get_open_evolution_pr_states.return_value = (  # type: ignore[attr-defined]
        _stranded_conflict_state()
    )
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert len(result.conflicts_routed) == 1
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.conflict_resolution_signature_attempts == 3


@pytest.mark.asyncio
async def test_pushing_lap_leaves_ladder_at_first_rung_for_new_head(
    mem_fs: dict[str, str],
) -> None:
    """The stranded-PR sequence: a pushing lap must not carry a rung onto the
    head it created.

    Lap 1 is anchored on head ``A`` and pushes; lap 2 faces the new head ``B``.
    Because lap 1 consumed nothing, the anchor lap 2 writes sits at rung ``0``
    — so the next genuine cooldown opens at the ladder's first rung (one hour)
    rather than part-way up it.
    """
    engine = _ahead_conflict_engine(mem_fs)
    heads = {"origin/maestro/evolve/ep1": "A"}

    def _rev_parse(ref: str) -> str | None:
        return heads.get(ref, "h")

    engine._vcs.rev_parse.side_effect = _rev_parse
    engine._memory.save_evolution_plan(_aged_rung_plan(prior=0, head="A"))
    engine._host.get_open_evolution_pr_states.return_value = (  # type: ignore[attr-defined]
        _stranded_conflict_state()
    )
    await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    after_push = engine._memory.get_plan("ep1")
    assert after_push is not None
    assert after_push.conflict_resolution_signature_attempts == 0
    assert after_push.last_conflict_resolution_head_sha == "A"

    # The push landed: the remote head is now ``B``. Age the anchor so the next
    # lap routes, and re-run.
    heads["origin/maestro/evolve/ep1"] = "B"
    engine._memory.update_plan_tend_fields(
        "ep1",
        last_conflict_resolution_attempt_at=(
            datetime.now(UTC) - timedelta(hours=48)
        ).isoformat(),
    )
    await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    after_new_head = engine._memory.get_plan("ep1")
    assert after_new_head is not None
    assert after_new_head.conflict_resolution_signature_attempts == 0
    assert after_new_head.last_conflict_resolution_head_sha == "B"


@pytest.mark.parametrize("keyed", [True, False])
@pytest.mark.asyncio
async def test_rung_gate_is_flag_independent(
    mem_fs: dict[str, str],
    keyed: bool,
) -> None:
    """With ``cooldown_signature_keying_enabled`` off the recorded count and
    the routing decision are identical — the gate is unconditional bookkeeping
    exactly like the stamps it sits beside."""
    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        cooldown_signature_keying_enabled=keyed,
    )
    engine = _ahead_conflict_engine(mem_fs, config=cfg)
    engine._memory.save_evolution_plan(_aged_rung_plan(prior=3))
    engine._host.get_open_evolution_pr_states.return_value = (  # type: ignore[attr-defined]
        _stranded_conflict_state()
    )
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert len(result.conflicts_routed) == 1
    assert result.conflicts_cooldown_skipped == 0
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.conflict_resolution_signature_attempts == 3


@pytest.mark.asyncio
async def test_hour_old_unchanged_anchor_reroutes_under_backoff(
    mem_fs: dict[str, str],
) -> None:
    """The seeded regression: an hour-old anchor on an UNCHANGED signature
    re-routes under the flag, where the flat 24h window skipped it.

    This is the production strand — 41 consecutive laps logged a cooldown skip
    and routed nothing while the fix was one lap's work.
    """
    seeded = _plan(
        last_conflict_attempt=(
            datetime.now(UTC) - timedelta(hours=1.5)
        ).isoformat(),
        last_conflict_base_sha="h",
        last_conflict_head_sha="h",
        last_conflict_ci_conclusion="success",
    )
    flat = _conflicted_engine(mem_fs, probe_files=["a.py"])
    flat._memory.save_evolution_plan(seeded)
    flat._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    flat_result = await flat.tend_evolution_prs(settled_oracle=merge_oracle())
    assert flat_result.conflicts_routed == []
    assert flat_result.conflicts_cooldown_skipped == 1

    # Same engine, same untouched anchor — only the flag changes.
    flat._config.cooldown_signature_keying_enabled = True
    keyed_result = await flat.tend_evolution_prs(settled_oracle=merge_oracle())
    assert keyed_result.conflicts_routed != []


@pytest.mark.asyncio
async def test_conflict_route_stamps_base_sha(
    mem_fs: dict[str, str],
) -> None:
    """Routing a conflict stamps the base SHA on the cooldown anchor (099).

    The base this cooldown anchor raced against is recorded unconditionally so
    ``_in_conflict_cooldown`` can release it on a later base advance.
    """
    engine = _conflicted_engine(mem_fs, probe_files=["a.py"])
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.conflicts_routed != []
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.last_conflict_resolution_attempt_at is not None
    # ``_conflicted_engine`` resolves every ``rev_parse`` to "h".
    assert plan.last_conflict_resolution_base_sha == "h"
    # Proposal 137: the rest of the signature this anchor raced against — the
    # branch head the walk resolved for the ledger and the CI rollup it read
    # once this lap — lands beside it, unconditionally.
    assert plan.last_conflict_resolution_head_sha == "h"
    assert plan.last_conflict_resolution_ci_conclusion == "success"


@pytest.mark.asyncio
async def test_dirty_pr_in_cooldown_skips_route(
    mem_fs: dict[str, str],
) -> None:
    """A DIRTY PR within ``conflict_resolution_cooldown_hours`` is not
    re-routed."""
    recent_iso = datetime.now().isoformat() + "+00:00"
    engine = _conflicted_engine(mem_fs, probe_files=["a.py"])
    engine._memory.save_evolution_plan(
        _plan(last_conflict_attempt=recent_iso),
    )
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.conflicts_cooldown_skipped == 1
    assert result.conflicts_routed == []


@pytest.mark.asyncio
async def test_conflict_cooldown_released_on_head_advance(
    mem_fs: dict[str, str],
) -> None:
    """A delivered head-advance clears the conflict cooldown so a
    freshly-resolved PR is re-attempted instead of stranded (#2821)."""
    recent_iso = datetime.now().isoformat() + "+00:00"
    engine = _conflicted_engine(mem_fs, probe_files=["a.py"])
    engine._memory.save_evolution_plan(
        _plan(last_conflict_attempt=recent_iso),
    )
    # Pre-seed the ledger with a *prior* head distinct from the one
    # ``rev_parse("origin/<branch>")`` returns now ("h"): a delivered advance.
    engine._pr_ledger.observe_head(1, "maestro/evolve/ep1", "oldhead")
    # Defer at the CI-wait gate so the fall-through reaches ``_tend_single_pr``
    # and stops there — proving the cooldown released — without a fresh
    # conflict route re-stamping the anchor.
    engine._host.get_pr_check_conclusion.return_value = ""  # type: ignore[attr-defined]
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Not cooldown-skipped: the advance released the cooldown and the PR fell
    # through into the merge body (which then defers on pending CI).
    assert result.conflicts_cooldown_skipped == 0
    assert result.deferred_for_ci == 1
    # The anchor is cleared and stays cleared — the CI defer precedes any
    # merge/route re-stamp.
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.last_conflict_resolution_attempt_at is None


@pytest.mark.asyncio
async def test_conflict_cooldown_preserved_on_unchanged_head(
    mem_fs: dict[str, str],
) -> None:
    """An unchanged origin head keeps the conflict cooldown intact — the
    head-advance release must not fire without a real advance (#2821)."""
    recent_iso = datetime.now().isoformat() + "+00:00"
    engine = _conflicted_engine(mem_fs, probe_files=["a.py"])
    engine._memory.save_evolution_plan(
        _plan(last_conflict_attempt=recent_iso),
    )
    # Pre-seed the ledger with the *same* head ``rev_parse`` returns ("h"):
    # no advance, so the cooldown must survive and still bounce tend.
    engine._pr_ledger.observe_head(1, "maestro/evolve/ep1", "h")
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.conflicts_cooldown_skipped == 1
    assert result.conflicts_routed == []
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.last_conflict_resolution_attempt_at == recent_iso


# ---------------------------------------------------------------------------
# Unconsumed awaiting-tend-push verdict bypasses the conflict cooldown (#2825)
# ---------------------------------------------------------------------------


def _awaiting_push_task(
    *,
    pr_number: int = 1,
    head: str | None = "h",
    summary: str | None = None,
    key_findings: str | None = None,
    task_id: str = "rem-1",
) -> TaskRecord:
    """A dedicated ``Remediate PR #N`` lap that deferred its push to tend.

    Defaults stamp the on-branch ``awaiting tend push`` marker into
    ``result_summary`` against origin head *head*, mirroring the real
    remediation verdict (``agent.py``).
    """
    if summary is None:
        summary = f"Resolved on branch, {Marker.AWAITING_TEND_PUSH} — deferred push"
    return TaskRecord(
        task_id=task_id,
        goal=f"Remediate PR #{pr_number}: resolve conflict",
        plan=[],
        outcome="partial",
        result_summary=summary,
        errors=[],
        duration_secs=0.0,
        timestamp="2026-06-26T00:00:00+00:00",
        pr_head_sha=head,
        key_findings=key_findings,
    )


@pytest.mark.asyncio
async def test_conflict_cooldown_bypassed_for_unconsumed_awaiting_tend_push(
    mem_fs: dict[str, str],
) -> None:
    """A CONFLICTING PR whose latest ``#N`` remediation deferred its push to
    tend bypasses the conflict cooldown and reaches ``_tend_single_pr`` (#2825).

    The plan is inside an active conflict cooldown with no local commits ahead,
    and the ledger is unseeded so the #2821 head-advance release cannot fire —
    the unconsumed awaiting-tend-push verdict for the *current* head ("h") is
    the sole reason the skip is bypassed. ``_tend_single_pr`` then defers on
    pending CI, proving fall-through without a fresh conflict route.
    """
    recent_iso = datetime.now().isoformat() + "+00:00"
    engine = _conflicted_engine(mem_fs, probe_files=["a.py"])
    engine._memory.save_evolution_plan(
        _plan(last_conflict_attempt=recent_iso),
    )
    # Verdict recorded against the current observed origin head ("h" — what
    # ``rev_parse("origin/<branch>")`` returns in ``_conflicted_engine``).
    engine._memory.record_task(_awaiting_push_task(pr_number=1, head="h"))
    # Defer at the CI-wait gate so fall-through stops in ``_tend_single_pr``.
    engine._host.get_pr_check_conclusion.return_value = ""  # type: ignore[attr-defined]
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # The bypass fired: not cooldown-skipped, not re-routed; it fell into the
    # merge body which deferred on pending CI.
    assert result.conflicts_cooldown_skipped == 0
    assert result.conflicts_routed == []
    assert result.deferred_for_ci == 1
    # No fresh conflict route — the CI defer precedes any re-stamp, so the
    # cooldown anchor is untouched (the #2821 release did not fire either).
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.last_conflict_resolution_attempt_at == recent_iso


@pytest.mark.asyncio
async def test_conflict_cooldown_not_bypassed_when_awaiting_push_consumed(
    mem_fs: dict[str, str],
) -> None:
    """Once the observed head advances past the verdict's head the bypass does
    not re-fire and the cooldown skip stands (#2825 bounding case).

    The verdict was recorded against ``"oldhead"`` but the PR's current
    observed head is ``"h"`` — tend already pushed an advance, consuming the
    verdict — so the awaiting-push disjunct is False and the conflict cooldown
    still bounces tend off the PR.
    """
    recent_iso = datetime.now().isoformat() + "+00:00"
    engine = _conflicted_engine(mem_fs, probe_files=["a.py"])
    engine._memory.save_evolution_plan(
        _plan(last_conflict_attempt=recent_iso),
    )
    # Verdict head ("oldhead") no longer matches the current observed head
    # ("h"): the verdict is stale/consumed.
    engine._memory.record_task(_awaiting_push_task(pr_number=1, head="oldhead"))
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.conflicts_cooldown_skipped == 1
    assert result.conflicts_routed == []
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.last_conflict_resolution_attempt_at == recent_iso


def test_has_unconsumed_awaiting_push_verdict_true_when_live(
    mem_fs: dict[str, str],
) -> None:
    """The helper returns True for a marker-bearing ``#N`` lap whose recorded
    head matches the current observed head."""
    engine = _make_engine(mem_fs)
    engine._memory.record_task(_awaiting_push_task(pr_number=7, head="h"))
    assert evolution_tend._has_unconsumed_awaiting_push_verdict(engine, 7, "h") is True


def test_has_unconsumed_awaiting_push_verdict_marker_in_key_findings(
    mem_fs: dict[str, str],
) -> None:
    """The marker is detected in ``key_findings`` as well as ``result_summary``."""
    engine = _make_engine(mem_fs)
    engine._memory.record_task(
        _awaiting_push_task(
            pr_number=7,
            head="h",
            summary="resolved",
            key_findings=f"note: {Marker.AWAITING_TEND_PUSH}",
        ),
    )
    assert evolution_tend._has_unconsumed_awaiting_push_verdict(engine, 7, "h") is True


def test_has_unconsumed_awaiting_push_verdict_true_for_delivered_marker(
    mem_fs: dict[str, str],
) -> None:
    """A delivered-then-reset head still earns the cooldown bypass: the helper
    matches DELIVERED_AWAITING_MERGE as well as AWAITING_TEND_PUSH so Tend PR
    re-enters and re-pushes the resolution (proposal 091 step 6)."""
    engine = _make_engine(mem_fs)
    engine._memory.record_task(
        _awaiting_push_task(
            pr_number=7,
            head="h",
            summary=(
                f"Resolved on branch, {Marker.DELIVERED_AWAITING_MERGE} "
                "— delivered"
            ),
        ),
    )
    assert evolution_tend._has_unconsumed_awaiting_push_verdict(engine, 7, "h") is True


def test_has_unconsumed_awaiting_push_verdict_true_for_owned_push_marker(
    mem_fs: dict[str, str],
) -> None:
    """An owned-push lap earns the cooldown bypass too (proposal 098).

    §5-push now stamps ``OWNED_PUSH_AVAILABLE`` on exactly the laps this hatch
    exists for — a resolution sitting un-pushed on the branch. Without the
    marker in the blob match a failed-push lineage would silently lose the
    #2825 bypass the moment the marker swapped.
    """
    engine = _make_engine(mem_fs)
    engine._memory.record_task(
        _awaiting_push_task(
            pr_number=7,
            head="h",
            summary=(
                f"Resolved on branch, {Marker.OWNED_PUSH_AVAILABLE} — "
                "push failed"
            ),
        ),
    )
    assert evolution_tend._has_unconsumed_awaiting_push_verdict(engine, 7, "h") is True


def test_has_unconsumed_awaiting_push_verdict_true_for_push_unconfirmed_marker(
    mem_fs: dict[str, str],
) -> None:
    """A push-unconfirmed lap earns the cooldown bypass too (proposal 099).

    A no-op push (exit 0, origin un-advanced) leaves the resolution sitting
    un-pushed on the branch — precisely the #2825 condition — so it is the
    OWNED_PUSH_AVAILABLE sibling and must keep the conflict-cooldown bypass so
    Tend PR re-enters and re-pushes.
    """
    engine = _make_engine(mem_fs)
    engine._memory.record_task(
        _awaiting_push_task(
            pr_number=7,
            head="h",
            summary=(
                f"Resolved on branch, {Marker.PUSH_UNCONFIRMED} — "
                "push exited 0 but origin did not advance"
            ),
        ),
    )
    assert evolution_tend._has_unconsumed_awaiting_push_verdict(engine, 7, "h") is True


def test_has_unconsumed_awaiting_push_verdict_false_on_head_mismatch(
    mem_fs: dict[str, str],
) -> None:
    """A verdict recorded against a different head is consumed → False."""
    engine = _make_engine(mem_fs)
    engine._memory.record_task(_awaiting_push_task(pr_number=7, head="oldhead"))
    assert evolution_tend._has_unconsumed_awaiting_push_verdict(engine, 7, "h") is False


def test_has_unconsumed_awaiting_push_verdict_false_without_marker(
    mem_fs: dict[str, str],
) -> None:
    """A matching ``#N`` lap without the awaiting-push marker → False."""
    engine = _make_engine(mem_fs)
    engine._memory.record_task(
        _awaiting_push_task(pr_number=7, head="h", summary="merged cleanly"),
    )
    assert evolution_tend._has_unconsumed_awaiting_push_verdict(engine, 7, "h") is False


def test_has_unconsumed_awaiting_push_verdict_false_without_matching_task(
    mem_fs: dict[str, str],
) -> None:
    """No dedicated ``#N`` lap in history → False."""
    engine = _make_engine(mem_fs)
    engine._memory.record_task(_awaiting_push_task(pr_number=99, head="h"))
    assert evolution_tend._has_unconsumed_awaiting_push_verdict(engine, 7, "h") is False


def test_has_unconsumed_awaiting_push_verdict_false_on_empty_head(
    mem_fs: dict[str, str],
) -> None:
    """An empty observed head can match no verdict → False, no history read."""
    engine = _make_engine(mem_fs)
    engine._memory.record_task(_awaiting_push_task(pr_number=7, head="h"))
    assert evolution_tend._has_unconsumed_awaiting_push_verdict(engine, 7, None) is False


def test_has_unconsumed_awaiting_push_verdict_false_on_read_error(
    mem_fs: dict[str, str],
) -> None:
    """A task-store read error is swallowed to False so a quirky store never
    forces a re-tend."""
    engine = _make_engine(mem_fs)
    engine._memory.get_recent_tasks = MagicMock(  # type: ignore[method-assign]
        side_effect=OSError("store down"),
    )
    assert evolution_tend._has_unconsumed_awaiting_push_verdict(engine, 7, "h") is False


@pytest.mark.asyncio
async def test_unexpected_merge_conflict_routes(mem_fs: dict[str, str]) -> None:
    """A PR the host called CLEAN but ``merge_from`` reports conflicted
    is still routed via ``conflicts_routed``."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    vcs.merge_from.return_value = MergeResult(
        success=False, conflicted_files=["surprise.py"],
    )
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.conflicts_routed == [
        (
            1, "t", ["surprise.py"],
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]


@pytest.mark.asyncio
async def test_dirty_pr_merge_raises_records_error_not_conflict(
    mem_fs: dict[str, str],
) -> None:
    """When the local merge raises ``VCSError`` on a host-flagged PR, the
    failure is recorded on ``result.errors`` — not routed as a phantom
    empty-file conflict directive."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=False)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    vcs.merge_from.side_effect = VCSError("merge boom")
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.conflicts_routed == []
    assert result.merged_updated == 0
    assert len(result.errors) == 1
    assert "merge boom" in result.errors[0]
    # No conflict cooldown armed by a transient merge failure.
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.last_conflict_resolution_attempt_at is None


@pytest.mark.asyncio
async def test_dirty_pr_genuine_conflict_aborts_and_routes(
    mem_fs: dict[str, str],
) -> None:
    """A host-flagged PR with a genuine local conflict aborts the merge,
    routes the real conflicted files, and arms the conflict cooldown."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=False)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    vcs.merge_from.return_value = MergeResult(
        success=False, conflicted_files=["x.py"],
    )
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.abort_merge.assert_called_once()
    assert result.conflicts_routed == [
        (
            1, "t", ["x.py"],
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    assert result.merged_updated == 0
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.last_conflict_resolution_attempt_at is not None


@pytest.mark.asyncio
async def test_conflict_with_local_commits_pushes_before_routing(
    mem_fs: dict[str, str],
) -> None:
    """#2479 — a branch whose local head strictly extends its own remote
    head (``ahead>0``, ``behind==0`` vs ``origin/<branch>``) must ship that
    head even when the fresh ``origin/main`` merge re-conflicts: the
    gate-validated resolution is pushed, and the bounded conflict is still
    routed for remediation."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (1, 0)  # local resolution ahead
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(
        success=False, conflicted_files=["hot.py"],
    )
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Local resolution delivered despite the re-conflict.
    vcs.push.assert_called_once_with("maestro/evolve/ep1")
    # Conflict still routed — bounded, spanning only main's recent movement.
    assert result.conflicts_routed == [
        (
            1, "t", ["hot.py"],
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    assert result.merged_updated == 0
    vcs.abort_merge.assert_called_once()


@pytest.mark.asyncio
async def test_conflict_local_push_failure_is_surfaced(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """#2479 — when the local-resolution push fails, the failure lands in
    ``result.errors`` AND is logged at WARNING by the fail-loud sink; the
    conflict is still routed."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (1, 0)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(
        success=False, conflicted_files=["hot.py"],
    )
    vcs.push.side_effect = VCSError("push boom")
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.push.assert_called_once_with("maestro/evolve/ep1")
    assert len(result.errors) == 1
    assert "push of local resolution failed" in result.errors[0]
    assert "push boom" in result.errors[0]
    # Fail-loud: the swallowed failure is now a WARNING log line.
    logged = [
        rec.getMessage() for rec in caplog.records
        if rec.levelno == logging.WARNING
    ]
    assert any(
        "push of local resolution failed" in msg for msg in logged
    )
    # The conflict is still routed despite the delivery failure.
    assert len(result.conflicts_routed) == 1


@pytest.mark.asyncio
async def test_conflict_without_local_commits_does_not_push(
    mem_fs: dict[str, str],
) -> None:
    """#2479 guard — a conflicting branch with NO local commits ahead
    (``ahead==0``) behaves exactly as before: abort + route, no early push,
    and no gate run on the conflict path."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=False)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(
        success=False, conflicted_files=["x.py"],
    )
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.push.assert_not_called()
    engine.run_quality_gates.assert_not_called()  # type: ignore[attr-defined]
    assert len(result.conflicts_routed) == 1
    assert result.merged_updated == 0


@pytest.mark.asyncio
async def test_conflict_local_commits_gate_failure_skips_push(
    mem_fs: dict[str, str],
) -> None:
    """#2479 guard — when a branch has local commits ahead
    (``ahead>0``, ``behind==0``) but the local resolution fails the quality
    gates, the unvalidated head must NOT be pushed; the conflict is still
    routed for remediation."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (1, 0)  # local resolution ahead
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(
        success=False, conflicted_files=["hot.py"],
    )
    engine = _make_engine(mem_fs, vcs=vcs, stub_quality_gates=False)
    engine.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_failing_gates(),
    )
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Gates ran but failed → the broken local head is held back.
    engine.run_quality_gates.assert_called_once()
    vcs.push.assert_not_called()
    # The conflict is still routed despite withholding the push.
    assert len(result.conflicts_routed) == 1
    assert result.merged_updated == 0


@pytest.mark.asyncio
async def test_dirty_pr_local_clean_merge_keeps_and_pushes(
    mem_fs: dict[str, str],
) -> None:
    """When the host flags conflict but the local merge succeeds (a stale
    ``CONFLICTING`` status), the local result wins: the clean merge is kept,
    quality gates run, the branch is pushed, and nothing is routed — no
    phantom conflict directive and no conflict cooldown armed."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=False)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.conflicts_routed == []
    assert result.merged_updated == 1
    vcs.push.assert_called_once_with("maestro/evolve/ep1")
    # The conflict cooldown is NOT armed: a perpetually host-stale-but-clean
    # PR converges to merged instead of cycling through cooldown.
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.last_conflict_resolution_attempt_at is None


# ---------------------------------------------------------------------------
# Conflict-cooldown skip — observability (#2595)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conflict_cooldown_skip_emits_info_log(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """Conflict-cooldown skip emits one INFO line naming PR and expiry (#2595)."""
    recent_iso = datetime.now().astimezone().isoformat()
    engine = _conflicted_engine(mem_fs, probe_files=["a.py"])
    engine._memory.save_evolution_plan(
        _plan(last_conflict_attempt=recent_iso),
    )
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }

    with caplog.at_level(logging.INFO, logger="maestro"):
        result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.conflicts_cooldown_skipped == 1
    cooldown_messages = [
        r.getMessage()
        for r in caplog.records
        if "conflict cooldown" in r.getMessage().lower()
    ]
    assert len(cooldown_messages) == 1
    msg = cooldown_messages[0]
    assert "PR #1" in msg
    assert "conflict cooldown" in msg.lower()
    assert "skipping" in msg.lower()


# ---------------------------------------------------------------------------
# Proposal 137 — cooldown-skip owed-act stamp (measurement only)
# ---------------------------------------------------------------------------


def _cooldown_skip_rows(engine: EvolutionEngine) -> list[object]:
    """Every ``tend_cooldown_skip`` row the engine's gate backend holds."""
    return [
        row
        for row in engine._gate_activations.read_all()
        if row.gate == COOLDOWN_SKIP_GATE
    ]


@pytest.mark.asyncio
async def test_conflict_cooldown_skip_stamps_diverged_row(
    mem_fs: dict[str, str],
) -> None:
    """A conflict-cooldown skip of a code-owed PR stamps one ``diverged`` row.

    The walk measures the owed act for this PR one frame above the skip; a
    DIRTY/CONFLICTING branch reads ``code-owed``, so declining it is exactly the
    divergence the sensor exists to count. Behaviour is unchanged — the skip and
    its counter are untouched.
    """
    recent = datetime.now().astimezone().isoformat()
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=False)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan(last_conflict_attempt=recent))
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            number=7, merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.conflicts_cooldown_skipped == 1
    rows = _cooldown_skip_rows(engine)
    assert len(rows) == 1
    assert rows[0].action == "diverged"
    assert rows[0].detail == format_cooldown_skip_detail(
        7, "conflict", OwedAct.CODE_OWED.value,
    )


@pytest.mark.asyncio
async def test_quality_fix_cooldown_skip_stamps_a_row(
    mem_fs: dict[str, str],
) -> None:
    """A quality-fix-cooldown skip stamps one row through the shared renderer.

    This is the site the dead-window pathology was measured at: a ``failure``
    head with no local commits, inside the cooldown window, previously left
    nothing behind but a log line.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    _ground_vcs(vcs, behind=1, merge_clean=True)
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    plan = _plan()
    plan.last_quality_fix_attempt_at = datetime.now(UTC).isoformat()
    engine._memory.save_evolution_plan(plan)
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            number=3, merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.quality_gates_routed == []
    rows = _cooldown_skip_rows(engine)
    assert len(rows) == 1
    assert rows[0].detail == format_cooldown_skip_detail(
        3, "quality_fix", result.owed_acts_observed[3],
    )


@pytest.mark.asyncio
async def test_no_cooldown_row_when_the_lap_routes(
    mem_fs: dict[str, str],
) -> None:
    """A lap that services the PR rather than skipping stamps nothing.

    The local-commits escape hatch bypasses the conflict cooldown, so the tend
    body runs — there is no skip to record.
    """
    recent = datetime.now().astimezone().isoformat()
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (2, 0)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan(last_conflict_attempt=recent))
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.conflicts_cooldown_skipped == 0
    assert _cooldown_skip_rows(engine) == []


def test_stamp_cooldown_skip_marks_a_non_code_owed_reading_aligned(
    mem_fs: dict[str, str],
) -> None:
    """A skip of a PR owing something other than code stamps ``aligned``.

    Driven directly at the helper because a conflict cooldown only holds on a
    branch that is already code-owed; the quality-fix site and any future caller
    can present any reading, including none at all.
    """
    engine = _make_engine(mem_fs)
    result = TendPRsResult()
    result.owed_acts_observed[4] = OwedAct.WAIT_CI.value
    evolution_tend._stamp_cooldown_skip(
        engine,
        pr_number=4, cooldown="quality_fix", result=result,
    )
    evolution_tend._stamp_cooldown_skip(
        engine,
        pr_number=5, cooldown="conflict", result=result,
    )
    rows = _cooldown_skip_rows(engine)
    assert [row.action for row in rows] == ["aligned", "aligned"]
    assert rows[0].detail == "pr=#4 cooldown=quality_fix owed=wait-ci"
    assert rows[1].detail == "pr=#5 cooldown=conflict owed=-"
