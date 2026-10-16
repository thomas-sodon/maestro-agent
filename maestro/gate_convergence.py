"""Pure gate-convergence predicate + result type (proposal 110).

A single owner of the decision *"grind this branch to green or stop honestly."*
The predicate is pure — zero I/O, zero SDK — so it is trivially testable and
carries no side effects; the :meth:`EvolutionEngine.converge_to_green`
orchestrator in :mod:`maestro.evolution` drives the trusted
``run_quality_gates`` + ``auto_fix`` primitives through it. The
:class:`QualityGateResult` reference is type-only (guarded by
``TYPE_CHECKING``) so this module imports nothing from
:mod:`maestro.evolution` at runtime, keeping the dependency one-directional.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from maestro.gate_observation import regression_markers

if TYPE_CHECKING:
    from maestro.evolution import QualityGateResult
    from maestro.gate_environment import GateRunnabilityVerdict
    from maestro.gate_observation import Attribution

ConvergenceDecision = Literal["green", "continue", "stuck"]


def plan_next_round(
    prev_failures: frozenset[str],
    curr_failures: frozenset[str],
    *,
    all_clean: bool,
    rounds_used: int,
    ceiling: int,
    prev_count: int | None = None,
    curr_count: int | None = None,
    truncated: bool = False,
) -> ConvergenceDecision:
    """Decide the next step after one fix round.

    Rules, applied in order:

    - ``all_clean`` → ``"green"`` (every observed gate is CLEAN; a green head
      wins even when the ceiling has also been reached). Green binds to the
      per-gate CLEAN status, **not** to an empty ``curr_failures`` set — a second,
      independent lock (proposal 124). The reachable case it guards is an
      observation that enumerated *no* gates at all: :meth:`GateRunObservation.all_clean`
      is fail-closed — ``False`` for an empty ``gates`` tuple — while its
      ``signature()`` is empty too, so binding green to ``all_clean`` refuses a
      run that observed nothing rather than passing it off as clean. (A ``TIMEOUT``
      or coverage-only red is already non-empty — :meth:`GateObservation.signature`
      gives it a synthetic ``<tool>:<kind>`` identity — so the old
      ``not curr_failures`` rule already refused those; this lock adds only the
      observed-nothing case that rule missed.)
    - ``rounds_used >= ceiling`` → ``"stuck"`` (the backstop terminates the
      loop even when the failing set strictly shrank this round, so the
      orchestrator always terminates).
    - ``curr_failures`` a strict subset of ``prev_failures`` → ``"continue"``
      (real progress — at least one failure cleared without a new one appearing).
      ``curr_failures`` / ``prev_failures`` now carry *failure-identity*
      signatures when the caller supplies them (:meth:`GateRunObservation.signature`,
      one identity per failing test / diagnostic) rather than the three coarse
      gate labels, so a fix round that clears eleven of twelve failing tests is a
      strict subset — real progress — instead of an unchanged single-label set.
      The caller passes the *progress* vocabulary
      (:meth:`GateRunObservation.progress_signature`), whose ruff/mypy members
      carry no line number, so a fix that shifts a surviving diagnostic's line
      does not rename it (#4309).
    - ``truncated`` (either side's enumeration hit the parser cap) →
      ``"continue"`` iff ``curr_count < prev_count``. Two capped windows are not
      comparable as sets — a partial fix pulls unseen rows into the window and
      xdist completion order reshuffles it — so the full-enumeration counts
      decide instead. This keeps "eleven of twelve" representable as progress
      for any failure count, not only below the cap. Both counts are required.
      A round that newly carries a synthetic non-judgement marker
      (:func:`~maestro.gate_observation.regression_markers` — a timeout, an
      unavailable runner, a collection error) is ``"stuck"`` regardless of the
      counts: its rows fell because the gate stopped looking, not because a
      failure was fixed.
    - otherwise → ``"stuck"`` (nothing cleared; grinding further is futile).
    """
    if all_clean:
        return "green"
    if rounds_used >= ceiling:
        return "stuck"
    if truncated:
        assert prev_count is not None and curr_count is not None, (
            "a truncated comparison must carry both enumeration counts"
        )
        if regression_markers(prev_failures, curr_failures):
            return "stuck"
        return "continue" if curr_count < prev_count else "stuck"
    if curr_failures < prev_failures:
        return "continue"
    return "stuck"


def plan_next_review_round(
    *,
    approved: bool,
    made_changes: bool,
    rounds_used: int,
    ceiling: int,
    gate_clean: bool,
    verify_grace_remaining: int,
) -> ConvergenceDecision:
    """Decide the next step after one self-review round (proposal 110/124).

    The self-review sibling of :func:`plan_next_round`: instead of a
    failing-gate set it reads two per-round signals — whether the reviewer
    approved (``LGTM``) and whether it committed a fix — plus two proposal-124
    signals that let a *verification* round (no approval, no commit) buy a
    further round when the branch is provably green, following the shape
    :func:`plan_next_round` already uses for its ``all_clean`` lock. Rules, in
    order, mirroring :func:`plan_next_round`:

    - ``approved`` → ``"green"`` (an approval wins even on the final ceiling
      round, exactly as an emptied failing set does — an approval outranks the
      grace below).
    - ``rounds_used >= ceiling`` → ``"stuck"`` (the generous backstop
      terminates the loop even when the reviewer is still committing fixes, so
      the loop always terminates — the ceiling backstop also outranks the grace,
      so the loop never re-buys past it).
    - ``made_changes`` → ``"continue"`` (the reviewer resolved something this
      round — an identical next round can make more progress).
    - ``gate_clean and verify_grace_remaining > 0`` → ``"continue"``
      (proposal 124): a round that neither approved nor changed anything, whose
      every observed gate is ``CLEAN`` at this round's terminal and whose
      bounded verify-only grace is unspent, buys one further review round
      instead of being demoted on the strength of an undirtied tree.
    - otherwise → ``"stuck"`` (no approval, no change, and either a red gate or
      the grace already spent; an identical next round cannot help).

    Pure and zero-I/O, like :func:`plan_next_round`.
    """
    if approved:
        return "green"
    if rounds_used >= ceiling:
        return "stuck"
    if made_changes:
        return "continue"
    if gate_clean and verify_grace_remaining > 0:
        return "continue"
    return "stuck"


@dataclass(frozen=True)
class ConvergenceResult:
    """Outcome of :meth:`EvolutionEngine.converge_to_green`.

    ``review`` is the final :class:`QualityGateResult` observed — clean on a
    ``"green"`` outcome, still-failing on ``"stuck"`` — and ``rounds_used`` is
    the number of ``auto_fix`` rounds that actually ran.

    ``attribution`` carries the proposal-124 verdict —
    :func:`~maestro.gate_observation.attribute`'s classification of a red exit as
    ``ATTRIBUTED`` / ``INHERITED`` / ``UNATTRIBUTABLE`` / ``ENVIRONMENT`` —
    computed by :meth:`EvolutionEngine._convergence_terminal` when
    ``[loop] gate_observation_enabled`` is on. Trailing and defaulted to ``None``
    so every existing positional construction is unaffected and it is ``None``
    whenever the flag is off (byte-identical to the pre-124 result).

    ``runnability`` carries the proposal-133
    :class:`~maestro.gate_environment.GateRunnabilityVerdict` that *produced* an
    ``ENVIRONMENT`` attribution, read once by the terminal. It is threaded rather
    than re-read by the consumer for a correctness reason, not a cost one: the
    hold in ``MaestroAgent.implement_plan`` needs the verdict's ``fingerprint``
    (the stamp), ``kind`` and ``detail`` (the repair directive), and a second
    independent read of the cached row could return ``None`` — a sidecar rewritten
    or cleared between the terminal and the hold — leaving the arm stamping a
    ``None`` fingerprint, which
    :meth:`~maestro.memory.Memory.update_plan_environment_hold` defines as
    *clearing* the stamp. Carrying the verdict makes it non-``None`` exactly when
    ``attribution == "ENVIRONMENT"``, by construction. ``None`` off the flag, on a
    ``SOUND`` environment, and on every non-``ENVIRONMENT`` terminal.
    """

    outcome: ConvergenceDecision
    review: QualityGateResult
    rounds_used: int
    attribution: Attribution | None = None
    runnability: GateRunnabilityVerdict | None = None
