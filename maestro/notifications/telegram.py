"""Telegram notifications for Thomas."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import functools
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar, Protocol

import httpx
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Conflict, NetworkError, RetryAfter, TimedOut

from maestro.notifications.base import (
    CALLBACK_HASH_PREFIX_LEN,
    Notifier,
    NotifierTransportError,
    PlanContentDriftError,
)
from maestro.plan_feedback import PlanRef, format_plan_ref_marker, parse_plan_ref

logger = logging.getLogger("maestro")

# A plan body longer than one Telegram message is delivered as at most this
# many consecutive chunks, the Approve/Reject keyboard riding only the last.
# Bounded so one plan cannot flood the chat; a body needing more is sent
# without controls and decided on the dashboard (#4589).
_PLAN_MAX_CHUNKS = 4
_PLAN_NO_CONTROLS_NOTICE = (
    "\n\n(Body too long for Telegram — review and decide on the dashboard.)"
)


def _continuation_prefix(marker: str, index: int, total: int) -> str:
    return f"{marker} (cont. {index}/{total})\n"


def _split_for_controls(
    message: str, marker: str, max_length: int,
) -> list[str] | None:
    """Split *message* into whole-delivery chunks of at most *max_length*.

    Splits on line boundaries (hard-splitting a single over-budget line);
    every chunk after the first is prefixed with *marker* so a reply to it
    still threads back to the plan. Returns ``None`` when no positive
    per-chunk budget remains or more than :data:`_PLAN_MAX_CHUNKS` chunks
    would be needed — the caller must then attach no controls.
    """
    reserve = len(_continuation_prefix(marker, _PLAN_MAX_CHUNKS, _PLAN_MAX_CHUNKS))
    budget = max_length - reserve
    if budget <= 0:
        return None
    bodies: list[str] = []
    current = ""
    for line in message.splitlines(keepends=True):
        pieces = [line[i:i + budget] for i in range(0, len(line), budget)]
        for piece in pieces:
            if len(current) + len(piece) > budget:
                bodies.append(current)
                current = ""
            current += piece
    bodies.append(current)
    if len(bodies) > _PLAN_MAX_CHUNKS:
        return None
    # Binding invariant: the chunks reassemble to exactly the hashed body.
    assert "".join(bodies) == message
    total = len(bodies)
    return [bodies[0]] + [
        _continuation_prefix(marker, i + 1, total) + body
        for i, body in enumerate(bodies[1:], start=1)
    ]


class InboundMessageHandler(Protocol):
    """Receives an authorised inbound text plus the refs its reply carries.

    ``reply_to`` is an ``ask_thomas`` question id; ``plan_ref`` the plan a
    replied-to review body names (proposal 145). The
    notifier only reports what it parsed — routing is the handler's decision.
    """

    def __call__(
        self, text: str, reply_to: str | None, *, plan_ref: PlanRef | None = None,
    ) -> Awaitable[None]: ...

# Telegram caps ``callback_data`` at 64 bytes; plan buttons carry
# ``action:plan_id:hash_prefix``, so the prefix length is bounded by it.
_CALLBACK_DATA_MAX_BYTES = 64

# ``ask_thomas`` stamps the Maestro question ``message_id`` into the sent text
# so a Telegram *reply* to that message threads back to the question: Telegram
# echoes the replied-to text in ``reply_to_message.text``, making the link
# stateless across restarts. The marker leads the text because ``_truncate``
# cuts from the end.
_QUESTION_REF_RE = re.compile(r"^Maestro has a question \[ref ([A-Za-z0-9_-]+)\]:")


def format_question(question: str, message_id: str) -> str:
    """Render an outbound question carrying its ``message_id`` marker."""
    return f"Maestro has a question [ref {message_id}]:\n\n{question}"


def parse_question_ref(text: object) -> str | None:
    """Return the question ``message_id`` a replied-to text carries, else ``None``."""
    if not isinstance(text, str):
        return None
    match = _QUESTION_REF_RE.match(text)
    return match.group(1) if match else None


# Transport transients a best-effort caller may absorb. Membership is decided
# by retryability, not class hierarchy: ``RetryAfter`` (a 429 flood-control
# reply) subclasses ``TelegramError`` directly, outside the ``NetworkError``
# family, yet is exactly a brief, server-bounded unavailability (issue #4155).
_TRANSIENT_TELEGRAM_ERRORS: tuple[type[Exception], ...] = (
    httpx.HTTPError,
    NetworkError,
    TimedOut,
    RetryAfter,
)


def _retry_after_seconds(exc: RetryAfter) -> float:
    """Normalise ``RetryAfter.retry_after`` (``int | float | timedelta``) to seconds."""
    delay = exc.retry_after
    if isinstance(delay, datetime.timedelta):
        return delay.total_seconds()
    return float(delay)


class PlanDecisionHandler(Protocol):
    """An approve/reject callback for an evolution plan button.

    ``rendered_hash`` is the content-hash prefix the pressed button was
    rendered against, or ``None`` for a legacy button that carries none.
    """

    def __call__(
        self, plan_id: str, *, rendered_hash: str | None,
    ) -> Awaitable[str]: ...


class TelegramNotifier(Notifier):
    _CONFLICT_BACKOFF_INITIAL: float = 5.0
    _CONFLICT_BACKOFF_MAX: float = 60.0

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        base_url: str | None = None,
        max_message_length: int = 4096,
    ) -> None:
        if not bot_token or not bot_token.strip():
            raise ValueError("bot_token must not be empty")
        if not chat_id or not chat_id.strip():
            raise ValueError("chat_id must not be empty")
        kwargs: dict[str, Any] = {"token": bot_token}
        if base_url is not None:
            kwargs["base_url"] = base_url
        self._bot = Bot(**kwargs)
        self._chat_id = chat_id
        self._max_length = max(1, max_message_length)
        self._plan_messages: dict[str, int] = {}
        self._publication_messages: dict[str, int] = {}
        self._polling = False
        self._poll_task: asyncio.Task[None] | None = None
        # A non-cancellation exception that killed the fire-and-forget poll
        # task, recorded by ``_on_poll_task_done`` so the main loop can probe
        # it and re-raise into its fail-loud crash path. ``None`` means the
        # polling channel is healthy. See :meth:`_on_poll_task_done`.
        self.poll_failure: BaseException | None = None
        self._poll_offset: int = 0
        self._on_message: InboundMessageHandler | None = None
        self._on_publication_approve: Callable[[str], Awaitable[str]] | None = None
        self._on_publication_reject: Callable[[str], Awaitable[str]] | None = None
        self._conflict_backoff: float = self._CONFLICT_BACKOFF_INITIAL

    def _truncate(self, message: str) -> str:
        """Truncate a message to fit within the max length."""
        if len(message) <= self._max_length:
            return message
        suffix = "\n\n(truncated)"
        truncate_at = self._max_length - len(suffix)
        if truncate_at > 0:
            return message[:truncate_at] + suffix
        return message[: self._max_length]

    async def notify(
        self, message: str, parse_mode: str | None = None,
    ) -> None:
        """Send a message to Thomas via Telegram."""
        if not message or not message.strip():
            logger.warning("Skipping empty Telegram message")
            return
        message = self._truncate(message)
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=message,
                parse_mode=parse_mode,
            )
        except BadRequest:
            # ``BadRequest`` subclasses ``NetworkError`` in
            # python-telegram-bot but is NOT a transport transient: it flags
            # a content/data defect in the message we built (malformed
            # ``parse_mode`` entities, chat not found, message too long).
            # Catch it *before* the transport arm so it propagates unchanged
            # for RCA rather than being mistranslated into a
            # ``NotifierTransportError`` and absorbed by the best-effort
            # status ping.
            logger.exception(
                "Failed to send Telegram notification (bad request)",
            )
            raise
        except _TRANSIENT_TELEGRAM_ERRORS as exc:
            # Narrow tolerance mirroring ``_poll_loop``: HTTP-transport
            # blips and the python-telegram-bot wrappers around them are
            # documented transients. Translate them into the notifier-
            # agnostic ``NotifierTransportError`` so a best-effort caller
            # can absorb a brief unreachability without crashing.
            logger.exception("Telegram notification transport error")
            raise NotifierTransportError(str(exc)) from exc
        except Exception:
            # Programming errors, ``OSError``, ``AssertionError``, etc. are
            # real bugs and propagate unchanged for RCA.
            logger.exception("Failed to send Telegram notification")
            raise

    async def _send_plan_with_controls(
        self,
        plan_id: str,
        message: str,
        content_hash: str,
        *,
        empty_warning: str,
        failure_log: str,
    ) -> None:
        """Send a plan message with inline Approve/Reject buttons.

        Shared by :meth:`notify_plan_for_review` and
        :meth:`notify_plan_refined`; both surface the same ``approve:`` /
        ``reject:`` callbacks and ``_plan_messages`` tracking. Each button
        carries a prefix of ``content_hash`` — the snapshot this message
        renders — so a decision binds to the message pressed, not to
        whatever the plan row was last stamped with (#4146). The
        ``empty_warning`` and ``failure_log`` strings keep each caller's log
        wording distinct.

        The keyboard is bound only to a body delivered whole (#4589): a body
        over ``max_length`` is sent as consecutive chunks with the keyboard on
        the final one, and a body too long even for that is sent cut, with
        no keyboard, pointing the operator at the dashboard.
        """
        if not message or not message.strip():
            logger.warning(empty_warning)
            return
        snapshot = content_hash[:CALLBACK_HASH_PREFIX_LEN]
        approve_data = f"approve:{plan_id}:{snapshot}"
        reject_data = f"reject:{plan_id}:{snapshot}"
        # ``approve:`` is the longer prefix, so it bounds both payloads.
        if len(approve_data.encode()) > _CALLBACK_DATA_MAX_BYTES:
            raise ValueError(
                f"plan {plan_id} callback payload exceeds Telegram's "
                f"{_CALLBACK_DATA_MAX_BYTES}-byte limit",
            )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Approve", callback_data=approve_data),
                InlineKeyboardButton("Reject", callback_data=reject_data),
            ],
        ])
        if len(message) <= self._max_length:
            chunks: list[str] | None = [message]
        else:
            chunks = _split_for_controls(
                message,
                format_plan_ref_marker(plan_id, content_hash),
                self._max_length,
            )
        prior_message_id = self._plan_messages.get(plan_id)
        try:
            if chunks is None:
                keep = self._max_length - len(_PLAN_NO_CONTROLS_NOTICE)
                text = (
                    message[:keep] + _PLAN_NO_CONTROLS_NOTICE
                    if keep > 0 else message[: self._max_length]
                )
                result = await self._bot.send_message(
                    chat_id=self._chat_id, text=text, parse_mode=None,
                )
                logger.info(
                    "Plan %s body too long for Telegram; sent without controls",
                    plan_id,
                )
            else:
                for lead in chunks[:-1]:
                    await self._bot.send_message(
                        chat_id=self._chat_id, text=lead, parse_mode=None,
                    )
                result = await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=chunks[-1],
                    parse_mode=None,
                    reply_markup=keyboard,
                )
            self._plan_messages[plan_id] = result.message_id
        except Exception:
            logger.exception(failure_log)
            raise
        # Retire the message this one replaces (e.g. notify_plan_refined
        # superseding a notify_plan_for_review message for the same plan).
        # We send a *new* message so the operator gets a fresh Telegram push
        # alert, but the prior message's Approve/Reject buttons must not
        # outlive it: _plan_messages now tracks only the new id, so resolve_plan
        # would never clean the old one up, leaving stale active controls.
        # Cosmetic only: the old buttons carry their own snapshot hash and
        # are refused on press, so a missed retire (restart, edit failure)
        # cannot land a decision on the refined body.
        if prior_message_id is not None:
            await self._retire_plan_message(prior_message_id)

    async def _retire_plan_message(self, message_id: int) -> None:
        """Strip the inline controls from a superseded plan message.

        Cosmetic cleanup with no safety role — a superseded button is
        refused by its own snapshot hash. Best-effort, mirroring :meth:`resolve_plan`: a failure to edit is
        logged but never raised, since the replacement message has already
        been sent and tracked.
        """
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=message_id,
                text=self._truncate("--- Superseded by a newer plan message ---"),
            )
        except Exception:
            logger.warning(
                "Failed to retire superseded Telegram plan message %s",
                message_id,
                exc_info=True,
            )

    async def notify_plan_for_review(
        self, plan_id: str, message: str, content_hash: str,
    ) -> None:
        """Send a plan notification with inline Approve/Reject buttons."""
        await self._send_plan_with_controls(
            plan_id,
            message,
            content_hash,
            empty_warning="Skipping empty Telegram plan review message",
            failure_log="Failed to send Telegram plan review notification",
        )

    async def notify_plan_refined(
        self, plan_id: str, message: str, content_hash: str,
    ) -> None:
        """Send a refined-plan notification with inline Approve/Reject buttons.

        The plan is still ``pending_review`` after refinement, so this reuses
        the ``approve:`` / ``reject:`` callbacks (and ``_plan_messages``
        tracking) of :meth:`notify_plan_for_review` so the operator can act on
        the refreshed plan directly.
        """
        await self._send_plan_with_controls(
            plan_id,
            message,
            content_hash,
            empty_warning="Skipping empty Telegram plan refinement message",
            failure_log="Failed to send Telegram plan refinement notification",
        )

    async def resolve_plan(self, plan_id: str, label: str) -> None:
        """Edit the Telegram message to remove buttons and show resolution status.

        Called when a plan is resolved outside of Telegram (e.g., via the web
        dashboard) so that stale Approve/Reject buttons are removed and
        ``_plan_messages`` does not grow without bound.
        """
        message_id = self._plan_messages.pop(plan_id, None)
        if message_id is None:
            return
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=message_id,
                text=self._truncate(f"--- {label} ---"),
            )
        except Exception:
            logger.warning(
                "Failed to update Telegram message after resolving plan %s",
                plan_id,
                exc_info=True,
            )

    async def notify_publication_for_review(
        self, proposal_id: str, message: str,
    ) -> None:
        """Send a publication-proposal notification with Approve/Reject buttons.

        Uses the ``pub_approve:`` / ``pub_reject:`` callback prefixes so the
        router can tell publication presses apart from evolution-plan presses.
        """
        if not message or not message.strip():
            logger.warning(
                "Skipping empty Telegram publication review message",
            )
            return
        message = self._truncate(message)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "Approve",
                    callback_data=f"pub_approve:{proposal_id}",
                ),
                InlineKeyboardButton(
                    "Reject",
                    callback_data=f"pub_reject:{proposal_id}",
                ),
            ],
        ])
        try:
            result = await self._bot.send_message(
                chat_id=self._chat_id,
                text=message,
                parse_mode=None,
                reply_markup=keyboard,
            )
            self._publication_messages[proposal_id] = result.message_id
        except Exception:
            logger.exception(
                "Failed to send Telegram publication review notification",
            )
            raise

    async def resolve_publication(
        self, proposal_id: str, label: str,
    ) -> None:
        """Edit the Telegram publication message to show external resolution."""
        message_id = self._publication_messages.pop(proposal_id, None)
        if message_id is None:
            return
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=message_id,
                text=self._truncate(f"--- {label} ---"),
            )
        except Exception:
            logger.warning(
                "Failed to update Telegram message after resolving "
                "publication %s",
                proposal_id,
                exc_info=True,
            )

    async def start_approval_polling(
        self,
        on_approve: PlanDecisionHandler,
        on_reject: PlanDecisionHandler,
        on_message: InboundMessageHandler | None = None,
        *,
        on_publication_approve: Callable[[str], Awaitable[str]] | None = None,
        on_publication_reject: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        """Start polling for Telegram callback queries and text messages.

        The publication callbacks are keyword-only so existing callers
        with the 3-positional-argument signature (on_approve, on_reject,
        on_message) continue to work unchanged.
        """
        if self._poll_task is not None:
            await self.stop_approval_polling()
        self._on_message = on_message
        self._on_publication_approve = on_publication_approve
        self._on_publication_reject = on_publication_reject
        self._polling = True
        # Start from a clean failure slate — a stored failure from a prior
        # session must not be re-read as this one's.
        self.poll_failure = None
        self._poll_task = asyncio.create_task(
            self._poll_loop(on_approve, on_reject),
        )
        # Observe the fire-and-forget task: without a done-callback a handler
        # exception would sit unretrieved on the task (only asyncio's default
        # "exception was never retrieved" logging would surface it) and the
        # polling channel would be dead for the rest of the run with no crash,
        # notification, or health flag — the silent-death this fix closes.
        self._poll_task.add_done_callback(self._on_poll_task_done)

    async def stop_approval_polling(self) -> None:
        """Stop the polling loop."""
        self._polling = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None

    def _on_poll_task_done(self, task: asyncio.Task[None]) -> None:
        """Record a non-cancellation poll-task failure as observable state.

        Done-callback for the fire-and-forget poll task. Retrieving the
        exception here (``task.exception()``) both prevents asyncio's silent
        "exception was never retrieved" default logging and stores the failure
        on :attr:`poll_failure` so the main loop can probe it and re-raise
        into its fail-loud crash path (``MaestroAgent._raise_pending_poll_failure``).
        This is the documented "classify or crash" consumer of the exceptions
        :meth:`_poll_loop` deliberately lets escape.

        ``CancelledError`` is the normal teardown signal (``stop_approval_polling``
        cancels the task) and is ignored — a graceful stop is not a failure.
        This never restarts the loop: auto-recovery would hide the evidence the
        operator needs; the loop is meant to die hard.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.poll_failure = exc
            logger.error(
                "Telegram polling task died; channel is down until restart",
                exc_info=exc,
            )

    async def _claim_polling_session(self) -> None:
        """Force-claim the polling session, terminating any lingering connection.

        When the process restarts, the previous long-poll may still be active
        on Telegram's servers (up to 30s).  A short ``get_updates`` call with
        ``timeout=0`` forces Telegram to drop the old connection, preventing
        409 Conflict errors in the main loop.

        The claim uses the current poll offset (``self._poll_offset``), not a
        negative offset: a negative offset would confirm and permanently drop
        every pending update, discarding any approval/rejection callbacks or
        messages Thomas sent while the bot was offline.  Passing the current
        offset terminates the stale long-poll without confirming anything, so
        those queued updates survive for the main loop to consume.
        """
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                await self._bot.get_updates(offset=self._poll_offset, timeout=0)
                logger.info(
                    "Claimed Telegram polling session (attempt %d)", attempt,
                )
                return
            except Conflict:
                if attempt < max_attempts:
                    logger.info(
                        "Claim attempt %d/%d got 409, retrying in 1s",
                        attempt, max_attempts,
                    )
                    await asyncio.sleep(1)
                else:
                    logger.warning(
                        "Failed to claim polling session after %d attempts; "
                        "proceeding with backoff fallback",
                        max_attempts,
                    )
            except Exception:
                logger.warning(
                    "Unexpected error during session claim (attempt %d), "
                    "proceeding",
                    attempt, exc_info=True,
                )
                return

    async def _poll_loop(
        self,
        on_approve: PlanDecisionHandler,
        on_reject: PlanDecisionHandler,
    ) -> None:
        """Long-poll Telegram for callback queries and text messages."""
        await self._claim_polling_session()
        while self._polling:
            try:
                updates = await self._bot.get_updates(
                    offset=self._poll_offset,
                    timeout=30,
                    allowed_updates=["callback_query", "message"],
                )
                self._conflict_backoff = self._CONFLICT_BACKOFF_INITIAL
                for update in updates:
                    uid = update.update_id
                    if not isinstance(uid, int):
                        logger.warning(
                            "Skipping update with non-integer update_id: %r",
                            uid,
                        )
                        continue
                    # Dispatch first, then confirm. The offset advances to
                    # ``uid + 1`` only *after* the handler returns without
                    # raising (below), so a handler that crashes leaves the
                    # crashing update unconfirmed and Telegram re-delivers it on
                    # the next poll instead of it being silently confirmed-and-
                    # skipped. Intentionally-discarded updates (unauthorized
                    # chat, unhandled type) fall through to the advance so they
                    # are not re-polled forever; only a *raising* handler stops
                    # the offset from advancing.
                    #
                    # A *deterministically*-failing handler (a real bug that
                    # raises on a specific update, not a transient error) thus
                    # re-crashes the loop on every process restart: ``__init__``
                    # resets the offset to ``0`` and the still-unconfirmed
                    # poison update is re-fetched and re-dispatched. That is
                    # bounded (Telegram drops unconfirmed updates after ~24h,
                    # and the authorized-chat guard limits who can send one) and
                    # is the intended die-hard / operator-intervention recovery
                    # model — deliberately *not* auto-recovered: capping crashes
                    # to drop the poison update would be the auto-recovery this
                    # fix forbids, hiding the very defect the loud crash exists
                    # to surface.
                    if update.callback_query:
                        if self._authorized_chat(
                            update.callback_query.message,
                        ):
                            await self._handle_callback(
                                update.callback_query, on_approve, on_reject,
                            )
                        else:
                            logger.warning(
                                "Ignoring Telegram callback_query from "
                                "unauthorized chat",
                            )
                    elif (
                        update.message
                        and update.message.text
                        and self._on_message is not None
                    ):
                        if self._authorized_chat(update.message):
                            # No swallow: inbound-message handler bugs are real
                            # defects. They propagate out of the polling loop so
                            # the iteration handler can classify or crash.
                            reply_src = update.message.reply_to_message
                            src_text = (
                                reply_src.text if reply_src is not None else None
                            )
                            await self._on_message(
                                update.message.text,
                                parse_question_ref(src_text),
                                plan_ref=parse_plan_ref(src_text),
                            )
                        else:
                            logger.warning(
                                "Ignoring Telegram message from unauthorized "
                                "chat",
                            )
                    # Handler dispatch succeeded (or the update was
                    # intentionally dropped) — confirm it so it is not
                    # re-delivered.
                    self._poll_offset = uid + 1
            except asyncio.CancelledError:
                raise
            except Conflict:
                logger.warning(
                    "Another bot instance is polling; backing off %gs",
                    self._conflict_backoff,
                )
                await asyncio.sleep(self._conflict_backoff)
                self._conflict_backoff = min(
                    self._conflict_backoff * 2,
                    self._CONFLICT_BACKOFF_MAX,
                )
            except RetryAfter as exc:
                # A 429 carries Telegram's explicit backoff contract: wait
                # the server-specified interval instead of the fixed 5s.
                delay = _retry_after_seconds(exc)
                logger.warning(
                    "Telegram polling rate-limited, retrying in %gs", delay,
                )
                await asyncio.sleep(delay)
            except _TRANSIENT_TELEGRAM_ERRORS:
                # Narrow tolerance: HTTP-transport blips and the
                # python-telegram-bot wrappers around them are documented
                # transients. Programming errors, ``OSError``,
                # ``AssertionError``, etc. propagate so the main loop sees
                # the failure when it ``await``s the polling task.
                logger.exception("Telegram polling error, retrying in 5s")
                await asyncio.sleep(5)

    def _authorized_chat(self, message: Any) -> bool:
        """Whether an inbound *message* originates from the configured chat.

        ``get_updates`` returns traffic from *every* Telegram user who
        contacts the bot, not just the configured one, so the ``chat_id``
        passed at construction is the sole authorization boundary for
        inbound messages and inline-button presses. Without this filter any
        Telegram user could DM the bot to inject instructions — which
        :meth:`_poll_loop` forwards to a handler that stores them as if they
        came from Thomas and which the loop later executes — or toggle
        maintenance mode. Telegram chat ids are ints while the configured
        value is a string, so compare string-wise; a missing chat id is
        treated as unauthorized — fail closed.

        Read the id off ``chat.id`` rather than the ``chat_id`` shortcut: a
        ``callback_query.message`` is typed ``MaybeInaccessibleMessage`` and,
        when the original message has become inaccessible (``date == 0``, e.g.
        too old or deleted), arrives as an ``InaccessibleMessage`` that carries
        ``chat`` but defines no ``chat_id`` property. Reading ``chat_id`` there
        would yield ``None`` and silently reject a legitimate press from the
        configured user. ``chat`` is declared on the shared base class, so
        ``chat.id`` is present on every concrete message variant; a ``None``
        message (no callback echo) still yields ``None`` and fails closed.
        """
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        return chat_id is not None and str(chat_id) == self._chat_id

    async def _handle_callback(
        self,
        callback_query: Any,
        on_approve: PlanDecisionHandler,
        on_reject: PlanDecisionHandler,
    ) -> None:
        """Process a single callback query from an inline button press.

        Recognized prefixes:
          - ``approve:{plan_id}[:{hash_prefix}]`` /
            ``reject:{plan_id}[:{hash_prefix}]`` — evolution plans; the
            optional hash prefix is the snapshot the button was rendered
            against (absent on legacy buttons).
          - ``pub_approve:{proposal_id}`` / ``pub_reject:{proposal_id}`` —
            publication proposals (routed to the keyword-only callbacks
            registered via :meth:`start_approval_polling`).
        Unknown prefixes log a warning and are ignored.
        """
        data = callback_query.data or ""
        if ":" not in data:
            return

        action, entity_id = data.split(":", 1)
        hash_prefix: str | None = None
        if action in ("approve", "reject") and ":" in entity_id:
            entity_id, hash_prefix = entity_id.split(":", 1)
            # An empty prefix would match every hash; treat it as legacy.
            hash_prefix = hash_prefix or None
        if not entity_id.strip():
            return

        handler: Callable[[str], Awaitable[str]] | None
        if action == "approve":
            handler = functools.partial(on_approve, rendered_hash=hash_prefix)
            kind = "plan"
        elif action == "reject":
            handler = functools.partial(on_reject, rendered_hash=hash_prefix)
            kind = "plan"
        elif action == "pub_approve":
            handler = self._on_publication_approve
            kind = "publication"
        elif action == "pub_reject":
            handler = self._on_publication_reject
            kind = "publication"
        else:
            logger.warning(
                "Telegram callback: unknown action prefix %r", action,
            )
            return

        if handler is None:
            # Publication callbacks weren't wired on this notifier.
            logger.warning(
                "Telegram callback %s ignored — no handler registered",
                action,
            )
            return

        try:
            await self._bot.answer_callback_query(
                callback_query.id, text="Processing...",
            )
        except Exception:
            logger.warning("Failed to answer callback query", exc_info=True)

        try:
            result = await handler(entity_id)
            if result == "ignored":
                await self._update_message_already_resolved(
                    callback_query.message,
                )
            else:
                await self._update_message_after_action(
                    entity_id, callback_query.message, action,
                )
        except PlanContentDriftError:
            # Routine for any superseded message: tell the human the plan
            # changed rather than reporting a failure.
            logger.warning(
                "Telegram callback %s refused for stale %s %s message",
                action, kind, entity_id,
            )
            if callback_query.message is not None:
                await self._update_message_already_resolved(
                    callback_query.message,
                    banner=(
                        "--- Plan changed since this message; use the latest "
                        "message or the dashboard ---"
                    ),
                )
        except Exception:
            logger.exception(
                "Telegram callback %s failed for %s %s",
                action, kind, entity_id,
            )
            if callback_query.message is not None:
                try:
                    error_text = (
                        f"{callback_query.message.text or ''}\n\n"
                        f"--- Failed to {action} {kind} ---"
                    )
                    await self._bot.edit_message_text(
                        chat_id=self._chat_id,
                        message_id=callback_query.message.message_id,
                        text=self._truncate(error_text),
                    )
                except Exception:
                    logger.warning(
                        "Failed to update Telegram message after error",
                        exc_info=True,
                    )

    _ACTION_LABELS: ClassVar[dict[str, str]] = {
        "approve": "Approved",
        "reject": "Rejected",
        "pub_approve": "Approved",
        "pub_reject": "Rejected",
    }

    async def _update_message_after_action(
        self,
        entity_id: str,
        message: Any,
        action: str,
    ) -> None:
        """Edit the original Telegram message to show result and remove buttons.

        ``action`` is one of the four prefixes recognised by
        :meth:`_handle_callback` and controls which message-id dict gets
        cleaned up (``_plan_messages`` for ``approve``/``reject``,
        ``_publication_messages`` for the ``pub_*`` variants).
        """
        if message is None:
            return
        label = self._ACTION_LABELS.get(action, "Resolved")
        try:
            original_text = message.text or ""
            updated_text = f"{original_text}\n\n--- {label} via Telegram ---"
            updated_text = self._truncate(updated_text)
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=message.message_id,
                text=updated_text,
            )
        except Exception:
            logger.warning(
                "Failed to update Telegram message after %s", action,
                exc_info=True,
            )
        if action in ("approve", "reject"):
            self._plan_messages.pop(entity_id, None)
        elif action.startswith("pub_"):
            self._publication_messages.pop(entity_id, None)

    async def _update_message_already_resolved(
        self,
        message: Any,
        banner: str = "--- Already resolved ---",
    ) -> None:
        """Edit the Telegram message to show that the entity was already resolved.

        Called when the handler returns ``'ignored'`` — meaning another
        approval surface (e.g. the dashboard) already resolved the entity
        before this Telegram button press was processed — and, with a
        "plan changed" *banner*, when a superseded plan button is refused.
        """
        if message is None:
            return
        try:
            original_text = message.text or ""
            updated_text = self._truncate(
                f"{original_text}\n\n{banner}",
            )
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=message.message_id,
                text=updated_text,
            )
        except Exception:
            logger.warning(
                "Failed to update Telegram message after ignored callback",
                exc_info=True,
            )

    async def ask_thomas(self, question: str, message_id: str) -> None:
        """Send a question to Thomas via Telegram."""
        await self.notify(format_question(question, message_id))
