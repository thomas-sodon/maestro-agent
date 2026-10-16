"""Curate-verdict servicing for a plan whose reviewer feedback was rendered.

Proposal 145: when ``curate_pending_plans`` renders unaddressed reviewer
feedback into its prompt, the verdict is applied here instead of the plain
curate path. The curator must answer the reviewer (``response``), may not
retire the plan (``obsolete`` downgrades to ``keep``), and only the entries
rendered into the prompt are stamped addressed — feedback that arrived during
the SDK call stays open. A malformed verdict, or a refine that does not land,
counts one attempt; at ``MAX_FEEDBACK_CURATE_ATTEMPTS`` the entries are
released with ``FEEDBACK_UNSERVICEABLE_RESPONSE`` so the selector's feedback
priority cannot pin one plan at the head of the backlog forever.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from maestro.notifications.base import NotifierTransportError
from maestro.plan_feedback import (
    FEEDBACK_RESPONSE_MAX_CHARS,
    FEEDBACK_UNSERVICEABLE_RESPONSE,
    MAX_FEEDBACK_CURATE_ATTEMPTS,
    MAX_UNADDRESSED_FEEDBACK,
    PlanFeedback,
)

if TYPE_CHECKING:
    from maestro.evolution import EvolutionEngine
    from maestro.memory import EvolutionPlan
    from maestro.notifications.base import Notifier
    from maestro.sdk import QueryResult

logger = logging.getLogger(__name__)

_ACTIONS = frozenset({"refine", "obsolete", "keep"})


async def notify_feedback_response(notifier: Notifier, message: str) -> None:
    """Send a reviewer-feedback response, absorbing only transport failures."""
    try:
        await notifier.notify(message)
    except NotifierTransportError:
        logger.info("Feedback response notification failed", exc_info=True)


def _response_message(plan: EvolutionPlan, response: str) -> str:
    return f"Response to your feedback on plan {plan.plan_id} ({plan.title}):\n{response}"


def _parse_verdict(result: QueryResult) -> tuple[dict[str, Any], str] | None:
    """Return ``(data, response)`` for a well-formed verdict, else ``None``."""
    try:
        data = result.parse_json()
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # Narrow to ``str`` first: frozenset membership hashes the value, so a
    # list/dict action would raise TypeError instead of failing the parse.
    action = data.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        return None
    response = data.get("response")
    if (
        not isinstance(response, str)
        or not response.strip()
        or len(response) > FEEDBACK_RESPONSE_MAX_CHARS
    ):
        return None
    return data, response.strip()


async def _record_failure(
    engine: EvolutionEngine, target: EvolutionPlan, ids: list[str],
) -> bool:
    """Count a failed pass; release each entry whose own budget is spent."""
    memory = engine._memory
    memory.touch_plan_curated(target.plan_id)
    counts = memory.record_feedback_attempt(target.plan_id, ids) or {}
    spent = [
        fid for fid, n in counts.items() if n >= MAX_FEEDBACK_CURATE_ATTEMPTS
    ]
    if spent and memory.mark_feedback_addressed(
        target.plan_id, spent, FEEDBACK_UNSERVICEABLE_RESPONSE,
    ):
        await notify_feedback_response(
            engine._notifier,
            _response_message(target, FEEDBACK_UNSERVICEABLE_RESPONSE),
        )
    return False


async def curate_with_feedback(
    engine: EvolutionEngine,
    target: EvolutionPlan,
    rendered: list[PlanFeedback],
    result: QueryResult,
) -> bool:
    """Apply a curate verdict for *target* whose *rendered* feedback was shown.

    Returns ``True`` only when a refine landed; ``keep`` (including a
    downgraded ``obsolete``) and every failure path return ``False``.
    """
    ids = [f.feedback_id for f in rendered[:MAX_UNADDRESSED_FEEDBACK]]
    parsed = _parse_verdict(result)
    if parsed is None:
        logger.warning(
            "curate_pending_plans: malformed feedback verdict for plan %s — "
            "counting an attempt",
            target.plan_id,
        )
        return await _record_failure(engine, target, ids)
    data, response = parsed
    if data["action"] == "refine":
        try:
            refined = await engine._apply_curate_refine(
                target, data,
                response_notice=_response_message(target, response),
            )
        except (ValueError, TypeError):
            # refine_plan's validators reject an empty or wrong-typed field;
            # count it so a persistently malformed refine is still released.
            logger.warning(
                "curate_pending_plans: invalid refine verdict for plan %s — "
                "counting an attempt",
                target.plan_id,
                exc_info=True,
            )
            refined = False
        if not refined:
            return await _record_failure(engine, target, ids)
        engine._memory.mark_feedback_addressed(target.plan_id, ids, response)
        return True
    if data["action"] == "obsolete":
        logger.info(
            "curate_pending_plans: obsolete verdict on plan %s with reviewer "
            "feedback — downgraded to keep",
            target.plan_id,
        )
    engine._memory.touch_plan_curated(target.plan_id)
    if engine._memory.mark_feedback_addressed(target.plan_id, ids, response):
        await notify_feedback_response(
            engine._notifier, _response_message(target, response),
        )
    return False
