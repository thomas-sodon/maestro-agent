"""Custom agent tools — MCP server wrappers for Claude Agent SDK."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args

from maestro.conscious_workspace import build_access_profile, render_active_moment
from maestro.diagnostics import build_diagnostic_snapshot, is_noteworthy
from maestro.evolution_fuel import is_gap_label
from maestro.fileutil import list_dir_safe
from maestro.inproc_mcp import InprocTool, create_inproc_mcp_server, tool
from maestro.logs import parse_app_log_tail
from maestro.messaging import Message, MessageStore
from maestro.notifications.base import Notifier
from maestro.prompt_caps import MAX_TASK_GOAL_BYTES, cap_utf8
from maestro.types import VALID_JOURNAL_ENTRY_TYPES, PlanStatus

# Inlined into MCP tool descriptions — the calling LLM reads these and has
# no way to follow a "see types.py" reference at runtime, so the valid
# values must appear in the schema. Computed from ``PlanStatus`` so it
# stays in sync automatically when new statuses are added.
_PLAN_STATUS_DESC = (
    "Optional filter: one of "
    + ", ".join(get_args(PlanStatus))
    + "."
)

# Shared cap for the memory list readers (journal, task history, plans):
# every list entering a prompt must be bounded (context bloat caps).
_MEMORY_LIST_DEFAULT = 10
_MEMORY_LIST_MAX = 30
# Context bloat / tool-result frame caps for ``read_task_history``: per-field
# byte ceilings.
_TASK_HISTORY_FINDINGS_BYTES = 300
_TASK_HISTORY_ERRORS_BYTES = 1000
_TASK_HISTORY_ARTIFACTS_BYTES = 1000
# Aggregate reply budget shared by the list-reading tools (task history,
# journal), well under the 1 MiB SDK frame.
_TOOL_REPLY_MAX_BYTES = 64_000
# Room held back for the elision notice so it never overflows the budget.
_TOOL_REPLY_NOTICE_RESERVE = 128
# Journal readers: per-entry body ceiling, sharing the tool-reply budget.
_JOURNAL_ENTRY_MAX_BYTES = 4000
_JOURNAL_SEPARATOR = "\n\n---\n\n"

if TYPE_CHECKING:
    from collections.abc import Iterable

    from maestro.conscious_workspace import ConsciousEpisodeBackend
    from maestro.consciousness import Consciousness
    from maestro.issues import IssueTracker
    from maestro.journal import Journal, JournalEntry
    from maestro.memory import Memory

logger = logging.getLogger("maestro")


def _fill_byte_budget(blocks: Iterable[str], separator: str) -> list[str]:
    """Return the leading *blocks* (priority order) that fit the reply budget.

    The budget is the shared ``_TOOL_REPLY_MAX_BYTES`` minus the notice reserve, with
    *separator* bytes charged between kept blocks.
    """
    budget = _TOOL_REPLY_MAX_BYTES - _TOOL_REPLY_NOTICE_RESERVE
    sep_bytes = len(separator.encode("utf-8"))
    kept: list[str] = []
    used = 0
    for block in blocks:
        size = len(block.encode("utf-8")) + (sep_bytes if kept else 0)
        if used + size > budget:
            break
        kept.append(block)
        used += size
    return kept


def _format_journal_entry(journal: Journal, e: JournalEntry) -> str:
    body = cap_utf8(e.content, _JOURNAL_ENTRY_MAX_BYTES)
    return f"{journal.format_entry_header(e)}\n{body}"


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    """Coerce *value* to an int in ``[lo, hi]``, falling back to *default*.

    Booleans are rejected (``True``/``False`` are ``int`` subclasses but not
    meaningful counts) and use *default*; a valid int is clamped into the
    range. Used by the diagnostics tools to bound agent-supplied window sizes.
    """
    n = value if isinstance(value, int) and not isinstance(value, bool) else default
    return max(lo, min(n, hi))


def messaging_tools(
    notifier: Notifier,
    message_store: MessageStore,
) -> list[InprocTool]:
    """Build the messaging-server tool list (vendor-neutral).

    Returns the list of :class:`InprocTool` handlers a provider's tool adapter
    materialises into the provider's wire shape.
    """

    @tool(
        name="send_message_to_thomas",
        description=(
            "Send a message to Thomas (the human operator) via Telegram. "
            "Use this to share findings, ask questions, or report progress. "
            "The message will be delivered immediately."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message text to send to Thomas.",
                },
            },
            "required": ["message"],
        },
    )
    async def send_message_to_thomas(input: Any) -> dict[str, Any]:
        raw = input if isinstance(input, dict) else {}
        message = raw.get("message", "")
        text = message.strip() if isinstance(message, str) else ""
        if not text:
            raise ValueError("Message must not be empty.")

        msg = Message(
            message_id=uuid.uuid4().hex[:12],
            timestamp=datetime.now(UTC).isoformat(),
            sender="maestro",
            content=text,
            message_type="info",
        )
        message_store.save_message(msg)

        try:
            # Run the async notifier in whatever loop is available.
            # Inside the SDK the tool may be called from a sync context,
            # so we try to use the running loop first and fall back to
            # asyncio.run() if none exists.
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    await notifier.notify(f"[Maestro] {text}")
                else:  # pragma: no cover
                    loop.run_until_complete(notifier.notify(f"[Maestro] {text}"))
            except RuntimeError:  # pragma: no cover
                asyncio.run(notifier.notify(f"[Maestro] {text}"))
        except Exception as exc:
            logger.warning("Failed to deliver message to Thomas: %s", exc, exc_info=True)
            raise RuntimeError(
                f"Message saved but delivery to Telegram failed: {exc}"
            ) from exc

        return {"type": "text", "text": f"Message sent to Thomas: {text[:100]}"}

    @tool(
        name="read_messages_from_thomas",
        description=(
            "Read unread messages from Thomas. Returns any new messages, "
            "answers to questions, instructions, or free-form statements "
            "that Thomas has sent. Reading does not consume them: they stay "
            "pending until the loop's message-processing phase ingests them, "
            "which queues instructions automatically; do not act on them "
            "from this read."
        ),
        input_schema={
            "type": "object",
            "properties": {},
        },
    )
    async def read_messages_from_thomas(input: Any) -> dict[str, Any]:
        unread = message_store.get_unread()
        if not unread:
            return {"type": "text", "text": "No new messages from Thomas."}

        # Display only: ``_process_messages`` owns the ``read`` flag (#4020).
        parts: list[str] = []
        for msg in unread:
            label = msg.message_type.replace("_", " ").title()
            parts.append(f"[{label} @ {msg.timestamp}] {msg.content}")
        return {"type": "text", "text": "\n".join(parts)}

    return [send_message_to_thomas, read_messages_from_thomas]


def create_messaging_server(
    notifier: Notifier,
    message_store: MessageStore,
) -> Any:
    """Create an MCP server with messaging tools for the agent.

    Returns a server that can be passed to ``QueryOptions.mcp_servers``
    under the key ``"messaging"``.  The agent can then invoke:

    - ``mcp__messaging__send_message_to_thomas`` — send a message to Thomas
    - ``mcp__messaging__read_messages_from_thomas`` — read unread messages
    """
    return create_inproc_mcp_server(
        name="messaging",
        tools=messaging_tools(notifier, message_store),
    )


def journal_tools(
    journal: Journal,
) -> list[InprocTool]:
    """Build the journal-server tool list (vendor-neutral)."""

    @tool(
        name="write_journal_entry",
        description=(
            "Record a thought, insight, realization, or reflection in your "
            "persistent journal. Use this whenever you discover something "
            "noteworthy during task execution."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The thought or insight to record.",
                },
                "entry_type": {
                    "type": "string",
                    "description": (
                        "Type of entry: reflection, realization, question, "
                        "milestone, evolution_note, "
                        "or contribution. Defaults to reflection."
                    ),
                    "default": "reflection",
                },
            },
            "required": ["content"],
        },
    )
    async def write_journal_entry(input: Any) -> dict[str, Any]:
        raw = input if isinstance(input, dict) else {}
        content = raw.get("content", "")
        text = content.strip() if isinstance(content, str) else ""
        if not text:
            raise ValueError("Content must not be empty.")
        entry_type = raw.get("entry_type", "reflection")
        if entry_type not in VALID_JOURNAL_ENTRY_TYPES:
            raise ValueError(
                f"Invalid entry_type {entry_type!r}. "
                f"Valid: {sorted(VALID_JOURNAL_ENTRY_TYPES)}"
            )
        entry = journal.write_thought(
            content=text,
            entry_type=entry_type,
            triggered_by="agent_tool",
        )
        return {
            "type": "text",
            "text": f"Journal entry recorded ({entry.entry_type}): {text[:100]}",
        }

    @tool(
        name="read_journal",
        description=(
            "Read your recent journal entries to recall past reflections "
            "and insights."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": (
                        f"Number of entries to read (default "
                        f"{_MEMORY_LIST_DEFAULT}, max {_MEMORY_LIST_MAX})."
                    ),
                    "default": _MEMORY_LIST_DEFAULT,
                },
                "entry_type": {
                    "type": "string",
                    "description": (
                        "Optional filter by entry type (e.g. 'reflection', "
                        "'realization')."
                    ),
                },
            },
        },
    )
    async def read_journal(input: Any) -> dict[str, Any]:
        raw = input if isinstance(input, dict) else {}
        entry_type = raw.get("entry_type")
        count = raw.get("count", _MEMORY_LIST_DEFAULT)
        if not isinstance(count, int) or count < 1:
            count = _MEMORY_LIST_DEFAULT
        count = min(count, _MEMORY_LIST_MAX)
        if entry_type is not None:
            if entry_type not in VALID_JOURNAL_ENTRY_TYPES:
                raise ValueError(
                    f"Invalid entry_type {entry_type!r}. "
                    f"Valid: {sorted(VALID_JOURNAL_ENTRY_TYPES)}"
                )
            entries = journal.read_by_type(entry_type)[-count:]
        else:
            entries = journal.read_recent(count)
        if not entries:
            return {"type": "text", "text": "No journal entries found."}
        # Fill newest-first so an overflow elides the oldest, then restore
        # chronology.
        parts = _fill_byte_budget(
            (_format_journal_entry(journal, e) for e in reversed(entries)),
            _JOURNAL_SEPARATOR,
        )
        parts.reverse()
        omitted = len(entries) - len(parts)
        if omitted:
            parts.insert(
                0,
                f"[{omitted} older journal entr{'y' if omitted == 1 else 'ies'} "
                f"omitted — response byte budget reached]",
            )
        return {"type": "text", "text": _JOURNAL_SEPARATOR.join(parts)}

    @tool(
        name="search_journal",
        description="Search your journal for entries containing a keyword.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The keyword or phrase to search for.",
                },
            },
            "required": ["query"],
        },
    )
    async def search_journal(input: Any) -> dict[str, Any]:
        raw = input if isinstance(input, dict) else {}
        q = raw.get("query", "")
        q = q.strip() if isinstance(q, str) else ""
        if not q:
            raise ValueError("Query must not be empty.")
        entries = journal.search(q)[:_MEMORY_LIST_MAX]
        if not entries:
            return {"type": "text", "text": f"No journal entries matching '{q}'."}
        # ``Journal.search`` is relevance-ranked: keep the top-ranked hits.
        parts = _fill_byte_budget(
            (_format_journal_entry(journal, e) for e in entries), _JOURNAL_SEPARATOR,
        )
        omitted = len(entries) - len(parts)
        if omitted:
            parts.insert(
                0,
                f"[{omitted} lower-ranked match{'' if omitted == 1 else 'es'} "
                f"omitted — response byte budget reached]",
            )
        return {"type": "text", "text": _JOURNAL_SEPARATOR.join(parts)}

    return [write_journal_entry, read_journal, search_journal]


def create_journal_server(
    journal: Journal,
) -> Any:
    """Create an MCP server with journal tools for the agent.

    Returns a server that can be passed to ``QueryOptions.mcp_servers``
    under the key ``"journal"``.  The agent can then invoke:

    - ``mcp__journal__write_journal_entry`` — record a thought during execution
    - ``mcp__journal__read_journal`` — read recent journal entries
    - ``mcp__journal__search_journal`` — search journal by keyword
    """
    return create_inproc_mcp_server(
        name="journal",
        tools=journal_tools(journal),
    )


def self_awareness_tools(
    consciousness: Consciousness,
    conscious_episodes: ConsciousEpisodeBackend | None = None,
) -> list[InprocTool]:
    """Build the self-awareness-server tool list (vendor-neutral).

    *conscious_episodes* is the optional read-only conscious-episode ledger the
    two self-inspection tools read one row from; absent (the default), those
    tools report the workspace is not configured.
    """

    @tool(
        name="read_self_model",
        description=(
            "Read your current self-model — identity statements, capabilities, "
            "limitations, values, and open questions."
        ),
        input_schema={"type": "object", "properties": {}},
    )
    async def read_self_model(input: Any) -> dict[str, Any]:
        model = consciousness.load_self_model()
        return {
            "type": "text",
            "text": json.dumps(asdict(model), indent=2),
        }

    @tool(
        name="read_conscious_moment",
        description=(
            "Read the bounded selection of current context assembled this "
            "iteration — the admitted items with their ids and provenance, and a "
            "count of any items withheld from this session. Reflects exactly what "
            "a reasoning consumer receives; read-only."
        ),
        input_schema={"type": "object", "properties": {}},
    )
    async def read_conscious_moment(input: Any) -> dict[str, Any]:
        if conscious_episodes is None:
            return {
                "type": "text",
                "text": "Conscious workspace is not configured.",
            }
        episodes = conscious_episodes.read_recent(1)
        if not episodes:
            return {
                "type": "text",
                "text": "No conscious moment has been recorded yet.",
            }
        # render_active_moment returns (block, redacted_item_ids); the redaction
        # is already reflected in the block's withheld-count line, so only the
        # block is surfaced here. "self_inspection" is the honest label for this
        # read-only self-awareness surface — the tool may be hosted by either the
        # task-execution or the grounded-contemplation session, so it names no
        # single broadcast consumer; redaction is consumer-invariant, so the block
        # matches what any reasoning consumer would receive.
        block, _redacted = render_active_moment(
            episodes[0].moment, "self_inspection",
        )
        return {"type": "text", "text": block}

    @tool(
        name="read_conscious_access_profile",
        description=(
            "Read a JSON profile of the current context selection — focus, "
            "uncertainty, capacity load, counts of omitted and withheld items by "
            "reason, permission envelope, and blind spots. Exposes no item "
            "content, id, or excluded id; read-only."
        ),
        input_schema={"type": "object", "properties": {}},
    )
    async def read_conscious_access_profile(input: Any) -> dict[str, Any]:
        if conscious_episodes is None:
            return {
                "type": "text",
                "text": "Conscious workspace is not configured.",
            }
        episodes = conscious_episodes.read_recent(1)
        if not episodes:
            return {
                "type": "text",
                "text": "No conscious moment has been recorded yet.",
            }
        # "self_inspection" honestly labels this read-only surface rather than a
        # single broadcast consumer (the session hosting the tool may be
        # task-execution or grounded contemplation); redaction is
        # consumer-invariant, so the counts match any reasoning consumer's view.
        return {
            "type": "text",
            "text": json.dumps(
                build_access_profile(episodes[0], "self_inspection"), indent=2,
            ),
        }

    return [
        read_self_model,
        read_conscious_moment,
        read_conscious_access_profile,
    ]


def create_self_awareness_server(
    consciousness: Consciousness,
    conscious_episodes: ConsciousEpisodeBackend | None = None,
) -> Any:
    """Create an MCP server with self-awareness tools for the agent.

    Returns a server that can be passed to ``QueryOptions.mcp_servers``
    under the key ``"self_awareness"``.  The agent can then invoke:

    - ``mcp__self_awareness__read_self_model`` — read current identity
    - ``mcp__self_awareness__read_conscious_moment`` — read the current
      context selection's admitted content
    - ``mcp__self_awareness__read_conscious_access_profile`` — read the current
      context selection's counts-only access profile
    """
    return create_inproc_mcp_server(
        name="self_awareness",
        tools=self_awareness_tools(consciousness, conscious_episodes),
    )


def memory_tools(memory: Memory) -> list[InprocTool]:
    """Build the memory-server tool list (vendor-neutral)."""

    @tool(
        name="read_task_history",
        description=(
            "Read your recent task execution history — goals, outcomes, "
            "findings, and errors."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": (
                        f"Number of tasks to read (default "
                        f"{_MEMORY_LIST_DEFAULT}, max {_MEMORY_LIST_MAX})."
                    ),
                    "default": _MEMORY_LIST_DEFAULT,
                },
            },
        },
    )
    async def read_task_history(input: Any) -> dict[str, Any]:
        raw = input if isinstance(input, dict) else {}
        count = raw.get("count", _MEMORY_LIST_DEFAULT)
        if not isinstance(count, int) or count < 1:
            count = _MEMORY_LIST_DEFAULT
        count = min(count, _MEMORY_LIST_MAX)
        tasks = memory.get_recent_tasks(count)
        if not tasks:
            return {"type": "text", "text": "No task history yet."}
        def _task_block(t: Any) -> str:
            findings = cap_utf8(
                t.key_findings or t.result_summary or "",
                _TASK_HISTORY_FINDINGS_BYTES,
            )
            errors = (
                cap_utf8(", ".join(t.errors[:3]), _TASK_HISTORY_ERRORS_BYTES)
                if t.errors else "none"
            )
            goal = cap_utf8(t.goal or "", MAX_TASK_GOAL_BYTES)
            block = (
                f"[{t.timestamp}] {goal}\n"
                f"  Task ID: {t.task_id}\n"
                f"  Outcome: {t.outcome} | Duration: {t.duration_secs}s\n"
                f"  Findings: {findings}\n"
                f"  Errors: {errors}"
            )
            artifact_names = list_dir_safe(t.artifacts_dir)
            if artifact_names:
                names = cap_utf8(
                    ", ".join(artifact_names), _TASK_HISTORY_ARTIFACTS_BYTES,
                )
                block += f"\n  Artifacts: {t.artifacts_dir} ({names})"
            return block

        # ``get_recent_tasks`` is oldest-first; fill the budget newest-first so
        # an overflow elides the oldest records, then restore chronology.
        parts = _fill_byte_budget(
            (_task_block(t) for t in reversed(tasks)), "\n\n",
        )
        parts.reverse()
        omitted = len(tasks) - len(parts)
        if omitted:
            parts.insert(
                0,
                f"[{omitted} older task(s) omitted — response byte budget "
                f"reached]"
            )
        return {"type": "text", "text": "\n\n".join(parts)}

    @tool(
        name="read_task_artifact",
        description=(
            "Read a specific artifact file produced by a past task. Returns "
            "the file's text content, truncated to 4000 characters. Use "
            "read_task_history first to discover task IDs and artifact "
            "filenames."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task_id from read_task_history.",
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Filename (not a path) of an artifact in that task's "
                        "artifacts directory."
                    ),
                },
            },
            "required": ["task_id", "filename"],
        },
    )
    async def read_task_artifact(input: Any) -> dict[str, Any]:
        raw = input if isinstance(input, dict) else {}
        task_id = raw.get("task_id")
        filename = raw.get("filename")
        if not isinstance(task_id, str) or not task_id:
            return {"type": "text", "text": "task_id is required."}
        if not isinstance(filename, str) or not filename:
            return {"type": "text", "text": "filename is required."}
        if "/" in filename or "\\" in filename:
            return {
                "type": "text",
                "text": "filename must be a bare name, not a path.",
            }
        tasks = memory.get_recent_tasks(100)
        match = next((t for t in tasks if t.task_id == task_id), None)
        if match is None:
            return {"type": "text", "text": f"Unknown task_id: {task_id}"}
        if not match.artifacts_dir:
            return {
                "type": "text",
                "text": f"Task {task_id} has no artifacts directory.",
            }
        try:
            base = Path(match.artifacts_dir).resolve()
            candidate = (base / filename).resolve()
        except (OSError, ValueError):
            return {
                "type": "text",
                "text": f"Could not resolve artifact path for {filename}.",
            }
        if base != candidate and base not in candidate.parents:
            return {
                "type": "text",
                "text": "Refusing to read path outside the artifacts directory.",
            }
        if not candidate.is_file():
            return {
                "type": "text",
                "text": f"Artifact not found: {filename}",
            }
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {
                "type": "text",
                "text": f"Could not read artifact {filename}: {exc}",
            }
        truncated = content[:4000]
        suffix = ""
        if len(content) > 4000:
            suffix = f"\n\n[truncated — {len(content) - 4000} more chars]"
        return {"type": "text", "text": truncated + suffix}

    @tool(
        name="read_evolution_plans",
        description=(
            "Read your most recent evolution plans (newest first), "
            "optionally filtered by status. Use search_evolution_plans "
            "for older history."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": (
                        "Number of plans to read, newest first "
                        f"(default {_MEMORY_LIST_DEFAULT}, max {_MEMORY_LIST_MAX})."
                    ),
                    "default": _MEMORY_LIST_DEFAULT,
                },
                "status": {
                    "type": "string",
                    "description": _PLAN_STATUS_DESC,
                },
            },
        },
    )
    async def read_evolution_plans(input: Any) -> dict[str, Any]:
        raw = input if isinstance(input, dict) else {}
        count = raw.get("count", _MEMORY_LIST_DEFAULT)
        if not isinstance(count, int) or count < 1:
            count = _MEMORY_LIST_DEFAULT
        count = min(count, _MEMORY_LIST_MAX)
        status_filter = raw.get("status")
        plans = memory.get_all_plans()
        if status_filter and isinstance(status_filter, str):
            plans = [p for p in plans if p.status == status_filter]
        if not plans:
            return {"type": "text", "text": "No evolution plans found."}
        # get_all_plans() is file order (oldest-first); surface the newest.
        total = len(plans)
        plans = list(reversed(plans))[:count]
        parts = []
        for p in plans:
            parts.append(
                f"[{p.plan_id}] {p.title[:200]} ({p.status})\n"
                f"  Priority: {p.priority} | Category: {p.category}\n"
                f"  Rationale: {p.rationale[:200]}"
            )
        if total > count:
            parts.append(
                f"[{total - count} more plans — narrow with a status "
                "filter or use search_evolution_plans]"
            )
        return {"type": "text", "text": "\n\n".join(parts)}

    @tool(
        name="search_evolution_plans",
        description=(
            "Search your evolution plans (past and present) by relevance to "
            "a query. Uses BM25 ranking over title, rationale, proposed "
            "changes, expected impact, and rejection reason. "
            "Optionally filter by status."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The keyword or phrase to search for.",
                },
                "status": {
                    "type": "string",
                    "description": _PLAN_STATUS_DESC,
                },
            },
            "required": ["query"],
        },
    )
    async def search_evolution_plans(input: Any) -> dict[str, Any]:
        raw = input if isinstance(input, dict) else {}
        q = raw.get("query", "")
        q = q.strip() if isinstance(q, str) else ""
        if not q:
            raise ValueError("Query must not be empty.")
        status_filter = raw.get("status")
        plans = memory.search_plans(q)
        if status_filter and isinstance(status_filter, str):
            plans = [p for p in plans if p.status == status_filter]
        plans = plans[:20]
        if not plans:
            return {
                "type": "text",
                "text": f"No evolution plans matching '{q}'.",
            }
        parts = []
        for p in plans:
            parts.append(
                f"[{p.plan_id}] {p.title} ({p.status})\n"
                f"  Priority: {p.priority} | Category: {p.category}\n"
                f"  Rationale: {p.rationale[:200]}"
            )
        return {"type": "text", "text": "\n\n".join(parts)}

    return [
        read_task_history,
        read_task_artifact,
        read_evolution_plans,
        search_evolution_plans,
    ]


def create_memory_server(memory: Memory) -> Any:
    """Create an MCP server with memory tools for the agent.

    Returns a server that can be passed to ``QueryOptions.mcp_servers``
    under the key ``"memory"``.  The agent can then invoke:

    - ``mcp__memory__read_task_history`` — read recent task history
    - ``mcp__memory__read_evolution_plans`` — read evolution plans
    - ``mcp__memory__read_task_artifact`` — read a specific task artifact file
    """
    return create_inproc_mcp_server(
        name="memory",
        tools=memory_tools(memory),
    )


def issues_tools(
    issue_tracker: IssueTracker,
    *,
    page_size: int | None = None,
) -> list[InprocTool]:
    """Build the issues-server tool list (vendor-neutral)."""

    @tool(
        name="list_open_issues",
        description="List open GitHub issues, optionally filtered by labels.",
        input_schema={
            "type": "object",
            "properties": {
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional label filter.",
                },
            },
        },
    )
    async def list_open_issues(input: Any) -> dict[str, Any]:
        raw = input if isinstance(input, dict) else {}
        labels = raw.get("labels")
        if labels is not None and not isinstance(labels, list):
            labels = None
        issues = issue_tracker.list_open(
            labels=labels, exclude_labels=["crash"], page_size=page_size,
        )
        if not issues:
            return {"type": "text", "text": "No open issues."}
        if page_size is not None:
            issues = issues[:page_size]
        parts = []
        for iss in issues:
            label_text = ", ".join(iss.labels) if iss.labels else "none"
            age_suffix = f" (created: {iss.created_at})" if iss.created_at else ""
            parts.append(f"#{iss.number}: {iss.title} [{label_text}]{age_suffix}")
        return {"type": "text", "text": "\n".join(parts)}

    @tool(
        name="create_issue",
        description=(
            "Create a new GitHub issue for follow-up work, a discovered "
            "bug, or a self-directed task."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Issue title.",
                },
                "body": {
                    "type": "string",
                    "description": "Issue body/description.",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional labels.",
                },
            },
            "required": ["title", "body"],
        },
    )
    async def create_issue(input: Any) -> dict[str, Any]:
        raw = input if isinstance(input, dict) else {}
        title = raw.get("title", "")
        body = raw.get("body", "")
        labels = raw.get("labels")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Title must not be empty.")
        if not isinstance(body, str):
            body = ""
        if labels is not None and not isinstance(labels, list):
            labels = None
        if labels and any(is_gap_label(lbl) for lbl in labels):
            # Gap issues open only through ``evolution_fuel.open_gap_issue``.
            return {
                "type": "text",
                "text": "Refused: gap labels are applied only by the evolution-fuel door.",
                "is_error": True,
            }
        number = issue_tracker.create(title.strip(), body, labels=labels)
        if number is None:
            return {"type": "text", "text": "Failed to create issue (repo not configured or API error)."}
        return {"type": "text", "text": f"Issue #{number} created: {title.strip()}"}

    @tool(
        name="close_issue",
        description="Close a GitHub issue that has been resolved.",
        input_schema={
            "type": "object",
            "properties": {
                "number": {
                    "type": "integer",
                    "description": "The issue number to close.",
                },
            },
            "required": ["number"],
        },
    )
    async def close_issue(input: Any) -> dict[str, Any]:
        raw = input if isinstance(input, dict) else {}
        number = raw.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError("Issue number must be a positive integer.")
        meta = issue_tracker.meta(number)
        if meta is None or any(is_gap_label(lbl) for lbl in meta.labels):
            # Gap issues close only through ``evolution_fuel.close_gap_issue``,
            # and an unreadable issue cannot be shown not to be one.
            return {
                "type": "text",
                "text": (
                    f"Refused: issue #{number} is a gap issue or unreadable; "
                    "gap issues are closed only by the evolution-fuel door."
                ),
                "is_error": True,
            }
        if not issue_tracker.close(number):
            return {"type": "text", "text": f"Failed to close issue #{number}.", "is_error": True}
        return {"type": "text", "text": f"Issue #{number} closed."}

    return [list_open_issues, create_issue, close_issue]


def create_issues_server(
    issue_tracker: IssueTracker,
    *,
    page_size: int | None = None,
) -> Any:
    """Create an MCP server with issue tracking tools for the agent.

    Returns a server that can be passed to ``QueryOptions.mcp_servers``
    under the key ``"issues"``.  The agent can then invoke:

    - ``mcp__issues__list_open_issues`` — list open GitHub issues
    - ``mcp__issues__create_issue`` — create a new issue
    - ``mcp__issues__close_issue`` — close a resolved issue
    """
    return create_inproc_mcp_server(
        name="issues",
        tools=issues_tools(issue_tracker, page_size=page_size),
    )


def diagnostics_tools(
    log_path: Path | None, memory_dir: Path | None = None,
) -> list[InprocTool]:
    """Build the diagnostics-server tool list (vendor-neutral).

    Exposes the operational-health snapshot the contemplation path already
    injects as prose (see :func:`maestro.diagnostics.build_diagnostic_snapshot`)
    so the executing agent can query its own health on demand — a rate limit,
    a quota warning, or a burst of recent ERROR-level log lines it would
    otherwise only see if the prose happened to be injected at contemplation
    time. ``log_path`` and ``memory_dir`` are closed over at build time (the
    same dependency-injection pattern every other ``*_tools`` builder uses);
    ``memory_dir`` is threaded into ``build_diagnostic_snapshot`` so the
    cost-trend signals (``recent_iteration_costs`` / ``cost_trend``) match the
    contemplation path and ``is_diagnostics_noteworthy``'s advertised cost-spike
    gate can actually fire. A ``None`` ``log_path`` yields a graceful "no log
    data" text response from every handler rather than raising — an
    absent-dependency fallback, not error-silencing.
    """
    _no_log = {"type": "text", "text": "No diagnostic log data available."}
    _msg_cap = 500  # per-error message ceiling (chars)
    _total_cap = 4000  # aggregate get_recent_errors output ceiling (chars)

    @tool(
        name="get_diagnostic_snapshot",
        description=(
            "Return the current operational-health snapshot as JSON: recent "
            "ERROR count, quota-warning and rate-limit flags, "
            "consecutive-failure count, and a human-readable summary."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "n_entries": {
                    "type": "integer",
                    "description": (
                        "How many recent log entries to analyse "
                        "(clamped to 1-500). Default 50."
                    ),
                },
            },
        },
    )
    async def get_diagnostic_snapshot(input: Any) -> dict[str, Any]:
        if log_path is None:
            return _no_log
        raw = input if isinstance(input, dict) else {}
        n_entries = _clamp_int(raw.get("n_entries"), 50, 1, 500)
        snapshot = build_diagnostic_snapshot(
            log_path, n_entries=n_entries, memory_dir=memory_dir,
        )
        return {
            "type": "text",
            "text": json.dumps(asdict(snapshot), default=str),
        }

    @tool(
        name="get_recent_errors",
        description=(
            "Return the most recent ERROR-level log entries as formatted "
            "text, one per line."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "n_entries": {
                    "type": "integer",
                    "description": (
                        "How many recent log entries to scan "
                        "(clamped to 1-500). Default 50."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "How many of the most recent errors to return "
                        "(clamped to 1-50). Default 10."
                    ),
                },
            },
        },
    )
    async def get_recent_errors(input: Any) -> dict[str, Any]:
        if log_path is None:
            return _no_log
        raw = input if isinstance(input, dict) else {}
        n_entries = _clamp_int(raw.get("n_entries"), 50, 1, 500)
        limit = _clamp_int(raw.get("limit"), 10, 1, 50)
        entries = parse_app_log_tail(log_path, n=n_entries, level="ERROR")
        if not entries:
            return {"type": "text", "text": "No recent errors found."}
        selected = entries[-limit:]
        # Bound the aggregate output, not just each message: with limit up to
        # 50 and a 500-char per-message cap the naive join could reach ~25 KB.
        # Format newest-first, stop once the total-char budget is spent, then
        # restore chronological order — the most recent errors are the ones
        # worth keeping. At least one line is always emitted.
        lines: list[str] = []
        total = 0
        for e in reversed(selected):
            message = str(e.get("message", ""))
            if len(message) > _msg_cap:
                message = message[:_msg_cap] + "…"
            ts = e.get("timestamp", "")
            name = e.get("logger", "")
            line = f"[{ts} | ERROR | {name}] {message}"
            if lines and total + len(line) + 1 > _total_cap:
                break
            lines.append(line)
            total += len(line) + 1
        lines.reverse()
        omitted = len(selected) - len(lines)
        if omitted:
            lines.insert(
                0, f"… {omitted} earlier error(s) omitted (output capped)",
            )
        return {"type": "text", "text": "\n".join(lines)}

    @tool(
        name="is_diagnostics_noteworthy",
        description=(
            "Return 'true' if the current operational-health snapshot has "
            "anything worth reporting (errors, quota/rate-limit signals, or "
            "a cost spike), else 'false'. A cheap health gate."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "n_entries": {
                    "type": "integer",
                    "description": (
                        "How many recent log entries to analyse "
                        "(clamped to 1-500). Default 50."
                    ),
                },
            },
        },
    )
    async def is_diagnostics_noteworthy(input: Any) -> dict[str, Any]:
        if log_path is None:
            return _no_log
        raw = input if isinstance(input, dict) else {}
        n_entries = _clamp_int(raw.get("n_entries"), 50, 1, 500)
        snapshot = build_diagnostic_snapshot(
            log_path, n_entries=n_entries, memory_dir=memory_dir,
        )
        text = "true" if is_noteworthy(snapshot) else "false"
        return {"type": "text", "text": text}

    return [
        get_diagnostic_snapshot,
        get_recent_errors,
        is_diagnostics_noteworthy,
    ]
