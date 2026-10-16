"""Crash detection and reporting.

On startup, :mod:`maestro.agent` calls :func:`build_crash_event` and
:func:`handle_crash` when it detects that the previous run terminated
without a clean shutdown (no ``last_shutdown`` in ``lifecycle.json`` but
evidence the loop was in progress).

``handle_crash`` pings Thomas on Telegram and writes a journal milestone.
Both name the structured SDK context (category, exit code, retries, and —
in the milestone — a scrubbed, whitespace-collapsed, debug-filtered
stderr tail) whenever the prior run died on an ``SDKError`` the unwind
path persisted, so an operator reading the ping learns *why* the process
died without opening the log tail.  The loop continues with normal startup after crash
reporting completes; deeper triage still relies on the app logs.

All operations in ``handle_crash`` are best-effort: crash reporting must
never itself crash the startup path, so ``Exception`` (and the spurious
``asyncio.CancelledError`` that can leak from SDK teardown) is logged and
swallowed.  A genuine ``KeyboardInterrupt``/``SystemExit`` still propagates
so an operator can abort a hung notifier (#465).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from maestro import secret_scan
from maestro.diagnostics import (
    DiagnosticSnapshot,
    build_diagnostic_snapshot,
)
from maestro.logs import parse_app_log_tail
from maestro.sdk import diagnostic_stderr, stderr_failure_tail

if TYPE_CHECKING:
    from maestro.journal import Journal
    from maestro.notifications.base import Notifier
    from maestro.sdk import SDKError

logger = logging.getLogger("maestro")

_LOG_TAIL_ENTRIES = 200
# Bound on the stderr tail rendered into the journal milestone.  The persist
# side (``Agent._MAX_PERSISTED_SDK_STDERR_CHARS``) already truncates to the
# same width, but the render asserts the bound locally rather than trusting
# an upstream cap: ``sdk_error`` may also arrive straight from a live
# ``SDKError`` whose stderr was never persisted.  The bound is applied
# after debug filtering and scrubbing but before whitespace collapse (see
# ``_render_stderr_tail``), anchored on the last error-shaped line
# (``sdk.stderr_failure_tail``) so a trailing stack trace cannot push the
# failure header out of the window.
_MAX_RENDERED_STDERR_CHARS = 500


@dataclass
class CrashEvent:
    """Everything we know about a detected crash at startup."""

    last_iteration: int
    last_phase: str | None
    detected_at: str
    log_tail: list[dict[str, Any]] = field(default_factory=list)
    diagnostic: DiagnosticSnapshot | None = None
    exit_code: int | None = None
    # Structured SDK context reconstituted by ``Agent._read_crash_sdk_context``
    # from the ``last_sdk_error`` block the crash unwind persisted.  A single
    # optional ``SDKError`` rather than four flat ``sdk_*`` fields: the reader
    # already hands back a whole error, and flattening is what rotted last
    # time (the fields lost their reader).  ``None`` when the prior run did
    # not die on an ``SDKError``.
    sdk_error: SDKError | None = None


def build_crash_event(
    *,
    last_iteration: int,
    last_phase: str | None,
    log_path: Path | None,
    exit_code: int | None = None,
    sdk_error: SDKError | None = None,
) -> CrashEvent:
    """Gather log tail + diagnostic snapshot for a detected crash."""
    log_tail = parse_app_log_tail(log_path, n=_LOG_TAIL_ENTRIES) if log_path else []
    diagnostic = build_diagnostic_snapshot(log_path, n_entries=_LOG_TAIL_ENTRIES)
    return CrashEvent(
        last_iteration=last_iteration,
        last_phase=last_phase,
        detected_at=datetime.now(UTC).isoformat(),
        log_tail=log_tail,
        diagnostic=diagnostic,
        exit_code=exit_code,
        sdk_error=sdk_error,
    )


async def handle_crash(
    event: CrashEvent,
    *,
    notifier: Notifier,
    journal: Journal,
) -> None:
    """Notify Thomas and journal a crash milestone.

    Best-effort: ``Exception`` (and the spurious ``asyncio.CancelledError``
    that can leak from SDK teardown) is logged and swallowed so crash
    reporting never itself crashes the startup path, while a genuine
    ``KeyboardInterrupt``/``SystemExit`` propagates so an operator can abort.
    The loop continues with normal startup after this returns.
    """
    try:
        ping = (
            f"Maestro crashed (phase={event.last_phase or 'unknown'}, "
            f"iter={event.last_iteration}"
            f"{_render_sdk_clause(event)}). Check app logs for details."
        )
        await notifier.notify(ping)
    # ``handle_crash`` runs on the *startup* path: a genuine
    # ``KeyboardInterrupt``/``SystemExit`` must propagate so an operator can
    # abort a hung notifier (#465).  ``asyncio.CancelledError`` is grouped
    # with ``Exception`` and suppressed — mirroring ``_notify_best_effort`` —
    # because a residual SDK teardown task can leak one spuriously into this
    # very Telegram POST; letting it escape would abort startup (the caller's
    # guard is ``except Exception:``, which never catches it) and skip the
    # journal milestone below.
    except (Exception, asyncio.CancelledError):
        logger.warning("Crash notification failed", exc_info=True)

    try:
        journal.record_event(
            content=(
                f"I crashed. Last known phase: {event.last_phase or 'unknown'} "
                f"at iteration {event.last_iteration}. "
                f"Diagnostic: "
                f"{event.diagnostic.summary if event.diagnostic else 'n/a'}."
                f"{_render_sdk_detail(event)}"
            ),
            entry_type="milestone",
            triggered_by="crash",
        )
    except Exception:
        logger.warning("Failed to write crash journal milestone", exc_info=True)


def _render_sdk_clause(event: CrashEvent) -> str:
    """Bounded ``, category=…, exit=…, retries=…`` clause for the ping.

    Empty when the crash carried neither structured SDK context nor an exit
    code.  When only ``exit_code`` is known (the prior run died on something
    other than an :class:`SDKError`) the exit code is still named, so the
    field the unwind path pays I/O to persist has a reader — and the
    milestone labels that branch neutrally (see :func:`_render_sdk_detail`)
    rather than calling a non-SDK death "SDK context".
    """
    if event.sdk_error is not None:
        err = event.sdk_error
        exit_code = err.exit_code if err.exit_code is not None else event.exit_code
        return (
            f", category={err.category.value}, "
            f"exit={exit_code if exit_code is not None else 'n/a'}, "
            f"retries={err.retries_attempted}"
        )
    if event.exit_code is not None:
        return f", exit={event.exit_code}"
    return ""


def _render_stderr_tail(stderr: str) -> str:
    """Filter, scrub, collapse, and tail-cap ``SDKError`` stderr for the journal.

    The milestone is a durable journal event an operator reads, so raw
    subprocess stderr must not reach it verbatim.  Four passes, in this order:
    ``[debug]`` lines are dropped via :func:`maestro.sdk.diagnostic_stderr`
    (the debug stream is oldest-first, failure line last), credential
    material is replaced via :func:`maestro.secret_scan.redact_credentials`
    (scrubbing before the cap so a truncation cannot strand a half-token the
    regex would have caught), the text is capped to
    :data:`_MAX_RENDERED_STDERR_CHARS` anchored on the last error-shaped line
    via :func:`maestro.sdk.stderr_failure_tail` (so an ``Error:`` header
    followed by stack frames keeps its header), and all whitespace runs —
    newlines included — collapse to single spaces so a multi-line traceback
    cannot garble the rendered event line.  The collapse follows the cap
    because the anchor reads raw lines, so indentation can leave the clause
    shorter than the cap.  Returns ``""`` when nothing
    survives, so the caller emits no empty ``stderr:`` label.
    """
    scrubbed = secret_scan.redact_credentials(diagnostic_stderr(stderr))
    # The cap runs on the raw lines, before any collapse: the anchor reads
    # line breaks and indentation (frames, Node property lines), so frame
    # indentation spends part of the window and the rendered clause can be
    # shorter than the cap.
    capped = stderr_failure_tail(scrubbed, _MAX_RENDERED_STDERR_CHARS)
    return " ".join(capped.split())[:_MAX_RENDERED_STDERR_CHARS]


def _render_sdk_detail(event: CrashEvent) -> str:
    """The ping clause plus a scrubbed, debug-filtered stderr tail.

    Rendered for the journal milestone.

    Empty when :func:`_render_sdk_clause` is, so a crash with no structured
    context renders exactly the text it did before SDK context existed.  The
    label tracks what is actually known: ``SDK context`` only when the prior
    run died on an :class:`SDKError`, otherwise the neutral ``Exit code`` —
    a non-SDK death is never narrated to the journal as an SDK one.
    """
    if event.sdk_error is None:
        if event.exit_code is None:
            return ""
        return f" Exit code: {event.exit_code}."
    detail = f" SDK context:{_render_sdk_clause(event).removeprefix(',')}."
    tail = _render_stderr_tail(event.sdk_error.stderr or "")
    if tail:
        detail += f" stderr: {tail}."
    return detail
