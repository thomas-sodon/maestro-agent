"""Wrapper around claude_agent_sdk.query for convenient use.

All in-process MCP servers in this project build their tool registries via
:mod:`maestro.inproc_mcp`; no module here re-exports SDK-private helper APIs.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import copy
import errno
import functools
import importlib.metadata
import json
import logging
import re
import sqlite3
import time
import uuid
from collections import deque
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk import query as _query
from claude_agent_sdk._errors import (  # type: ignore[import-untyped]
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    MessageParseError,
    ProcessError,
)
from claude_agent_sdk.types import (
    AssistantMessage,
    ConversationResetMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskBudget,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

# The plan-headroom signal type surfaced by ``latest_plan_headroom``
# (proposal 062 Step 5). ``cost_tracking`` imports nothing from maestro
# beyond ``fileutil``, so this module-level import is acyclic too.
from maestro.cost_tracking import PlanHeadroom, resolve_billing

# Quota-exhaustion phrases shared with the log-tail diagnostics observer.
# Reused here (rather than re-declared) to detect the degraded plain-text
# rate-limit payload at SDK finalization. The import direction is acyclic:
# ``diagnostics`` imports ``logs`` (which imports nothing from maestro) at
# module level and only imports ``sdk`` from inside a function.
from maestro.diagnostics import QUOTA_KEYWORDS, ContextWindowSnapshot

# Durable single-line sidecar write for the structured transcript record
# (proposal 104). ``fileutil`` imports only stdlib, so this import is acyclic.
from maestro.fileutil import atomic_write

# The ``[1m]`` pricing label, owned by the model profile registry so the
# label cannot drift from the ``sdk_pricing`` projection. ``model_profiles``
# imports nothing from maestro, so this import is acyclic.
from maestro.model_profiles import LONG_CONTEXT_PRICING_SUFFIX

# Call-level taint (proposal 150). ``taint`` is a leaf module, so this is acyclic.
from maestro.taint import derive_taint, prompt_holds_untrusted

# The ungated-shell deny-list union (issue #4381). ``tool_surface`` imports
# ``sdk`` only under ``TYPE_CHECKING``, so this import is acyclic.
from maestro.tool_surface import deny_ungated_shell

# The serializable transcript record and its one-line JSONL serializer
# (proposal 104). ``transcript`` imports only ``maestro.types``, so this is
# acyclic; the provenance-aware ``SessionLogger`` builds and emits the sidecar
# header through these.
from maestro.transcript import TranscriptRecord, TranscriptTurn, to_jsonl_line
from maestro.types import TranscriptTurnKind
from maestro.vcs import DirtyWorkingTreeError

if TYPE_CHECKING:
    # Type-only: the write path holds the backend structurally (it only calls
    # ``.append``), so no runtime import is needed — importing ``cost_tracking``
    # here would pull it into the SDK's import graph unnecessarily.
    from maestro.config import Config
    from maestro.cost_tracking import CostSampleBackend

    # Type-only import: ``maestro.providers.base`` imports the contract types
    # from this module, so importing it at runtime would re-enter the cycle.
    # ``from __future__ import annotations`` keeps the annotation a string, so
    # the name is needed only for the type checker.
    from maestro.providers.base import Provider
    from maestro.tool_surface import ToolSurface

logger = logging.getLogger("maestro")


# Coroutine names of the child tasks the upstream SDK's
# ``Query.spawn_task`` (claude_agent_sdk/_internal/query.py:229-234) creates
# via ``spawn_detached`` (``loop.create_task`` under asyncio) and that
# ``Query.close()`` (query.py:856) cancels without awaiting.
# Unlike ``_collect_athrow_tasks`` — which substring-matches the
# Python-internal token ``async_generator_athrow`` that cannot appear in a
# user coroutine name — the SDK-child match is leaf-name-anchored (the exact
# final dotted component of the qualname) so an unrelated coroutine whose
# name merely *contains* one of these tokens is never collected (#1722). The
# disjoint name sets keep the two collectors from overlapping. Keep this
# in sync with the call sites: ``stream_input`` and
# ``wait_for_result_and_end_input`` come from ``_internal/client.py:217,220``;
# ``_handle_control_request`` comes from ``query.py:239`` via
# ``_spawn_control_request_handler``.
_SDK_CHILD_TASK_NAMES = frozenset(
    {
        "stream_input",
        "wait_for_result_and_end_input",
        "_handle_control_request",
    },
)

# Owner token for SDK child tasks (#4158). ``_query_impl`` and
# ``_create_session_impl`` set a fresh token while the SDK spawns its
# children; ``asyncio`` copies the spawning task's context into every child,
# so ``task.get_context()`` carries the token that identifies whose children
# they are. The drain matches on it, so a query finishing first never cancels
# a concurrent sibling query's stdin-closer (``wait_for_result_and_end_input``)
# — which would leave the sibling's CLI waiting on open stdin until
# ``STREAM_STALL``.
_SDK_CHILD_OWNER: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "maestro_sdk_child_owner", default=None,
)

# Sidecar attribute carrying a session's owner token from
# ``_create_session_impl`` to ``_disconnect_session_impl``.
_SESSION_CHILD_OWNER_ATTR = "_maestro_sdk_child_owner"


@contextlib.contextmanager
def _sdk_child_owner_scope() -> Iterator[object]:
    """Tag every task spawned inside this block with a fresh owner token."""
    owner = object()
    token = _SDK_CHILD_OWNER.set(owner)
    try:
        yield owner
    finally:
        _SDK_CHILD_OWNER.reset(token)


def _collect_athrow_tasks() -> list[asyncio.Task[Any]]:
    """Return pending tasks whose coroutine names ``async_generator_athrow``.

    Shared scan used by both :func:`_snapshot_athrow_tasks` (#1643 probe)
    and :func:`_drain_athrow_tasks` (#1648 drain) so the two helpers
    cannot drift on what counts as an athrow task. Any introspection
    failure is swallowed at DEBUG level — diagnostic instrumentation
    must never break its caller.
    """
    try:
        all_tasks = asyncio.all_tasks()
    except RuntimeError:
        # No running loop (called from sync context). Nothing to inspect.
        logger.debug("athrow collect: no running loop")
        return []
    except Exception:
        logger.debug("athrow collect: introspection failed", exc_info=True)
        return []

    matches: list[asyncio.Task[Any]] = []
    for task in all_tasks:
        try:
            coro = task.get_coro()
            qualname = getattr(coro, "__qualname__", "") or ""
            coro_repr = repr(coro)
            if (
                "async_generator_athrow" in qualname
                or "async_generator_athrow" in coro_repr
            ):
                matches.append(task)
        except Exception:
            # Honor the docstring's DEBUG-swallow contract: a single
            # un-introspectable task is logged (not silent) but never
            # aborts the scan, mirroring the outer guard above.
            logger.debug(
                "athrow collect: task introspection failed", exc_info=True
            )
            continue
    return matches


def _collect_sdk_child_tasks(owner: object | None) -> list[asyncio.Task[Any]]:
    """Return *owner*'s pending tasks whose coroutine names match an SDK child.

    Sibling of :func:`_collect_athrow_tasks`. The upstream SDK
    ``Query.close()`` (claude_agent_sdk/_internal/query.py:856) cancels
    every entry in ``self._child_tasks`` but does **not** await them, so
    the cancel-scope unwind can fire ``CancelledError`` into an arbitrary
    later ``await`` in maestro's loop (#1719 and the prior containment
    PRs #1648, #1643, #1656, #1696, #1704). This collector — paired with
    :func:`_drain_sdk_child_tasks` below — bounds the lifetime of those
    leaked child tasks inside the caller's task so the next ``await`` is
    safe. Same introspection-error discipline as the athrow collector:
    every failure is swallowed at DEBUG so the helper is safe inside
    ``finally`` blocks.

    Only tasks whose context carries *owner* under :data:`_SDK_CHILD_OWNER`
    are returned (#4158): a name match alone would also collect a concurrent
    query's children. A ``None`` owner identifies nobody, so nothing is
    returned — an unowned task is never cancelled.
    """
    if owner is None:
        return []
    try:
        all_tasks = asyncio.all_tasks()
    except RuntimeError:
        # No running loop (called from sync context). Nothing to inspect.
        logger.debug("sdk child collect: no running loop")
        return []
    except Exception:
        logger.debug("sdk child collect: introspection failed", exc_info=True)
        return []

    matches: list[asyncio.Task[Any]] = []
    foreign = 0
    for task in all_tasks:
        try:
            coro = task.get_coro()
            qualname = getattr(coro, "__qualname__", "") or ""
            if not qualname:
                # ``__qualname__`` unavailable: recover it from the repr,
                # whose form is ``<coroutine object <qualname> at 0x...>``.
                qualname = (
                    repr(coro)
                    .removeprefix("<coroutine object ")
                    .split(" at ", 1)[0]
                )
            # Leaf name = final dotted component of the qualname, so a
            # ``<locals>``-nested or class-bound child task (e.g.
            # ``Query._handle_control_request``) reduces to its bare name.
            # Exact leaf match — not substring — so an unrelated coroutine
            # whose name merely *contains* a token (e.g.
            # ``custom_stream_input_wrapper`` contains ``stream_input``) is
            # never collected and then cancelled by the drain helper (#1722).
            leaf = qualname.rsplit(".", 1)[-1]
            if leaf not in _SDK_CHILD_TASK_NAMES:
                continue
            if task.get_context().get(_SDK_CHILD_OWNER) is owner:
                matches.append(task)
            else:
                foreign += 1
        except Exception:
            # Honor the docstring's DEBUG-swallow contract: a single
            # un-introspectable task is logged (not silent) but never
            # aborts the scan, mirroring the outer guard above.
            logger.debug(
                "sdk child collect: task introspection failed", exc_info=True
            )
            continue
    if foreign:
        logger.debug(
            "sdk child collect: skipped %d foreign-owned SDK child task(s)",
            foreign,
        )
    return matches


def _format_athrow_task(task: asyncio.Task[Any]) -> str | None:
    """Render a pending athrow task for log lines, or ``None`` if it cannot
    be safely introspected. Helper for the warning lines emitted by both
    the snapshot probe and the drain wrapper."""
    try:
        return f"{task.get_name()}={repr(task.get_coro())[:120]}"
    except Exception:
        return None


def _snapshot_athrow_tasks(label: str) -> int:
    """Diagnostic probe for the residual ``async_generator_athrow`` leak (#1643).

    Scans the event loop and emits a single WARNING line listing every
    pending task whose coroutine names ``async_generator_athrow``. Logs
    nothing when the count is zero, so the probe is silent in healthy
    operation and safe to leave enabled in production.

    Returns the count of matching tasks. Never raises.
    """
    tasks = _collect_athrow_tasks()
    if tasks:
        descriptions = [d for d in (_format_athrow_task(t) for t in tasks) if d]
        logger.warning(
            "%d pending async_generator_athrow task(s) at %s: %s",
            len(tasks),
            label,
            "; ".join(descriptions),
        )
    return len(tasks)


async def _drain_athrow_tasks(
    label: str, *, timeout_seconds: float = 0.5,
) -> tuple[int, int]:
    """Drain residual ``async_generator_athrow`` tasks (#1648, #1704).

    The SDK's anyio cancel-scope teardown can spawn child athrow tasks for
    inner generators that complete asynchronously after ``aclose()`` returns.
    Without this drain, those tasks fire ``CancelledError`` into whatever
    ``await`` is alive at the moment of finalization — in production, the
    next phase's ``client.connect``. Bounding their lifetime inside the
    caller's task ensures any subsequent ``await`` sees an empty event-loop
    task set.

    The helper *actively cancels* every collected athrow task before
    awaiting them (#1704). A passive ``asyncio.wait`` is not enough: the
    orphan that leaks out of an SDK session's anyio cancel-scope teardown
    does not complete on its own — its cancel scope keeps re-firing
    ``CancelledError`` into whatever task awaits next. Calling
    ``task.cancel()`` schedules the orphan's cancel scope to unwind in
    *its own* task context (not ours), so the cancel scope releases
    instead of bleeding into successive awaits. ``asyncio.wait`` then
    absorbs the resulting per-task ``CancelledError`` into the
    ``(done, pending)`` tuple without propagating.

    Uses ``asyncio.wait(..., timeout=...)`` deliberately — it returns
    ``(done, pending)`` and never raises ``TimeoutError``. ``asyncio.wait_for``
    would re-introduce the raise-from-``finally`` hazard. Timeout default
    0.5 s: normal SDK teardown is sub-millisecond; a stuck athrow indicates
    an SDK-side bug we cannot fix here, and blocking the caller longer
    just amplifies user-visible latency.

    Returns ``(done_count, pending_count)``. Logs a WARNING when any task
    remains pending after the timeout — relevant for the shielded-orphan
    edge case where the athrow handler swallows ``CancelledError`` and
    cannot be force-completed. Never raises ``Exception``.
    """
    tasks = _collect_athrow_tasks()
    if not tasks:
        return (0, 0)
    for task in tasks:
        if not task.done():
            task.cancel()
    try:
        done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
    except Exception:
        # Defence-in-depth — ``asyncio.wait`` is documented to return
        # ``(done, pending)`` and not raise for the timeout case, but
        # this helper runs in ``finally`` blocks where any escaping
        # exception would clobber the in-flight one. ``CancelledError``
        # is intentionally NOT caught: it must propagate so the caller's
        # cancellation intent is preserved.
        logger.debug("athrow drain %s: wait failed", label, exc_info=True)
        return (0, len(tasks))
    if pending:
        names = [n for n in (_format_athrow_task(t) for t in pending) if n]
        logger.warning(
            "%d pending async_generator_athrow task(s) at %s after "
            "%.2fs drain: %s",
            len(pending),
            label,
            timeout_seconds,
            "; ".join(names),
        )
    return (len(done), len(pending))


async def _drain_sdk_child_tasks(
    label: str, *, owner: object | None, timeout_seconds: float = 2.0,
) -> tuple[int, int]:
    """Drain residual SDK ``Query`` child tasks (#1719).

    The upstream ``Query.close()`` (claude_agent_sdk/_internal/query.py:856)
    calls ``task.cancel()`` on every entry in ``self._child_tasks`` and then
    returns without awaiting any of them. ``task.cancel()`` only schedules a
    ``CancelledError`` to fire at the cancelled task's next ``await``; the
    asyncio scheduler then resumes that task whenever it next gets the loop —
    which can be inside an arbitrary later ``await`` in maestro's loop. The
    deferral was observed at 92 seconds in production for #1719 (crash at
    ``agent.py:4573``). Bounding the lifetime of those child tasks inside
    the SDK boundary's caller stops the leak at the source rather than
    catching ``CancelledError`` at every vulnerable ``await``.

    The helper *actively cancels* any not-yet-done child task before
    awaiting completion — same shape as :func:`_drain_athrow_tasks`
    (#1704). This is harmless when ``Query.close()`` already called
    ``cancel()``: the second cancel is a no-op once a ``CancelledError``
    is in flight. The benefit kicks in for the rarer case where the
    drain runs from a different code path (e.g. a connect failure
    fallback) and the child task has not been cancelled yet.

    Default timeout 2.0 s vs. 0.5 s for the athrow drain because SDK
    child tasks may be mid-stdin-write to the subprocess transport;
    waiting longer here is cheap and catches the slow-cancel case the
    athrow drain does not need to.

    Only *owner*'s children are drained (#4158) — see
    :func:`_collect_sdk_child_tasks`. *owner* is required so no caller can
    reintroduce a loop-wide sweep that cancels a concurrent query's children.

    Returns ``(done_count, pending_count)``. Logs a WARNING when any
    task remains pending after the timeout. Never raises ``Exception``;
    ``CancelledError`` is intentionally allowed to propagate so external
    cancellation intent is preserved.
    """
    tasks = _collect_sdk_child_tasks(owner)
    if not tasks:
        return (0, 0)
    for task in tasks:
        if not task.done():
            task.cancel()
    try:
        done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
    except Exception:
        # Defence-in-depth, mirroring the athrow drain. ``asyncio.wait``
        # is documented to return ``(done, pending)`` for timeouts, but
        # this helper runs in ``finally`` blocks where any escaping
        # exception would clobber the in-flight one. ``CancelledError``
        # is intentionally NOT caught.
        logger.debug("sdk child drain %s: wait failed", label, exc_info=True)
        return (0, len(tasks))
    if pending:
        names = [n for n in (_format_athrow_task(t) for t in pending) if n]
        logger.warning(
            "%d pending SDK child task(s) at %s after %.2fs drain: %s",
            len(pending),
            label,
            timeout_seconds,
            "; ".join(names),
        )
    return (len(done), len(pending))


class ErrorCategory(Enum):
    """Classification of SDK subprocess failures."""

    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    CLI_CRASH = "cli_crash"
    TIMEOUT = "timeout"
    NETWORK = "network"
    # First turn on a client opened with ``resume=<id>`` failed, likely
    # because the server-side transcript for that id has expired or was
    # garbage-collected. Callers should clear the persisted id and retry
    # with a fresh session.
    STALE_SESSION = "stale_session"
    # The SDK message stream went silent for longer than the configured
    # idle threshold without delivering a ``ResultMessage``. The session
    # is poisoned (the CLI subprocess is wedged); callers should clear
    # any persisted session id, disconnect the client, and retry the
    # phase with a fresh session. No cooldown — the failure is not
    # capacity-driven.
    STREAM_STALL = "stream_stall"
    # The CLI's ``StructuredOutput`` validator rejected every attempt the
    # model made at a schema-bound reply in one turn (terminal subtype
    # ``error_max_structured_output_retries``). Model-output invalidity,
    # not a transport fault: deliberately non-transient, so a caller that
    # owns an "unparseable reply" contract translates it and every other
    # caller still crashes loudly (#4535).
    STRUCTURED_OUTPUT_INVALID = "structured_output_invalid"
    UNKNOWN = "unknown"


# Terminal ``ResultMessage.subtype`` the CLI emits once its
# ``StructuredOutput`` validator exhausts its retries within a turn.
_STRUCTURED_OUTPUT_EXHAUSTED_SUBTYPE = "error_max_structured_output_retries"


# Categories ``run_loop`` retries via resume-in-place. ``implement_plan``
# reads it to tell a transient failure (leave plan approved, re-raise)
# from a terminal one (demote, notify, open an issue). Expanding it
# swallows unrecoverable failures — keep in sync with ``run_loop``.
# Transport retry (timeout / dropped conn / 5xx) is a *provider* duty
# exhausted before the error gets here, so omitting TIMEOUT/NETWORK is
# deliberate (``providers.openai``).
#
# It lives here, beside ``ErrorCategory``, rather than in ``agent.py``
# so every module that must key a swallow on transience can reach the
# one policy: ``agent`` imports ``evolution``, so ``evolution`` cannot
# import back. ``agent._TRANSIENT_SDK_CATEGORIES`` aliases this set.
TRANSIENT_SDK_CATEGORIES: frozenset[ErrorCategory] = frozenset({
    ErrorCategory.STREAM_STALL,
    ErrorCategory.RATE_LIMIT,
    ErrorCategory.CLI_CRASH,
    ErrorCategory.STALE_SESSION,
})


class SDKError(Exception):
    """Structured exception from SDK subprocess failures.

    Carries the original exception, captured stderr, error category,
    and retry metadata so callers can produce actionable diagnostics
    without diving into session logs.
    """

    def __init__(
        self,
        message: str,
        *,
        original: BaseException | None = None,
        stderr: str | None = None,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
        retries_attempted: int = 0,
        retries_exhausted: bool = False,
        exit_code: int | None = None,
        caller: str | None = None,
        session_reusable: bool = True,
    ) -> None:
        super().__init__(message)
        self.original = original
        self.stderr = stderr
        self.category = category
        self.retries_attempted = retries_attempted
        self.retries_exhausted = retries_exhausted
        self.exit_code = exit_code
        self.caller = caller
        # ``False`` means the persistent session is poisoned (a wedged
        # turn may still be running) or its CLI subprocess has exited, so
        # a caller must not issue another turn on it — only a fresh
        # session can recover.
        self.session_reusable = session_reusable
        # A received-but-unbooked ``ResultMessage``'s consume state
        # (a ``_ConsumeResult``), attached by ``_consume_messages`` when it
        # raises after a billed result arrived. The caller's re-raise arm
        # books it once and clears it (#4572).
        self.unbooked: _ConsumeResult | None = None

    @property
    def detail(self) -> str:
        """Human-readable error detail including stderr and retry info."""
        parts = [str(self)]
        if self.exit_code is not None:
            parts.append(f"exit_code: {self.exit_code}")
        if self.stderr:
            parts.append(f"stderr: {self.stderr}")
        if self.retries_exhausted:
            parts.append(f"(retries exhausted: {self.retries_attempted} attempts)")
        return "\n".join(parts)


# Subprocess exit codes that indicate permanent failure — never worth retrying.
# 2    — CLI usage error / invalid arguments
# 126  — command found but not executable
# 127  — command not found
# 137  — SIGKILL (OOM killer or forced termination)
_NON_TRANSIENT_EXIT_CODES: frozenset[int] = frozenset({2, 126, 127, 137})

# OSError errno values that mean a network transport fault. Shared by
# _is_transient() and _classify_error() so retry eligibility and category
# reporting can never disagree about the same exception (#405).
_NETWORK_ERRNOS: frozenset[int] = frozenset({
    errno.ECONNRESET, errno.EPIPE, errno.ECONNREFUSED,
})

# "429" must match as a standalone token: a bare substring fires on any
# digit run containing 429 (byte counts, IDs, ports), and a false
# RATE_LIMIT silently reroutes a real failure into transient
# resume-in-place handling instead of crashing loudly.
_HTTP_429_RE = re.compile(r"\b429\b")

# Substring of the subprocess transport's oversized-frame message
# ("JSON message exceeded maximum buffer size of N bytes", raised by
# ``claude_agent_sdk._internal.transport.subprocess_cli``). Pinned against
# the installed SDK source in ``tests/test_sdk_retry.py`` so an upstream reword
# fails loud instead of silently restoring the CLI_CRASH retry storm.
_BUFFER_OVERFLOW_MARKER = "exceeded maximum buffer size"

# Credential-failure wording shared by ``_classify_error`` and
# ``_is_transient`` so the two verdicts cannot drift apart.
_AUTH_KEYWORDS = (
    "invalid api key", "unauthorized", "authentication failed",
    "permission denied",
)

# The CLI logs "<Tool> tool permission denied" on every non-allow tool
# decision — including each action-gate block — so that phrase describes a
# policy deny, never a credential failure (#4310).
_TOOL_DENY_PHRASE = "tool permission denied"


def _is_auth_failure(text: str) -> bool:
    """Return True if lowercased *text* names a credential failure."""
    return any(kw in text.replace(_TOOL_DENY_PHRASE, "") for kw in _AUTH_KEYWORDS)


# Marker on every line of the CLI's forced ``--debug-to-stderr`` stream.
_DEBUG_LINE_MARKER = "[debug]"


def _non_debug_lines(text: str) -> str:
    """Drop ``[debug]`` log lines from already-lowercased *text*."""
    return "\n".join(
        line for line in text.splitlines() if _DEBUG_LINE_MARKER not in line
    )


_NODE_VERSION_BANNER = re.compile(r"^Node\.js v\d")
_INDENTED_PROPERTY_LINE = re.compile(r"^\s+[\w$]+: ")
# A line naming an error (``Error: x``, ``TypeError: y``, ``[cause]: Error``):
# the preferred anchor, so a nested property block never displaces it.
_ERROR_HEADER_LINE = re.compile(r"^\s*(\[cause\]: )?[\w.$]*Error\b")


def diagnostic_stderr(text: str) -> str:
    """Return the diagnostic lines of a CLI stderr stream, original case kept.

    Both SDK paths force ``--debug-to-stderr``, so stderr is the whole debug
    stream oldest-first, with the line that names the failure last.  Lines
    carrying the ``[debug]`` marker (any case) are dropped.  Surviving lines
    are rejoined with ``"\\n"`` (so a trailing newline is dropped and CRLF
    normalised whether or not any line was removed); when no non-blank
    line survives, the stream is returned unchanged so an all-debug stream
    still yields its tail to a caller that keeps the last N characters.
    """
    kept = "\n".join(
        ln for ln in text.splitlines() if _DEBUG_LINE_MARKER not in ln.lower()
    )
    return kept if kept.strip() else text


def _is_failure_trailer(line: str) -> bool:
    """True for a stderr line that follows, and never names, a failure.

    Covers stack frames (``    at frame``) and Node's uncaught-exception
    trailer: the error's property block (``{``, indented ``key: value``
    lines, quoted array items, nested closers ``}``/``},``/``]``/``],``)
    and the ``Node.js v<version>`` banner.  Only the fallback anchor in
    :func:`stderr_failure_tail` uses it; an ``...Error`` header wins first.
    """
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith("at ")
        or stripped in ("{", "}", "},", "]", "],")
        or (line[:1].isspace() and stripped.startswith(("'", '"')))
        or _NODE_VERSION_BANNER.match(stripped) is not None
        or _INDENTED_PROPERTY_LINE.match(line) is not None
    )


def stderr_failure_tail(text: str, cap: int) -> str:
    """Cap *text* to *cap* characters, keeping the line that names the failure.

    A plain ``text[-cap:]`` assumes the failure line is the stream's last
    line, but an ``Error: ...`` header is often followed by a stack trace
    and Node's uncaught-exception trailer (see :func:`_is_failure_trailer`).
    The anchor is the last line that names an error (``...Error`` header);
    absent one, the last line that is not such a trailer.  When the
    anchor-to-end span exceeds *cap* it is kept from the anchor (``[:cap]``)
    so the header survives and trailing lines are dropped.  Otherwise the
    last-*cap* tail (which contains it) is returned, starting at a line
    boundary: a leading partial line is dropped so a later re-filter (the
    persisted tail is re-rendered on restart) sees whole lines only.
    """
    if len(text) <= cap:
        return text
    lines = text.splitlines()
    candidates = [i for i, ln in enumerate(lines) if _ERROR_HEADER_LINE.match(ln)]
    if not candidates:
        candidates = [i for i, ln in enumerate(lines) if not _is_failure_trailer(ln)]
    if candidates:
        anchored = "\n".join(lines[candidates[-1]:])
        if len(anchored) > cap:
            return anchored[:cap]
    tail = text[-cap:]
    if text[-cap - 1] not in "\r\n":
        _partial, sep, rest = tail.partition("\n")
        if sep and rest.strip():
            return rest
    return tail


def _exit_code_of(exc: Exception) -> int | None:
    """Return the structured ``exit_code`` from a ``ProcessError`` (or ``None``)."""
    val = getattr(exc, "exit_code", None)
    return val if isinstance(val, int) else None


def is_environment_fault(exc: BaseException) -> bool:
    """Return True when *exc* reports a permanent fault in the *environment*.

    An ``SDKError`` carrying one of :data:`_NON_TRANSIENT_EXIT_CODES` is a fault
    of the host, not of whatever the CLI was asked to do: the OOM killer took
    the subprocess (137), the binary is missing or not executable (126/127), or
    it was invoked with bad arguments (2). Callers that would otherwise charge a
    failure to the *work* — ``agent.implement_plan``'s demote arm charges an
    attempt against the plan — use this to re-raise instead, so an environment
    fault never spends a plan's ``failure_count`` (#3488, ledger r1).

    Narrower than "not transient": ``CLINotFoundError``/``MessageParseError``
    and the buffer-overflow ``CLIJSONDecodeError`` are permanent too, but only
    the exit-code set is unambiguously environmental — a parse/frame fault can
    be provoked by the work itself (an oversized Edit result), so charging the
    plan for it stays correct.

    A :class:`~maestro.vcs.DirtyWorkingTreeError` joins the set for the same
    reason: the mutating phase refused *before* cutting a branch or calling the
    model, so nothing about the work was tested — the fault is an operator's
    in-flight edit sitting on ``main`` (#3711). Demoting the plan there would
    spend a human approval on a host condition, so the caller re-raises and the
    loop dies loudly on the tree state instead.
    """
    if isinstance(exc, DirtyWorkingTreeError):
        return True
    return isinstance(exc, SDKError) and exc.exit_code in _NON_TRANSIENT_EXIT_CODES


def _classify_error(exc: Exception) -> ErrorCategory:
    """Categorize an SDK exception for structured reporting."""
    msg = str(exc).lower()
    stderr_raw = getattr(exc, "stderr", "") or ""
    if isinstance(stderr_raw, bytes):
        stderr_raw = stderr_raw.decode(errors="replace")
    combined = f"{msg} {stderr_raw.lower()}"
    # The TIMEOUT and NETWORK arms read only non-[DEBUG] lines: the forced
    # debug stream logs "Execution timeout: 10000ms" on every Bash call, which
    # would otherwise turn any later CLI crash into TIMEOUT (#4373). AUTH and
    # RATE_LIMIT keep reading ``combined`` so a debug-logged 401/429 still wins.
    signal = _non_debug_lines(f"{msg}\n{stderr_raw.lower()}")

    # Content signals win over exception type: a ProcessError with
    # "rate limit" in stderr is a rate-limit, not a generic CLI crash.
    # Forced debug stderr carries a tool-deny line after every action-gate
    # block (#4310); ``_is_auth_failure`` ignores that phrase, while a
    # credential failure logged at [DEBUG] still classifies as AUTH.
    if _is_auth_failure(combined):
        return ErrorCategory.AUTH
    if (
        any(kw in combined for kw in ("rate limit", "rate_limit", "too many requests"))
        or _HTTP_429_RE.search(combined)
    ):
        return ErrorCategory.RATE_LIMIT
    # "etimedout": Node's "connect ETIMEDOUT <addr>" — the errno token has
    # no space, so neither "timeout" nor "timed out" matches it (#405).
    if any(kw in signal for kw in (
        "timeout", "timed out", "deadline exceeded", "etimedout",
    )):
        return ErrorCategory.TIMEOUT
    # After TIMEOUT so "connection timed out" keeps classifying as TIMEOUT.
    # Python phrasing plus Node-style errno tokens: the CLI subprocess is
    # Node.js, whose transport faults reach stderr as "connect ECONNREFUSED
    # <addr>", "read ECONNRESET", "write EPIPE", "socket hang up" — phrasings
    # the Python keywords never match (#405). DNS faults keep the
    # "getaddrinfo" prefix: a bare "enotfound" would substring-match
    # "FileNotFoundError" in a wrapped Python message.
    if any(kw in signal for kw in (
        "connection refused", "connection reset", "connection aborted",
        "broken pipe", "network is unreachable",
        "econnrefused", "econnreset", "epipe", "socket hang up",
        "getaddrinfo enotfound", "getaddrinfo eai_again",
    )):
        return ErrorCategory.NETWORK
    # Typed SDK exceptions. CLINotFoundError must come before
    # CLIConnectionError (subclass relationship); reversing the order
    # misclassifies a missing CLI binary as NETWORK and would flip
    # _is_transient() back to True.
    if isinstance(exc, CLINotFoundError):
        return ErrorCategory.UNKNOWN
    if isinstance(exc, CLIConnectionError):
        return ErrorCategory.NETWORK
    if isinstance(exc, MessageParseError):
        return ErrorCategory.UNKNOWN
    if isinstance(exc, CLIJSONDecodeError):
        # A stream-json frame above the subprocess transport's buffer ceiling
        # is not a crashed CLI: the oversized message (e.g. an Edit result on
        # a >1 MiB source file) is produced deterministically, so resume-in-
        # place replays it and fails identically. Keep it out of CLI_CRASH —
        # which ``agent._TRANSIENT_SDK_CATEGORIES`` retries — so the defect
        # fails loud on the first hit instead of burning the retry ceiling.
        if _BUFFER_OVERFLOW_MARKER in combined:
            return ErrorCategory.UNKNOWN
        return ErrorCategory.CLI_CRASH
    if isinstance(exc, ConnectionError):
        return ErrorCategory.NETWORK
    if isinstance(exc, OSError) and exc.errno in _NETWORK_ERRNOS:
        return ErrorCategory.NETWORK
    exit_code = _exit_code_of(exc)
    if exit_code is not None:
        # ``_is_transient`` refuses to retry these codes (usage error, not
        # executable, command not found, SIGKILL/OOM) because they are
        # permanent. Reading the same set here keeps the two verdicts in
        # agreement: UNKNOWN sits outside ``agent._TRANSIENT_SDK_CATEGORIES``,
        # so the loop crashes loudly on the first hit instead of resuming in
        # place until the CLI_CRASH ceiling is burned (#3488).
        #
        # A non-transient category otherwise takes ``implement_plan``'s demote
        # arm, which would charge an environment fault (OOM kill, missing
        # binary) against the plan's ``failure_count``. ``is_environment_fault``
        # carves exactly this exit-code set back out there, so the loop still
        # crashes loudly but the plan keeps its attempt (ledger r1).
        if exit_code in _NON_TRANSIENT_EXIT_CODES:
            return ErrorCategory.UNKNOWN
        return ErrorCategory.CLI_CRASH
    if "exit code" in combined:
        return ErrorCategory.CLI_CRASH
    return ErrorCategory.UNKNOWN

_sdk_log_dir: Path | None = None
# When true, ``SessionLogger`` writes a provenance-stamped ``.jsonl`` transcript
# sidecar next to each SDK ``.log`` file (proposal 104). Set at startup by
# ``configure_sdk_transcripts`` from ``[logs] structured_transcripts_enabled``,
# which now defaults on. The module seed stays ``False`` so a construction
# before configuration (tests, the CLI before startup) leaves the raw ``.log``
# path unchanged; the raw ``.log`` is byte-compatible with or without a sidecar.
_structured_transcripts_enabled: bool = False
# Per-turn content cap for the transcript sidecar (proposal 104). Set at startup
# by ``configure_sdk_transcripts``; seeds the ``SessionLogger`` default so a
# construction without the arg (tests, the CLI before configuration) still
# bounds turn content. Independent of the human ``.log``'s ``_truncate`` bound.
_DEFAULT_TRANSCRIPT_CONTENT_MAX = 20_000
_transcript_content_max: int = _DEFAULT_TRANSCRIPT_CONTENT_MAX
_sdk_model: str | None = None
_sdk_fallback_model: str | None = None
_sdk_task_budget_tokens: int = 0
# Maximum seconds of silence on the SDK message stream before
# ``_consume_messages`` raises ``SDKError(STREAM_STALL)``. Configured at
# startup via :func:`configure_sdk_stream_idle_timeout`. ``None`` disables
# the safety net (no per-message timeout). Default mirrors the production
# TOML default so unit tests see the same behavior as production.
_sdk_stream_idle_timeout_seconds: float | None = 180.0
_cost_sample_backend: CostSampleBackend | None = None
# Auth method (``"oauth"`` / ``"api_key"`` / ``None``) the cost stamp's
# ``billing`` resolves against; set by :func:`configure_cost_tracking`.
_cost_sample_auth_method: str | None = None
_concurrency_semaphore: asyncio.Semaphore | None = None
_usage_sink: Callable[[QueryUsage], None] | None = None
# Per-model pricing table for cost estimation when ``ResultMessage`` never
# arrives (early-exit predicate path in ``_finalize_usage``). Populated by
# :func:`configure_sdk_pricing` from ``Config.sdk_pricing`` (the
# ``model_profiles.pricing_table()`` projection, or an operator's
# ``[sdk.pricing.<model>]`` override). An empty table is the safe default — the estimator returns
# ``None`` for unknown models so we never silently fabricate cost. Values
# are constructed by :class:`Pricing` (see definition further down).
_sdk_pricing: dict[str, Pricing] = {}
# Per-process set of models we have already warned about missing pricing
# for. Avoids spamming the log once per call when a model has no entry.
_pricing_warned: set[str] = set()

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0
_RATE_LIMIT_MAX_RETRIES = 5
_RATE_LIMIT_RETRY_BASE_DELAY = 60.0
_RATE_LIMIT_MAX_SLEEP = 300.0
# Cap on a single sleep-until-reset after a hard ``rejected`` rate limit.
# An unbounded in-``query()`` sleep would hold the phase hostage for hours;
# the agent's extended-idle probe ladder is the designed shutdown-aware home
# for very long waits. Truncation is loud — see
# :func:`_rate_limit_sleep_seconds`.
_RATE_LIMIT_SLEEP_CAP_SECONDS = 1800.0

# Cap on the per-session CLI-stderr ring buffer. A persistent session forces
# ``--debug-to-stderr``, so the CLI emits stderr on every successful turn; the
# buffer is only emptied on the exception path in ``query_on_session``. Backing
# it with a fixed-size deque keeps the most-recent lines for diagnostic
# attach-to-exception while bounding memory across a long quiet run.
_SESSION_STDERR_MAX_LINES = 100

# Cap on the ``errors``/``result`` head embedded in a terminal-error
# ``SDKError`` so the cause reaches ``maestro.log`` / ``lifecycle.json``
# without bloating either (context-bloat-cap invariant).
_TERMINAL_ERROR_DETAIL_MAX = 200

# Throttle interval for INFO-level emission of partial-message stream deltas.
# Without throttling, a long think-only span would flood maestro.log with
# one INFO line per delta; without any emission, the log falls silent for
# tens of seconds during the span and looks indistinguishable from a hang.
_STREAM_HEARTBEAT_INTERVAL_SECONDS = 10.0

# Bounds for the post-``client.interrupt()`` drain in
# :func:`query_on_session`. ``client.interrupt()`` only sends an SDK
# control request — it does not drain the underlying anyio memory stream.
# The CLI's background reader keeps pumping messages from the prior turn
# into the buffer until the CLI honours the interrupt, and the next
# ``client.query()`` would then read those leftovers before the new
# turn's response. We drain the stream defensively after every early-exit
# interrupt; both bounds are belt-and-braces protection against a
# misbehaving CLI hanging the loop.
_INTERRUPT_DRAIN_IDLE_TIMEOUT = 2.0  # seconds between consecutive drained messages
_INTERRUPT_DRAIN_MAX_MESSAGES = 50  # hard cap on drained message count



# ---------------------------------------------------------------------------
# Sticky ``allowed_warning`` throttle — per-SDK-call state machine.
#
# The Anthropic SDK emits ``RateLimitEvent(status="allowed_warning")`` when a
# call is close to (but not yet rejected by) a quota limit.  It's the API
# telling us "slow down or the next call gets rejected".  Honouring that is
# much cheaper than absorbing a hard reject, which blocks the loop for hours.
#
# State machine:
#   * On a query() call that observed an ``allowed_warning`` event, the
#     multiplier doubles (1 -> 2 -> 4 -> 8 -> 16, capped at
#     ``_WARNING_THROTTLE_MAX_MULTIPLIER``).  The clean-call counter resets
#     to zero.  The ``max(..., 2)`` in the doubling step guarantees the very
#     first sighting jumps 1 -> 2 rather than 1 -> 1.
#   * On a query() call that did NOT observe a warning, the clean-call
#     counter increments.  Once it reaches the configured hysteresis, the
#     multiplier halves and the counter resets.  Hysteresis prevents a
#     single cheap call from leaking the throttle back to full speed while
#     the rate-limit window is still narrow.
#   * Before the next query() call begins (at the top of :func:`query`,
#     before the retry loop), if ``multiplier > 1`` we sleep
#     ``base_seconds * multiplier`` (capped at
#     ``_WARNING_THROTTLE_MAX_SLEEP_SECONDS``) so the slowdown fires between
#     every SDK call rather than only between iterations.
#
# This used to live in ``MaestroAgent.run_loop()`` as a between-iteration
# sleep multiplier; moving it here means a warning observed during
# ``contemplate`` also slows down ``evolve`` and later phases within the same
# iteration, not just the next iteration.
_WARNING_THROTTLE_MAX_MULTIPLIER = 16
_WARNING_THROTTLE_MAX_SLEEP_SECONDS = 1800  # 30 minutes

_warning_throttle_multiplier: int = 1
_warning_throttle_clean_calls: int = 0
_warning_throttle_consecutive_max_warnings: int = 0
# Last ``resets_at`` reported on an ``allowed_warning`` event (#4514).
# ``allowed_warning`` is a window-level state attached to every call once
# usage crosses the warning band, so the held loop waits for ``resets_at``
# rather than probing mid-window.
_warning_last_resets_at: int | None = None

# Configured at startup by :func:`configure_sdk_warning_throttle` from TOML.
_sdk_warning_throttle_base_seconds: int = 10
_sdk_warning_throttle_hysteresis: int = 3
_sdk_warning_hold_threshold: int = 3
_sdk_warning_hold_probe_interval: int = 60
_sdk_warning_hold_min_utilization: float = 0.95


def configure_sdk_warning_throttle(
    base_seconds: int, hysteresis: int,
    hold_threshold: int = 3, hold_probe_interval: int = 60,
    hold_min_utilization: float = 0.95,
) -> None:
    """Set the base sleep seconds and decay hysteresis for the per-call
    ``allowed_warning`` throttle.

    Called from :func:`maestro.cli.main` alongside
    :func:`configure_sdk_model` / :func:`configure_sdk_logging`.
    """
    global _sdk_warning_throttle_base_seconds, _sdk_warning_throttle_hysteresis
    global _sdk_warning_hold_threshold, _sdk_warning_hold_probe_interval
    global _sdk_warning_hold_min_utilization
    _sdk_warning_throttle_base_seconds = base_seconds
    _sdk_warning_throttle_hysteresis = hysteresis
    _sdk_warning_hold_threshold = hold_threshold
    _sdk_warning_hold_probe_interval = hold_probe_interval
    _sdk_warning_hold_min_utilization = hold_min_utilization


def _get_warning_throttle_state_for_tests() -> tuple[int, int, int]:
    """Return (multiplier, clean_calls, consecutive_max_warnings). Test-only hook."""
    return (
        _warning_throttle_multiplier,
        _warning_throttle_clean_calls,
        _warning_throttle_consecutive_max_warnings,
    )


def get_warning_throttle_multiplier() -> int:
    """Current warning throttle multiplier. 1 = no throttle active."""
    return _warning_throttle_multiplier


def is_warning_hold_active() -> bool:
    """True when the throttle is stuck at max and enough consecutive warnings
    have been observed to trigger the loop-level hold gate."""
    return (
        _warning_throttle_multiplier >= _WARNING_THROTTLE_MAX_MULTIPLIER
        and _warning_throttle_consecutive_max_warnings >= _sdk_warning_hold_threshold
    )


def get_warning_hold_probe_interval() -> int:
    """Base seconds between warning-hold probe calls."""
    return _sdk_warning_hold_probe_interval


def get_warning_hold_resets_at() -> int | None:
    """Epoch seconds the last warned rate-limit window resets, or ``None``."""
    return _warning_last_resets_at


#: Bound on one wait for a warned window's ``resets_at`` so a far-future or
#: bogus timestamp cannot park the loop indefinitely (#4514).
_WARNING_HOLD_MAX_RESET_WAIT = 6 * 3600
_WARNING_HOLD_RESET_SLACK_SECONDS = 10


def get_warning_hold_reset_wait() -> int | None:
    """Seconds a held loop should wait for the warned window to reset.

    ``None`` when no future ``resets_at`` was reported — the caller then
    falls back to its exponential-backoff probe. A probe cannot clear the
    hold mid-window, so waiting for the reset spares the wasted calls.
    """
    if _warning_last_resets_at is None:
        return None
    remaining = _warning_last_resets_at - time.time()
    if remaining <= 0:
        return None
    return int(min(
        remaining + _WARNING_HOLD_RESET_SLACK_SECONDS,
        _WARNING_HOLD_MAX_RESET_WAIT,
    ))


def _update_warning_throttle_state(
    saw_warning: bool,
    utilization: float | None = None,
    resets_at: int | None = None,
) -> None:
    """Apply the post-call state transition.  Called from :func:`query` after
    a successful return.  Broken out so tests can exercise the state machine
    without driving a full mock query.

    Any warning doubles the per-call multiplier, but only an at-cap warning
    whose *utilization* reaches ``_sdk_warning_hold_min_utilization`` (or
    carries no utilization at all — the legacy path) counts toward the
    loop-level hold (#4514)."""
    global _warning_throttle_multiplier, _warning_throttle_clean_calls
    global _warning_throttle_consecutive_max_warnings
    global _warning_last_resets_at
    if saw_warning:
        _warning_last_resets_at = resets_at
        _warning_throttle_multiplier = min(
            max(_warning_throttle_multiplier * 2, 2),
            _WARNING_THROTTLE_MAX_MULTIPLIER,
        )
        _warning_throttle_clean_calls = 0
        near_exhaustion = (
            utilization is None
            or utilization >= _sdk_warning_hold_min_utilization
        )
        if (
            _warning_throttle_multiplier >= _WARNING_THROTTLE_MAX_MULTIPLIER
            and near_exhaustion
        ):
            _warning_throttle_consecutive_max_warnings += 1
        else:
            _warning_throttle_consecutive_max_warnings = 0
    else:
        _warning_last_resets_at = None
        _warning_throttle_consecutive_max_warnings = 0
        if _warning_throttle_multiplier > 1:
            _warning_throttle_clean_calls += 1
            if _warning_throttle_clean_calls >= _sdk_warning_throttle_hysteresis:
                _warning_throttle_multiplier = max(
                    _warning_throttle_multiplier // 2, 1,
                )
                _warning_throttle_clean_calls = 0


def _patch_sdk_stderr_drain() -> None:
    """Monkey-patch the SDK subprocess transport to drain stderr before raising.

    The SDK's ``SubprocessCLITransport._read_messages`` raises a ``ProcessError``
    immediately when the CLI exits non-zero, with a hardcoded message
    "Check stderr output for details".  But ``close()`` cancels the async stderr
    reader task before the ``stderr`` callback fires, so the actual error is lost.

    This patch replaces the post-exit error block to first let the stderr task
    finish (with a short timeout), then include any captured output in the
    ``ProcessError.stderr`` field.
    """
    try:
        from claude_agent_sdk._internal.transport.subprocess_cli import (  # type: ignore[import-untyped]
            SubprocessCLITransport,
        )
    except ImportError:  # pragma: no cover — SDK always installed
        return

    _original = SubprocessCLITransport._read_messages_impl  # type: ignore[attr-defined]

    async def _patched_read_messages_impl(self: Any) -> Any:
        """Yield to the concurrent stderr reader before re-raising ProcessError.

        We cannot await ``self._stderr_task`` to completion from here.
        ``asyncio.wait_for`` wraps the coro in a new task, and awaiting the
        detached stderr task cross-task can surface teardown races when the
        transport is reused across sessions. SDK ``close()`` already handles
        the stderr task cancellation wrapped in ``suppress(Exception)``. All
        we need is a brief yield so the already-running ``_handle_stderr``
        task (spawned via ``spawn_detached``) drains pipe-buffered bytes
        through our stderr callback before we unwind.
        """
        try:
            async for msg in _original(self):
                yield msg
        except ProcessError:
            if self._stderr_task is not None:
                await asyncio.sleep(0.05)
            raise

    SubprocessCLITransport._read_messages_impl = _patched_read_messages_impl  # type: ignore[attr-defined,method-assign]


_patch_sdk_stderr_drain()
_RETRY_BACKOFF_FACTOR = 2.0


def _is_transient(exc: Exception) -> bool:
    """Return True if the exception is likely transient and worth retrying."""
    msg = str(exc).lower()
    # Never retry auth-related errors.
    if _is_auth_failure(msg):
        return False
    # Missing CLI binary (config/install failure) and SDK/CLI protocol
    # mismatches are permanent: retrying burns 3 attempts and hides the
    # root cause. CLINotFoundError must be checked before CLIConnectionError
    # because it subclasses it — a parent-class check first would treat a
    # missing CLI as transient.
    if isinstance(exc, CLINotFoundError | MessageParseError):
        return False
    if isinstance(exc, CLIConnectionError):
        return True
    # Contained internal anyio cancel-scope deadlines (wrapped as TimeoutError
    # by the BaseException containment block in query()).
    if isinstance(exc, TimeoutError) and "cancelled by internal scope" in msg:
        return True
    # Structured subprocess exit code — prefer over fragile string matching.
    # Permanent failures (usage errors, OOM kills, missing CLI) must not burn
    # retries: raise immediately so the loop crashes with full diagnostics.
    exit_code = _exit_code_of(exc)
    if exit_code is not None:
        return exit_code not in _NON_TRANSIENT_EXIT_CODES
    # Subprocess crashes / exit code failures (string fallback).
    if "exit code" in msg:
        return True
    if isinstance(exc, ConnectionError):
        return True
    return isinstance(exc, OSError) and exc.errno in _NETWORK_ERRNOS


def _extract_error_detail(exc: Exception) -> str:
    """Build a detailed error string, including subprocess stderr if available."""
    base = f"{type(exc).__name__}: {exc}"
    stderr = getattr(exc, "stderr", None)
    if stderr is None:
        return base
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    if stderr:
        return f"{base}\nstderr: {stderr}"
    return base


def _is_productive_aggregate(text: str, tool_calls: list[ToolCall]) -> bool:
    """Whether assistant text + tool-call set make a productive completion.

    Mirrors :func:`maestro.executor._is_productive` but on raw fields rather
    than a :class:`QueryResult`, so :func:`_consume_messages` can detect
    unproductive completions before the caller's per-step retry loop runs.
    """
    if text:
        return True
    return any(not tc.is_error for tc in tool_calls)


def _is_rate_limit_ghost_completion(
    text: str,
    *,
    input_tokens: int,
    output_tokens: int,
    total_cost_usd: float | None,
) -> bool:
    """Whether a completion is a CLI-synthesized rate-limit ghost completion.

    When the account quota is exhausted, the Claude Code CLI can synthesize the
    rate-limit notice itself as the assistant's text — the model is never
    invoked — and end the stream with a zero-usage ``ResultMessage`` (or, on the
    early-exit path, leave it in the accumulated text buffer) carrying no
    exception and no structured ``RateLimitEvent``.

    Returns ``True`` only when **both** hold: the text carries a
    quota-exhaustion phrase (:data:`maestro.diagnostics.QUOTA_KEYWORDS`, the
    single shared list also used by the log-tail observer) **and** the result
    reports zero usage (zero input/output tokens and zero/absent cost). The
    zero-usage guard is load-bearing — it distinguishes a CLI-synthesized ghost
    reply from a real, paid model reply that merely discusses rate limits, which
    would report non-zero usage. Phrases are anchored on the specific
    quota-exhaustion wording rather than a loose ``"rate limit"`` so such a
    genuine reply is not matched.
    """
    lowered = text.strip().lower()
    if not any(phrase in lowered for phrase in QUOTA_KEYWORDS):
        return False
    return (
        input_tokens == 0
        and output_tokens == 0
        and total_cost_usd in (None, 0.0)
    )


def _is_unproductive_after_rejection(
    *,
    last: ResultMessage | None,
    early_exit: bool,
    text_buffer: str,
    usage: QueryUsage,
    tool_calls: list[ToolCall],
    structured_output_requested: bool,
) -> bool:
    """Whether a completion following a rejected rate limit is unproductive.

    The union of the predicates the unproductive-completion guards in
    :func:`_consume_messages` defer on a ``RateLimitEvent("rejected")``:
    missing result, terminal ``is_error``, zero-turn zero-token, quota-prose
    ghost (on ``last.result``, or ``text_buffer`` for an early exit), and the
    unproductive aggregate. A productive result still returns normally —
    work can continue after a rejection (#542).
    """
    if last is None:
        if not early_exit:
            return True
        return _is_rate_limit_ghost_completion(
            text_buffer,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_cost_usd=usage.total_cost_usd,
        )
    usage_dict: dict[str, Any] = last.usage if isinstance(last.usage, dict) else {}
    input_tokens = _coerce_int(usage_dict.get("input_tokens"))
    output_tokens = _coerce_int(usage_dict.get("output_tokens"))
    if last.is_error:
        return True
    if last.num_turns == 0 and input_tokens == 0 and output_tokens == 0:
        return True
    if _is_rate_limit_ghost_completion(
        last.result or "",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_cost_usd=last.total_cost_usd,
    ):
        return True
    return not structured_output_requested and not _is_productive_aggregate(
        last.result or "", tool_calls,
    )


def _error_result_is_transient(
    *,
    api_error_status: int | None,
    result: str,
    errors: list[str] | None,
) -> bool:
    """Whether a terminal ``is_error`` ResultMessage names a transient failure.

    The reliable primary signal is ``api_error_status`` — populated by the CLI
    (since v2.1.110) for API-status errors. Two status bands are transient and
    route to resume-in-place RATE_LIMIT handling: the 5xx band (e.g. a
    ``529 Overloaded`` burst) and the retryable-4xx band (``408`` Request
    Timeout, ``425`` Too Early, ``429`` Too Many Requests). The ``429`` case is
    load-bearing: the CLI's terminal rate-limit prose ("Too Many Requests" /
    "rate limit exceeded") is deliberately *absent* from ``QUOTA_KEYWORDS``, so
    the status band is the only signal that catches a retries-exhausted 429 and
    keeps the loop resuming in place instead of hard-crashing on UNKNOWN. As a
    fallback for a transient overload/quota condition that surfaces *without* an
    HTTP status (older CLI, or a non-HTTP transient), the error prose in
    ``.result`` / ``.errors`` is scanned for the shared quota/overload phrases
    (:data:`maestro.diagnostics.QUOTA_KEYWORDS`, the same list the zero-cost
    ghost guard keys on). A present ``api_error_status`` is authoritative — the
    prose scan runs **only** when it is absent, so a definitively non-transient
    status carrying incidental quota/overload prose (e.g. a ``403`` billing
    error mentioning "quota") still fails loud rather than being misrouted to
    RATE_LIMIT resume-in-place. Everything else stays non-transient so genuinely
    permanent failures (non-retryable 4xx, max-turns, execution errors) fail
    loud rather than resuming in place forever.
    """
    if api_error_status is not None:
        return 500 <= api_error_status <= 599 or api_error_status in (408, 425, 429)
    haystack = " ".join([result, *(errors or [])]).lower()
    return any(phrase in haystack for phrase in QUOTA_KEYWORDS)


def _terminal_error_detail(message: ResultMessage) -> str:
    """Name a terminal error result's cause: its subtype plus a capped head.

    The head is the joined ``errors`` list when present, else ``result``,
    capped at ``_TERMINAL_ERROR_DETAIL_MAX`` chars.
    """
    errors = "; ".join(str(e) for e in message.errors or [])
    label, head = ("errors", errors) if errors else ("result", message.result or "")
    return (
        f"subtype={message.subtype} "
        f"{label}={head[:_TERMINAL_ERROR_DETAIL_MAX]!r}"
    )


def _emit_empty_response_diagnostic(
    *,
    caller: str | None,
    last_message_kind: str | None,
    pending_tools: int,
    session_log: SessionLogger | None,
    reason: str,
    category: ErrorCategory,
) -> str:
    """Log + session-log an empty/degraded-completion diagnostic.

    Returns the fingerprint string so callers can include it in the
    raised :class:`SDKError` message. ``reason`` distinguishes the narrow
    (``num_turns=0 tokens=0``), broad (``unproductive aggregate``), and
    rate-limit ghost-completion cases in post-mortem. ``category`` names the
    :class:`ErrorCategory` the caller actually raises (``STREAM_STALL`` for
    the empty-completion shapes, ``RATE_LIMIT`` for the ghost completion) so
    the log line never claims a category other than the one raised.
    """
    fingerprint = (
        f"last_message={last_message_kind} pending_tools={pending_tools}"
    )
    logger.warning(
        "SDK returned empty completion (%s, caller=%s, %s) — raising %s",
        reason, caller or "unknown", fingerprint, category.name,
    )
    if session_log:
        session_log.log(
            f"{category.name}: empty completion reason={reason} "
            f"caller={caller or 'unknown'} {fingerprint}"
        )
    return fingerprint


def configure_sdk_logging(sdk_log_dir: Path) -> None:
    """Set the directory for SDK session log files."""
    global _sdk_log_dir
    _sdk_log_dir = sdk_log_dir


def open_session_logger(caller: str | None) -> SessionLogger | None:
    """Build a :class:`SessionLogger` from the configured logging switches.

    ``None`` when :func:`configure_sdk_logging` never set a log directory;
    otherwise the logger obeys :func:`configure_sdk_transcripts`. The single
    constructor every provider uses, so none copies the module-global plumbing.
    """
    if _sdk_log_dir is None:
        return None
    return SessionLogger(
        _sdk_log_dir,
        caller=caller,
        transcripts_enabled=_structured_transcripts_enabled,
        transcript_content_max=_transcript_content_max,
    )


def configure_sdk_transcripts(
    enabled: bool, content_max: int = _DEFAULT_TRANSCRIPT_CONTENT_MAX,
) -> None:
    """Enable or disable provenance-stamped ``.jsonl`` transcript sidecars.

    When enabled, ``SessionLogger`` writes a structured transcript header next
    to each SDK ``.log`` file (proposal 104) and mirrors each turn into it,
    bounded per turn by *content_max*. Mirrors :func:`configure_sdk_logging`;
    called once at startup from ``cli.main``. *content_max* defaults so an
    existing single-arg call still configures the enable flag.
    """
    global _structured_transcripts_enabled, _transcript_content_max
    _structured_transcripts_enabled = enabled
    _transcript_content_max = content_max


def configure_cost_tracking(
    backend: CostSampleBackend, *, auth_method: str | None = None,
) -> None:
    """Set the cost-sample backend the write path appends to.

    Either the default ``JsonlCostSampleStore`` (``cost_samples.jsonl``) or, when
    ``[paths] sqlite_state_enabled`` is on, a ``SqliteCostSampleStore`` over
    ``state.db`` — both satisfy :class:`~maestro.cost_tracking.CostSampleBackend`,
    so ``_log_sdk_call`` never branches on which is live (proposal 106).
    *auth_method* is the credential the stamp's ``billing`` resolves against.
    """
    global _cost_sample_backend, _cost_sample_auth_method
    _cost_sample_backend = backend
    _cost_sample_auth_method = auth_method


def configure_sdk_model(model: str | None) -> None:
    """Set the default model for all SDK queries.

    When non-None, any :func:`query` call whose ``options.model is None``
    will have its model substituted with this value.  Explicit
    ``model=...`` on a :class:`QueryOptions` always wins, so individual
    call sites can opt out.  An empty string is normalised to ``None``
    so that an unconfigured TOML value behaves as "no override" and
    preserves the SDK default.
    """
    global _sdk_model
    _sdk_model = model or None


def configure_sdk_concurrency(max_concurrent: int) -> None:
    """Set the maximum number of concurrent ``query()`` calls.

    Creates an :class:`asyncio.Semaphore` that ``query()`` acquires before
    executing and releases in its ``finally`` block.  Call from
    :func:`maestro.cli.main` alongside the other ``configure_*`` helpers.
    """
    global _concurrency_semaphore
    _concurrency_semaphore = asyncio.Semaphore(max_concurrent)


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens for one model.

    Used by :func:`_estimate_cost_usd` to derive a cost figure when the
    SDK never delivers a ``ResultMessage`` (early-exit predicate path).
    Missing rates default to ``0.0`` so a partial table still produces a
    lower-bound estimate.
    """

    input: float = 0.0
    output: float = 0.0
    cache_creation: float = 0.0
    cache_read: float = 0.0


def configure_sdk_pricing(
    pricing: Mapping[str, Mapping[str, float]] | None,
) -> None:
    """Set the per-model pricing table for early-exit cost estimation.

    *pricing* maps a model name (matching the SDK ``model`` value) to a
    ``{input, output, cache_creation, cache_read}`` rate dict in USD per
    million tokens. ``None`` or an empty mapping clears the table — the
    estimator then returns ``None`` for every model and the early-exit
    branch falls back to ``$0`` rather than silently fabricating cost.
    """
    global _sdk_pricing, _pricing_warned
    _pricing_warned = set()
    if not pricing:
        _sdk_pricing = {}
        return
    _sdk_pricing = {
        name: Pricing(
            input=float(rates.get("input", 0.0)),
            output=float(rates.get("output", 0.0)),
            cache_creation=float(rates.get("cache_creation", 0.0)),
            cache_read=float(rates.get("cache_read", 0.0)),
        )
        for name, rates in pricing.items()
    }


def _estimate_cost_usd(usage: QueryUsage) -> float | None:
    """Estimate cost from token counts using the configured pricing table.

    Returns ``None`` when the model is unknown or pricing is unconfigured —
    callers should treat that as "leave cost unset" rather than write a
    fabricated $0 estimate. Logs a single ``WARNING`` per unknown model so
    operators see the gap without log spam.
    """
    model = usage.model
    if model is None:
        return None
    rates = _sdk_pricing.get(model)
    if rates is None:
        if model not in _pricing_warned:
            _pricing_warned.add(model)
            logger.warning(
                "No SDK pricing entry for model=%s — early-exit cost "
                "will read $0 (configure [sdk.pricing.<model>] in TOML)",
                model,
            )
        return None
    return (
        usage.input_tokens * rates.input
        + usage.output_tokens * rates.output
        + usage.cache_creation_tokens * rates.cache_creation
        + usage.cache_read_tokens * rates.cache_read
    ) / 1_000_000


def configure_sdk_fallback_model(model: str | None) -> None:
    """Set the fallback model for all SDK queries.

    When the primary model is unavailable, the SDK transparently
    fails over to this model.  ``None`` or empty string disables
    fallback.  Called from :func:`maestro.cli.main`.
    """
    global _sdk_fallback_model
    _sdk_fallback_model = model or None


def configure_sdk_task_budget(budget_tokens: int) -> None:
    """Set the default per-call token budget.

    When > 0, every ``query()`` call whose ``QueryOptions.task_budget_tokens``
    is ``None`` will use ``TaskBudget(total=N)`` so the model paces itself.
    Zero disables the global default.

    Called from :func:`maestro.cli.main`.
    """
    global _sdk_task_budget_tokens
    _sdk_task_budget_tokens = budget_tokens


def configure_sdk_stream_idle_timeout(seconds: float | None) -> None:
    """Set the default per-message idle timeout for the SDK stream.

    When non-None, every ``_consume_messages`` call wraps each ``__anext__``
    in :func:`asyncio.wait_for` so a silent stream raises
    ``SDKError(ErrorCategory.STREAM_STALL)`` after this many seconds. ``None``
    disables the safety net entirely. Per-call override available via the
    ``stream_idle_timeout_seconds`` kwarg on :func:`query` and
    :func:`query_on_session`.

    Called from :func:`maestro.cli.main`.
    """
    global _sdk_stream_idle_timeout_seconds
    _sdk_stream_idle_timeout_seconds = seconds


def set_usage_sink(sink: Callable[[QueryUsage], None]) -> None:
    """Register a callback that receives every :class:`QueryUsage` after logging.

    The sink is observation-only — exceptions are swallowed so a broken
    sink never fails a query.  Only one sink is active at a time; calling
    this again replaces the previous sink.
    """
    global _usage_sink
    _usage_sink = sink


def clear_usage_sink() -> None:
    """Remove the active usage sink (if any)."""
    global _usage_sink
    _usage_sink = None


# Every module global assigned under a ``global`` statement — the process-wide
# runtime sinks and defaults the ``configure_*`` calls set. The test suite's
# autouse owner snapshots and restores exactly this set around every test so a
# sink configured by one test (e.g. a cost backend pointing at the live
# ``data/``) cannot outlive it. ``tests/test_sdk_runtime_owner.py`` AST-checks
# that the tuple matches the module's ``global`` statements.
_RUNTIME_GLOBALS: tuple[str, ...] = (
    "_cost_sample_backend",
    "_cost_sample_auth_method",
    "_sdk_log_dir",
    "_structured_transcripts_enabled",
    "_transcript_content_max",
    "_usage_sink",
    "_sdk_model",
    "_sdk_fallback_model",
    "_sdk_pricing",
    "_pricing_warned",
    "_sdk_task_budget_tokens",
    "_sdk_stream_idle_timeout_seconds",
    "_concurrency_semaphore",
    "_warning_throttle_multiplier",
    "_warning_throttle_clean_calls",
    "_warning_throttle_consecutive_max_warnings",
    "_sdk_warning_throttle_base_seconds",
    "_sdk_warning_throttle_hysteresis",
    "_sdk_warning_hold_threshold",
    "_sdk_warning_hold_probe_interval",
    "_sdk_warning_hold_min_utilization",
    "_warning_last_resets_at",
    "_anyio_format_verified",
    "_call_policy_resolution_enabled",
    "_enforced_tool_surface_config",
)

#: ``[sdk] call_policy_resolution_enabled`` (proposal 140 step 4) — when on,
#: the public seam resolves every call's effort from
#: :func:`maestro.call_policy.tier_effort` instead of the call site's literal.
_call_policy_resolution_enabled: bool = False


def configure_call_policy_resolution(enabled: bool) -> None:
    """Toggle seam-side effort resolution from the call-policy registry."""
    global _call_policy_resolution_enabled
    _call_policy_resolution_enabled = enabled


#: ``[sdk] enforced_tool_surface_enabled`` (proposal 142) — the config the
#: seam resolves tool surfaces against; ``None`` means the flag is off.
_enforced_tool_surface_config: Config | None = None


def configure_enforced_tool_surface(config: Config | None) -> None:
    """Enable seam-side tool-surface resolution against *config* (``None`` = off)."""
    global _enforced_tool_surface_config
    _enforced_tool_surface_config = config


def _copy_container(value: object) -> object:
    """Shallow-copy mutable containers; keep every other object by identity.

    Copying a backend, sink or ``asyncio.Semaphore`` would restore a clone
    (sharing the original's internals) rather than the original object.
    """
    if isinstance(value, (set, dict, list)):
        return copy.copy(value)
    return value


def snapshot_sdk_runtime() -> dict[str, object]:
    """Capture every runtime global, shallow-copying containers.

    The copy matters for in-place mutation (``_pricing_warned.add``): without
    it the snapshot would alias the live set and restore would undo nothing.
    """
    module_globals = globals()
    return {
        name: _copy_container(module_globals[name]) for name in _RUNTIME_GLOBALS
    }


def restore_sdk_runtime(snapshot: Mapping[str, object]) -> None:
    """Write a :func:`snapshot_sdk_runtime` capture back into the module."""
    module_globals = globals()
    for name in _RUNTIME_GLOBALS:
        module_globals[name] = snapshot[name]


# ---------------------------------------------------------------------------
# Per-iteration usage aggregation
# ---------------------------------------------------------------------------

@dataclass
class IterationUsage:
    """Aggregated SDK usage for a single loop iteration."""

    iteration: int
    started_at: str  # ISO timestamp
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    per_caller: dict[str, int] = field(default_factory=dict)
    task_id: str | None = None
    total_turns: int = 0
    per_caller_turns: dict[str, int] = field(default_factory=dict)
    # ``True`` once any call this iteration came from a ``usage_metering=False``
    # provider (proposal 058 Step 13). The token/cost totals are then a *lower
    # bound* — the unmetered calls contributed nothing — so consumers
    # (``cost_per_task``, the dashboard) treat the row's spend as unknown
    # rather than measured-as-zero. The default-metering provider
    # (``anthropic``) leaves this ``False`` and behaviour is unchanged.
    unmetered: bool = False
    # Distinct registry names of the providers that served calls this
    # iteration, in first-seen order (proposal 067 Step 2). Carried alongside
    # ``unmetered`` so a usage row says *which* provider produced unknown
    # spend rather than leaving the operator to guess. Empty when no call
    # carried a provider stamp (pre-067 rows, hand-rolled usages).
    providers: list[str] = field(default_factory=list)
    # Task-scoped slice of the totals above (#4066): the cost, call count and
    # unmetered flag of only those calls made while a task run's scope was
    # open (``begin_task_run`` .. ``teardown_task_run``). ``task_id`` labels
    # the whole row, but the row's totals also carry every other phase's spend
    # this iteration, so per-task queries (``cost_per_task``) sum these fields
    # instead. Iteration totals are unchanged.
    task_cost_usd: float = 0.0
    task_calls: int = 0
    task_unmetered: bool = False
    # Transient: ``True`` while a task run's scope is open, so
    # ``MaestroAgent._on_query_usage`` credits the task fields. Not serialized.
    _task_scope_active: bool = field(default=False, repr=False)
    # Transient bookkeeping for session-aware ``total_turns`` dedupe.
    # Tracks the highest ``num_turns`` value already credited per session_id
    # so a resumed session whose SDK reports cumulative turn counts is
    # incremented only by the *new* turns above the prior peak.
    # Not serialized — IterationUsage is written-once at iteration end and
    # never read back into the live aggregator.
    _session_peak_turns: dict[str, int] = field(default_factory=dict, repr=False)

    def credit_cost(self, usage: QueryUsage) -> None:
        """Add one call's cost to the totals, and to the task slice if open."""
        # ``usage.total_cost_usd`` is per-call spend by contract: a session's
        # running CLI total is converted to a per-turn delta at the SDK seam
        # (``_finalize_usage``'s ``cost_baseline``, #4306), never here.
        cost = usage.total_cost_usd if usage.total_cost_usd is not None else 0.0
        self.total_cost_usd += cost
        # A single unmetered call (a usage_metering=False provider) makes the
        # iteration's cost a lower bound, not a measurement — flag it so
        # cost_per_task / the dashboard treat the spend as unknown (058 Step 13).
        if usage.unmetered:
            self.unmetered = True
        # Task-scoped slice (#4066): only calls made inside an open task run
        # count toward the task's cost; other phases stay iteration overhead.
        if self._task_scope_active:
            self.task_calls += 1
            self.task_cost_usd += cost
            if usage.unmetered:
                self.task_unmetered = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON storage (JSONL or lifecycle.json)."""
        d: dict[str, Any] = {
            "iteration": self.iteration,
            "started_at": self.started_at,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "total_cost_usd": self.total_cost_usd,
            "per_caller": dict(self.per_caller),
            "task_id": self.task_id,
            "total_turns": self.total_turns,
            "per_caller_turns": dict(self.per_caller_turns),
            "unmetered": self.unmetered,
            "providers": list(self.providers),
            "task_cost_usd": self.task_cost_usd,
            "task_calls": self.task_calls,
            "task_unmetered": self.task_unmetered,
        }
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IterationUsage:
        """Deserialize, defaulting missing fields to zero/empty.

        Tolerates unknown fields and non-int-coercible values without
        raising — corrupt lifecycle.json records should not crash startup.
        """
        raw_caller = data.get("per_caller") or {}
        per_caller: dict[str, int] = {}
        if isinstance(raw_caller, dict):
            for k, v in raw_caller.items():
                if isinstance(k, str):
                    try:
                        per_caller[k] = int(v)
                    except (TypeError, ValueError):
                        continue

        raw_caller_turns = data.get("per_caller_turns") or {}
        per_caller_turns: dict[str, int] = {}
        if isinstance(raw_caller_turns, dict):
            for k, v in raw_caller_turns.items():
                if isinstance(k, str):
                    try:
                        per_caller_turns[k] = int(v)
                    except (TypeError, ValueError):
                        continue

        def _int(key: str, default: int = 0) -> int:
            try:
                return int(data.get(key, default))
            except (TypeError, ValueError):
                return default

        def _float(key: str, default: float = 0.0) -> float:
            try:
                return float(data.get(key, default))
            except (TypeError, ValueError):
                return default

        started_raw = data.get("started_at", "")
        started = started_raw if isinstance(started_raw, str) else ""

        raw_task_id = data.get("task_id")
        task_id = raw_task_id if isinstance(raw_task_id, str) else None

        raw_providers = data.get("providers") or []
        providers: list[str] = []
        if isinstance(raw_providers, list):
            providers = [p for p in raw_providers if isinstance(p, str)]

        return cls(
            iteration=_int("iteration"),
            started_at=started,
            calls=_int("calls"),
            input_tokens=_int("input_tokens"),
            output_tokens=_int("output_tokens"),
            cache_read_tokens=_int("cache_read_tokens"),
            cache_creation_tokens=_int("cache_creation_tokens"),
            total_cost_usd=_float("total_cost_usd"),
            per_caller=per_caller,
            task_id=task_id,
            total_turns=_int("total_turns"),
            per_caller_turns=per_caller_turns,
            unmetered=bool(data.get("unmetered", False)),
            providers=providers,
            task_cost_usd=_float("task_cost_usd"),
            task_calls=_int("task_calls"),
            task_unmetered=bool(data.get("task_unmetered", False)),
        )


def write_iteration_usage(path: Path, usage: IterationUsage) -> None:
    """Append one :class:`IterationUsage` record as a JSONL line.

    Uses :func:`maestro.fileutil.file_lock` for safe concurrent access.
    """
    import json as _json

    from maestro.fileutil import file_lock

    line = _json.dumps(usage.to_dict())
    with file_lock(path), open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_iteration_usage(path: Path, n: int = 20) -> list[IterationUsage]:
    """Read the last *n* :class:`IterationUsage` records from a JSONL file.

    Uses :class:`collections.deque` with ``maxlen`` to bound memory.
    Returns an empty list when the file does not exist.
    """
    import json as _json

    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            tail: deque[str] = deque(fh, maxlen=n)
    except OSError:
        return []
    results: list[IterationUsage] = []
    for raw in tail:
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = _json.loads(raw)
            if not isinstance(d, dict):
                continue
            results.append(IterationUsage.from_dict(d))
        except (ValueError, TypeError):
            continue
    return results


def _safe_caller(caller: str | None) -> str:
    """Slugify *caller* into a filesystem-safe transcript-filename component.

    A ``None``, empty, or all-unsafe *caller* degrades to ``"unknown"`` so the
    sidecar filename is always valid. Non-alphanumeric runs collapse to a single
    ``-`` and leading/trailing ``-`` are trimmed, mirroring the slug idiom the
    plan-writing commands use for branch/file names.
    """
    if not caller:
        return "unknown"
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", caller).strip("-")
    return slug or "unknown"


class SessionLogger:
    """Direct file writer for a single SDK query session.

    When *transcripts_enabled*, the logger also builds a provenance-stamped
    :class:`~maestro.transcript.TranscriptRecord` header (phase ``caller`` plus
    the optional persistent-session label, loop iteration, and task id) and
    writes it as a one-line ``.jsonl`` sidecar next to the ``.log`` at
    :meth:`close` (proposal 104). Each conversation turn is mirrored into the
    record via :meth:`add_turn` at full content, bounded per turn by
    *transcript_content_max* independently of the ``.log``'s ``_truncate`` bound.
    """

    def __init__(
        self,
        sdk_log_dir: Path,
        *,
        caller: str | None,
        session_label: str | None = None,
        iteration: int | None = None,
        task_id: str | None = None,
        transcripts_enabled: bool = False,
        transcript_content_max: int = _DEFAULT_TRANSCRIPT_CONTENT_MAX,
    ) -> None:
        self._transcript_content_max = transcript_content_max
        now = datetime.now(UTC)
        date_dir = sdk_log_dir / now.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        short_id = uuid.uuid4().hex[:6]
        stem = now.strftime("%H-%M-%S")
        filename = f"{stem}_{short_id}.log"
        self._path = date_dir / filename
        self._file = open(self._path, "w", encoding="utf-8")  # noqa: SIM115
        # Provenance sidecar (proposal 104): reuse the same ``now``/``short_id``
        # as the ``.log`` so both share a stem, and slug the phase into the
        # filename so transcripts are self-describing and groupable.
        self._transcript_path: Path | None = None
        self._transcript_record: TranscriptRecord | None = None
        if transcripts_enabled:
            self._transcript_path = (
                date_dir / f"{stem}_{_safe_caller(caller)}_{short_id}.jsonl"
            )
            self._transcript_record = TranscriptRecord(
                caller=caller or "unknown",
                session_id=short_id,
                started_at=now.isoformat(),
                session_label=session_label,
                iteration=iteration,
                task_id=task_id,
                turns=[],
            )
        try:
            self.log(f"SDK session started at {now.isoformat()}")
        except Exception:
            self._file.close()
            raise

    @property
    def path(self) -> Path:
        return self._path

    def log(self, message: str) -> None:
        if self._file.closed:
            return
        try:
            ts = datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3]
            self._file.write(f"[{ts}] {message}\n")
            self._file.flush()
        except OSError:
            logger.debug("Failed to write to SDK session log", exc_info=True)

    def add_turn(
        self, kind: TranscriptTurnKind, role: str, content: str,
    ) -> None:
        """Append one full-content turn to the transcript sidecar record.

        A no-op when transcripts are disabled (``self._transcript_record is
        None``); the human ``.log`` path is untouched either way. ``content`` is
        bounded by ``self._transcript_content_max`` as a plain slice — no
        ``[N chars total]`` suffix, since that idiom belongs to the ``.log``'s
        ``_truncate``. The per-chunk streaming deltas are deliberately not
        routed here, so a streamed answer collapses to one final ``text`` turn.
        """
        if self._transcript_record is None:
            return
        self._transcript_record.turns.append(
            TranscriptTurn(
                kind=kind,
                role=role,
                ts=datetime.now(UTC).isoformat(),
                content=content[: self._transcript_content_max],
            )
        )

    def set_tools_advertised(self, tools: list[str]) -> None:
        """Stamp the CLI's init-message tool list into the sidecar header.

        A no-op on the sidecar when transcripts are disabled, like
        :meth:`add_turn`; the ``.log`` line is written either way. Last write
        wins, so a stream carrying several init messages records the latest.
        """
        self.log(f"Tools advertised: {_truncate(', '.join(tools))}")
        if self._transcript_record is None:
            return
        self._transcript_record.tools_advertised = list(tools)

    def set_mcp_servers_advertised(self, servers: list[str]) -> None:
        """Stamp the CLI's init-message MCP server names into the sidecar header.

        The twin of :meth:`set_tools_advertised`: the ``.log`` line is written
        either way, the sidecar field only when transcripts are enabled, and
        the last write wins.
        """
        self.log(f"MCP servers advertised: {_truncate(', '.join(servers))}")
        if self._transcript_record is None:
            return
        self._transcript_record.mcp_servers_advertised = list(servers)

    def close(self) -> None:
        if not self._file.closed:
            self.log("SDK session ended")
            self._file.close()
            # Emit the provenance-stamped ``.jsonl`` sidecar once, after the
            # ``.log`` teardown (proposal 104). Guarded by the closed check so
            # a double ``close()`` never re-writes it. ``close()`` runs in the
            # ``finally`` of ``_query_impl``/``_query_on_session_impl``, so a
            # sidecar OSError must be swallowed (mirroring ``log()``) — else it
            # would mask an in-flight exception and crash an otherwise-successful
            # query. The sidecar is best-effort observability, not a correctness
            # requirement.
            if (
                self._transcript_record is not None
                and self._transcript_path is not None
            ):
                try:
                    atomic_write(
                        self._transcript_path,
                        to_jsonl_line(self._transcript_record) + "\n",
                    )
                except OSError:
                    logger.debug(
                        "Failed to write SDK transcript sidecar",
                        exc_info=True,
                    )

    def __enter__(self) -> SessionLogger:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _truncate(text: str, max_len: int = 500) -> str:
    """Truncate text for log output, appending total length if shortened.

    The returned string is guaranteed to be at most *max_len* characters.
    """
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    suffix = f"... [{len(text)} chars total]"
    truncate_at = max_len - len(suffix)
    if truncate_at > 0:
        return text[:truncate_at] + suffix
    return text[:max_len]


def _coerce_int(value: object) -> int:
    """Best-effort coerce an SDK ``usage`` dict value to ``int``.

    The Anthropic SDK types ``usage`` as a ``TypedDict`` with ``int`` values
    for known keys.  We accept any int-castable input and fall back to ``0``
    on missing / unparseable data so unknown extension keys (e.g. cache token
    splits the SDK reports but does not type) parse cleanly.
    """
    if value is None:
        return 0
    try:
        return int(value)  # type: ignore[call-overload, no-any-return]
    except (TypeError, ValueError):
        return 0


def _note_session_effort_override(
    caller: str | None,
    effort: str | None,
    session: object | None,
) -> None:
    """Log when an ``effort`` value is silently dropped on an existing session.

    When both *session* and *effort* are non-``None``, the per-turn effort
    override has no effect — the session-level effort configured at creation
    applies instead.  This helper emits a ``debug`` trace so the silent
    override is visible in logs rather than invisible.
    """
    if session is not None and effort is not None:
        logger.debug(
            "effort=%s ignored on existing session (caller=%s)", effort, caller,
        )


def _log_sdk_call(usage: QueryUsage) -> None:
    """Emit a single structured log line summarising one SDK call.

    The line uses a stable ``"SDK call:"`` prefix and ``key=value`` pairs so
    that :func:`maestro.diagnostics.build_sdk_usage_stats` can parse it back.
    *usage.total_cost_usd* is this call's own spend — session running totals
    are already reduced to per-turn deltas by :func:`_finalize_usage` on
    success, or by :func:`_book_unbooked_error_result` on a categorized raise
    (#4306, #4572): every received ``ResultMessage`` is booked exactly once.
    """
    cost = usage.total_cost_usd if usage.total_cost_usd is not None else 0.0
    caller = usage.caller or "unknown"
    logger.info(
        "SDK call: caller=%s tokens_in=%d tokens_out=%d "
        "cache_read=%d cache_create=%d cost=%.4f duration_ms=%d "
        "turns=%d attempts=%d",
        caller,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_creation_tokens,
        cost,
        usage.duration_ms,
        usage.num_turns,
        usage.attempts,
    )
    if _cost_sample_backend is not None:
        try:
            from maestro.cost_tracking import CostSample
            sample = CostSample(
                timestamp=datetime.now(UTC).isoformat(),
                cost_usd=cost,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_creation_tokens=usage.cache_creation_tokens,
                caller=caller,
                duration_ms=usage.duration_ms,
                estimated=usage.estimated,
                unmetered=usage.unmetered,
                provider=usage.provider,
                # ``"default"`` distinguishes a measured "no effort sent" from
                # a legacy row (``None``); a non-``str`` model (a test double's
                # sidecar) is dropped rather than persisted.
                effort=usage.effort or "default",
                model=usage.model if isinstance(usage.model, str) else None,
                billing=resolve_billing(usage.provider, _cost_sample_auth_method),
            )
            # Cost-sample telemetry is best-effort — a write failure must never
            # crash the SDK result path. ``sqlite3.Error`` joins ``OSError`` now
            # that the backend may be the SQLite store; both are narrow, typed
            # catches (not a broad silencer), so the fail-loud guard is honoured.
            _cost_sample_backend.append(sample)
        except (OSError, sqlite3.Error):
            logger.debug("Failed to write cost sample", exc_info=True)
    if _usage_sink is not None:
        try:
            _usage_sink(usage)
        except Exception:
            logger.debug("Usage sink failed", exc_info=True)


def _log_rate_limit(caller: str | None, attempt: int, resets_at: int) -> None:
    """Emit a structured warning when an SDK call is rate-limited."""
    resets_in_s = max(0, resets_at - int(time.time()))
    logger.warning(
        "SDK rate-limited: caller=%s attempt=%d resets_in_s=%d",
        caller or "unknown",
        attempt,
        resets_in_s,
    )


def _format_rate_limit_detail(info: RateLimitInfo) -> str:
    """Render a rate-limit event's set window fields as `` key=value`` tokens.

    Returns ``""`` when the SDK left every field ``None``; otherwise each token
    is prefixed by a space so callers append it directly after the status.
    """
    fields = (
        ("type", info.rate_limit_type),
        ("utilization", info.utilization),
        ("resets_at", info.resets_at),
        ("overage_status", info.overage_status),
        ("overage_resets_at", info.overage_resets_at),
    )
    return "".join(f" {key}={value}" for key, value in fields if value is not None)


def _rate_limit_sleep_seconds(
    resets_at: int, caller: str | None,
) -> float:
    """Compute the bounded sleep before a rate-limit retry and log it honestly.

    The full wait is ``resets_at - now + 10``; the return value is capped at
    ``_RATE_LIMIT_SLEEP_CAP_SECONDS``. This is the single place that words the
    rate-limit sleep log, so a truncated sleep can never claim to reach the
    reset: below the cap the log says "until reset" (then accurate); above it
    a WARNING names both the true time-to-reset (buffer excluded, matching
    :func:`_log_rate_limit`) and the capped sleep, and flags that a repeat
    rate-limit on wake is expected.
    """
    resets_in = max(resets_at - time.time(), 0)
    full_wait = resets_in + 10
    wait_s = min(full_wait, _RATE_LIMIT_SLEEP_CAP_SECONDS)
    if wait_s < full_wait:
        logger.warning(
            "Rate limited — reset is %ds away; sleeping capped %ds, expect "
            "a repeat rate-limit on wake (caller=%s)",
            int(resets_in),
            int(wait_s),
            caller or "unknown",
        )
    else:
        logger.warning(
            "Rate limited — sleeping %ds until reset (caller=%s)",
            int(wait_s),
            caller or "unknown",
        )
    return wait_s


@dataclass
class ToolCall:
    """Record of a tool invocation during a query."""

    name: str
    input: dict[str, Any]
    result: str | None = None
    is_error: bool = False


@dataclass
class QueryUsage:
    """Usage statistics from the query."""

    total_tokens: int = 0
    tool_uses: int = 0
    duration_ms: int = 0
    total_cost_usd: float | None = None
    num_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    caller: str | None = None
    attempts: int = 1
    session_id: str | None = None
    # Resolved SDK model name for this call (e.g. ``claude-opus-4-7``).
    # Populated by :func:`query` and :func:`query_on_session` so that
    # :func:`_estimate_cost_usd` can look up the right pricing entry on
    # the early-exit path. ``None`` when no model was configured.
    model: str | None = None
    # The effort the provider actually received for this call — the
    # session's creation-time effort on a session turn (a turn's own declared
    # effort never reaches the model), the call's own effort on a one-shot
    # call. ``None`` when none was sent (proposal 140).
    effort: str | None = None
    # ``True`` when ``total_cost_usd`` was derived from token counts via
    # :func:`_estimate_cost_usd` rather than read from a ``ResultMessage``.
    # Lets cost samples and per-iteration usage stay auditable.
    estimated: bool = False
    # ``True`` when the provider cannot meter this call at all (a
    # ``usage_metering=False`` provider, e.g. the human-paced ``cli_paced``
    # CLI). The token counts and ``total_cost_usd`` are then *unknown*, not
    # measured-as-zero: downstream aggregation contributes nothing for the
    # row and the dashboard renders the missing counts as ``—`` rather than
    # ``$0.00`` (proposal 058 Step 13). Distinct from ``estimated``, which
    # still carries a real (token-derived) dollar figure.
    unmetered: bool = False
    # Registry name of the provider that served this call (``"anthropic"`` /
    # ``"openai"`` / ``"cli_paced"``), stamped at row creation by each
    # provider (proposal 067 Step 2) so cost samples and per-iteration usage
    # attribute spend — in particular, so an unmetered row names the provider
    # whose spend is unknown-by-design. ``None`` only for rows built outside
    # a provider path (tests, hand-rolled usages).
    provider: str | None = None


#: SDK beta string that enables Anthropic's 1M-token context window.
#: Mirrors :class:`claude_agent_sdk.types.SdkBeta`; the long-context
#: opt-in call sites (``evolution.self_review``, ``executor`` session,
#: and ``agent.implement_plan``'s implementation session) pass this on
#: :attr:`QueryOptions.betas` when they switch to the long-context model.
LONG_CONTEXT_BETA = "context-1m-2025-08-07"


#: The beta strings :func:`_build_sdk_options` will pass through to the
#: Anthropic SDK. Seeded from the single beta Maestro actually uses so the
#: ``[1m]`` pricing label (:func:`_label_long_context_pricing`, keyed off the
#: same :data:`LONG_CONTEXT_BETA`) and the SDK beta header cannot drift: a
#: typo'd beta literal would otherwise attach verbatim, silently disabling the
#: 1M-context window while the pricing label silently falls back to the base
#: tier. Mirrors the
#: cli_paced provider's :data:`~maestro.providers.cli_paced._SUPPORTED_BETAS`
#: fail-loud precedent, keeping each provider's beta-reachability local.
_KNOWN_BETAS: frozenset[str] = frozenset({LONG_CONTEXT_BETA})


#: Suffix appended to ``QueryUsage.model`` when the long-context beta is
#: active so :func:`_estimate_cost_usd` can hit the 1M-tier pricing row.
#: Anthropic enables the 1M-token context window via the
#: ``context-1m-2025-08-07`` beta header on existing model identifiers, not
#: via a separate model name — so ``message.model`` reports the *real*
#: model string the CLI was invoked with (e.g. ``claude-opus-4-7``).
#: Without this seam, the synthetic ``claude-opus-4-7[1m]`` row in
#: ``[sdk.pricing]`` would never fire and 1M-tier calls would be priced
#: at the standard row rather than the 1M-tier rates the registry
#: (``maestro.model_profiles``) owns. The label is a Maestro-internal
#: pricing key, *not* a model identifier — never pass it to the SDK.
_LONG_CONTEXT_PRICING_SUFFIX = LONG_CONTEXT_PRICING_SUFFIX


def _label_long_context_pricing(
    model: str | None, betas: list[str] | None,
) -> str | None:
    """Return ``model`` with the ``[1m]`` suffix if the long-context beta is active.

    Used to keep :attr:`QueryUsage.model` in sync with the per-tier rows in
    the ``[sdk.pricing]`` table. ``None`` and the empty case pass through
    unchanged so non-long-context callers see no behaviour change.
    """
    if model is None:
        return None
    if betas and LONG_CONTEXT_BETA in betas:
        if model.endswith(_LONG_CONTEXT_PRICING_SUFFIX):
            return model
        return f"{model}{_LONG_CONTEXT_PRICING_SUFFIX}"
    return model


@dataclass
class QueryOptions:
    """Vendor-neutral options for an SDK query.

    Wraps the subset of agent configuration that Maestro uses.
    The fields map to universal concepts shared across agent SDKs
    (Claude, Codex, Gemini).  :func:`query` converts this to the
    vendor-specific ``ClaudeAgentOptions`` internally.

    Note: ``system`` (the system prompt) is intentionally kept as a
    separate keyword on :func:`query` rather than included here.
    ``system`` is a prompt-level concern — it shapes *what* the model
    is told, like ``prompt`` itself — whereas ``QueryOptions`` captures
    *runtime configuration* (tool access, working directory, turn limits).
    """

    allowed_tools: list[str] | None = None
    tools: list[str] | None = None
    """Base tool set for the session.  Distinct from ``allowed_tools`` —
    ``tools`` declares the universe of tools the session may use, while
    ``allowed_tools`` filters that universe.  Setting ``tools=[]``
    materially differs from ``allowed_tools=[]``: the former emits
    ``--tools ""`` to the CLI (empty base set ⇒ no tools), while the
    latter is silently dropped by ``claude-agent-sdk`` 's truthiness
    check at ``subprocess_cli.py:196``.  Use ``tools=[]`` for
    tool-less sessions; use ``allowed_tools=[...]`` only when narrowing
    a non-empty set.
    """
    cwd: str | None = None
    max_turns: int | None = None
    model: str | None = None
    effort: str | None = None  # "low" | "medium" | "high"
    output_format: dict[str, Any] | None = None
    mcp_servers: dict[str, Any] | None = None
    disallowed_tools: list[str] | None = None
    hooks: dict[str, Any] | None = None
    """SDK lifecycle hooks (maps to ``ClaudeAgentOptions.hooks``).

    A ``{event: [HookMatcher, ...]}`` mapping.  Hooks fire regardless of
    ``permission_mode`` (unlike a ``can_use_tool`` permission callback, which
    ``bypassPermissions`` short-circuits past), so the task executor uses a
    ``PreToolUse`` hook here to deny PR-mutating / protected-ref Bash commands
    at the action level.  ``None`` (the default) installs no hooks.  Additive —
    it never relaxes the ``disallowed_tools=["Agent"]`` default.
    """
    fallback_model: str | None = None
    task_budget_tokens: int | None = None
    betas: list[str] | None = None
    """SDK beta features to enable for this call.

    Maps to ``ClaudeAgentOptions.betas`` (passed as ``--betas`` to the
    CLI).  The only beta Maestro currently uses is
    ``"context-1m-2025-08-07"``, injected automatically by the
    long-context call sites in ``evolution.py`` and ``executor.py`` when
    they switch to the long-context model.  ``None`` (the default) leaves
    the beta list empty.
    """
    caller: str | None = None
    """Logical name of the call site (e.g. ``"contemplate"``, ``"propose_plan"``).

    Used purely for telemetry — does **not** flow into the underlying
    ``ClaudeAgentOptions``.  Diagnostics aggregate per-call SDK usage
    by this label so we can answer "which call site burns the budget?".
    """
    bypass_warning_throttle: bool = False
    """Skip the per-call ``_warning_throttle_multiplier`` sleep for this call.

    Probes opt in because they *are* the signal that detects when the
    throttle should ease — gating them on the same throttle they exist to
    measure would compound the wait (e.g. with the multiplier pinned at
    ``_WARNING_THROTTLE_MAX_MULTIPLIER`` the warning-hold probe would pay
    an extra 160s on top of its exponential probe-interval sleep). The
    hold-gate / idle-probe loops already enforce the protective wait at
    the probe-interval level via ``_safe_shutdown_wait``.

    Like ``caller``, this is telemetry-adjacent and does **not** flow
    into the underlying ``ClaudeAgentOptions``.  Bypassing the sleep
    does not bypass ``_update_warning_throttle_state()`` — a bypassed
    probe still observes warnings and contributes to the decay counter,
    which is what allows the hold gate to clear.
    """


# Sentinel distinguishing "no structured output was produced" from a produced
# value that is legitimately ``None`` (a validated JSON ``null`` against a
# top-level null-permitting schema). Mirrors the ``_UNSET`` pattern in
# :mod:`maestro.memory`. ``None`` cannot serve as this sentinel because it is
# also a valid produced value; conflating the two made ``parse_json`` discard a
# validated ``null`` and re-parse text, and made the unproductive-completion
# guard misread a valid-but-textless turn as a stall (#2533).
_STRUCTURED_UNSET: Any = object()


@dataclass
class QueryResult:
    """Simplified result from a query call."""

    response: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: QueryUsage = field(default_factory=QueryUsage)
    rate_limit_events: list[str] = field(default_factory=list)
    # ``_STRUCTURED_UNSET`` means "no structured output was produced"; any other
    # value (including a legitimate ``None``) is the validated structured value.
    # The packing sites (:func:`_finalize_usage`, the provider packers) forward
    # the produced value only when a schema was requested, else leave this unset.
    structured_output: Any = _STRUCTURED_UNSET
    # SDK session identifier captured from the final ``ResultMessage``. Stable
    # across turns within a session; the loop persists this to
    # ``lifecycle.json`` so a retry/restart can resume transcript continuity
    # via ``ClaudeAgentOptions(resume=<id>)``. ``None`` when the SDK did not
    # report a session_id (e.g. stream ended without a ResultMessage).
    session_id: str | None = None
    # True when the call's prompt, system or any earlier turn of its session
    # carried an untrusted fence (proposal 150 step 4). Stamped only by the SDK
    # seam (:func:`query` / :func:`query_on_session`), never by a constructor.
    untrusted_input: bool = field(default=False, init=False)

    def parse_json(self) -> Any:
        """Extract and parse JSON from the response.

        Handles responses where the model wraps JSON in markdown code blocks
        (e.g. ```json ... ```) or includes surrounding text.

        Raises ``json.JSONDecodeError`` if no valid JSON is found.
        """
        # A produced value — including a validated ``null`` (``None``) — is
        # returned as-is; only the ``_STRUCTURED_UNSET`` sentinel falls through
        # to text parsing (#2533).
        if self.structured_output is not _STRUCTURED_UNSET:
            return self.structured_output

        text = self.response.strip()

        # Try parsing the raw response first.
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting from a fenced code block.
        m = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass  # Fall through to bracket extraction

        # Last resort: find the outermost { ... } or [ ... ].
        # Try both bracket types and return the one with the largest valid span
        # so that e.g. '{"key": [1,2]}' returns the object, not the inner array.
        # When the widest span (first open → last close) fails — e.g. because
        # there are multiple separate JSON objects — fall back to the last
        # open → last close span which typically captures the final answer.
        best: tuple[int, Any] | None = None  # (span_size, parsed)
        for open_ch, close_ch in (("[", "]"), ("{", "}")):
            end = text.rfind(close_ch)
            if end == -1:
                continue
            # Try widest span first (first open → last close), then
            # narrowest span (last open → last close) as fallback.
            first_start = text.find(open_ch)
            last_start = text.rfind(open_ch, 0, end)
            starts = []
            if first_start != -1 and first_start < end:
                starts.append(first_start)
            if last_start != -1 and last_start < end and last_start != first_start:
                starts.append(last_start)
            for start in starts:
                try:
                    parsed = json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    continue
                span = end - start
                if best is None or span > best[0]:
                    best = (span, parsed)
        if best is not None:
            return best[1]

        raise json.JSONDecodeError("No JSON found in response", text, 0)


def structured_output_fallback(exc: SDKError) -> QueryResult:
    """Return an empty result for a schema-exhausted call, else re-raise *exc*.

    A schema caller whose existing "unparseable reply" branch is safe to run
    on an empty response wraps its call in ``except SDKError`` and parses this
    instead, so ``STRUCTURED_OUTPUT_INVALID`` reaches that branch rather than
    crashing the loop (#4535). Every other category propagates unchanged.
    """
    if exc.category is not ErrorCategory.STRUCTURED_OUTPUT_INVALID:
        raise exc
    logger.warning(
        "%s: structured output rejected by schema validator — %s",
        exc.caller, exc,
    )
    return QueryResult(response="")


# ---------------------------------------------------------------------------
# Early-exit predicates
#
# Shared shapes for ``complete_on`` passed to :func:`query` and
# :func:`query_on_session`. The consumer evaluates these after each
# :class:`AssistantMessage` and short-circuits the stream when the predicate
# returns True, so the loop is never stuck waiting for a ``ResultMessage``
# that the server has silently stopped sending.
#
# DO NOT use these for tool-using callers (``implement_plan``, ``auto_fix``,
# executor steps). Interrupting a session mid-tool drops the tool output and
# corrupts the evolution — such callers must consume to completion.
# ---------------------------------------------------------------------------


def json_complete(_message: Any, accumulated_text: str) -> bool:
    """Fire once *accumulated_text* contains a parseable JSON object or array.

    Mirrors the extraction strategies of :meth:`QueryResult.parse_json`:
    raw parse first, fenced ``code block`` second, outermost ``{...}`` or
    ``[...]`` last. This means callers that feed the response through
    ``parse_json()`` downstream can short-circuit as soon as a parseable
    structure has been emitted, regardless of surrounding prose.
    """
    del _message  # only the cumulative buffer matters
    text = accumulated_text.strip()
    if not text:
        return False
    try:
        json.loads(text)
        return True
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", text, re.DOTALL)
    if m:
        try:
            json.loads(m.group(1))
            return True
        except json.JSONDecodeError:
            pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        end = text.rfind(close_ch)
        if end == -1:
            continue
        # Try widest span first (first open → last close), then narrowest
        # span (last open → last close) as fallback — mirroring parse_json,
        # so a multi-object buffer (e.g. 'Draft: {...}\n\nFinal: {...}')
        # short-circuits on the final object instead of waiting out the
        # stream-idle timeout.
        first_start = text.find(open_ch)
        last_start = text.rfind(open_ch, 0, end)
        starts = []
        if first_start != -1 and first_start < end:
            starts.append(first_start)
        if last_start != -1 and last_start < end and last_start != first_start:
            starts.append(last_start)
        for start in starts:
            try:
                json.loads(text[start:end + 1])
                return True
            except json.JSONDecodeError:
                continue
    return False


@functools.cache
def chars_complete(min_chars: int) -> Callable[[Any, str], bool]:
    """Factory: predicate that fires once the text buffer reaches *min_chars*.

    Cached so repeated calls with the same threshold return the same
    callable — this lets wiring tests assert identity
    (``call_args.kwargs["complete_on"] is chars_complete(200)``) without
    exporting a named singleton per caller.
    """
    if min_chars < 0:
        raise ValueError(f"min_chars must be >= 0, got {min_chars}")

    def _predicate(_message: Any, accumulated_text: str) -> bool:
        return len(accumulated_text) >= min_chars

    _predicate.__name__ = f"chars_complete_{min_chars}"
    _predicate.__qualname__ = _predicate.__name__
    return _predicate


def _process_content_blocks(
    blocks: list[Any],
    tool_calls: list[ToolCall],
    tool_call_by_id: dict[str, ToolCall],
    session_log: SessionLogger | None = None,
) -> None:
    """Process content blocks from assistant or user messages."""
    # Promote the first thinking/text block per batch to INFO so long
    # think-only spans (esp. propose_plan's high-effort adaptive thinking) don't look hung
    # in maestro.log; subsequent blocks in the same batch stay at DEBUG.
    saw_thinking = False
    saw_text = False
    for block in blocks:
        if isinstance(block, ThinkingBlock):
            if not saw_thinking:
                logger.info("SDK thinking: %s", _truncate(block.thinking, 120))
                saw_thinking = True
            else:
                logger.debug("SDK thinking: %s", _truncate(block.thinking, 200))
            if session_log:
                session_log.log(f"Thinking: {_truncate(block.thinking, 500)}")
                session_log.add_turn("thinking", "assistant", block.thinking)
        elif isinstance(block, TextBlock):
            if not saw_text:
                logger.info("SDK text: %s", _truncate(block.text, 120))
                saw_text = True
            else:
                logger.debug("SDK text: %s", _truncate(block.text, 300))
            if session_log:
                session_log.log(f"Text: {_truncate(block.text, 500)}")
                session_log.add_turn("text", "assistant", block.text)
        elif isinstance(block, ToolUseBlock):
            logger.info("SDK tool call: %s", block.name)
            logger.debug(
                "SDK tool input [%s]: %s",
                block.name,
                _truncate(str(block.input), 500),
            )
            if session_log:
                session_log.log(
                    f"Tool call: {block.name} "
                    f"input={_truncate(str(block.input), 500)}"
                )
                session_log.add_turn(
                    "tool_call", "assistant",
                    f"{block.name} input={block.input}",
                )
            tc = ToolCall(name=block.name, input=block.input)
            tool_call_by_id[block.id] = tc
            tool_calls.append(tc)
        elif isinstance(block, ToolResultBlock):
            content_str = str(block.content) if block.content else ""
            is_error = bool(block.is_error)
            matched = tool_call_by_id.get(block.tool_use_id)
            tool_name = matched.name if matched else block.tool_use_id
            logger.info(
                "SDK tool result [%s]: %s%s",
                tool_name,
                "ERROR " if is_error else "",
                _truncate(content_str, 300),
            )
            if session_log:
                session_log.log(
                    f"Tool result [{tool_name}]: "
                    f"{'ERROR ' if is_error else ''}"
                    f"{_truncate(content_str, 500)}"
                )
                session_log.add_turn(
                    "tool_result", "tool",
                    f"{'ERROR ' if is_error else ''}{content_str}",
                )
            if matched:
                matched.result = content_str
                matched.is_error = is_error


@dataclass
class _ConsumeResult:
    """Internal result from consuming a message stream."""

    last: ResultMessage | None
    tool_calls: list[ToolCall]
    usage: QueryUsage
    rate_limits: list[str]
    rejected_resets_at: int | None
    # Peak ``utilization`` and latest ``resets_at`` seen on this call's
    # ``allowed_warning`` events (#4514); ``None`` when none reported them.
    warning_utilization: float | None = None
    warning_resets_at: int | None = None
    # Set when an in-stream ``complete_on`` predicate fired and the loop
    # broke before a ``ResultMessage`` arrived. ``text_buffer`` carries the
    # accumulated assistant text (used by :func:`_finalize_usage` to build a
    # partial :class:`QueryResult`). ``last_session_id`` is captured from
    # the most recent ``AssistantMessage`` so the caller can persist it.
    early_exit: bool = False
    text_buffer: str = ""
    last_session_id: str | None = None
    # Whether the caller requested structured output (``QueryOptions.output_format``
    # set). Carried so :func:`_finalize_usage` forwards the SDK's
    # ``structured_output`` (which may be a validated ``None``) only when a schema
    # was asked for — an ordinary no-schema call must leave the field unset rather
    # than acquire a phantom ``None`` the sentinel would misread as "produced
    # null" (#2533).
    structured_output_requested: bool = False
    # Set when a ``ConversationResetMessage`` arrived: the CLI zeroed its
    # running ``total_cost_usd``, so the session caller must drop its cost
    # baseline before forming this turn's delta (#4306).
    conversation_reset: bool = False


def _extract_assistant_text(blocks: list[Any]) -> str:
    """Concatenate the text from all ``TextBlock`` items in *blocks*."""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            parts.append(block.text)
    return "".join(parts)


def message_has_tool_use(message: Any) -> bool:
    """True when *message* is an assistant message carrying a ``ToolUseBlock``.

    Vendor-neutral (like :func:`_extract_assistant_text`): a non-
    :class:`AssistantMessage`, or one with no content, yields ``False``. Shared
    by :func:`_consume_messages`' stream fingerprint and the priced-reflection
    early-exit predicates — the latter must return ``False`` while a tool call is
    still pending so the stream is not broken mid-tool-loop (proposal 132).
    """
    if not isinstance(message, AssistantMessage):
        return False
    return any(isinstance(b, ToolUseBlock) for b in (message.content or []))


def json_complete_tool_loop_safe(message: Any, accumulated_text: str) -> bool:
    """Tool-loop-safe :func:`json_complete` for sessions that carry tools.

    A JSON caller whose session carries bounded read-only tools — slot A's
    ``update_self_model`` under grounded reflection (proposal 132), the
    ``propose_plan`` tree session under grounded planning (proposal 143) —
    must never early-exit the stream while a tool call is pending: breaking
    mid-tool-loop drops the tool result and corrupts the turn. So a message
    carrying a ``ToolUseBlock`` returns ``False`` outright; otherwise it
    delegates to :func:`json_complete`, which is left unchanged so the
    tool-free callers stay byte-identical.
    """
    if message_has_tool_use(message):
        return False
    return json_complete(message, accumulated_text)


async def _consume_messages(
    messages: AsyncIterator[Any],
    caller: str | None,
    session_log: SessionLogger | None,
    usage: QueryUsage | None = None,
    partial_rejected_resets_at: list[int | None] | None = None,
    *,
    complete_on: Callable[[Any, str], bool] | None = None,
    stream_idle_timeout_seconds: float | None = None,
    break_on_result: bool = False,
    betas: list[str] | None = None,
    structured_output_requested: bool = False,
) -> _ConsumeResult:
    """Consume an SDK message stream and return processed results.

    Shared by :func:`query` (streaming from ``_query()``) and
    :func:`query_on_session` (streaming from ``ClaudeSDKClient.receive_messages()``).

    *partial_rejected_resets_at* is an optional single-element mutable list
    that receives the ``rejected`` ``resets_at`` value as soon as it is seen —
    even if the stream subsequently raises.  This lets the caller's ``except``
    block read the value for rate-limit retry logic.

    *complete_on* is an optional predicate evaluated after each
    :class:`AssistantMessage`. It receives the message and the accumulated
    text buffer so far. When it returns ``True`` the loop breaks cleanly
    (no exception raised) and the result carries ``early_exit=True``.

    *stream_idle_timeout_seconds* (``None`` = use module default; pass
    ``0`` or a negative value to disable) caps the wait between consecutive
    stream messages. On timeout the function raises
    ``SDKError(ErrorCategory.STREAM_STALL)`` so the caller's retry path can
    disconnect the wedged session and try again.
    """
    tool_calls: list[ToolCall] = []
    tool_call_by_id: dict[str, ToolCall] = {}
    if usage is None:
        usage = QueryUsage(caller=caller)
    rate_limits: list[str] = []
    rejected_resets_at: int | None = None
    warning_utilization: float | None = None
    warning_resets_at: int | None = None
    last: ResultMessage | None = None
    # Per-kind stream throttle for partial-message delta INFO emission so a
    # long think-only span produces periodic liveness logs without flooding.
    # Each kind (thinking/text) has its own window so the first delta of a
    # new kind always emits — the thinking→text transition is the most
    # operationally interesting moment and must not be suppressed by a
    # recent thinking delta.
    last_stream_emit_at: dict[str, float] = {"thinking": 0.0, "text": 0.0}
    text_buffer = ""
    last_session_id: str | None = None
    early_exit = False
    conversation_reset = False
    # Fingerprint state: the last message kind seen and whether any
    # tool calls are still awaiting their results. Surfaced in the
    # STREAM_STALL log line and ``SDKError`` message so post-mortem
    # can tell an API-side stop-reason hang (``assistant_text`` +
    # ``pending_tools=0``) from a long tool call (``assistant_tool_use``
    # + ``pending_tools >= 1``) without re-running the incident.
    last_message_kind = "none"

    def _snapshot() -> _ConsumeResult:
        # The consume state as of now: the normal return value, and the
        # ``unbooked`` payload a billed raise carries to its caller (#4572).
        assert usage is not None  # narrowed above; closure loses narrowing
        return _ConsumeResult(
            last=last,
            tool_calls=tool_calls,
            usage=usage,
            rate_limits=rate_limits,
            rejected_resets_at=rejected_resets_at,
            warning_utilization=warning_utilization,
            warning_resets_at=warning_resets_at,
            early_exit=early_exit,
            text_buffer=text_buffer,
            last_session_id=last_session_id,
            structured_output_requested=structured_output_requested,
            conversation_reset=conversation_reset,
        )

    effective_idle = (
        stream_idle_timeout_seconds
        if stream_idle_timeout_seconds is not None
        else _sdk_stream_idle_timeout_seconds
    )
    # Treat 0 / negative as "disabled" so callers can opt out per-call.
    use_idle_timeout = effective_idle is not None and effective_idle > 0

    iterator = messages.__aiter__()
    while True:
        try:
            if use_idle_timeout:
                # A silent stream emits a single mid-budget heartbeat
                # WARNING before the terminal STREAM_STALL, so visibility
                # lands sooner when the stream is stuck.
                #
                # The heartbeat is purely *observational*: it runs as a
                # side timer while one ``__anext__()`` await spans the
                # whole budget. Splitting the await into two
                # ``asyncio.wait_for`` calls instead threw
                # ``CancelledError`` into the SDK's single-use async
                # generator at the mid-budget mark, finalizing it, so the
                # second ``__anext__()`` answered ``StopAsyncIteration``
                # and any tool call quiet for longer than *half* the
                # budget became a bogus "stream ended without
                # ResultMessage" STREAM_STALL at half the configured
                # budget (#3877). The awaited ``__anext__()`` also stays
                # in *this* task — ``asyncio.wait_for`` awaits a bare
                # coroutine inline — so the generator is never driven
                # from a wrapper task, which would break BaseException
                # propagation and the caller-task finalization the
                # anyio cancel scopes inside the SDK transport rely on.
                half = effective_idle / 2 if effective_idle else 0

                # ``half``/``last_message_kind`` are bound as defaults: the
                # timer measures the message currently being awaited, and
                # neither value can change while that await is pending.
                async def _emit_heartbeat(
                    half: float = half,
                    last_message_kind: str = last_message_kind,
                ) -> None:
                    await asyncio.sleep(half)
                    pending_tools = sum(
                        1 for tc in tool_call_by_id.values()
                        if tc.result is None
                    )
                    logger.warning(
                        "SDK stream quiet for %.1fs (half of %.1fs "
                        "budget, caller=%s, last_message=%s, "
                        "pending_tools=%d) — awaiting remainder",
                        half, effective_idle, caller or "unknown",
                        last_message_kind, pending_tools,
                    )
                    if session_log:
                        session_log.log(
                            f"Heartbeat: idle {half:.1f}s "
                            f"last_message={last_message_kind} "
                            f"pending_tools={pending_tools}"
                        )

                heartbeat = asyncio.ensure_future(_emit_heartbeat())
                try:
                    message = await asyncio.wait_for(
                        iterator.__anext__(),
                        timeout=effective_idle,
                    )
                finally:
                    # Every exit from the acquisition step — a message, a
                    # StopAsyncIteration, the terminal timeout, or an
                    # external cancellation — retires the timer, so no
                    # heartbeat outlives the message it was measuring.
                    heartbeat.cancel()
                    # A timer that already finished may carry an exception
                    # (a failing ``session_log.log``, say). Retrieving it
                    # here keeps the failure loud — an unretrieved task
                    # exception would otherwise surface only as an asyncio
                    # "exception was never retrieved" warning at GC time,
                    # which is the silenced-failure shape the fail-loud
                    # invariant forbids.
                    if heartbeat.done() and not heartbeat.cancelled():
                        heartbeat.result()
            else:
                message = await iterator.__anext__()
        except StopAsyncIteration:
            break
        except TimeoutError as timeout_exc:
            # Only classify as STREAM_STALL when *we* installed the
            # asyncio.wait_for guard.  If idle timeout is disabled, any
            # TimeoutError originated inside the SDK transport and must
            # propagate with its original semantics — not be misclassified.
            if not use_idle_timeout:
                raise
            # ``asyncio.wait_for`` raises :class:`TimeoutError`
            # (alias of ``asyncio.TimeoutError`` since 3.11) when the
            # idle threshold elapses without a new message. Wrap it in an
            # SDKError so the caller's retry path treats it like
            # STALE_SESSION (no cooldown, clear session ids, recreate).
            elapsed = effective_idle
            pending_tools = sum(
                1 for tc in tool_call_by_id.values()
                if tc.result is None
            )
            fingerprint = (
                f"last_message={last_message_kind} "
                f"pending_tools={pending_tools}"
            )
            logger.warning(
                "SDK stream idle for %.1fs (caller=%s, %s) — "
                "raising STREAM_STALL",
                elapsed if elapsed is not None else -1.0,
                caller or "unknown",
                fingerprint,
            )
            if session_log:
                session_log.log(
                    f"STREAM STALL: idle for {elapsed}s "
                    f"caller={caller or 'unknown'} {fingerprint}"
                )
            stall_message = (
                f"SDK stream idle for {elapsed:.1f}s without a new "
                f"message ({fingerprint})"
                if elapsed is not None
                else f"SDK stream idle timeout ({fingerprint})"
            )
            raise SDKError(
                stall_message,
                category=ErrorCategory.STREAM_STALL,
                caller=caller,
                session_reusable=False,
            ) from timeout_exc

        if isinstance(message, AssistantMessage):
            _process_content_blocks(
                message.content, tool_calls, tool_call_by_id, session_log,
            )
            has_tool_use = message_has_tool_use(message)
            last_message_kind = (
                "assistant_tool_use" if has_tool_use else "assistant_text"
            )
            sid = message.session_id
            if isinstance(sid, str) and sid:
                last_session_id = sid
            # Capture cumulative token counts and the served model from
            # every AssistantMessage so the early-exit predicate path
            # (pure-text callers like introspect / update_self_model that
            # never see TaskProgressMessage or ResultMessage) still has
            # real numbers for ``_estimate_cost_usd`` to price against.
            raw_msg_usage = message.usage
            if isinstance(raw_msg_usage, dict) and raw_msg_usage:
                if "input_tokens" in raw_msg_usage:
                    usage.input_tokens = _coerce_int(
                        raw_msg_usage["input_tokens"],
                    )
                if "output_tokens" in raw_msg_usage:
                    usage.output_tokens = _coerce_int(
                        raw_msg_usage["output_tokens"],
                    )
                if "cache_creation_input_tokens" in raw_msg_usage:
                    usage.cache_creation_tokens = _coerce_int(
                        raw_msg_usage["cache_creation_input_tokens"],
                    )
                if "cache_read_input_tokens" in raw_msg_usage:
                    usage.cache_read_tokens = _coerce_int(
                        raw_msg_usage["cache_read_input_tokens"],
                    )
            msg_model = message.model
            if isinstance(msg_model, str) and msg_model:
                # When the long-context beta is active, append the
                # ``[1m]`` pricing suffix so ``_estimate_cost_usd`` hits
                # the 1M-tier row. Anthropic returns the real model name
                # (no suffix) on AssistantMessage.model.
                usage.model = _label_long_context_pricing(msg_model, betas)
            if complete_on is not None:
                text_buffer += _extract_assistant_text(message.content)
                if complete_on(message, text_buffer):
                    logger.info(
                        "SDK stream early-exit: predicate satisfied "
                        "(caller=%s, buffer=%d chars)",
                        caller or "unknown", len(text_buffer),
                    )
                    if session_log:
                        session_log.log(
                            f"Early exit: predicate satisfied, "
                            f"buffer={len(text_buffer)} chars"
                        )
                    early_exit = True
                    break
        elif isinstance(message, UserMessage):
            if isinstance(message.content, list):
                _process_content_blocks(
                    message.content, tool_calls, tool_call_by_id,
                    session_log,
                )
                has_tool_result = any(
                    isinstance(b, ToolResultBlock) for b in message.content
                )
                last_message_kind = (
                    "tool_result" if has_tool_result else "user"
                )
        elif isinstance(message, TaskStartedMessage):
            last_message_kind = "task_started"
            logger.debug("SDK task started: %s", message.description)
            if session_log:
                session_log.log(f"Task started: {message.description}")
        elif isinstance(message, TaskProgressMessage):
            last_message_kind = "task_progress"
            raw_usage = message.usage or {}
            msg_usage: dict[str, Any] = (
                dict(raw_usage) if isinstance(raw_usage, dict) else {}
            )
            total_tokens = _coerce_int(msg_usage.get("total_tokens"))
            tool_uses = _coerce_int(msg_usage.get("tool_uses"))
            duration_ms = _coerce_int(msg_usage.get("duration_ms"))
            # Progress usage values are cumulative task-level snapshots: the
            # same payload's ``total_tokens``/``duration_ms`` are running
            # totals (assigned below), and ``_finalize_usage``'s early-exit
            # path treats these numbers as snapshots. So plain assignment —
            # last snapshot wins — is the correct capture; ``+=`` would
            # double-count across successive progress messages.
            if "input_tokens" in msg_usage:
                usage.input_tokens = _coerce_int(msg_usage["input_tokens"])
            if "output_tokens" in msg_usage:
                usage.output_tokens = _coerce_int(msg_usage["output_tokens"])
            if "cache_creation_input_tokens" in msg_usage:
                usage.cache_creation_tokens = _coerce_int(
                    msg_usage["cache_creation_input_tokens"],
                )
            if "cache_read_input_tokens" in msg_usage:
                usage.cache_read_tokens = _coerce_int(
                    msg_usage["cache_read_input_tokens"],
                )
            logger.info(
                "SDK progress: %d tokens, %d tool uses, %dms%s",
                total_tokens,
                tool_uses,
                duration_ms,
                f", last tool: {message.last_tool_name}"
                if message.last_tool_name
                else "",
            )
            if session_log:
                session_log.log(
                    f"Progress: {total_tokens} tokens, "
                    f"{tool_uses} tool uses, "
                    f"{duration_ms}ms"
                )
            usage.total_tokens = total_tokens
            usage.tool_uses = tool_uses
            usage.duration_ms = duration_ms
        elif isinstance(message, TaskNotificationMessage):
            last_message_kind = "task_notification"
            logger.info("SDK task %s: %s", message.status, message.summary)
            if session_log:
                session_log.log(
                    f"Task {message.status}: {message.summary}"
                )
        elif isinstance(message, SystemMessage) and message.subtype == "init":
            # The CLI's advertised tool surface (proposal 142). The Task*
            # subclasses matched above; ``last_message_kind`` is deliberately
            # untouched so stall/ghost diagnostics are unaffected.
            advertised = message.data.get("tools")
            if session_log and isinstance(advertised, list):
                session_log.set_tools_advertised(
                    [tool for tool in advertised if isinstance(tool, str)],
                )
            servers = message.data.get("mcp_servers")
            if session_log and isinstance(servers, list):
                session_log.set_mcp_servers_advertised([
                    entry["name"] for entry in servers
                    if isinstance(entry, dict)
                    and isinstance(entry.get("name"), str)
                ])
        elif isinstance(message, RateLimitEvent):
            last_message_kind = "rate_limit"
            info = message.rate_limit_info
            detail = _format_rate_limit_detail(info)
            if info.status == "allowed":
                logger.debug("SDK rate limit: %s%s", info.status, detail)
            else:
                logger.warning("SDK rate limit: %s%s", info.status, detail)
            rate_limits.append(info.status)
            if info.status == "rejected":
                rejected_resets_at = info.resets_at
                if partial_rejected_resets_at is not None:
                    partial_rejected_resets_at[0] = info.resets_at
            elif info.status == "allowed_warning":
                # Keep the peak window's ``resets_at`` paired with its
                # utilization so the hold never waits on another window.
                if info.utilization is not None and (
                    warning_utilization is None
                    or info.utilization >= warning_utilization
                ):
                    warning_utilization = info.utilization
                    if info.resets_at is not None:
                        warning_resets_at = info.resets_at
                elif warning_utilization is None and info.resets_at is not None:
                    warning_resets_at = info.resets_at
            if session_log:
                session_log.log(f"RATE LIMIT: {info.status}{detail}")
        elif isinstance(message, StreamEvent):
            last_message_kind = "stream_event"
            event_data = message.event
            text: str | None = None
            kind: str | None = None
            if (
                isinstance(event_data, dict)
                and event_data.get("type") == "content_block_delta"
            ):
                delta = event_data.get("delta", {})
                if isinstance(delta, dict):
                    delta_type = delta.get("type")
                    if delta_type == "thinking_delta":
                        value = delta.get("thinking", "")
                        if isinstance(value, str) and value:
                            text = value
                            kind = "thinking"
                    elif delta_type == "text_delta":
                        value = delta.get("text", "")
                        if isinstance(value, str) and value:
                            text = value
                            kind = "text"
            if text is not None and kind is not None:
                if session_log:
                    session_log.log(
                        f"{kind.capitalize()} delta: {_truncate(text, 500)}",
                    )
                now = time.monotonic()
                if (
                    now - last_stream_emit_at[kind]
                    >= _STREAM_HEARTBEAT_INTERVAL_SECONDS
                ):
                    logger.info("SDK %s: %s", kind, _truncate(text, 120))
                    last_stream_emit_at[kind] = now
        elif isinstance(message, ConversationResetMessage):
            conversation_reset = True
        elif isinstance(message, ResultMessage):
            last_message_kind = "result"
            last = message
            # A ``ResultMessage`` with zero turns and zero tokens is an
            # empty completion — the CLI's upstream connection dropped
            # mid-stream and the subprocess ended cleanly without ever
            # producing a real reply. Treat it like the idle-timer stall
            # so the iteration-level resume-in-place path clears session
            # ids and retries on a fresh session. The
            # ``rejected_resets_at`` gate preserves the stateless
            # rate-limit retry path: a zero-token completion following
            # a ``RateLimitEvent("rejected")`` must stay classified as
            # RATE_LIMIT so the outer sleep-until-reset code runs; the
            # session path raises RATE_LIMIT from the post-loop branch.
            usage_dict: dict[str, Any] = (
                message.usage if isinstance(message.usage, dict) else {}
            )
            input_tokens_reported = _coerce_int(
                usage_dict.get("input_tokens"),
            )
            output_tokens_reported = _coerce_int(
                usage_dict.get("output_tokens"),
            )
            # The ``rejected_resets_at is None`` guard is intentional: a
            # RateLimitEvent("rejected") without a reset timestamp should
            # fall through to STREAM_STALL rather than the RATE_LIMIT retry
            # path, because the retry path needs a reset time to sleep until.
            if (
                rejected_resets_at is None
                and message.num_turns == 0
                and input_tokens_reported == 0
                and output_tokens_reported == 0
            ):
                pending_tools = sum(
                    1 for tc in tool_call_by_id.values()
                    if tc.result is None
                )
                fingerprint = _emit_empty_response_diagnostic(
                    caller=caller,
                    last_message_kind=last_message_kind,
                    pending_tools=pending_tools,
                    session_log=session_log,
                    reason="num_turns=0 tokens=0",
                    category=ErrorCategory.STREAM_STALL,
                )
                raise SDKError(
                    f"SDK returned empty completion "
                    f"(num_turns=0 tokens=0, {fingerprint})",
                    category=ErrorCategory.STREAM_STALL,
                    caller=caller,
                )
            # Rate-limit ghost completion: on quota exhaustion the CLI can
            # synthesize a rate-limit notice as the assistant's text and end
            # the stream with a zero-usage ResultMessage (``num_turns >= 1``,
            # zero tokens, zero cost) — no exception, no structured
            # RateLimitEvent. Without this branch that prose flows on to
            # ``_finalize_usage`` as a successful QueryResult and crashes the
            # first JSON-bound caller on ``parse_json``. Classify it as
            # RATE_LIMIT (in ``_TRANSIENT_SDK_CATEGORIES``) so the existing
            # resume-in-place + rate-limit idle path handles it. The
            # ``num_turns == 0`` zero-token shape is already caught above, so
            # this branch fires only for ``num_turns >= 1``. The
            # ``rejected_resets_at is None`` guard preserves the stateless
            # RateLimitEvent("rejected") sleep-until-reset path (sessions
            # are handled by the post-loop rejected branch).
            if rejected_resets_at is None and _is_rate_limit_ghost_completion(
                message.result or "",
                input_tokens=input_tokens_reported,
                output_tokens=output_tokens_reported,
                total_cost_usd=message.total_cost_usd,
            ):
                pending_tools = sum(
                    1 for tc in tool_call_by_id.values()
                    if tc.result is None
                )
                fingerprint = _emit_empty_response_diagnostic(
                    caller=caller,
                    last_message_kind=last_message_kind,
                    pending_tools=pending_tools,
                    session_log=session_log,
                    reason="rate-limit ghost completion num_turns>=1 zero-cost",
                    category=ErrorCategory.RATE_LIMIT,
                )
                raise SDKError(
                    f"SDK returned rate-limit ghost completion "
                    f"(num_turns={message.num_turns} tokens=0 cost=0, "
                    f"{fingerprint})",
                    category=ErrorCategory.RATE_LIMIT,
                    caller=caller,
                )
            # Terminal error-result classification (#2870): when the CLI
            # exhausts its own internal retries against a transient upstream
            # failure — e.g. a ``529 Overloaded`` burst — it hands back a
            # *terminal* ResultMessage with ``is_error`` set and the error
            # notice in ``.result``, but reports zero tokens together with a
            # small non-zero ``total_cost_usd`` (billed for the failed retry
            # attempts). That non-zero cost defeats the zero-cost ghost guard
            # above (its ``overloaded`` keyword matches, but the cost clause
            # rejects it), so without this branch the error prose flows on to
            # ``_finalize_usage`` as a productive QueryResult and crashes the
            # first JSON-bound caller (``update_self_model`` via
            # ``contemplate``) on ``parse_json``. Inspect the SDK's own
            # terminal error state instead of guessing from cost
            # (``_error_result_is_transient``): a transient overload/5xx status
            # — or, when no HTTP status is emitted, overload/quota prose in
            # ``.result``/``.errors`` — routes into RATE_LIMIT (in
            # ``_TRANSIENT_SDK_CATEGORIES``) so the resume-in-place +
            # rate-limit idle path handles it, while a non-transient error
            # result still surfaces loudly as a categorized SDKError rather
            # than a silently mislabeled success (fail-loud). The
            # ``rejected_resets_at is None`` guard mirrors the sibling
            # branches, leaving the stateless RateLimitEvent("rejected")
            # sleep-until-reset path untouched (sessions: post-loop branch). This is the ``is_error``-based
            # sibling of the zero-cost ghost guard, not a replacement.
            if rejected_resets_at is None and message.is_error:
                status = message.api_error_status
                is_transient = _error_result_is_transient(
                    api_error_status=status,
                    result=message.result or "",
                    errors=message.errors,
                )
                # Transient precedence first; then structured-output
                # exhaustion gets its own typed, non-transient category so a
                # schema caller can tell model-output invalidity from an
                # unexplained SDK fault (#4535).
                if is_transient:
                    error_category = ErrorCategory.RATE_LIMIT
                elif message.subtype == _STRUCTURED_OUTPUT_EXHAUSTED_SUBTYPE:
                    error_category = ErrorCategory.STRUCTURED_OUTPUT_INVALID
                else:
                    error_category = ErrorCategory.UNKNOWN
                pending_tools = sum(
                    1 for tc in tool_call_by_id.values()
                    if tc.result is None
                )
                detail = _terminal_error_detail(message)
                fingerprint = _emit_empty_response_diagnostic(
                    caller=caller,
                    last_message_kind=last_message_kind,
                    pending_tools=pending_tools,
                    session_log=session_log,
                    reason=(
                        f"terminal error result is_error status={status} "
                        f"{detail}"
                    ),
                    category=error_category,
                )
                terminal_exc = SDKError(
                    f"SDK returned terminal error result "
                    f"(is_error status={status} {detail}, {fingerprint})",
                    category=error_category,
                    caller=caller,
                )
                terminal_exc.unbooked = _snapshot()
                raise terminal_exc
            # Persistent ``ClaudeSDKClient`` callers must break —
            # ``client.receive_messages()`` never terminates between turns.
            # Stateless ``query()`` callers must NOT break: the underlying
            # async generator has to drain to ``StopAsyncIteration`` inside
            # the caller's task, or GC will later run its cleanup from an
            # unrelated task and leak ``CancelledError`` into whatever await
            # is live then (PR #1299 rolled back 2026-04-21 on a Telegram
            # send inside the next iteration's startup).
            if break_on_result:
                break

    # Session-path rejected rate limit (#4046): the guards above and below step
    # aside on ``rejected_resets_at`` so the stateless ``query()`` handler can
    # sleep until reset — that hand-off rides the exception the CLI raises
    # when it exits non-zero. A persistent session breaks on the
    # ResultMessage and its CLI stays alive, so no exception follows and the
    # quota notice would return as success. Raise RATE_LIMIT here instead
    # (in ``_TRANSIENT_SDK_CATEGORIES``) so the loop's resume-in-place +
    # rate-limit idle path takes over on a fresh session. No in-session
    # sleep-and-requery: the session already accepted the prompt (#1216).
    if (
        break_on_result
        and rejected_resets_at is not None
        and _is_unproductive_after_rejection(
            last=last,
            early_exit=early_exit,
            text_buffer=text_buffer,
            usage=usage,
            tool_calls=tool_calls,
            structured_output_requested=structured_output_requested,
        )
    ):
        pending_tools = sum(
            1 for tc in tool_call_by_id.values() if tc.result is None
        )
        fingerprint = _emit_empty_response_diagnostic(
            caller=caller,
            last_message_kind=last_message_kind,
            pending_tools=pending_tools,
            session_log=session_log,
            reason="rejected rate limit then unproductive result (session)",
            category=ErrorCategory.RATE_LIMIT,
        )
        rejected_exc = SDKError(
            f"SDK rate limit rejected on session "
            f"(resets_at={rejected_resets_at}, {fingerprint})",
            category=ErrorCategory.RATE_LIMIT,
            caller=caller,
        )
        # ``last`` may be None (early-exit) — only a received result is billed.
        if last is not None:
            rejected_exc.unbooked = _snapshot()
        raise rejected_exc

    # Early-exit rate-limit ghost completion: when a ``complete_on`` predicate
    # fires, the loop breaks with the synthesized quota notice sitting in
    # ``text_buffer`` and no ResultMessage processed, so the in-loop ghost guard
    # above never runs. Re-check the buffer here against ``usage`` — which is
    # still the *raw*, pre-estimate snapshot (``_estimate_cost_usd`` only runs
    # later in ``_finalize_usage``), so the zero-cost guard cannot be defeated by
    # a cache-token cost backfill. Detecting upstream of finalization is why this
    # is the single home for the classification. The ``rejected_resets_at is
    # None`` guard preserves the stateless sleep-until-reset path, mirroring the
    # in-loop guard; the session path was handled by the rejected branch above.
    if (
        early_exit
        and rejected_resets_at is None
        and _is_rate_limit_ghost_completion(
            text_buffer,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_cost_usd=usage.total_cost_usd,
        )
    ):
        pending_tools = sum(
            1 for tc in tool_call_by_id.values() if tc.result is None
        )
        fingerprint = _emit_empty_response_diagnostic(
            caller=caller,
            last_message_kind=last_message_kind,
            pending_tools=pending_tools,
            session_log=session_log,
            reason="rate-limit ghost completion early-exit zero-cost",
            category=ErrorCategory.RATE_LIMIT,
        )
        raise SDKError(
            f"SDK returned rate-limit ghost completion "
            f"(early-exit tokens=0 cost=0, {fingerprint})",
            category=ErrorCategory.RATE_LIMIT,
            caller=caller,
        )

    # No-ResultMessage check: catches the case where a persistent-session
    # stream closes cleanly (StopAsyncIteration) without ever yielding a
    # ResultMessage — typically after a long thinking-only span where the
    # upstream connection drops. The aggregate check below skips this shape
    # via ``last is not None``, so a JSON-shaped caller would otherwise
    # receive an empty QueryResult and crash on parse. Only fires for
    # persistent-session callers (``break_on_result=True``); stateless
    # ``query()`` callers must drain to ``StopAsyncIteration`` cleanly so
    # GC does not later run their cleanup from an unrelated task.
    if (
        last is None
        and not early_exit
        and rejected_resets_at is None
        and break_on_result
    ):
        pending_tools = sum(
            1 for tc in tool_call_by_id.values() if tc.result is None
        )
        fingerprint = _emit_empty_response_diagnostic(
            caller=caller,
            last_message_kind=last_message_kind,
            pending_tools=pending_tools,
            session_log=session_log,
            reason="no result message",
            category=ErrorCategory.STREAM_STALL,
        )
        raise SDKError(
            f"SDK stream ended without ResultMessage ({fingerprint})",
            category=ErrorCategory.STREAM_STALL,
            caller=caller,
            session_reusable=False,
        )

    # Aggregate-level empty-response check: catches the case where a
    # ResultMessage carried non-zero turns/tokens but the assistant emitted
    # no text and no successful tool calls (output went to thinking blocks,
    # or every tool call errored). Mirrors the narrow check's guards:
    # ``rejected_resets_at is None`` keeps the stateless rate-limit retry
    # path intact (sessions: the rejected branch above),
    # ``not early_exit`` honors deliberate ``complete_on`` early-exits, and
    # ``last is not None`` skips the no-ResultMessage case — the sibling
    # check above classifies that shape for persistent-session callers.
    # Together the three branches (narrow num_turns/tokens, aggregate
    # unproductive, and missing-ResultMessage) exhaust the unproductive-
    # completion space. Structured output is treated as a productive
    # channel — the model can emit a JSON response instead of text and
    # still be productive. The request flag (not ``last.structured_output``)
    # is the productivity signal: a validated ``null`` leaves
    # ``last.structured_output`` at ``None``, byte-for-byte identical to "no
    # structured output", so a completed non-error turn that *requested* a
    # schema is productive regardless of the validated value (#2533). The
    # text/tool productivity source is the same field :func:`_finalize_usage`
    # returns as ``response`` — ``last.result`` in the normal path,
    # ``text_buffer`` in early-exit (skipped here).
    if (
        last is not None
        and not early_exit
        and rejected_resets_at is None
        and not structured_output_requested
        and not _is_productive_aggregate(last.result or "", tool_calls)
    ):
        pending_tools = sum(
            1 for tc in tool_call_by_id.values() if tc.result is None
        )
        fingerprint = _emit_empty_response_diagnostic(
            caller=caller,
            last_message_kind=last_message_kind,
            pending_tools=pending_tools,
            session_log=session_log,
            reason="unproductive aggregate",
            category=ErrorCategory.STREAM_STALL,
        )
        unproductive_exc = SDKError(
            f"SDK returned unproductive completion ({fingerprint})",
            category=ErrorCategory.STREAM_STALL,
            caller=caller,
        )
        unproductive_exc.unbooked = _snapshot()
        raise unproductive_exc

    return _snapshot()


def _project_result_usage(
    cr: _ConsumeResult,
    last: ResultMessage,
    *,
    cost_baseline: float | None,
) -> None:
    """Project *last*'s cost and token fields onto ``cr.usage``.

    On a persistent session the booked cost is the delta of the running
    ``total_cost_usd`` above *cost_baseline* (#4306), clamped at zero.
    """
    cr.usage.total_cost_usd = last.total_cost_usd
    if cost_baseline is not None and last.total_cost_usd is not None:
        # Clamp at zero: a running total below the baseline is never booked
        # as negative spend.
        cr.usage.total_cost_usd = max(
            0.0, last.total_cost_usd - cost_baseline,
        )
    cr.usage.num_turns = last.num_turns
    cr.usage.session_id = getattr(last, "session_id", None)
    cr.usage.duration_ms = last.duration_ms
    if last.usage:
        last_usage_d: dict[str, Any] = dict(last.usage)
        if "total_tokens" in last_usage_d:
            cr.usage.total_tokens = _coerce_int(last_usage_d["total_tokens"])
        if "tool_uses" in last_usage_d:
            cr.usage.tool_uses = _coerce_int(last_usage_d["tool_uses"])
        if "input_tokens" in last_usage_d:
            cr.usage.input_tokens = _coerce_int(last_usage_d["input_tokens"])
        if "output_tokens" in last_usage_d:
            cr.usage.output_tokens = _coerce_int(last_usage_d["output_tokens"])
        if "cache_creation_input_tokens" in last_usage_d:
            cr.usage.cache_creation_tokens = _coerce_int(
                last_usage_d["cache_creation_input_tokens"],
            )
        if "cache_read_input_tokens" in last_usage_d:
            cr.usage.cache_read_tokens = _coerce_int(
                last_usage_d["cache_read_input_tokens"],
            )


def _book_unbooked_error_result(
    exc: SDKError, *, cost_baseline: float | None,
) -> _ConsumeResult | None:
    """Book the billed ``ResultMessage`` a categorized raise carried.

    Contract (#4572): every ``ResultMessage`` received on the Anthropic path
    is booked exactly once — by :func:`_finalize_usage` on success, or here
    on a categorized ``SDKError`` raised after it arrived. Clears
    ``exc.unbooked`` so a re-entering outer arm cannot book twice. Returns
    the booked consume state (``None`` when nothing was owed).
    """
    cr = exc.unbooked
    exc.unbooked = None
    if cr is None or cr.last is None:
        return None
    cr.usage.provider = "anthropic"
    _project_result_usage(cr, cr.last, cost_baseline=cost_baseline)
    _log_sdk_call(cr.usage)
    return cr


def _session_cost_baseline(client: Any, cr: _ConsumeResult) -> float:
    """The session client's prior running cost total, 0 after a reset (#4306).

    Read via ``__dict__`` so a MagicMock client cannot auto-vivify one.
    """
    _cost_dict = getattr(client, "__dict__", None)
    _raw_baseline = (
        _cost_dict.get("_maestro_cost_baseline")
        if isinstance(_cost_dict, dict)
        else None
    )
    return (
        float(_raw_baseline)
        if isinstance(_raw_baseline, int | float)
        and not cr.conversation_reset
        else 0.0
    )


def _advance_session_cost_baseline(
    client: Any,
    cr: _ConsumeResult,
    drained_result: ResultMessage | None = None,
) -> None:
    """Advance the session client's cost baseline past this turn (#4306)."""
    running_cost = cr.last.total_cost_usd if cr.last is not None else None
    if drained_result is not None:
        # The interrupted turn's ResultMessage surfaced in the drain; its
        # cost was already estimated, so fold it into the baseline rather
        # than re-booking it inside the next turn's delta. An undrained
        # result (drain timed out) leaves the baseline put, so that turn's
        # real cost lands in the next delta on top of its estimate.
        running_cost = drained_result.total_cost_usd
    if running_cost is not None:
        client._maestro_cost_baseline = running_cost
    elif cr.conversation_reset:
        client._maestro_cost_baseline = 0.0


def _finalize_usage(
    cr: _ConsumeResult,
    session_log: SessionLogger | None,
    *,
    cost_baseline: float | None = None,
) -> QueryResult:
    """Build a QueryResult from a _ConsumeResult, updating usage from the ResultMessage.

    *cost_baseline* is the session client's previous running
    ``total_cost_usd``. On a persistent session the CLI reports a running
    total per process, so the call's own spend is the delta above that
    baseline (#4306); ``None`` (one-shot ``query()``) books the total as-is.
    """
    # Provider attribution (proposal 067 Step 2): every row finalized here was
    # served by the Anthropic agent SDK — the ``_*_impl`` bodies the
    # AnthropicProvider (and the documented bare-``query`` branch) delegate to.
    cr.usage.provider = "anthropic"
    if cr.last is None:
        # Early-exit predicate fired before any ResultMessage arrived.
        # Build a partial result from the accumulated assistant text so
        # the caller still gets the substance of the reply. ``TaskProgress``
        # snapshots populated token counts on ``cr.usage``; estimate cost
        # from those via :func:`_estimate_cost_usd` so the burn-rate gate
        # (which reads ``cost_samples.jsonl``) doesn't undercount.
        if cr.early_exit:
            if cr.usage.total_cost_usd in (None, 0.0):
                estimated = _estimate_cost_usd(cr.usage)
                if estimated is not None:
                    cr.usage.total_cost_usd = estimated
                    cr.usage.estimated = True
            if cr.usage.num_turns == 0:
                # The predicate fired on assistant output, so at least one
                # turn ran even though no ResultMessage arrived to report it.
                # Stamp a floor of 1 so per_caller_turns / total_turns
                # (Agent._on_query_usage) don't under-count early-exit callers
                # (introspect, update_self_model). Guard on == 0 so a real
                # count is never clobbered.
                cr.usage.num_turns = 1
            # Carry the session the same way the normal path does
            # (``cr.usage.session_id`` below) so the stamped turn enters
            # ``_on_query_usage``'s per-session peak-dedupe. Without this the
            # row credits via the one-shot sum branch and a later cumulative
            # ResultMessage on the same session re-credits its full count
            # against a peak of 0, double-counting the early-exit turn (#1288).
            cr.usage.session_id = cr.last_session_id
            _log_sdk_call(cr.usage)
            _update_warning_throttle_state(
                "allowed_warning" in cr.rate_limits,
                cr.warning_utilization, cr.warning_resets_at,
            )
            return QueryResult(
                response=cr.text_buffer,
                tool_calls=cr.tool_calls,
                usage=cr.usage,
                rate_limit_events=cr.rate_limits,
                session_id=cr.last_session_id,
            )
        _log_sdk_call(cr.usage)
        _update_warning_throttle_state(
            "allowed_warning" in cr.rate_limits,
            cr.warning_utilization, cr.warning_resets_at,
        )
        return QueryResult(
            response="", tool_calls=cr.tool_calls, usage=cr.usage,
            rate_limit_events=cr.rate_limits,
            session_id=None,
        )

    _project_result_usage(cr, cr.last, cost_baseline=cost_baseline)

    cost_str = (
        f"${cr.usage.total_cost_usd:.4f}"
        if cr.usage.total_cost_usd is not None
        else "N/A"
    )
    if cost_baseline is not None and cr.last.total_cost_usd is not None:
        cost_str += f" (running ${cr.last.total_cost_usd:.4f})"

    logger.info(
        "SDK query complete: %d turns, %dms, cost=%s",
        cr.last.num_turns,
        cr.last.duration_ms,
        cost_str,
    )
    if session_log:
        session_log.log(
            f"Query complete: {cr.last.num_turns} turns, "
            f"{cr.last.duration_ms}ms, cost={cost_str}"
        )

    _log_sdk_call(cr.usage)
    _update_warning_throttle_state(
        "allowed_warning" in cr.rate_limits,
        cr.warning_utilization, cr.warning_resets_at,
    )

    return QueryResult(
        response=cr.last.result or "",
        tool_calls=cr.tool_calls,
        usage=cr.usage,
        rate_limit_events=cr.rate_limits,
        # Forward the SDK's ``structured_output`` (possibly a validated ``None``)
        # only when a schema was requested; otherwise leave it
        # ``_STRUCTURED_UNSET`` so an ordinary no-schema call is not given a
        # phantom ``None`` the sentinel would misread as "produced null" (#2533).
        structured_output=(
            cr.last.structured_output
            if cr.structured_output_requested
            else _STRUCTURED_UNSET
        ),
        session_id=getattr(cr.last, "session_id", None),
    )


def _build_sdk_options(
    options: QueryOptions | None,
    system: str | None,
) -> tuple[dict[str, Any], str | None]:
    """Build ClaudeAgentOptions kwargs and extract caller from QueryOptions.

    Returns ``(sdk_opts_dict, caller)``.  The dict can be passed to
    ``ClaudeAgentOptions(**sdk_opts_dict)``.
    """
    sdk_opts: dict[str, Any] = {}
    caller: str | None = None
    if system is not None:
        sdk_opts["system_prompt"] = system
    if options is not None:
        if options.allowed_tools is not None:
            sdk_opts["allowed_tools"] = options.allowed_tools
        if options.tools is not None:
            sdk_opts["tools"] = options.tools
        if options.cwd is not None:
            sdk_opts["cwd"] = options.cwd
        if options.max_turns is not None:
            sdk_opts["max_turns"] = options.max_turns
        effective_model = options.model if options.model is not None else _sdk_model
        if effective_model is not None:
            sdk_opts["model"] = effective_model
        if options.effort is not None:
            sdk_opts["effort"] = options.effort
        if options.output_format is not None:
            sdk_opts["output_format"] = options.output_format
        if options.mcp_servers is not None:
            sdk_opts["mcp_servers"] = options.mcp_servers
        if options.disallowed_tools is not None:
            sdk_opts["disallowed_tools"] = options.disallowed_tools

        # Lifecycle hooks — additive on top of ``disallowed_tools``.  The task
        # executor registers a ``PreToolUse`` hook here to deny PR-mutating /
        # protected-ref Bash commands at the action level; hooks fire under
        # ``bypassPermissions`` (a ``can_use_tool`` callback would not).  This
        # never relaxes the ``Agent`` block above.
        if options.hooks is not None:
            sdk_opts["hooks"] = options.hooks

        # Thinking — the declared ``effort`` decides thinking depth; adaptive
        # thinking is the on-switch. On current models omitting every thinking
        # option disables thinking, and a fixed budget is deprecated (rejected
        # outright on newer models), so an effort-bearing call sends adaptive.
        if options.effort is not None:
            sdk_opts["thinking"] = {"type": "adaptive"}

        effective_fallback = (
            options.fallback_model
            if options.fallback_model is not None
            else _sdk_fallback_model
        )
        if effective_fallback is not None:
            sdk_opts["fallback_model"] = effective_fallback

        # SDK betas — opt-in beta features passed verbatim to the CLI.
        # The only one Maestro currently uses is the 1M-context window;
        # call sites flip ``options.betas`` when they switch to the
        # long-context model.  ``None``/empty leaves the SDK default.
        # Reject an unrecognised beta here rather than passing it through: a
        # typo'd literal would attach verbatim, silently disabling the 1M
        # window while the pricing label silently falls back to the base
        # tier. Fail loud at the boundary that supplies the beta, mirroring
        # the cli_paced
        # provider's ``_beta_env`` guard against ``_SUPPORTED_BETAS``.
        if options.betas:
            unknown = [b for b in options.betas if b not in _KNOWN_BETAS]
            if unknown:
                raise ValueError(
                    f"Unknown SDK beta(s) {unknown!r}; known betas are "
                    f"{sorted(_KNOWN_BETAS)!r}"
                )
            sdk_opts["betas"] = list(options.betas)

        # Task budget — the ternary always yields an ``int``: the ``None`` arm
        # falls through to the module default ``_sdk_task_budget_tokens``, which
        # is declared ``int = 0``. So the guard below tests enablement only, and
        # needs no ``None`` check: 0 and negatives disable, positives attach.
        effective_task_budget = (
            options.task_budget_tokens
            if options.task_budget_tokens is not None
            else _sdk_task_budget_tokens
        )
        if effective_task_budget > 0:
            sdk_opts["task_budget"] = TaskBudget(total=effective_task_budget)

        caller = options.caller
    else:
        # No options supplied — still honour the configured module default so
        # the "no options" path resolves the model exactly like the
        # "options without a model" path above (``effective_model`` fallback).
        if _sdk_model is not None:
            sdk_opts["model"] = _sdk_model

    sdk_opts["permission_mode"] = "bypassPermissions"
    # The single deny-list site: ``Agent`` by default (``None`` caller list),
    # always unioned with the ungated shell built-ins (issue #4381).
    sdk_opts["disallowed_tools"] = deny_ungated_shell(
        sdk_opts.get("disallowed_tools"),
    )
    # Enable partial-message streaming so the SDK emits ``StreamEvent``
    # objects with raw Anthropic API delta events during the response
    # stream.  Without this, ``AssistantMessage`` only arrives after a
    # content block completes, which means a long think-only span (e.g.
    # propose_plan's high-effort adaptive thinking) produces no log output for tens of
    # seconds.  ``_consume_messages`` throttles the emission so log volume
    # stays bounded.
    sdk_opts.setdefault("include_partial_messages", True)
    return sdk_opts, caller


# ---------------------------------------------------------------------------
# anyio cancel-scope deadline message-format probe
# ---------------------------------------------------------------------------
#
# The cancel-containment blocks below in ``query()`` and ``query_on_session()``
# distinguish a recoverable internal anyio cancel-scope deadline from a
# must-propagate outside cancel by string-matching ``"deadline exceeded"``
# against the ``CancelledError``'s message. anyio stamps the wording when a
# scope deadline fires; a bare ``task.cancel()`` from an outside party does
# not. If a future anyio release rewords the message, both heuristic call
# sites silently misclassify every internal-deadline cancel as external —
# the precise broken path PR #521 fixed two production crashes against.
#
# This probe materialises the wording against the installed anyio at startup
# (gated to run once per process) and fails loudly if the stamped message no
# longer contains ``"deadline exceeded"``. A loud failure surfaces the
# regression on the next pip-install instead of via a flaky production crash
# a year later.
_anyio_format_verified: bool = False


async def _probe_anyio_deadline_message_format() -> None:
    """Verify the installed anyio still stamps ``"deadline exceeded"``.

    Materialises an immediate cancel-scope deadline via
    ``anyio.move_on_after(0)`` and captures the resulting ``CancelledError``
    inside the with-block (the scope swallows the cancel by design when it
    propagates out). Raises :class:`RuntimeError` with the actual message
    and the installed anyio version if the wording no longer contains
    ``"deadline exceeded"`` — the heuristic in ``query()`` /
    ``query_on_session()`` would silently misclassify internal deadlines as
    external cancels.
    """
    captured: BaseException | None = None
    with anyio.move_on_after(0):
        try:
            await anyio.sleep_forever()
        except asyncio.CancelledError as exc:
            captured = exc
            raise
    anyio_version = importlib.metadata.version("anyio")
    if captured is None:
        raise RuntimeError(
            "anyio cancel-scope deadline probe failed to capture a "
            f"CancelledError (anyio={anyio_version}); the heuristic in "
            "sdk.py cannot be verified.",
        )
    msg = str(captured) if captured.args else ""
    if "deadline exceeded" not in msg:
        raise RuntimeError(
            "anyio CancelledError message format changed "
            f"(anyio={anyio_version}): expected 'deadline exceeded' in "
            f"{msg!r}; the cancel-containment heuristic in "
            "maestro/sdk.py query() / query_on_session() will misclassify "
            "internal scope deadlines as external cancels.",
        )


async def _ensure_anyio_message_format_verified() -> None:
    """Run :func:`_probe_anyio_deadline_message_format` once per process."""
    global _anyio_format_verified
    if _anyio_format_verified:
        return
    await _probe_anyio_deadline_message_format()
    _anyio_format_verified = True


async def _query_impl(
    prompt: str,
    *,
    options: QueryOptions | None = None,
    system: str | None = None,
    complete_on: Callable[[Any, str], bool] | None = None,
    stream_idle_timeout_seconds: float | None = None,
) -> QueryResult:
    """Run a claude_agent_sdk query and return the final result.

    The SDK's ``query`` is an async generator that yields streaming messages.
    This wrapper consumes the stream, logs intermediate messages for full
    visibility, and returns the final ``ResultMessage`` content as a
    ``QueryResult`` with ``.response``, ``.tool_calls``, and ``.usage``.

    *complete_on* is an optional in-stream predicate. When supplied, the
    function evaluates it after each :class:`AssistantMessage` and stops
    consuming the stream as soon as it returns ``True`` — useful for phases
    that only need a single substantive reply (e.g. ``introspect``) and can
    short-circuit before the model decides to wrap up. The returned
    :class:`QueryResult` carries the accumulated assistant text in
    ``.response`` and zero usage (the SDK never emitted a ``ResultMessage``).

    *stream_idle_timeout_seconds* overrides the module default
    (``configure_sdk_stream_idle_timeout``). Pass ``0`` or a negative value
    to disable the safety net for this call.
    """
    # Verify anyio's CancelledError wording still matches the heuristic
    # below (one-shot, gated by a module-level flag). See
    # ``_probe_anyio_deadline_message_format``.
    await _ensure_anyio_message_format_verified()
    # Convert vendor-neutral QueryOptions to SDK-specific ClaudeAgentOptions.
    sdk_opts, caller = _build_sdk_options(options, system)
    bypass_warning_throttle = bool(
        options is not None and options.bypass_warning_throttle
    )
    sdk_kwargs: dict[str, Any] = {"prompt": prompt}
    # Capture CLI stderr so subprocess errors are visible in logs.
    # Lines are collected in a list AND logged individually.  When the SDK
    # raises after a CLI crash the async stderr reader may be cancelled before
    # the callback fires, so we also dump collected lines in the except block.
    stderr_lines: list[str] = []

    def _stderr_callback(line: str) -> None:
        stderr_lines.append(line)
        # INFO, not WARNING: ``--debug-to-stderr`` (forced below) turns this
        # into a routine per-line stream on every call, not a crash signal.
        # The alertable event is the ERROR dump on failure further down.
        logger.info("SDK CLI stderr: %s", line)
        if session_log:
            session_log.log(f"CLI stderr: {line}")

    sdk_opts["stderr"] = _stderr_callback
    # Force the CLI to emit its own debug stream to stderr so the
    # callback above has something to capture when the CLI crashes
    # silently.  The flag is hidden from ``claude --help`` but honored
    # by the CLI; without it a crashing ``claude --print --verbose``
    # writes zero bytes to stderr on exit-code-1 failures.
    sdk_opts.setdefault("extra_args", {})
    sdk_opts["extra_args"].setdefault("debug-to-stderr", None)
    # The SDK runs inside an automated loop with no human to approve
    # commands.  Without this, blocked commands crash the loop.
    sdk_opts["permission_mode"] = "bypassPermissions"

    if sdk_opts:
        sdk_kwargs["options"] = ClaudeAgentOptions(**sdk_opts)

    # Defensive entry drain: if the task enters query() with a dirty
    # cancellation counter (e.g. an earlier caller caught a TimeoutError
    # from a contained internal cancel without itself uncancelling, or a
    # previous query() invocation's containment failed to fully clean up),
    # drain it now before any await checkpoint — otherwise the first
    # checkpoint would re-deliver a stale CancelledError.  This is
    # strictly defence-in-depth; in normal operation the in-flight drain
    # loop below keeps the counter clean across query() boundaries.
    _entry_task = asyncio.current_task()
    if _entry_task is not None and _entry_task.cancelling() > 0:
        _entry_dirty = _entry_task.cancelling()
        while _entry_task.cancelling() > 0:
            _entry_task.uncancel()
        logger.warning(
            "SDK query() entered with dirty cancellation counter: "
            "caller=%s counter=%d (drained)",
            caller, _entry_dirty,
        )

    # Concurrency gate — limit parallel query() calls.  Acquired here
    # (outside the session-log try/finally) and released in the same
    # finally block to keep the indentation flat.
    sem = _concurrency_semaphore
    if sem is not None:
        await sem.acquire()

    session_log: SessionLogger | None = None
    try:
        session_log = open_session_logger(caller)
        if session_log is not None:
            session_log.log(f"Prompt: {_truncate(prompt, 1000)}")
            session_log.add_turn("prompt", "user", prompt)

        last_exc: Exception | None = None
        attempt = 0
        _scope_contaminated = False
        _slept_for_rate_limit = False
        # Invariant: every await inside this retry loop must live inside
        # the ``try/except BaseException`` block so that internal cancel-
        # scope deadlines are caught and drained rather than leaked.
        while True:
            try:
                _partial_rejected: list[int | None] = [None]
                rejected_resets_at: int | None = None

                # Retry backoff delay.  Lives inside the ``try:`` block
                # so that an internal cancel-scope deadline firing during
                # the sleep is caught by ``except BaseException`` below,
                # not leaked as an unhandled ``CancelledError``.  Skipped
                # when ``_scope_contaminated`` — a prior iteration already
                # drained a cancel scope, so sleeping would risk
                # re-delivering a residual cancel at this checkpoint.
                # Also skipped when ``_slept_for_rate_limit`` — the prior
                # iteration already slept for the full rate-limit reset
                # window, so an additional backoff delay is redundant.
                if attempt > 0 and not _slept_for_rate_limit:
                    delay = _RETRY_BASE_DELAY * (
                        _RETRY_BACKOFF_FACTOR ** (min(attempt, _MAX_RETRIES) - 1)
                    )
                    logger.warning(
                        "SDK query retry %d/%d after %.1fs: %s",
                        attempt, _MAX_RETRIES, delay,
                        _extract_error_detail(last_exc) if last_exc else "unknown",
                    )
                    if session_log:
                        detail = _extract_error_detail(last_exc) if last_exc else "unknown"
                        session_log.log(f"RETRY {attempt}/{_MAX_RETRIES} after {delay:.1f}s: {detail}")
                    if not _scope_contaminated:
                        await asyncio.sleep(delay)
                _slept_for_rate_limit = False

                # Sticky ``allowed_warning`` throttle — slow down when the
                # API has recently told us we're close to a quota limit.
                # The sleep fires once per query() (guarded by
                # ``attempt == 0``) so a failing call that retries
                # internally doesn't compound the throttle delay.  It lives
                # inside this ``try:`` block — not above the retry loop —
                # so that an internal cancel-scope deadline firing during
                # the sleep is caught by the ``except BaseException``
                # below and flows through the drain loop, instead of
                # leaking a ``CancelledError`` past the containment
                # boundary the way the pre-2026-04-09 layout did.  Probes
                # opt out via ``QueryOptions.bypass_warning_throttle`` —
                # see field docstring for rationale.
                if (
                    attempt == 0
                    and _warning_throttle_multiplier > 1
                    and not bypass_warning_throttle
                ):
                    sleep_s = min(
                        _sdk_warning_throttle_base_seconds
                        * _warning_throttle_multiplier,
                        _WARNING_THROTTLE_MAX_SLEEP_SECONDS,
                    )
                    logger.info(
                        "SDK warning throttle active (x%d, clean=%d/%d) — "
                        "sleeping %ds before next call (caller=%s)",
                        _warning_throttle_multiplier,
                        _warning_throttle_clean_calls,
                        _sdk_warning_throttle_hysteresis,
                        int(sleep_s),
                        caller or "unknown",
                    )
                    if session_log:
                        session_log.log(
                            f"Warning throttle: x{_warning_throttle_multiplier} "
                            f"sleep={int(sleep_s)}s caller={caller or 'unknown'}"
                        )
                    await asyncio.sleep(sleep_s)

                # Hoist the generator into a local so we own its lifecycle
                # and can ``aclose()`` it inside this task — see the
                # ``finally`` block below. The SDK annotates ``query`` as
                # ``AsyncIterator`` (no ``aclose``), but at runtime it is
                # always an async generator function, so the cast is safe.
                # #4158: tag this attempt's SDK children so the post-aclose
                # drain cancels only them, never a concurrent query's.
                with _sdk_child_owner_scope() as child_owner:
                    gen = cast(
                        AsyncGenerator[Any], _query(**sdk_kwargs),
                    )
                    try:
                        sdk_betas = sdk_opts.get("betas")
                        cr = await _consume_messages(
                            gen, caller, session_log,
                            usage=QueryUsage(
                                caller=caller,
                                attempts=attempt + 1,
                                model=_label_long_context_pricing(
                                    sdk_opts.get("model"), sdk_betas,
                                ),
                                effort=sdk_opts.get("effort"),
                            ),
                            partial_rejected_resets_at=_partial_rejected,
                            complete_on=complete_on,
                            stream_idle_timeout_seconds=stream_idle_timeout_seconds,
                            betas=sdk_betas,
                            structured_output_requested=(
                                options is not None
                                and options.output_format is not None
                            ),
                        )
                        rejected_resets_at = cr.rejected_resets_at
                        return _finalize_usage(cr, session_log)
                    finally:
                        # Close the generator inside this task. On the normal
                        # drain-to-StopAsyncIteration path this is a no-op; on
                        # the ``complete_on`` early-exit path (sdk.py:1452,
                        # ``break``) the generator is still alive, so without
                        # this aclose() its anyio cancel-scope teardown would
                        # be deferred to GC and run from whatever task is live
                        # at that moment, leaking ``CancelledError`` into an
                        # unrelated await. Same lifecycle invariant the
                        # ``break_on_result`` comment above documents and PR
                        # #1299's regression test pins for the drain path.
                        # #1643/#1648: aclose() can still leave residual
                        # ``async_generator_athrow`` tasks for inner generators
                        # the SDK's anyio scope teardown spawns. The pre-aclose
                        # snapshot is a zero-cost canary distinguishing
                        # "aclose() drained cleanly" from "aclose() left residue
                        # we then drained"; the post-aclose drain bounds the
                        # lifetime of any residue inside this task so it cannot
                        # fire CancelledError into the next phase's await.
                        _snapshot_athrow_tasks("sdk.query pre-aclose")
                        await gen.aclose()
                        # #1719: ``gen.aclose()`` walks through ``Query.close()``
                        # (via SDK transport finalize), which cancels every
                        # ``self._child_tasks`` entry without awaiting them.
                        # Drain those child tasks before the athrow drain so any
                        # athrow tasks their cancel scopes spawn for inner
                        # generators are visible to the second drain.
                        await _drain_sdk_child_tasks(
                            "sdk.query post-aclose", owner=child_owner,
                        )
                        await _drain_athrow_tasks("sdk.query post-aclose")
            except BaseException as raw_exc:
                # Distinguish "the task was cancelled by an outside party"
                # (must propagate) from "an internal cancel-scope deadline
                # that leaked out of the SDK transport" (should be treated
                # as a transient failure so the surrounding loop can
                # recover).
                #
                # The primary historical source of these leaks — the
                # ``anyio.move_on_after(2)`` scope in
                # ``_patch_sdk_stderr_drain`` — was first replaced with
                # ``asyncio.wait_for`` and then removed entirely (the
                # patch now skips the task-group exit and just yields
                # to the concurrent stderr reader).  This containment
                # block remains as defence-in-depth for any internal
                # anyio scopes inside the SDK transport that we do not
                # control.
                #
                # The reliable signal is the exception message: anyio
                # stamps ``"Cancelled via cancel scope …; reason: deadline
                # exceeded"`` when a scope deadline fires, whereas a bare
                # ``task.cancel()`` from an outside party produces a
                # ``CancelledError`` with no such message. anyio version
                # compatibility is verified at startup by
                # ``_probe_anyio_deadline_message_format()``; a wording
                # change there fails loudly before this heuristic can
                # silently misclassify.
                if isinstance(raw_exc, asyncio.CancelledError):
                    msg = str(raw_exc) if raw_exc.args else ""
                    is_internal_deadline = "deadline exceeded" in msg
                    if not is_internal_deadline:
                        current = asyncio.current_task()
                        if current is None or current.cancelling() > 0:
                            raise
                    # Internal anyio cancel: before wrapping the exception
                    # value, also clean up the task's cancellation *state*.
                    # anyio cancel scopes drive their own cancellation by
                    # calling ``task.cancel()`` internally, which bumps
                    # ``current_task().cancelling()`` to >= 1 — and crucially
                    # can *stack* multiple cancels before the CancelledError
                    # surfaces here.  A single ``uncancel()`` only decrements
                    # the counter by one; any residual cancels re-deliver at
                    # the next ``await`` checkpoint and contaminate the rest
                    # of the task's lifetime.
                    #
                    # Two production crashes drove this shape.  2026-04-08
                    # (iteration 15, phase=evolve, scope id 7eb4b23ef080)
                    # appeared in BOTH the planner SDKError AND the later
                    # shutdown Telegram-notify CancelledError: the value-level
                    # wrap below was in place but no state cleanup at all,
                    # so the loop crashed at the next await.  2026-04-09
                    # (scope id 731a8ef82db0) then logged ``remaining=3``
                    # after a single ``uncancel()`` — proving that at least
                    # four ``task.cancel()`` calls had been stacked by the
                    # time the exception fired, and three survived the
                    # single-call cleanup, one of which then fired on the
                    # next query's warning-throttle sleep and crashed dream.
                    #
                    # Fix: drain the counter fully with a ``while`` loop.
                    # ``uncancel()`` (Python 3.11+) is safe at 0, so the
                    # ``> 0`` guard is cosmetic — it just keeps ``drained``
                    # as an accurate per-call counter for telemetry.
                    current = asyncio.current_task()
                    initial_cancelling = (
                        current.cancelling() if current is not None else 0
                    )
                    drained = 0
                    if current is not None:
                        while current.cancelling() > 0:
                            current.uncancel()
                            drained += 1
                    _scope_contaminated = True
                    logger.warning(
                        "SDK internal-cancel contained: "
                        "caller=%s initial=%d drained=%d msg=%s",
                        caller, initial_cancelling, drained, msg,
                    )
                    # Wrap in a normal Exception so the retry /
                    # classification path treats it as a transient
                    # timeout and the loop can continue.
                    exc: Exception = TimeoutError(
                        f"SDK call cancelled by internal scope: {raw_exc}",
                    )
                elif not isinstance(raw_exc, Exception):
                    # SystemExit, KeyboardInterrupt and other non-Exception
                    # BaseException subclasses must always propagate.
                    raise
                else:
                    exc = raw_exc
                # Pre-categorized SDKError (e.g. STREAM_STALL raised by
                # ``_consume_messages``) skips retry and reclassification —
                # the inner code already decided this failure has a
                # specific category the agent's retry handler must see.
                if isinstance(exc, SDKError):
                    # A billed result received before the raise is booked
                    # here so spend is metered on error too (#4572).
                    _book_unbooked_error_result(exc, cost_baseline=None)
                    raise
                last_exc = exc
                # Dump any stderr lines collected before the async reader
                # was cancelled — this is the primary diagnostic for CLI
                # crashes where the SDK only says "Check stderr output".
                if stderr_lines:
                    joined = "\n".join(stderr_lines)
                    logger.error(
                        "SDK CLI stderr captured (%d lines):\n%s",
                        len(stderr_lines), joined,
                    )
                    if session_log:
                        session_log.log(f"CLI stderr dump:\n{joined}")
                    # Attach to exception so _extract_error_detail picks it up.
                    # Always overwrite: the SDK sets ProcessError.stderr to
                    # the useless placeholder "Check stderr output for
                    # details" which is truthy but carries no diagnostic
                    # value.  Callback-captured lines are always more useful.
                    # Mostly [DEBUG] noise: classifiers must not keyword-match
                    # it unfiltered (#4310, #4373).
                    exc.stderr = joined  # type: ignore[attr-defined]
                    stderr_lines.clear()
                else:
                    # Defence-in-depth: ``--debug-to-stderr`` should ensure
                    # the callback always captures something on a CLI
                    # crash.  If it didn't, log the empty state explicitly
                    # so future silent crashes are unmistakable.
                    logger.error(
                        "SDK CLI crash with empty captured stderr "
                        "(attempt=%d, caller=%s, exc_type=%s): %s",
                        attempt + 1, caller, type(exc).__name__, exc,
                    )
                    if session_log:
                        session_log.log(
                            f"EMPTY STDERR on crash "
                            f"(attempt {attempt + 1}, caller={caller}, "
                            f"exc_type={type(exc).__name__})",
                        )

                if session_log:
                    session_log.log(
                        f"EXCEPTION (attempt {attempt + 1}): {_extract_error_detail(exc)}"
                    )
                # If a rate limit rejection was observed, sleep toward the
                # reset window (capped at _RATE_LIMIT_SLEEP_CAP_SECONDS) and
                # retry internally instead of raising to callers.
                if rejected_resets_at is None and _partial_rejected[0] is not None:
                    rejected_resets_at = _partial_rejected[0]
                if rejected_resets_at is not None:
                    _log_rate_limit(
                        caller=caller,
                        attempt=attempt + 1,
                        resets_at=rejected_resets_at,
                    )
                    if attempt >= _MAX_RETRIES:
                        stderr_text = getattr(exc, "stderr", None)
                        if isinstance(stderr_text, bytes):
                            stderr_text = stderr_text.decode(errors="replace")
                        raise SDKError(
                            f"Rate limited after {attempt + 1} attempts",
                            original=exc,
                            stderr=stderr_text,
                            category=ErrorCategory.RATE_LIMIT,
                            retries_attempted=attempt,
                            retries_exhausted=True,
                            exit_code=_exit_code_of(exc),
                        ) from exc
                    wait_s = _rate_limit_sleep_seconds(
                        rejected_resets_at, caller,
                    )
                    if session_log:
                        session_log.log(
                            f"Rate limited — sleeping {int(wait_s)}s",
                        )
                    await asyncio.sleep(wait_s)
                    _slept_for_rate_limit = True
                    attempt += 1
                    continue  # retry after rate limit sleep

                # Rate-limit detected from stderr/message keywords but no
                # RateLimitEvent was streamed (CLI crashed before emitting
                # it).  Use a much longer backoff than the generic transient
                # path to respect the API's rate-limit window.
                if _classify_error(exc) == ErrorCategory.RATE_LIMIT:
                    if attempt >= _RATE_LIMIT_MAX_RETRIES:
                        stderr_text = getattr(exc, "stderr", None)
                        if isinstance(stderr_text, bytes):
                            stderr_text = stderr_text.decode(errors="replace")
                        raise SDKError(
                            f"Rate limited (stderr) after {attempt + 1} attempts",
                            original=exc,
                            stderr=stderr_text,
                            category=ErrorCategory.RATE_LIMIT,
                            retries_attempted=attempt,
                            retries_exhausted=True,
                            exit_code=_exit_code_of(exc),
                        ) from exc
                    rate_limit_delay = min(
                        _RATE_LIMIT_RETRY_BASE_DELAY
                        * (_RETRY_BACKOFF_FACTOR ** attempt),
                        _RATE_LIMIT_MAX_SLEEP,
                    )
                    logger.warning(
                        "Rate limited (stderr keywords) — sleeping %ds "
                        "(caller=%s, attempt=%d/%d)",
                        int(rate_limit_delay),
                        caller or "unknown",
                        attempt + 1,
                        _RATE_LIMIT_MAX_RETRIES,
                    )
                    if session_log:
                        session_log.log(
                            f"Rate limited (stderr) — sleeping "
                            f"{int(rate_limit_delay)}s "
                            f"(attempt {attempt + 1}/"
                            f"{_RATE_LIMIT_MAX_RETRIES})"
                        )
                    await asyncio.sleep(rate_limit_delay)
                    _slept_for_rate_limit = True
                    attempt += 1
                    continue

                if not _is_transient(exc) or attempt >= _MAX_RETRIES:
                    if attempt >= _MAX_RETRIES:
                        logger.error(
                            "SDK query failed after %d retries: %s",
                            attempt,
                            _extract_error_detail(exc),
                        )
                    stderr_text = getattr(exc, "stderr", None)
                    if isinstance(stderr_text, bytes):
                        stderr_text = stderr_text.decode(errors="replace")
                    raise SDKError(
                        _extract_error_detail(exc),
                        original=exc,
                        stderr=stderr_text,
                        category=_classify_error(exc),
                        retries_attempted=attempt,
                        retries_exhausted=(attempt >= _MAX_RETRIES),
                        exit_code=_exit_code_of(exc),
                    ) from exc
                # Reached only when the guarded raise above did not fire — the
                # error was transient and retries remain; advance to the next
                # attempt.
                attempt += 1
    finally:
        if sem is not None:
            sem.release()
        if session_log:
            session_log.close()


async def _connect_with_resume_fallback(
    client: ClaudeSDKClient,
    sdk_opts: dict[str, Any],
    child_owner: object,
    initial_prompt: str | None,
    resume_session_id: str,
) -> ClaudeSDKClient:
    """Replace a client whose ``resume=<id>`` connect failed with a fresh one.

    Called from inside ``_create_session_impl``'s resume-at-connect handler
    (with the failure still in ``sys.exc_info``): warns with the traceback,
    tears the failed client down, and connects a fresh session without
    ``resume``. A failure of the fresh connect propagates raw; the caller
    wraps it via :func:`_connect_failure_error` (#4570). Extracted so the
    fail-loud fence sees the handler re-raise-or-delegate, not a
    log-and-fall-through body (#4165).
    """
    logger.warning(
        "Session resume failed at connect for id=%s — "
        "falling back to fresh session",
        resume_session_id,
        exc_info=True,
    )
    await _disconnect_best_effort(client)
    sdk_opts.pop("resume", None)
    fresh = ClaudeSDKClient(options=ClaudeAgentOptions(**sdk_opts))
    setattr(fresh, _SESSION_CHILD_OWNER_ATTR, child_owner)
    await fresh.connect(initial_prompt)
    return fresh


def _connect_failure_error(
    exc: Exception, stderr_lines: Sequence[str],
) -> SDKError:
    """Classify a session-connect failure into an :class:`SDKError` (#4570).

    Mirrors the terminal ``SDKError`` shape of the query paths so callers
    branching on ``category`` / ``exit_code`` (``is_environment_fault``,
    ``_TRANSIENT_SDK_CATEGORIES``) see a connect-time OOM kill or crash the
    same way as a query-time one. An existing ``SDKError`` passes through.
    """
    if isinstance(exc, SDKError):
        return exc
    if stderr_lines:
        joined = "\n".join(stderr_lines)
        logger.error(
            "SDK CLI stderr captured (session connect, %d lines):\n%s",
            len(stderr_lines), joined,
        )
        exc.stderr = joined  # type: ignore[attr-defined]
    stderr_text = getattr(exc, "stderr", None)
    if isinstance(stderr_text, bytes):
        stderr_text = stderr_text.decode(errors="replace")
    return SDKError(
        _extract_error_detail(exc),
        original=exc,
        stderr=stderr_text,
        category=_classify_error(exc),
        exit_code=_exit_code_of(exc),
        session_reusable=False,
    )


async def _create_session_impl(
    initial_prompt: str | None = None,
    *,
    options: QueryOptions | None = None,
    system: str | None = None,
    resume_session_id: str | None = None,
) -> ClaudeSDKClient:
    """Create and connect a :class:`ClaudeSDKClient` with standard Maestro options.

    When *initial_prompt* is ``None`` the session is connected without
    sending a user message — the first real prompt is expected to arrive
    via :func:`query_on_session`.

    The caller owns the lifecycle and **must** call
    ``client.disconnect()`` when done.

    A stderr callback is installed so that CLI subprocess errors are
    captured in logs for the lifetime of the session (including follow-up
    ``query_on_session`` calls).

    When *resume_session_id* is provided, ``ClaudeAgentOptions.resume`` is
    set so the CLI loads the prior server-side transcript and this session
    continues where the original left off (not a forked branch —
    ``fork_session`` stays at its SDK default of ``False``). On
    ``connect()`` failure with a resume id, the function logs a warning
    and retries once with ``resume=None`` — the caller should treat this
    as a silent "session was stale" and clear any persisted id. Note that
    stale-session errors may also surface from the first query turn rather
    than at connect; the caller is expected to handle that case separately.

    Any other connect failure (including the fallback's fresh connect)
    surfaces as a classified :class:`SDKError` (#4570).
    """
    # Verify anyio's CancelledError wording still matches the heuristic
    # used by ``query_on_session`` (one-shot, gated by a module-level
    # flag). See ``_probe_anyio_deadline_message_format``.
    await _ensure_anyio_message_format_verified()
    sdk_opts, _caller = _build_sdk_options(options, system)

    # Fixed-size ring buffer: a persistent session accumulates CLI stderr on
    # every successful turn (only the error path clears it), so cap retention
    # to the most-recent lines. ``append``/``join``/``len``/``clear`` behave
    # identically on a deque, so the consumer needs no change.
    stderr_lines: deque[str] = deque(maxlen=_SESSION_STDERR_MAX_LINES)

    def _stderr_callback(line: str) -> None:
        stderr_lines.append(line)
        # INFO, not WARNING — see the matching note in stateless ``query()``.
        logger.info("SDK CLI stderr (session): %s", line)

    sdk_opts["stderr"] = _stderr_callback
    # Force the CLI to emit its own debug stream to stderr — see the
    # matching block in stateless ``query()`` for rationale.
    sdk_opts.setdefault("extra_args", {})
    sdk_opts["extra_args"].setdefault("debug-to-stderr", None)

    if resume_session_id is not None:
        sdk_opts["resume"] = resume_session_id

    # #4158: tag the session's SDK children (read loop, control handlers)
    # with an owner token so ``disconnect_session`` drains only them.
    with _sdk_child_owner_scope() as child_owner:
        client = ClaudeSDKClient(options=ClaudeAgentOptions(**sdk_opts))
        setattr(client, _SESSION_CHILD_OWNER_ATTR, child_owner)
        resume_active = resume_session_id is not None
        # Diagnostic probe for #1643: any pending athrow task here is a
        # candidate for the CancelledError that crashes the connect scope.
        # The drain bounds the lifetime of any residue inside this task so
        # it cannot fire CancelledError into client.connect() (#1648/#1656).
        # Same shape as the post-aclose pair at sdk.py:2172-2174.
        _snapshot_athrow_tasks("sdk.create_session pre-connect")
        try:
            # The drain helper deliberately lets ``CancelledError``
            # propagate so external cancellation intent is preserved
            # (sdk.py:147-154). When the residual athrow task this drain
            # is meant to neutralize fires ``CancelledError`` into the
            # drain's own ``asyncio.wait``, that exception must take the
            # same recovery path as a cancel during ``client.connect()``
            # (#1696) - hence the drain lives inside this guard.
            await _drain_athrow_tasks("sdk.create_session pre-connect")
            await client.connect(initial_prompt)
        except asyncio.CancelledError as cancel_exc:
            # CancelledError is a BaseException (Python 3.8+) so the
            # ``except Exception`` below does not catch it.  A residual
            # ``async_generator_athrow`` task from SDK teardown can fire
            # this into ``client.connect()`` (#1643).  Drain the cancel
            # state so it does not re-deliver at the next await, then
            # raise a classifiable ``SDKError`` so the agent's retry
            # machinery (CLI_CRASH handler in ``run_loop``) can recover.
            current = asyncio.current_task()
            if current is not None:
                while current.cancelling() > 0:
                    current.uncancel()
            logger.warning(
                "CancelledError during create_session pre-connect/connect "
                "(resume=%s): %s — converting to SDKError",
                resume_session_id,
                cancel_exc,
            )
            await _disconnect_best_effort(client)
            # Late-materializing athrow orphans — those born inside the
            # very ``client.connect()`` call we are recovering from —
            # slip past the pre-connect drain (it snapshots the loop's
            # task set before connect runs, #1648) and can leak past
            # ``disconnect_session``'s post-child athrow drain too.
            # Drain again here, inside the failing task, so a single
            # residual orphan does not contaminate the agent's
            # resume-in-place retry and exhaust the CLI-crash quota in
            # milliseconds (#1893). Mirror ``disconnect_session``'s
            # cancel-absorption shape (sdk.py:3307-3310): a residual
            # cancel firing into this drain itself would otherwise
            # re-introduce the leak.
            pending_after_drain = 0
            try:
                _, pending_after_drain = await _drain_athrow_tasks(
                    "sdk.create_session post-cancel",
                )
            except asyncio.CancelledError:
                _absorb_drain_cancel("sdk.create_session post-cancel")
            if pending_after_drain:
                logger.warning(
                    "post-cancel athrow drain in create_session left "
                    "pending_athrow_after_drain=%d task(s) — orphan may "
                    "still leak into next attempt",
                    pending_after_drain,
                )
            raise SDKError(
                f"Session connect cancelled by residual task: {cancel_exc}",
                original=cancel_exc,
                category=ErrorCategory.CLI_CRASH,
            ) from cancel_exc
        except Exception as exc:
            if resume_session_id is None:
                raise _connect_failure_error(exc, stderr_lines) from exc
            try:
                client = await _connect_with_resume_fallback(
                    client, sdk_opts, child_owner, initial_prompt,
                    resume_session_id,
                )
            except Exception as fresh_exc:
                raise _connect_failure_error(
                    fresh_exc, stderr_lines,
                ) from fresh_exc
            resume_active = False
    # Initialize the session_id sidecar. ``query_on_session`` updates this
    # after every successful turn so callers can recover the ID without
    # threading ``QueryResult`` back through helper methods.
    # Expose the stderr collection list so query_on_session can attach
    # captured lines to exceptions (same fix as stateless query()).
    client._maestro_stderr_lines = stderr_lines  # type: ignore[attr-defined]
    client._maestro_last_session_id = None  # type: ignore[attr-defined]
    # Remember whether this session was actually opened with ``resume=<id>``
    # (i.e. connect succeeded without falling back). ``query_on_session``
    # uses this flag to raise a distinguishable
    # ``ErrorCategory.STALE_SESSION`` on a first-turn failure — useful when
    # the CLI accepts ``resume=<id>`` at connect but rejects it on the
    # first real call. The flag survives only until the first successful
    # turn, after which ``query_on_session`` clears it.
    client._maestro_resume_used = resume_active  # type: ignore[attr-defined]
    # Stash the resolved model so :func:`query_on_session` can populate
    # ``QueryUsage.model`` for downstream pricing estimation. Sessions
    # bake the model in at construction time, so reading it back from the
    # client's options later is non-trivial — the sidecar attribute is
    # the simplest path.
    client._maestro_model = sdk_opts.get("model")  # type: ignore[attr-defined]
    # Stash the resolved betas list too — ``query_on_session`` reads this
    # so the ``[1m]`` pricing suffix is applied consistently across the
    # session's lifetime (sessions bake the beta set in at construction).
    client._maestro_betas = sdk_opts.get("betas")  # type: ignore[attr-defined]
    # Stash whether this session baked in an ``output_format`` (structured
    # output). Sessions freeze their options at construction, so
    # ``query_on_session`` reads this back to thread
    # ``structured_output_requested`` into ``_consume_messages`` — giving the
    # session path the same #2533 sentinel semantics as the one-shot path (a
    # validated ``null`` stays productive; a no-schema turn keeps the unset
    # sentinel). Without it the session path defaulted the flag to ``False``,
    # dropping a produced structured value and skipping the guard fix.
    client._maestro_output_format = sdk_opts.get("output_format")  # type: ignore[attr-defined]
    # Stash the effort the session was opened with: every turn runs at it,
    # whatever effort the turn's call site declares, so ``query_on_session``
    # stamps this — not the turn's intent — on ``QueryUsage.effort``.
    client._maestro_effort = sdk_opts.get("effort")  # type: ignore[attr-defined]
    return client


async def _query_on_session_impl(
    client: ClaudeSDKClient,
    prompt: str,
    *,
    caller: str | None = None,
    complete_on: Callable[[Any, str], bool] | None = None,
    stream_idle_timeout_seconds: float | None = None,
    bypass_warning_throttle: bool = False,
) -> QueryResult:
    """Send a follow-up message on an existing session and return the result.

    Uses the same message-processing pipeline and retry / rate-limit /
    cancel-scope containment logic as :func:`query`, but operates on a
    persistent ``ClaudeSDKClient`` instead of spawning a fresh subprocess.

    *complete_on* and *stream_idle_timeout_seconds* mirror the matching
    kwargs on :func:`query`. When the predicate fires the function calls
    ``client.interrupt()`` so the session is left in a clean state for the
    next turn (without it the underlying CLI would still be streaming the
    aborted reply when the next ``client.query()`` arrives).
    """
    _entry_task = asyncio.current_task()
    if _entry_task is not None and _entry_task.cancelling() > 0:
        _entry_dirty = _entry_task.cancelling()
        while _entry_task.cancelling() > 0:
            _entry_task.uncancel()
        logger.warning(
            "SDK query_on_session() entered with dirty cancellation "
            "counter: caller=%s counter=%d (drained)",
            caller, _entry_dirty,
        )

    sem = _concurrency_semaphore
    if sem is not None:
        await sem.acquire()

    session_log: SessionLogger | None = None
    try:
        session_log = open_session_logger(caller)
        if session_log is not None:
            session_log.log(f"Session follow-up: {_truncate(prompt, 1000)}")
            session_log.add_turn("prompt", "user", prompt)

        last_exc: Exception | None = None
        attempt = 0
        _scope_contaminated = False
        _slept_for_rate_limit = False
        while True:
            try:
                _partial_rejected: list[int | None] = [None]
                rejected_resets_at: int | None = None
                # Reset per attempt: True once ``client.query(prompt)`` has
                # committed this prompt to the session's conversation history.
                prompt_accepted = False

                if attempt > 0 and not _slept_for_rate_limit:
                    delay = _RETRY_BASE_DELAY * (
                        _RETRY_BACKOFF_FACTOR ** (min(attempt, _MAX_RETRIES) - 1)
                    )
                    logger.warning(
                        "SDK session query retry %d/%d after %.1fs: %s",
                        attempt, _MAX_RETRIES, delay,
                        _extract_error_detail(last_exc) if last_exc else "unknown",
                    )
                    if session_log:
                        detail = _extract_error_detail(last_exc) if last_exc else "unknown"
                        session_log.log(f"RETRY {attempt}/{_MAX_RETRIES} after {delay:.1f}s: {detail}")
                    if not _scope_contaminated:
                        await asyncio.sleep(delay)
                _slept_for_rate_limit = False

                # Mirror of the one-shot path's ``allowed_warning`` throttle.
                # The gate-convergence fix loop opts out via
                # ``bypass_warning_throttle`` so its must-finish grind is not
                # paced by soft backpressure. ``_finalize_usage`` still runs
                # ``_update_warning_throttle_state`` after the call, so a
                # bypassed round keeps feeding the decay counter.
                if (
                    attempt == 0
                    and _warning_throttle_multiplier > 1
                    and not bypass_warning_throttle
                ):
                    sleep_s = min(
                        _sdk_warning_throttle_base_seconds
                        * _warning_throttle_multiplier,
                        _WARNING_THROTTLE_MAX_SLEEP_SECONDS,
                    )
                    logger.info(
                        "SDK warning throttle active (x%d, clean=%d/%d) — "
                        "sleeping %ds before session query (caller=%s)",
                        _warning_throttle_multiplier,
                        _warning_throttle_clean_calls,
                        _sdk_warning_throttle_hysteresis,
                        int(sleep_s),
                        caller or "unknown",
                    )
                    if session_log:
                        session_log.log(
                            f"Warning throttle: x{_warning_throttle_multiplier} "
                            f"sleep={int(sleep_s)}s caller={caller or 'unknown'}"
                        )
                    await asyncio.sleep(sleep_s)

                await client.query(prompt)
                # The prompt is now committed to the session's conversation
                # history; any transient failure from here on must not re-send
                # it in place (see the prompt_accepted gate in the handler).
                prompt_accepted = True
                session_model = getattr(client, "_maestro_model", None)
                session_betas = getattr(client, "_maestro_betas", None)
                # Read the baked ``output_format`` from the instance ``__dict__``
                # (not ``getattr``) so a ``MagicMock`` session double — whose
                # attribute access auto-vivifies a truthy child for *any* name —
                # cannot flip ``structured_output_requested`` on; only a value
                # ``_create_session_impl`` actually stamped counts. Mirrors the
                # MagicMock-safe read in :func:`_provider_for_session` (#2533).
                _session_dict = getattr(client, "__dict__", None)
                session_output_format = (
                    _session_dict.get("_maestro_output_format")
                    if isinstance(_session_dict, dict)
                    else None
                )
                # Same MagicMock-safe read: only a stamped effort counts.
                _raw_effort = (
                    _session_dict.get("_maestro_effort")
                    if isinstance(_session_dict, dict)
                    else None
                )
                session_effort = (
                    _raw_effort if isinstance(_raw_effort, str) else None
                )
                cr = await _consume_messages(
                    client.receive_messages(), caller, session_log,
                    usage=QueryUsage(
                        caller=caller,
                        attempts=attempt + 1,
                        model=_label_long_context_pricing(
                            session_model, session_betas,
                        ),
                        effort=session_effort,
                    ),
                    partial_rejected_resets_at=_partial_rejected,
                    complete_on=complete_on,
                    stream_idle_timeout_seconds=stream_idle_timeout_seconds,
                    break_on_result=True,
                    betas=session_betas,
                    structured_output_requested=session_output_format is not None,
                )
                drained_result: ResultMessage | None = None
                if cr.early_exit:
                    # Tell the CLI to stop generating the current turn so a
                    # subsequent ``client.query()`` lands on a quiet stream.
                    # ``interrupt`` is best-effort — log and continue if the
                    # SDK rejects it (the session may already be torn down).
                    try:
                        await client.interrupt()
                    except Exception:
                        logger.debug(
                            "client.interrupt() after early-exit failed "
                            "(caller=%s)",
                            caller or "unknown",
                            exc_info=True,
                        )
                    # Drain whatever the background reader pumped into the
                    # shared buffer before the CLI honoured the interrupt.
                    # Without this, the next ``client.query()`` on this
                    # session would read stale messages from the previous
                    # turn before the new turn's response arrives. The
                    # caller already has its result; this is purely defensive
                    # cleanup and is bounded by both an idle timeout and a
                    # message cap so a misbehaving CLI cannot wedge the loop.
                    drained = 0
                    drain_iter = client.receive_messages().__aiter__()
                    while drained < _INTERRUPT_DRAIN_MAX_MESSAGES:
                        try:
                            drained_msg = await asyncio.wait_for(
                                drain_iter.__anext__(),
                                timeout=_INTERRUPT_DRAIN_IDLE_TIMEOUT,
                            )
                        except (TimeoutError, StopAsyncIteration):
                            break
                        except Exception:
                            # Never let the drain hang or surface to the
                            # caller — they already got their result.
                            logger.debug(
                                "Post-interrupt drain aborted (caller=%s)",
                                caller or "unknown",
                                exc_info=True,
                            )
                            break
                        drained += 1
                        if isinstance(drained_msg, ResultMessage):
                            drained_result = drained_msg
                            break
                    if drained:
                        logger.debug(
                            "Drained %d post-interrupt message(s) (caller=%s)",
                            drained, caller or "unknown",
                        )
                        if session_log:
                            session_log.log(
                                f"Drained {drained} post-interrupt "
                                f"message(s)",
                            )
                # Per-turn cost delta (#4306): the CLI's ``total_cost_usd``
                # is a running total for this process, so book only the
                # spend above the last total this client saw.
                result = _finalize_usage(
                    cr, session_log,
                    cost_baseline=_session_cost_baseline(client, cr),
                )
                _advance_session_cost_baseline(client, cr, drained_result)
                # Sidecar: stash the session_id on the client so callers can
                # read it without having to thread the QueryResult back.
                # Used by the loop to persist A/B session IDs across retries
                # and restarts via lifecycle.json.
                if result.session_id is not None:
                    client._maestro_last_session_id = result.session_id  # type: ignore[attr-defined]
                # After the first successful turn the ``resume=<id>`` has
                # been proved good; clear the flag so any subsequent
                # failure is classified normally (not as STALE_SESSION).
                client._maestro_resume_used = False  # type: ignore[attr-defined]
                return result
            except BaseException as raw_exc:
                # Same internal-deadline vs outside-cancel classifier as the
                # stateless ``query()`` path above — see the rationale block
                # near ``is_internal_deadline = "deadline exceeded" in msg``.
                # anyio version compatibility is verified at startup by
                # ``_probe_anyio_deadline_message_format()``; a wording
                # change there fails loudly before this heuristic can
                # silently misclassify.
                if isinstance(raw_exc, asyncio.CancelledError):
                    msg = str(raw_exc) if raw_exc.args else ""
                    is_internal_deadline = "deadline exceeded" in msg
                    if not is_internal_deadline:
                        current = asyncio.current_task()
                        if current is None or current.cancelling() > 0:
                            raise
                    current = asyncio.current_task()
                    initial_cancelling = (
                        current.cancelling() if current is not None else 0
                    )
                    drained = 0
                    if current is not None:
                        while current.cancelling() > 0:
                            current.uncancel()
                            drained += 1
                    _scope_contaminated = True
                    logger.warning(
                        "SDK internal-cancel contained (session): "
                        "caller=%s initial=%d drained=%d msg=%s",
                        caller, initial_cancelling, drained, msg,
                    )
                    exc: Exception = TimeoutError(
                        f"SDK session call cancelled by internal scope: {raw_exc}",
                    )
                elif not isinstance(raw_exc, Exception):
                    raise
                else:
                    exc = raw_exc
                # Pre-categorized SDKError (e.g. STREAM_STALL raised by
                # ``_consume_messages``) skips retry and reclassification —
                # the inner code already decided this failure has a
                # specific category the agent's retry handler must see.
                if isinstance(exc, SDKError):
                    # A billed result received before the raise is booked
                    # here, against this session's baseline (#4572).
                    if exc.unbooked is not None:
                        booked = _book_unbooked_error_result(
                            exc,
                            cost_baseline=_session_cost_baseline(
                                client, exc.unbooked,
                            ),
                        )
                        if booked is not None:
                            _advance_session_cost_baseline(client, booked)
                    raise
                last_exc = exc

                # Dump any stderr lines collected by the session callback —
                # same logic as stateless query().
                _sess_stderr: list[str] = getattr(
                    client, "_maestro_stderr_lines", [],
                )
                if _sess_stderr:
                    joined = "\n".join(_sess_stderr)
                    logger.error(
                        "SDK CLI stderr captured (session, %d lines):\n%s",
                        len(_sess_stderr), joined,
                    )
                    if session_log:
                        session_log.log(f"CLI stderr dump (session):\n{joined}")
                    # Mostly [DEBUG] noise: classifiers must not keyword-match
                    # it unfiltered (#4310, #4373).
                    exc.stderr = joined  # type: ignore[attr-defined]
                    _sess_stderr.clear()
                else:
                    # Defence-in-depth: see matching block in stateless
                    # ``query()``.  Empty captured stderr on a session
                    # crash is itself a signal worth logging.
                    logger.error(
                        "SDK CLI crash with empty captured stderr "
                        "(session, attempt=%d, caller=%s, exc_type=%s): %s",
                        attempt + 1, caller, type(exc).__name__, exc,
                    )
                    if session_log:
                        session_log.log(
                            f"EMPTY STDERR on crash (session, "
                            f"attempt {attempt + 1}, caller={caller}, "
                            f"exc_type={type(exc).__name__})",
                        )

                if session_log:
                    session_log.log(
                        f"EXCEPTION (attempt {attempt + 1}): {_extract_error_detail(exc)}"
                    )
                if rejected_resets_at is None and _partial_rejected[0] is not None:
                    rejected_resets_at = _partial_rejected[0]
                if rejected_resets_at is not None:
                    _log_rate_limit(
                        caller=caller,
                        attempt=attempt + 1,
                        resets_at=rejected_resets_at,
                    )
                # A rate-limit retry sleeps and re-sends in place — sound only
                # before ``client.query`` returned. Once the prompt is accepted
                # (a rejected event is only ever observed then), re-sending
                # would duplicate it (#1216) or, on a dead CLI, degrade into a
                # NETWORK crash; raise RATE_LIMIT instead so resume-in-place
                # recovers on a fresh session (#4046, #4159).
                if prompt_accepted and (
                    rejected_resets_at is not None
                    or _classify_error(exc) == ErrorCategory.RATE_LIMIT
                ):
                    logger.warning(
                        "Rate limited after prompt accepted (session) — raising "
                        "for resume-in-place, not re-sending (caller=%s, "
                        "attempt=%d)",
                        caller or "unknown",
                        attempt + 1,
                    )
                    if session_log:
                        session_log.log(
                            "Rate limited after prompt accepted — raising "
                            "RATE_LIMIT for resume-in-place"
                        )
                    rl_stderr = getattr(exc, "stderr", None)
                    if isinstance(rl_stderr, bytes):
                        rl_stderr = rl_stderr.decode(errors="replace")
                    raise SDKError(
                        f"Rate limited after prompt accepted (session): "
                        f"{_extract_error_detail(exc)}",
                        original=exc,
                        stderr=rl_stderr,
                        category=ErrorCategory.RATE_LIMIT,
                        retries_attempted=attempt,
                        exit_code=_exit_code_of(exc),
                    ) from exc
                if _classify_error(exc) == ErrorCategory.RATE_LIMIT:
                    if attempt >= _RATE_LIMIT_MAX_RETRIES:
                        raise SDKError(
                            f"Rate limited (session) after {attempt + 1} attempts",
                            original=exc,
                            category=ErrorCategory.RATE_LIMIT,
                            retries_attempted=attempt,
                            retries_exhausted=True,
                            exit_code=_exit_code_of(exc),
                        ) from exc
                    rate_limit_delay = min(
                        _RATE_LIMIT_RETRY_BASE_DELAY
                        * (_RETRY_BACKOFF_FACTOR ** attempt),
                        _RATE_LIMIT_MAX_SLEEP,
                    )
                    logger.warning(
                        "Rate limited (session, error classification) — sleeping %ds "
                        "(caller=%s, attempt=%d/%d)",
                        int(rate_limit_delay),
                        caller or "unknown",
                        attempt + 1,
                        _RATE_LIMIT_MAX_RETRIES,
                    )
                    if session_log:
                        session_log.log(
                            f"Rate limited (classification) — sleeping "
                            f"{int(rate_limit_delay)}s "
                            f"(attempt {attempt + 1}/"
                            f"{_RATE_LIMIT_MAX_RETRIES})"
                        )
                    await asyncio.sleep(rate_limit_delay)
                    _slept_for_rate_limit = True
                    attempt += 1
                    continue

                # ``prompt_accepted`` short-circuits the in-place retry once
                # the prompt has been committed to the session's conversation
                # history (the failure struck inside ``_consume_messages``,
                # not before ``client.query`` returned). Re-sending the same
                # prompt on the same persistent session would duplicate it in
                # the transcript (prompt + partial response + prompt again),
                # confusing the model — especially when the aborted turn had
                # already emitted tool calls. Raise the categorized SDKError
                # instead, mirroring the pre-categorized STREAM_STALL raise
                # above, so the caller's resume-in-place recreates a fresh
                # session. The pre-acceptance path (``prompt_accepted`` False,
                # e.g. ``client.query`` itself raised) keeps retrying in place.
                # The same gate guards the rate-limit arm above, so no arm
                # of this handler re-sends an accepted prompt (#1216/#4046/#4159).
                if (
                    not _is_transient(exc)
                    or attempt >= _MAX_RETRIES
                    or prompt_accepted
                ):
                    if attempt >= _MAX_RETRIES:
                        logger.error(
                            "SDK session query failed after %d retries: %s",
                            attempt,
                            _extract_error_detail(exc),
                        )
                    # When the failure happened on the first turn of a
                    # session opened with ``resume=<id>`` (no successful
                    # turn ever completed), promote only the categories
                    # that plausibly mean the resumed server-side
                    # transcript is stale — CLI_CRASH (e.g. the
                    # ``session not found`` exit) and UNKNOWN — to
                    # STALE_SESSION, so the caller can clear the persisted
                    # id and recreate the session fresh. NETWORK / TIMEOUT
                    # are transport faults and AUTH is a real credential
                    # problem; none is a stale transcript, so they keep
                    # their natural category and surface (crash loudly)
                    # rather than masquerading as a stale session (#1250).
                    category = _classify_error(exc)
                    if (
                        category in (
                            ErrorCategory.CLI_CRASH, ErrorCategory.UNKNOWN,
                        )
                        and getattr(client, "_maestro_resume_used", False)
                        and getattr(
                            client, "_maestro_last_session_id", None,
                        ) is None
                    ):
                        category = ErrorCategory.STALE_SESSION
                    elif (
                        prompt_accepted
                        and isinstance(exc, TimeoutError)
                        and "cancelled by internal scope"
                        in str(exc).lower()
                    ):
                        # A *contained* anyio internal-cancel (wrapped into
                        # this TimeoutError by the BaseException block above)
                        # struck inside ``_consume_messages`` after the prompt
                        # was accepted. Its natural category is TIMEOUT, which
                        # the caller does not resume-in-place — so without this
                        # promotion the ``prompt_accepted`` gate would crash
                        # the loop on a transient the containment machinery
                        # exists to recover from. Re-sending in place is barred
                        # (it would duplicate the prompt, #1216), so route it
                        # to STALE_SESSION: the caller clears the persisted ids
                        # and retries the phase on a fresh session, delivering
                        # the recovery the gate promises. This is deliberately
                        # narrower than the #1250 policy above — a *genuine*
                        # NETWORK / external TIMEOUT transport fault keeps its
                        # natural category and still surfaces loudly; only
                        # Maestro's own contained cancel, which proves the
                        # session was healthy enough to accept the prompt, is
                        # promoted here.
                        category = ErrorCategory.STALE_SESSION
                    stderr_text = getattr(exc, "stderr", None)
                    if isinstance(stderr_text, bytes):
                        stderr_text = stderr_text.decode(errors="replace")
                    raise SDKError(
                        _extract_error_detail(exc),
                        original=exc,
                        stderr=stderr_text,
                        category=category,
                        retries_attempted=attempt,
                        retries_exhausted=(attempt >= _MAX_RETRIES),
                        exit_code=_exit_code_of(exc),
                    ) from exc
                # Reached only when the guarded raise above did not fire — the
                # error was transient and retries remain; advance to the next
                # attempt.
                attempt += 1
    finally:
        if sem is not None:
            sem.release()
        if session_log:
            session_log.close()


async def _disconnect_best_effort(client: ClaudeSDKClient) -> None:
    """Best-effort teardown: disconnect *client*, swallowing any failure.

    Lifts the two identical ``with contextlib.suppress(Exception): await
    _disconnect_session_impl(client)`` blocks in ``_create_session_impl``'s
    cancel-recovery and resume-fallback arms into one helper. Suppression *is*
    the documented contract here — ``_disconnect_session_impl`` is finally-safe
    by construction (see its docstring) and both arms re-raise / retry the
    primary failure right after the teardown, so a secondary disconnect error
    must never displace it. Allowlisted by name in
    ``tests/test_no_silenced_exceptions.py`` rather than exempting the whole
    hundreds-of-lines ``_create_session_impl`` — see the fence's
    ``suppress`` arm.
    """
    with contextlib.suppress(Exception):
        await _disconnect_session_impl(client)


async def _disconnect_session_impl(client: ClaudeSDKClient) -> None:
    """Disconnect *client* and drain residual SDK orphan tasks (#1719).

    Wraps ``client.disconnect()`` with the two-stage drain that
    eliminates the recurring SDK orphan-task crash class (prior PRs
    #1648, #1643, #1656, #1696, #1704 each added containment for one
    symptom; this drain stops the producer at the SDK boundary).

    The helper is **finally-safe** by construction: every existing call
    site wraps the bare ``disconnect()`` in
    ``contextlib.suppress(Exception)`` or ``try/except Exception:``, and
    the substitution must preserve that contract. ``disconnect()`` is
    wrapped in ``contextlib.suppress(Exception)`` here. The drains
    themselves are wrapped in ``except asyncio.CancelledError`` blocks
    that drain the current task's cancellation counter via
    ``uncancel()``: a residual orphan firing into the drain's own
    ``asyncio.wait`` would otherwise re-introduce exactly the leak this
    helper exists to eliminate. The same shape ``create_session``'s
    pre-connect cancel guard uses (#1696, sdk.py around line 2731).

    Drain ordering is **SDK-child first, athrow second**. ``Query.close()``
    cancels child tasks (``stream_input``, ``wait_for_result_and_end_input``,
    ``_handle_control_request``) without awaiting them; their cancel
    scopes can spawn ``async_generator_athrow`` tasks for inner generators
    as they unwind. Draining the child tasks first lets those athrow
    tasks materialize before the athrow drain scans the loop, so the
    pair fully neutralizes the orphan chain.

    **A cancel already in flight at entry is preserved (#1721).** The
    caller's ``cancelling()`` baseline is captured before either drain
    and passed to :func:`_absorb_drain_cancel` as its ``floor``. A
    residual orphan cancel arising *during* a drain is still absorbed,
    but a cancel that a real shutdown (loop stop, ``KeyboardInterrupt``)
    had already delivered before this helper ran survives the cleanup
    and re-fires at the caller's next checkpoint — so a legitimate
    external cancel is not silently swallowed. Only residual cancels
    that arise during the drains are absorbed.
    """
    current = asyncio.current_task()
    entry_cancels = current.cancelling() if current is not None else 0
    with contextlib.suppress(Exception):
        await client.disconnect()
    try:
        await _drain_sdk_child_tasks(
            "sdk.disconnect_session",
            owner=getattr(client, _SESSION_CHILD_OWNER_ATTR, None),
        )
    except asyncio.CancelledError:
        _absorb_drain_cancel("sdk.disconnect_session", floor=entry_cancels)
    try:
        await _drain_athrow_tasks("sdk.disconnect_session post-child")
    except asyncio.CancelledError:
        _absorb_drain_cancel("sdk.disconnect_session post-child", floor=entry_cancels)


# --- Provider delegation seam (proposal 058, Phase 2 Step 5) -----------------
#
# The four public functions below are thin delegators that route every call
# through the active :class:`~maestro.providers.base.Provider`. The real bodies
# live above as ``_query_impl`` / ``_create_session_impl`` /
# ``_query_on_session_impl`` / ``_disconnect_session_impl`` (and the ~30 module
# globals they read stay co-located with them here — no relocation).
#
# ``get_active_provider`` is imported *lazily* inside each body, not at module
# top: ``providers.registry`` imports ``providers.anthropic`` which imports this
# module, so a top-level import would form the cycle
# ``sdk → providers.registry → providers.anthropic → sdk``. The default provider
# (``AnthropicProvider``) delegates straight back to the ``_*_impl`` bodies, so
# the inert default path is byte-identical to calling the impls directly.
#
# All delegation uses *attribute* calls (``get_active_provider().query(...)``),
# never a bare ``query()``, so the session-attachment AST guard
# (``tests/test_structural_guards.py``) is satisfied for free. Keeping the
# public names patchable also preserves every existing ``patch("maestro.sdk.query")``
# mock boundary across the suite (the mock now replaces the delegator).
#
# Per-phase routing (proposal 058 Step 6's runtime half; the proposal 067
# prerequisite): ``query`` / ``create_session`` take an optional ``phase`` and
# resolve the provider through ``get_phase_provider(phase)`` — which honours the
# ``[sdk] <phase>_provider`` override map and falls back to the active provider
# for ``None`` / un-overridden phases, so the default path is unchanged.
# ``create_session`` then *tags* the returned session with the provider that
# built it (``_SESSION_PROVIDER_ATTR``); ``query_on_session`` /
# ``disconnect_session`` read that tag via ``_provider_for_session`` so a
# follow-up turn (or a cross-phase teardown — e.g. the contemplation session
# disconnected during the implementation phase) always routes back to the same
# provider, independent of which phase is current when it runs.

#: Sidecar attribute name recording which :class:`~maestro.providers.base.Provider`
#: opened a session. Set by :func:`create_session`, read by
#: :func:`query_on_session` / :func:`disconnect_session`. Sits alongside the
#: other ``_maestro_*`` session sidecars (session_id, model, betas) the seam
#: already attaches; both concrete session types (Anthropic's ``ClaudeSDKClient``
#: and cli_paced's ``_CLISession`` dataclass) accept attribute assignment.
_SESSION_PROVIDER_ATTR = "_maestro_provider"

#: Sidecar attribute recording the effort a session was opened with under
#: ``[sdk] call_policy_resolution_enabled``. Provider-agnostic, distinct from
#: the Anthropic-only ``_maestro_effort`` the cost-sample writer reads.
_SESSION_EFFORT_ATTR = "_maestro_policy_effort"

#: Sidecar attribute recording the effective :class:`ToolSurface` a session
#: was opened with under ``[sdk] enforced_tool_surface_enabled``.
_SESSION_SURFACE_ATTR = "_maestro_policy_surface"

#: Sticky sidecar attribute: True once any prompt on the session held an
#: untrusted fence (proposal 150 step 4).
_SESSION_TAINT_ATTR = "_maestro_untrusted_input"

#: Session ids this process saw only fence-free prompts on. A resumed
#: conversation is clean only when its id is here: one that carried a fence,
#: or one opened before a restart, is stamped tainted (conservative).
_CLEAN_SESSION_IDS: dict[str, None] = {}
_CLEAN_SESSION_IDS_CAP = 1024

#: Sidecar attribute holding the id a session was resumed from, so a turn that
#: raises before the provider reports a session id still updates that id's
#: entry in :data:`_CLEAN_SESSION_IDS`.
_SESSION_RESUME_SID_ATTR = "_maestro_taint_sid"


def _record_session_taint(client: Any, tainted: bool) -> None:
    """Carry *client*'s taint with its session id so a resume inherits it."""
    session_dict = getattr(client, "__dict__", {})
    sid = session_dict.get("_maestro_last_session_id") or session_dict.get(
        _SESSION_RESUME_SID_ATTR,
    )
    if not isinstance(sid, str):
        return
    if tainted:
        _CLEAN_SESSION_IDS.pop(sid, None)
        return
    # Re-insert so eviction drops the least recently used id, not the oldest.
    _CLEAN_SESSION_IDS.pop(sid, None)
    _CLEAN_SESSION_IDS[sid] = None
    while len(_CLEAN_SESSION_IDS) > _CLEAN_SESSION_IDS_CAP:
        del _CLEAN_SESSION_IDS[next(iter(_CLEAN_SESSION_IDS))]


def _policy_effort(caller: str | None) -> str:
    """Return *caller*'s declared tier effort; ``ValueError`` if it has none."""
    # Lazy: ``maestro.call_policy`` imports this module at top level.
    from maestro.call_policy import tier_effort

    if caller is None:
        raise ValueError(
            "call_policy_resolution_enabled: SDK call carries no caller tag"
        )
    try:
        return tier_effort(caller)
    except KeyError:
        raise ValueError(
            f"call_policy_resolution_enabled: caller {caller!r} is not "
            "registered in CALL_POLICIES"
        ) from None


def _resolve_policy_options(options: QueryOptions | None) -> QueryOptions | None:
    """Return *options* with effort resolved from the registry under the flag.

    Flag off → *options* unchanged (same object). Flag on → a copy whose
    ``effort`` is the caller's tier effort; a missing options object, a missing
    caller, or an unregistered caller raises ``ValueError`` before dispatch.
    """
    if not _call_policy_resolution_enabled:
        return options
    if options is None:
        raise ValueError(
            "call_policy_resolution_enabled: SDK call carries no QueryOptions"
        )
    return replace(options, effort=_policy_effort(options.caller))


def _resolve_tool_surface_options(
    options: QueryOptions | None,
) -> QueryOptions | None:
    """Return *options* with ``tools`` derived from the caller's surface.

    Flag off → *options* unchanged (same object). Flag on → the copy
    :func:`maestro.tool_surface.resolve_call_options` returns, which raises
    ``ValueError`` before dispatch for a call its surface does not admit.
    """
    if _enforced_tool_surface_config is None:
        return options
    # Lazy: ``tool_surface`` reads ``call_policy``, which imports this module.
    from maestro.tool_surface import resolve_call_options

    return resolve_call_options(options, _enforced_tool_surface_config)


def _check_session_surface(client: object, caller: str | None) -> None:
    """Refuse a turn whose caller's surface differs from its session's tag."""
    from maestro.call_policy import CALL_POLICIES
    from maestro.tool_surface import RUNTIME_GROUNDED_SURFACES, effective_surface

    prefix = "enforced_tool_surface_enabled"
    if caller is None:
        raise ValueError(f"{prefix}: session turn carries no caller tag")
    if caller not in CALL_POLICIES:
        raise ValueError(
            f"{prefix}: caller {caller!r} is not registered in CALL_POLICIES"
        )
    instance_dict = getattr(client, "__dict__", None)
    tagged = (
        instance_dict.get(_SESSION_SURFACE_ATTR)
        if isinstance(instance_dict, dict)
        else None
    )
    if tagged is None:
        raise ValueError(
            f"{prefix}: caller {caller!r} turn on a session with no surface tag"
        )
    surface: ToolSurface = effective_surface(
        caller, grounded=tagged in RUNTIME_GROUNDED_SURFACES,
    )
    if surface != tagged:
        raise ValueError(
            f"{prefix}: caller {caller!r} surface {surface!r} differs from "
            f"its session's {tagged!r}"
        )


def _provider_for_session(client: object) -> Provider:
    """Return the provider that opened *client*, or the active provider.

    Reads the :data:`_SESSION_PROVIDER_ATTR` tag :func:`create_session` stamps on
    every session it opens. The tag is read from the instance ``__dict__`` rather
    than via ``getattr`` so a ``MagicMock`` test double — whose attribute access
    auto-vivifies a child mock for *any* name — does not masquerade as a tagged
    session; only a tag this seam actually stamped (a real instance attribute)
    counts. A session built outside this seam (an untagged real client, an
    ``object()`` with no ``__dict__``, or an auto-vivifying mock) falls back to
    :func:`get_active_provider`; under the default config the tag and the active
    provider are the same object, so the fallback is behaviour-preserving rather
    than a silent re-route.
    """
    from maestro.providers.registry import get_active_provider

    instance_dict = getattr(client, "__dict__", None)
    tagged = (
        instance_dict.get(_SESSION_PROVIDER_ATTR)
        if isinstance(instance_dict, dict)
        else None
    )
    if tagged is not None:
        return cast("Provider", tagged)
    return get_active_provider()


async def query(
    prompt: str,
    *,
    options: QueryOptions | None = None,
    system: str | None = None,
    complete_on: Callable[[Any, str], bool] | None = None,
    stream_idle_timeout_seconds: float | None = None,
    phase: str | None = None,
) -> QueryResult:
    """Run a one-shot query through the provider routed for *phase* (proposal 058).

    *phase* selects the provider via ``get_phase_provider`` (its
    ``[sdk] <phase>_provider`` override, else the active provider); ``None`` — the
    default — keeps the active-provider dispatch unchanged.
    """
    from maestro.providers.registry import get_phase_provider

    options = _resolve_policy_options(options)
    options = _resolve_tool_surface_options(options)
    result = await get_phase_provider(phase).query(
        prompt,
        options=options,
        system=system,
        complete_on=complete_on,
        stream_idle_timeout_seconds=stream_idle_timeout_seconds,
    )
    result.untrusted_input = prompt_holds_untrusted(prompt, system)
    return result


async def create_session(
    initial_prompt: str | None = None,
    *,
    options: QueryOptions | None = None,
    system: str | None = None,
    resume_session_id: str | None = None,
    phase: str | None = None,
) -> ClaudeSDKClient:
    """Open a session through the provider routed for *phase* (proposal 058).

    *phase* selects the provider via ``get_phase_provider`` so a
    ``[sdk] <phase>_provider`` override actually serves the phase's session;
    ``None`` keeps the active-provider default. The opened session is tagged with
    its provider so :func:`query_on_session` / :func:`disconnect_session` route
    every later operation back to it.
    """
    from maestro.providers.registry import get_phase_provider

    # The provider contract returns the opaque ``Session`` (``Any``); the public
    # ``create_session`` keeps its concrete ``ClaudeSDKClient`` annotation (the
    # active Anthropic provider returns exactly that), so bridge with a cast.
    options = _resolve_policy_options(options)
    options = _resolve_tool_surface_options(options)
    provider = get_phase_provider(phase)
    session = await provider.create_session(
        initial_prompt,
        options=options,
        system=system,
        resume_session_id=resume_session_id,
    )
    # Tag the session with its opening provider so follow-up turns and teardown
    # route to the same provider regardless of the active/global selection or
    # the phase current when they run (``Session`` is ``Any``; the attribute
    # assignment needs no cast).
    setattr(session, _SESSION_PROVIDER_ATTR, provider)
    session_tainted = derive_taint(
        prompt_holds_untrusted(initial_prompt, system),
        resume_session_id is not None
        and resume_session_id not in _CLEAN_SESSION_IDS,
    )
    setattr(session, _SESSION_TAINT_ATTR, session_tainted)
    if resume_session_id is not None:
        setattr(session, _SESSION_RESUME_SID_ATTR, resume_session_id)
    _record_session_taint(session, session_tainted)
    if options is not None and _call_policy_resolution_enabled:
        setattr(session, _SESSION_EFFORT_ATTR, options.effort)
    if options is not None and _enforced_tool_surface_config is not None:
        from maestro.tool_surface import call_effective_surface

        # ``resolve_call_options`` already refused a missing/unregistered caller.
        setattr(
            session,
            _SESSION_SURFACE_ATTR,
            call_effective_surface(cast(str, options.caller), options),
        )
    return cast(ClaudeSDKClient, session)


async def query_on_session(
    client: ClaudeSDKClient,
    prompt: str,
    *,
    caller: str | None = None,
    complete_on: Callable[[Any, str], bool] | None = None,
    stream_idle_timeout_seconds: float | None = None,
    bypass_warning_throttle: bool = False,
) -> QueryResult:
    """Send a follow-up turn on *client* through *client*'s provider (proposal 058).

    Routes to the provider that opened *client* (the per-phase tag set by
    :func:`create_session`), falling back to the active provider for an untagged
    session.

    ``bypass_warning_throttle`` mirrors :attr:`QueryOptions.bypass_warning_throttle`
    for the session path: the gate-convergence fix loop passes ``True`` so its
    must-finish grind skips the soft per-call warning-throttle sleep. Only the
    Anthropic provider drives that throttle; the others accept and ignore it.

    Under ``[sdk] call_policy_resolution_enabled`` a turn whose caller is
    missing or unregistered, or whose tier effort differs from the effort the
    session was tagged with at open (or an untagged session), raises
    ``ValueError`` before dispatch instead of silently running at the
    session's effort. Likewise under ``[sdk] enforced_tool_surface_enabled``
    for a turn whose caller's effective tool surface differs from the one the
    session was opened with.
    """
    if _enforced_tool_surface_config is not None:
        _check_session_surface(client, caller)
    if _call_policy_resolution_enabled:
        effort = _policy_effort(caller)
        instance_dict = getattr(client, "__dict__", None)
        tagged = (
            instance_dict.get(_SESSION_EFFORT_ATTR)
            if isinstance(instance_dict, dict)
            else None
        )
        if tagged != effort:
            raise ValueError(
                f"call_policy_resolution_enabled: caller {caller!r} declares "
                f"effort {effort!r} but its session was opened at {tagged!r}"
            )
    # Stamp the sticky tag before dispatch: a turn that raises after its
    # fenced prompt was sent still leaves the conversation holding the fence,
    # and resume-in-place reuses this client. Read and write via the instance
    # ``__dict__`` so an untagged ``MagicMock`` session reads False rather than
    # an auto-created attribute; a slotted session reads a tag
    # ``create_session`` set through its slot, but cannot record a new one.
    session_dict = getattr(client, "__dict__", None)
    if isinstance(session_dict, dict):
        tainted = derive_taint(
            session_dict.get(_SESSION_TAINT_ATTR) is True,
            prompt_holds_untrusted(prompt),
        )
        session_dict[_SESSION_TAINT_ATTR] = tainted
    else:
        tainted = derive_taint(
            getattr(client, _SESSION_TAINT_ATTR, False) is True,
            prompt_holds_untrusted(prompt),
        )
    try:
        result = await _provider_for_session(client).query_on_session(
            client,
            prompt,
            caller=caller,
            complete_on=complete_on,
            stream_idle_timeout_seconds=stream_idle_timeout_seconds,
            bypass_warning_throttle=bypass_warning_throttle,
        )
    finally:
        _record_session_taint(client, tainted)
    result.untrusted_input = tainted
    return result


async def disconnect_session(client: ClaudeSDKClient) -> None:
    """Tear down *client* through *client*'s provider (proposal 058).

    Uses the per-phase tag :func:`create_session` set so a session opened by a
    per-phase override is torn down by that same provider even when a different
    phase is current at teardown.
    """
    await _provider_for_session(client).disconnect_session(client)


def latest_context_window() -> ContextWindowSnapshot | None:
    """Return the active provider's freshest context-window composition, if any.

    Read-only companion to the four delegators above (proposal 062 Step 4):
    surfaces the most recent slash-command ``/context`` probe reading so
    :func:`maestro.diagnostics.build_diagnostic_snapshot` can fold it into a
    :class:`~maestro.diagnostics.DiagnosticSnapshot`. Only ``cli_paced`` records
    one; every other provider — the default ``anthropic`` path — lacks the
    accessor, so this returns ``None`` and ``blind_spots()`` names the gap. The
    ``getattr`` probe keeps the optional capability off the shared
    :class:`~maestro.providers.base.Provider` protocol, so no other provider has
    to grow a stub.
    """
    from maestro.providers.registry import get_active_provider

    getter = getattr(get_active_provider(), "latest_context_window", None)
    if getter is None:
        return None
    return cast("ContextWindowSnapshot | None", getter())


def latest_plan_headroom() -> PlanHeadroom | None:
    """Return the active provider's freshest plan rate-limit headroom, if any.

    The Step 5 sibling of :func:`latest_context_window` (proposal 062):
    surfaces the most recent slash-command ``/usage`` probe reading — the one
    session signal neither the API response nor the transcript sidecar exposes
    — so the burn-rate throttle can report it on transitions. Read-only by
    contract: consuming it in the throttle's gating math is explicitly
    deferred. Only ``cli_paced`` records one; every other provider — the
    default ``anthropic`` path — lacks the accessor, so this returns ``None``.
    The ``getattr`` probe keeps the optional capability off the shared
    :class:`~maestro.providers.base.Provider` protocol, so no other provider
    has to grow a stub.
    """
    from maestro.providers.registry import get_active_provider

    getter = getattr(get_active_provider(), "latest_plan_headroom", None)
    if getter is None:
        return None
    return cast("PlanHeadroom | None", getter())


def _absorb_drain_cancel(label: str, floor: int = 0) -> None:
    """Drain the caller's stacked cancellation counter above ``floor`` (#1719, #1721).

    A residual SDK orphan firing into one of ``disconnect_session``'s
    drains arrives as ``CancelledError`` on the *caller's* task, even
    though the cancel originates from the orphan, not from any external
    party. Without an ``uncancel()`` drain, the residual cancels
    re-deliver at the next ``await`` checkpoint and contaminate the rest
    of the task's lifetime — precisely the failure mode #1696 fixed for
    ``create_session``'s pre-connect drain. Mirrored here for the
    cleanup path's drains.

    Only cancels *above* ``floor`` are absorbed. ``floor`` is the count
    of cancellations that must be preserved: ``_disconnect_session_impl``
    passes the caller's entry-time ``cancelling()`` baseline so a cancel
    already in flight when cleanup began (a real shutdown) survives to
    re-fire at the next checkpoint, while cancels arising purely during
    the drains are still drained away (#1721). The default ``floor=0``
    keeps the unconditional absorb ``create_session``'s post-cancel
    drain relies on (#1696).
    """
    current = asyncio.current_task()
    if current is None:
        return
    drained = 0
    while current.cancelling() > floor:
        current.uncancel()
        drained += 1
    if drained:
        logger.warning(
            "Drained %d residual cancel(s) from %s — CancelledError "
            "absorbed during cleanup",
            drained,
            label,
        )
