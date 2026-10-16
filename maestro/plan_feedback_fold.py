"""Fold Telegram and dashboard review feedback into a plan's reviewer ledger (proposal 145).

A Thomas reply to a plan's review body is stored as a ``plan_feedback``
``Message`` threaded to ``<plan_id>@<hash prefix>``. The message drain hands
those rows here instead of enqueueing them as tasks: each lands on the plan
through ``Memory.add_plan_feedback`` (idempotent on the message id, so a crash
re-drain records it once) and is acknowledged — or its refusal named — on the
notifier. Sends are best-effort via ``notify_feedback_response``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from maestro.messaging import DASHBOARD_FEEDBACK_MESSAGE_ID_PREFIX
from maestro.plan_feedback import FEEDBACK_TEXT_MAX_CHARS, MAX_UNADDRESSED_FEEDBACK
from maestro.plan_feedback_curation import notify_feedback_response

if TYPE_CHECKING:
    from collections.abc import Iterable

    from maestro.config import Config
    from maestro.memory import Memory
    from maestro.messaging import Message
    from maestro.notifications.base import Notifier

logger = logging.getLogger(__name__)


def feedback_serviceable(config: Config, curate_enabled: bool) -> bool:
    """Whether a curate pass will service a review reply diverted to a ledger.

    Mirrors the off-cadence trigger's gate (flag and curation on) plus the
    ``external_delivery`` focus suppression; when ``False`` the reply stays an
    instruction rather than vanishing into a ledger no curator reads.
    """
    return (
        config.loop_plan_feedback_enabled
        and curate_enabled
        and config.work_target().focus_profile != "external_delivery"
    )


def _refusal(memory: Memory, plan_id: str) -> str:
    plan = memory.get_plan(plan_id)
    if plan is None:
        return f"Feedback not recorded — plan {plan_id} was not found."
    if plan.status != "pending_review":
        return (
            f"Feedback not recorded — plan {plan_id} ({plan.title}) is no "
            f"longer pending review (status: {plan.status})."
        )
    return (
        f"Feedback not recorded — plan {plan_id} ({plan.title}) already "
        f"holds {MAX_UNADDRESSED_FEEDBACK} unaddressed entries."
    )


async def fold_feedback_messages(
    messages: Iterable[Message],
    *,
    memory: Memory,
    notifier: Notifier,
    max_length: int,
) -> int:
    """Record each Thomas ``plan_feedback`` message; return entries written."""
    limit = min(max_length, FEEDBACK_TEXT_MAX_CHARS)
    written = 0
    for msg in messages:
        if msg.sender != "thomas" or msg.message_type != "plan_feedback":
            continue
        plan_id, _, prefix = (msg.reply_to or "").rpartition("@")
        text = msg.content[:limit].strip()
        if not plan_id or not text:
            logger.info("Skipping unroutable plan_feedback message %s", msg.message_id)
            await notify_feedback_response(
                notifier, "Feedback not recorded — the reply was empty or unthreaded.",
            )
            continue
        prior = memory.get_plan(plan_id)
        if prior is not None and any(
            f.feedback_id == msg.message_id for f in prior.reviewer_feedback
        ):
            # A crash re-drain of an already-folded row: acknowledged once.
            logger.info("plan_feedback message %s already folded", msg.message_id)
            continue
        source = (
            "dashboard"
            if msg.message_id.startswith(DASHBOARD_FEEDBACK_MESSAGE_ID_PREFIX)
            else "telegram"
        )
        if memory.add_plan_feedback(
            plan_id, text, source=source,
            content_hash_seen=prefix, feedback_id=msg.message_id,
        ):
            written += 1
            title = prior.title if prior is not None else ""
            await notify_feedback_response(
                notifier,
                f"Feedback recorded — Maestro will revise plan {plan_id} ({title}).",
            )
        else:
            await notify_feedback_response(notifier, _refusal(memory, plan_id))
    return written
