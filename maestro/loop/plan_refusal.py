"""Pre-flight refusals ``implement_plan`` applies before buying any SDK work.

Two clusters live here, lifted out of ``maestro/agent.py`` (then in the
shrink-only upper band) so the refusal reasoning sits in one leaf module rather than inline in
the implementation lineage:

* :func:`journal_plan_refusal` — the shared best-effort journal write every
  pre-flight refusal records, so a refused plan leaves a subjective trace and
  not only a log line.
* :func:`over_cap_criteria_reason` / :func:`dispose_over_cap_plan` — the
  acceptance-criteria count gate (#3712). The read door admits a legacy
  over-cap row intact rather than silently rewriting an already-reviewed
  contract, and ``format_acceptance_criteria_block`` — the sole prompt
  renderer — refuses it. Without this gate that refusal would surface as a
  ``ValueError`` from deep inside ``implement`` / ``self_review`` /
  ``create_pr`` / ``auto_fix`` and crash the loop phase on every iteration,
  with no self-extinguishing disposition. Disposing the plan here is zero-SDK
  and terminal: an over-cap contract needs a human to re-scope it, not a retry.

Nothing here reaches the SDK, a code host, or VCS.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS

if TYPE_CHECKING:
    from maestro.journal import Journal
    from maestro.memory import EvolutionPlan, Memory

logger = logging.getLogger("maestro")


def journal_plan_refusal(
    journal: Journal,
    *,
    content: str,
    triggered_by: str,
) -> None:
    """Record *content* as an ``evolution_note``; never raise into the caller.

    The refusal itself has already been decided and logged by the time this
    runs, so a journal-write failure must not turn a clean pre-flight refusal
    into a loop crash. *triggered_by* names the gate in the fallback WARNING.
    """
    try:
        journal.record_event(
            content=content,
            entry_type="evolution_note",
            triggered_by=triggered_by,
        )
    except Exception:
        logger.warning(
            "Failed to write plan-refusal journal entry (%s)",
            triggered_by, exc_info=True,
        )


def over_cap_criteria_reason(plan: EvolutionPlan) -> str | None:
    """Return the refusal reason when *plan* carries an over-cap criteria list.

    ``None`` when the plan is at or under ``ACCEPTANCE_CRITERIA_MAX_ITEMS`` —
    the bound is inclusive, so only cap + 1 and above refuse.
    """
    count = len(plan.acceptance_criteria)
    if count <= ACCEPTANCE_CRITERIA_MAX_ITEMS:
        return None
    return (
        f"Plan carries {count} acceptance criteria (max "
        f"{ACCEPTANCE_CRITERIA_MAX_ITEMS}); it predates the count bound and "
        "cannot be rendered into an implementation prompt. Re-scope it to at "
        f"most {ACCEPTANCE_CRITERIA_MAX_ITEMS} binding criteria and re-propose."
    )


# Statuses ``_implement_approved_plans`` re-selects; a plan outside them can
# never hold the implementation slot again, so a lost disposal race is benign.
_RETRY_ELIGIBLE_STATUSES = frozenset({"approved", "failed"})


def dispose_over_cap_plan(
    plan: EvolutionPlan,
    *,
    memory: Memory,
    journal: Journal,
) -> bool:
    """Retire *plan* when it is over-cap; return whether it was refused.

    ``obsolete`` is disposal, not rejection — no rejection-learning cooldown is
    spent on a legacy row's shape. Both ``approved`` and ``failed`` rows are
    retired to ``obsolete``: a ``failed`` row left in place would stay
    retry-eligible (this gate bumps no ``failure_count``) and hold the
    implementation slot every iteration (#4123). The refusal is journaled only
    after the flip persists, so a re-selected plan never stacks duplicate
    notes. A disposal that did not persist while the plan is still
    retry-eligible (or missing) is logged at ERROR; one a concurrent writer
    already moved out of the retry-eligible set (``_RETRY_ELIGIBLE_STATUSES``)
    is logged at INFO. Either way the plan is refused,
    so the renderer's ``ValueError`` is never reached.
    """
    reason = over_cap_criteria_reason(plan)
    if reason is None:
        return False
    logger.warning("Plan %s not implemented: %s", plan.plan_id, reason)
    disposed = memory.mark_plan_obsolete(
        plan.plan_id, reason, allow_approved=True, allow_failed=True,
    )
    if disposed:
        journal_plan_refusal(
            journal,
            content=reason,
            triggered_by=f"criteria_cap_gate:{plan.plan_id}",
        )
        return True
    current = memory.get_plan(plan.plan_id)
    if current is not None and current.status not in _RETRY_ELIGIBLE_STATUSES:
        logger.info(
            "Over-cap plan %s already left the retry pool (status %s)",
            plan.plan_id, current.status,
        )
        return True
    logger.error(
        "Over-cap disposal of plan %s did not persist (status now %s); "
        "it remains refused but may be re-selected",
        plan.plan_id, current.status if current is not None else "missing",
    )
    return True
