"""Session pair the rejection phase runs on.

Rejection learning makes two SDK calls per plan with *different* effort
policies: the rejection classifier is cheap and independent across plans, so
it runs on one shared ``effort="low"`` session, while
:meth:`Consciousness.update_self_model` asks for ``effort="high"`` on its own
one-shot arm. A per-turn effort override on an existing session is silently
dropped by the SDK, so handing the classifier's session to the self-model
update would quietly demote it — the two must be distinct objects (#3803).

Both rejection arms in :mod:`maestro.agent` (the merged-check block and the
``process_rejected_plans`` phase) open the same pair with the same teardown,
so the open/disconnect shape lives here rather than being duplicated at each
call site.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from maestro.sdk import QueryOptions, create_session, disconnect_session

if TYPE_CHECKING:  # pragma: no cover - typing only
    from maestro.providers.base import Session

logger = logging.getLogger(__name__)

CLASSIFIER_CALLER = "rejection_session"
SELF_MODEL_CALLER = "rejection_self_model_session"


@asynccontextmanager
async def rejection_sessions(
    *,
    enabled: bool,
    identity: Callable[[], str],
) -> AsyncIterator[tuple[Session | None, Session | None]]:
    """Yield ``(classifier_session, self_model_session)`` for the phase.

    When *enabled* is ``False`` both are ``None`` — the documented
    ``enable_session_reuse=False`` path, where each call takes its one-shot
    ``query()`` arm — and no connect cost is paid: *identity* is a callable so
    the system prompt is not even built in that case.

    Both sessions are opened once and shared by every plan in the caller's
    batch, so the self-model session accumulates one full self-model JSON
    turn per plan — heavier per turn than the classifier's one-word answer.
    The bound is the batch size, and the batch is small by construction:
    each rejection is stamped learned and never re-enters, and both arms run
    once per loop iteration, so a batch holds only the plans rejected since
    the previous iteration. Only a first run draining a standing backlog is
    large; if that ever becomes the common case, this is the seam to split
    into a per-plan self-model session.

    Teardown is best-effort and debug-logged: the phase's own result is
    already recorded by the time the sessions are closed, so a disconnect
    failure must not mask it. Both are attempted even if the first raises.
    """
    classifier: Session | None = None
    self_model: Session | None = None
    try:
        if enabled:
            system = identity()
            classifier = await create_session(
                system=system,
                options=QueryOptions(
                    effort="low",
                    tools=[],
                    caller=CLASSIFIER_CALLER,
                ),
                phase="rejection",
            )
            self_model = await create_session(
                system=system,
                options=QueryOptions(
                    effort="high",
                    tools=[],
                    caller=SELF_MODEL_CALLER,
                ),
                phase="rejection",
            )
        yield classifier, self_model
    finally:
        for session, label in (
            (classifier, CLASSIFIER_CALLER),
            (self_model, SELF_MODEL_CALLER),
        ):
            if session is None:
                continue
            try:
                await disconnect_session(session)
            except Exception:
                logger.debug("%s disconnect failed", label, exc_info=True)
