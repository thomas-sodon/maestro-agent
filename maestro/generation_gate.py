"""Generation authority — may a net-new evolution plan be minted this tick?

Proposal 120, Phase B. Maestro's ``evolve()`` phase once minted a fresh plan
every cadence tick by spending a priced high-effort ``propose_plan`` call
(once preceded by ``reflect``, deleted by proposal 155 step 4) and only *afterwards* let the submission door
(:meth:`maestro.evolution.EvolutionEngine.submit_for_review`) decide whether the
plan could exist. When the ``pending_review`` backlog was saturated — or the
recent obsolete graveyard was dominated by one self-measurement theme — that
door recorded the just-authored plan ``obsolete`` within minutes, so the loop
paid to author plans that were dead on arrival.

:func:`may_mint_plan` is the single *pre-mint* owner of that decision, mirroring
the pure-predicate + frozen-dataclass shape of :mod:`maestro.outcome_authority`.
It computes exactly the two refusal arms the submission gate already applies —
backlog saturation and self-measurement theme concentration — from the same
inputs, introducing **no** new threshold. Pure, zero SDK cost, no I/O: the caller
supplies every fact. On a refusal the caller skips both priced calls and writes
the same bounded prune/land directive the submission door writes, sourced from
:data:`GENERATION_STANDDOWN_DIRECTIVE` here so both faces draw it from one owner.
The submission gate keeps running unchanged as a downstream backstop, so no path
loses coverage.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Bounded prune/land directive written into an empty ``next_task_directive``
#: slot when a net-new mint is refused — by the pre-mint gate here and, via
#: ``maestro.evolution_submission._GENERATION_ADMISSION_DIRECTIVE``
#: (re-pointed at this constant), by the submission door. Names the disposal lever
#: (``mark_plan_obsolete``) so the next self-directed pass clears the graveyard
#: instead of growing it. ≤200 chars (``save_self_model`` re-truncates to
#: ``Consciousness.MAX_DIRECTIVE_CHARS``).
GENERATION_STANDDOWN_DIRECTIVE = (
    "The evolution-plan backlog is saturated or theme-dominated. Land or "
    "dispose an existing pending plan (mark_plan_obsolete) before minting "
    "another — clear the graveyard, don't grow it."
)


@dataclass(frozen=True)
class GenerationVerdict:
    """The pre-mint decision for a single cadence tick.

    Pure data — the caller reads :attr:`admit` to decide whether to spend the
    priced ``propose_plan`` call, and surfaces
    :attr:`reason` in the log/journal on a refusal.
    """

    #: ``True`` when a net-new plan may be minted this tick; ``False`` when the
    #: backlog is saturated or the candidate's self-measurement theme dominates.
    admit: bool
    #: Short refusal detail (``saturation`` / ``theme`` / ``saturation+theme``);
    #: the theme arm is pre-mint only until proposal 144 Step 9 retires it.
    #: Empty when :attr:`admit`.
    reason: str


def may_mint_plan(
    *,
    pending_count: int,
    block_threshold: int,
    candidate_is_self_measurement: bool,
    themed_fraction: float,
    theme_window_total: int,
    theme_fraction_threshold: float,
) -> GenerationVerdict:
    """Return whether a net-new evolution plan may be minted this tick.

    Computes two refusal arms; the submission door
    (:meth:`maestro.evolution.EvolutionEngine.submit_for_review`) applies only
    the saturation arm, and the theme arm is pre-mint only until proposal 144
    Step 9 retires it:

    * **backlog saturation** — ``block_threshold > 0`` and
      ``pending_count >= block_threshold`` (``block_threshold <= 0`` disables
      the cap, exactly as the submit-block does);
    * **theme concentration** — the candidate is a self-measurement plan and the
      recent-obsolete window is non-empty and its themed fraction is at or above
      ``theme_fraction_threshold``.

    Refuses (``admit=False``) when either arm holds, with a ``reason`` of
    ``saturation`` / ``theme`` / ``saturation+theme``; admits otherwise. No new threshold, no I/O, no SDK — every input is
    passed in.
    """
    backlog_saturated = block_threshold > 0 and pending_count >= block_threshold
    theme_over = (
        candidate_is_self_measurement
        and theme_window_total > 0
        and themed_fraction >= theme_fraction_threshold
    )
    if backlog_saturated and theme_over:
        return GenerationVerdict(
            admit=False,
            reason=(
                f"saturation+theme: backlog saturated ({pending_count} >= "
                f"{block_threshold}) and the recent obsolete graveyard is "
                "theme-dominated"
            ),
        )
    if backlog_saturated:
        return GenerationVerdict(
            admit=False,
            reason=(
                f"saturation: backlog saturated ({pending_count} >= "
                f"{block_threshold})"
            ),
        )
    if theme_over:
        return GenerationVerdict(
            admit=False,
            reason="theme: the recent obsolete graveyard is theme-dominated",
        )
    return GenerationVerdict(admit=True, reason="")
