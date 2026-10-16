"""Telegram-surface plan approve/reject, extracted from the loop.

The Telegram inline buttons resolve a ``pending_review`` evolution plan.
Both arms — approve and reject — carry the identical guard chain, so the
chain lives here once rather than twice inside
:mod:`maestro.agent`'s ``run_loop``:

1. The plan must still be ``pending_review`` at read time.
2. Its content must still match the snapshot the pressed button was
   rendered against (**Plan content-hash binding**, proposal 021). Each
   button carries that snapshot's hash prefix in its callback payload, so
   a superseded message's button can never ride in on a later refined
   body even when the plan row's ``notified_content_hash`` was re-stamped
   (#4146). A legacy payload with no prefix falls back to
   ``notified_content_hash`` — and is refused outright once the plan was
   refined, since it cannot say which body it was rendered against.
3. The write itself re-checks *both* preconditions under
   :meth:`Memory.update_plan_status`'s file lock, so neither a rival
   surface's decision nor a concurrent :meth:`Memory.refine_plan` can be
   overwritten or ridden in on.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from maestro.memory import compute_plan_content_hash
from maestro.notifications.base import (
    CALLBACK_HASH_PREFIX_LEN,
    PlanContentDriftError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from maestro.memory import Memory
    from maestro.notifications.telegram import PlanDecisionHandler

logger = logging.getLogger(__name__)


def apply_telegram_plan_decision(
    memory: Memory,
    plan_id: str,
    status: Literal["approved", "rejected"],
    *,
    rejection_reason: str | None = None,
    rendered_hash_prefix: str | None = None,
) -> str:
    """Record a Telegram approve/reject decision for *plan_id*.

    Returns the recorded status, or ``"ignored"`` when another surface
    resolved the plan first. Raises ``ValueError`` when the plan is not
    ``pending_review`` or when its content drifted from the notified
    snapshot — a decision about content the human never saw would break
    the human-in-the-loop guarantee, and for a rejection it would also
    poison rejection learning with feedback about the wrong body.

    ``rendered_hash_prefix`` is the content-hash prefix carried by the
    pressed button; ``None`` means a legacy payload that predates it.
    """
    verb = "approve" if status == "approved" else "reject"
    plan = memory.get_plan(plan_id)
    if not plan or plan.status != "pending_review":
        logger.warning(
            "Telegram %s ignored: plan %s not in pending_review", verb, plan_id,
        )
        raise ValueError(f"plan {plan_id} not in pending_review")
    current_hash = compute_plan_content_hash(plan)
    if rendered_hash_prefix is not None:
        # A short prefix would match a large share of hashes; only the
        # exact length the notifier emits counts as a binding.
        drifted = (
            len(rendered_hash_prefix) != CALLBACK_HASH_PREFIX_LEN
            or not current_hash.startswith(rendered_hash_prefix)
        )
        expect_hash: str | None = current_hash
    elif plan.refined_at:
        # A hash-less button cannot say which body it was rendered
        # against, and the plan has had more than one.
        drifted = True
        expect_hash = None
    else:
        drifted = bool(
            plan.notified_content_hash
            and current_hash != plan.notified_content_hash
        )
        expect_hash = plan.notified_content_hash or None
    if drifted:
        logger.warning(
            "Telegram %s refused: plan %s content drifted since notification",
            verb, plan_id,
        )
        raise PlanContentDriftError(
            f"plan {plan_id} content drifted since notification",
        )

    now = datetime.now(UTC).isoformat()
    # Only the reject arm carries a reason; the approve arm must leave the
    # field untouched rather than stamping it None, so the two arms are
    # written as two explicit calls rather than a splatted kwargs dict —
    # a splat would need a blanket ``type: ignore`` and silence mypy on
    # every argument of this write.
    if rejection_reason is None:
        written = memory.update_plan_status(
            plan_id, status,
            reviewed_at=now,
            expect_status="pending_review",
            expect_content_hash=expect_hash,
        )
    else:
        written = memory.update_plan_status(
            plan_id, status,
            rejection_reason=rejection_reason,
            reviewed_at=now,
            expect_status="pending_review",
            expect_content_hash=expect_hash,
        )
    if not written:
        # The writer's False is ambiguous between a status loss and a
        # content loss. Re-read: still ``pending_review`` with a drifted
        # hash means a concurrent refine_plan() rewrote the body after
        # the cheap check above, so refuse the same way it does.
        after = memory.get_plan(plan_id)
        if (
            after is not None
            and after.status == "pending_review"
            and expect_hash is not None
            and compute_plan_content_hash(after) != expect_hash
        ):
            logger.warning(
                "Telegram %s refused: plan %s content drifted since notification",
                verb, plan_id,
            )
            raise PlanContentDriftError(
            f"plan {plan_id} content drifted since notification",
        )
        logger.warning(
            "Telegram %s: plan %s disappeared or was resolved by another "
            "surface during update",
            verb, plan_id,
        )
        return "ignored"
    return status


def telegram_plan_handlers(
    memory: Memory,
) -> tuple[PlanDecisionHandler, PlanDecisionHandler]:
    """Build the Telegram approve/reject button callbacks over *memory*.

    Each forwards the pressed button's rendered-snapshot hash prefix, so the
    decision binds to the message pressed rather than the plan row's latest
    ``notified_content_hash`` stamp (#4146).
    """

    async def on_approve(plan_id: str, *, rendered_hash: str | None = None) -> str:
        return apply_telegram_plan_decision(
            memory, plan_id, "approved", rendered_hash_prefix=rendered_hash,
        )

    async def on_reject(plan_id: str, *, rendered_hash: str | None = None) -> str:
        return apply_telegram_plan_decision(
            memory, plan_id, "rejected",
            rejection_reason="Rejected via Telegram",
            rendered_hash_prefix=rendered_hash,
        )

    return on_approve, on_reject
