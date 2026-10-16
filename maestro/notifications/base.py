"""Notification interface."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

logger = logging.getLogger("maestro")


#: Length of the plan content-hash prefix an interactive notifier embeds in
#: each approve/reject control; the decision path requires exactly this length.
CALLBACK_HASH_PREFIX_LEN = 16


class PlanContentDriftError(ValueError):
    """A plan decision refused because the pressed control's rendered
    snapshot no longer matches the plan content (#4146).

    A routine outcome for any superseded plan message, so interactive
    notifiers report it to the human as "plan changed" rather than as a
    failure. Subclasses ``ValueError`` so existing callers keep catching it.
    """


class NotifierTransportError(Exception):
    """A notifier's transport/timeout failure — the channel was briefly
    unreachable, not a programming error.

    Notifier-agnostic so callers (e.g. the best-effort status ping) can
    absorb a transient network blip without coupling to any concrete
    transport library. Concrete notifiers translate their transport-layer
    transients into this type; everything else propagates unchanged.
    """


class Notifier(ABC):
    @abstractmethod
    async def notify(
        self, message: str, parse_mode: str | None = None,
    ) -> None: ...

    async def notify_plan_for_review(
        self, plan_id: str, message: str, content_hash: str,
    ) -> None:
        """Send a plan review notification, optionally with approve/reject controls.

        ``content_hash`` is the plan-content snapshot *message* renders;
        interactive subclasses bind their controls to it so a decision
        lands only on the content that was shown.
        Subclasses may override to add interactive buttons.
        Default falls back to plain text notification.
        """
        await self.notify(message)

    async def notify_plan_refined(
        self, plan_id: str, message: str, content_hash: str,
    ) -> None:
        """Notify that a pending plan's body was refined by the curator.

        ``content_hash`` is the refined snapshot *message* renders, as in
        :meth:`notify_plan_for_review`.

        The plan stays ``pending_review`` after refinement, so subclasses may
        reuse the Approve/Reject controls. Default falls back to plain text
        notification.
        """
        await self.notify(message)

    async def resolve_plan(self, plan_id: str, label: str) -> None:
        """Notify that a plan has been resolved outside of the notifier.

        Called after a plan is approved or rejected via an external channel
        (e.g., the web dashboard) so that any interactive controls (such as
        Telegram inline buttons) can be cleaned up.  The default
        implementation is a no-op; subclasses with interactive message
        support should override this.
        """
        return  # no-op by default

    async def notify_publication_for_review(
        self, proposal_id: str, message: str,
    ) -> None:
        """Send a publication-proposal review notification.

        Subclasses may override to add interactive Approve/Reject buttons
        specific to publication proposals. Default falls back to a plain
        text notification.
        """
        await self.notify(message)

    async def resolve_publication(
        self, proposal_id: str, label: str,
    ) -> None:
        """Notify that a publication proposal has been resolved externally.

        Mirrors :meth:`resolve_plan` for the publication approval surface.
        Default implementation is a no-op; subclasses with interactive
        message support should override this.
        """
        return  # no-op by default

    async def ask_thomas(self, question: str, message_id: str) -> None:
        """Send a question to Thomas. Default falls back to notify()."""
        await self.notify(f"Question: {question}")

    async def start_message_polling(
        self, on_message: Callable[[str, str | None], Awaitable[None]],
    ) -> None:
        """Start listening for inbound text messages. Default no-op.

        ``on_message`` receives ``(text, reply_to)``: ``reply_to`` is the
        question ``message_id`` the inbound text answers, or ``None``.
        """
        return  # no-op by default

    async def stop_message_polling(self) -> None:
        """Stop listening for inbound text messages. Default no-op."""
        return  # no-op by default


class NullNotifier(Notifier):
    """No-op notifier when no notification service is configured."""

    async def notify(
        self, message: str, parse_mode: str | None = None,
    ) -> None:
        logger.debug("Notification (null): %s", (message or "")[:100])
