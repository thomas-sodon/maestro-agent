"""Mutable on-disk queue of candidate tasks awaiting execution.

``generate_task`` enqueues vetted ideas here; ``_run_task_phase`` pops the
head every iteration. The queue is persisted as JSON (not JSONL) because
entries are pruned, reordered, and edited — JSONL's append-only invariant
would not fit. Reads and writes go through :func:`fileutil.atomic_write`
so a crash mid-write cannot corrupt the file.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

from maestro.fileutil import atomic_write, file_lock

logger = logging.getLogger("maestro")

#: Return type of a :meth:`TaskQueueBackend.mutate` callback.
T = TypeVar("T")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_task_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class QueuedTask:
    task_id: str = field(default_factory=_new_task_id)
    goal: str = ""
    linked_question: str | None = None
    rationale: str | None = None
    created_at: str = field(default_factory=_now_iso)
    last_touched_at: str = field(default_factory=_now_iso)
    # Optional binding success criteria forwarded into ``MaestroAgent.run``
    # so the criteria-anchored outcome classification path is reachable from
    # ``_run_task_phase`` (the only production task entry point). ``None``
    # preserves step-based classification for queued tasks that predate
    # criteria authoring.
    acceptance_criteria: list[str] | None = None
    # Conscious-access citation provenance (proposal 130): which conscious
    # *moment* the task-generation decision that enqueued this task saw, and
    # which admitted workspace *item IDs* it declared it used. Populated only
    # behind ``conscious_workspace_broadcast_enabled``; advisory only — a
    # citation grants no capability, alters no priority, and gates nothing.
    # ``None`` (never cited / legacy row) is distinct from ``[]`` (saw the
    # moment, cited no admitted item), so the load-cleaning helper preserves
    # that distinction (unlike ``_clean_acceptance_criteria``, which collapses
    # ``[]`` to ``None``). These ride the queue verbatim onto the completed
    # ``TaskRecord`` (memory.py) via ``MaestroAgent.run``.
    workspace_moment_id: str | None = None
    workspace_item_ids: list[str] | None = None


def _clean_acceptance_criteria(crit: object) -> list[str] | None:
    """Canonicalize an ``acceptance_criteria`` value.

    Drops non-string / blank entries and collapses an empty (or non-list)
    result to ``None`` so downstream ``if acceptance_criteria:`` checks treat
    malformed lists the same as a missing field. Defined once so both backends'
    cleaning paths share a single implementation.
    """
    if not isinstance(crit, list):
        return None
    cleaned = [c.strip() for c in crit if isinstance(c, str) and c.strip()]
    return cleaned or None


def _clean_workspace_item_ids(ids: object) -> list[str] | None:
    """Canonicalize a ``workspace_item_ids`` value, preserving ``None`` vs ``[]``.

    Drops non-string members of a present list but keeps an emptied list as
    ``[]``: the conscious-access citation (proposal 130) distinguishes ``None``
    (never cited / legacy row) from ``[]`` (the decision saw the moment but
    cited no admitted item). So — unlike :func:`_clean_acceptance_criteria`,
    which collapses ``[]`` to ``None`` — an empty result is retained here. A
    present non-list collapses to ``None``. Ids are copied verbatim (not
    stripped): they are opaque ``wi-...`` handles resolved by exact match
    against the delivered admitted set, so whitespace mangling would silently
    break a citation rather than repair it. Defined once so both backends'
    cleaning paths share a single implementation.
    """
    if not isinstance(ids, list):
        return None
    return [c for c in ids if isinstance(c, str)]


def normalize_task(task: QueuedTask) -> QueuedTask | None:
    """Apply the queue's defensive cleaning to *task*, or ``None`` to drop it.

    Mirrors :meth:`TaskQueue.load`'s per-item cleaning so both backends yield
    identical ``load()`` output regardless of substrate: a blank goal drops the
    task, a blank / missing ``task_id`` is regenerated, a non-string
    ``created_at`` collapses to ``""`` and a non-string ``last_touched_at`` to
    the coerced ``created_at`` (so the SQLite path's ``NOT NULL`` timestamp
    columns can never see ``None``), non-string ``linked_question`` /
    ``rationale`` / ``workspace_moment_id`` collapse to ``None``,
    ``acceptance_criteria`` is canonicalized via
    :func:`_clean_acceptance_criteria`, and ``workspace_item_ids`` via
    :func:`_clean_workspace_item_ids` (which keeps ``[]`` distinct from
    ``None``). The JSON backend
    cleans on read; :class:`~maestro.state_store.SqliteTaskQueue` cleans on
    write so every read path (``load`` / ``head`` / ``pop_head``) is
    substrate-parity-clean.
    """
    if not isinstance(task.goal, str) or not task.goal.strip():
        return None
    task_id = (
        task.task_id
        if isinstance(task.task_id, str) and task.task_id
        else _new_task_id()
    )
    created_at = task.created_at if isinstance(task.created_at, str) else ""
    last_touched_at = (
        task.last_touched_at
        if isinstance(task.last_touched_at, str)
        else created_at
    )
    return replace(
        task,
        task_id=task_id,
        created_at=created_at,
        last_touched_at=last_touched_at,
        linked_question=(
            task.linked_question
            if isinstance(task.linked_question, str)
            else None
        ),
        rationale=(
            task.rationale if isinstance(task.rationale, str) else None
        ),
        acceptance_criteria=_clean_acceptance_criteria(
            task.acceptance_criteria,
        ),
        workspace_moment_id=(
            task.workspace_moment_id
            if isinstance(task.workspace_moment_id, str)
            else None
        ),
        workspace_item_ids=_clean_workspace_item_ids(task.workspace_item_ids),
    )


@runtime_checkable
class TaskQueueBackend(Protocol):
    """Structural contract shared by the JSON and SQLite task-queue backends.

    ``MaestroAgent`` holds exactly one backend and never branches on which; the
    two implementations — :class:`TaskQueue` (the JSON file) and
    :class:`~maestro.state_store.SqliteTaskQueue` (the SQLite store) — satisfy
    this surface structurally, so the construction seam is the only site that
    knows which is live. Mirrors the house ``@runtime_checkable``
    :class:`typing.Protocol` idiom of
    :class:`~maestro.providers.base.Provider` and
    :class:`~maestro.settlement_record_reconciler.SettlementReconciler`:
    ``@runtime_checkable`` enables a structural ``isinstance`` check of the
    surface (method presence only — it does not verify signatures).
    """

    def load(self) -> list[QueuedTask]:
        """Return every queued task in FIFO order."""
        ...

    def save(self, items: list[QueuedTask]) -> None:
        """Replace the queue with *items*, preserving their order."""
        ...

    def head(self) -> QueuedTask | None:
        """Return the FIFO head without removing it, or ``None`` if empty."""
        ...

    def pop_head(self) -> QueuedTask | None:
        """Remove and return the FIFO head, or ``None`` if empty."""
        ...

    def mutate(self, fn: Callable[[list[QueuedTask]], T]) -> T:
        """Load → *fn* → save, serialised against every other writer.

        *fn* receives the **live** queue (not a caller-held snapshot) and
        mutates it in place; the mutated list is persisted before the lock is
        released. Mirrors :meth:`~maestro.pr_ledger.PRLedgerBackend.mutate`:
        it is the only primitive on this surface that gives a whole-queue
        read-modify-write lost-update protection, so a concurrent
        :meth:`pop_head` landing mid-sequence is never erased.
        """
        ...


class TaskQueue:
    """JSON-backed FIFO of :class:`QueuedTask` items.

    A whole-queue read-modify-write — the curator's prune/edit/enqueue
    sequence — must route through :meth:`mutate`, which holds the same
    :func:`~maestro.fileutil.file_lock` :meth:`pop_head` takes across its
    load → modify → save span, so the two serialise against each other. A bare
    :meth:`save` is a whole-state replacement with *no* lost-update
    protection: any pop that lands between a caller's :meth:`load` and its
    :meth:`save` is erased, resurrecting an already-dispatched task. It is
    retained for the Protocol's parity surface and for the callers that hold
    no concurrently-mutated state — seeding
    (:meth:`~maestro.state_store.SqliteTaskQueue._maybe_seed`,
    :meth:`~maestro.state_store.StateStore._migrate_task_queue`) and tests.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> list[QueuedTask]:
        if not self._path.exists():
            return []
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Failed to load task_queue.json; treating as empty",
                           exc_info=True)
            return []
        if not isinstance(data, list):
            return []
        known = {f.name for f in fields(QueuedTask)}
        out: list[QueuedTask] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            filtered = {k: v for k, v in item.items() if k in known}
            goal = filtered.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                continue
            if "task_id" not in filtered or not isinstance(
                filtered["task_id"], str,
            ) or not filtered["task_id"]:
                filtered["task_id"] = _new_task_id()
            if "created_at" not in filtered or not isinstance(
                filtered.get("created_at"), str,
            ):
                filtered["created_at"] = ""
            if "last_touched_at" not in filtered or not isinstance(
                filtered.get("last_touched_at"), str,
            ):
                filtered["last_touched_at"] = filtered["created_at"]
            for opt in ("linked_question", "rationale", "workspace_moment_id"):
                val = filtered.get(opt)
                if val is not None and not isinstance(val, str):
                    filtered[opt] = None
            filtered["acceptance_criteria"] = _clean_acceptance_criteria(
                filtered.get("acceptance_criteria"),
            )
            filtered["workspace_item_ids"] = _clean_workspace_item_ids(
                filtered.get("workspace_item_ids"),
            )
            out.append(QueuedTask(**filtered))
        return out

    def save(self, items: list[QueuedTask]) -> None:
        payload = json.dumps([asdict(t) for t in items], indent=2)
        atomic_write(self._path, payload)

    def head(self) -> QueuedTask | None:
        items = self.load()
        return items[0] if items else None

    def pop_head(self) -> QueuedTask | None:
        with file_lock(self._path):
            items = self.load()
            if not items:
                return None
            head = items[0]
            self.save(items[1:])
        return head

    def mutate(self, fn: Callable[[list[QueuedTask]], T]) -> T:
        """Load-modify-write under the file lock (see the class docstring).

        The lock is the same one :meth:`pop_head` holds, so a pop that would
        otherwise land inside this load → save window blocks until the queue
        has been persisted — and *fn* sees the post-pop queue rather than a
        stale snapshot.
        """
        with file_lock(self._path):
            items = self.load()
            result = fn(items)
            self.save(items)
            return result

# ---------------------------------------------------------------------------
# Deterministic oldest-bug-first sort
# ---------------------------------------------------------------------------

#: Regex to extract GitHub issue numbers from a task goal.
#: Mirrors :data:`audit_checks._ISSUE_NUMBER_RE` — duplicated here to avoid
#: a circular import (audit_checks imports from this module indirectly via
#: agent).  The pattern is trivial enough that duplication is safer than
#: adding an import cycle.
_ISSUE_RE = re.compile(r"#(\d+)")


def _min_issue_number(goal: str) -> int | None:
    """Return the smallest GitHub issue number referenced in *goal*, or None."""
    matches = _ISSUE_RE.findall(goal)
    if not matches:
        return None
    return min(int(m) for m in matches)


def sort_queue_oldest_bugs_first(queue: list[QueuedTask]) -> list[QueuedTask]:
    """Stable-sort *queue* so bug-fix tasks (referencing ``#NNN``) come first.

    Within the bug-fix partition, tasks are ordered by their lowest
    referenced issue number (ascending — lowest number ≈ oldest issue).
    Non-bug tasks retain their original relative order after the bug
    partition.

    This is a pure function: it returns a new list and does not mutate
    *queue*.  The sort is deterministic and zero-SDK-cost — no LLM
    judgment required.
    """
    bugs: list[tuple[int, int, QueuedTask]] = []
    others: list[tuple[int, QueuedTask]] = []
    for idx, task in enumerate(queue):
        issue_num = _min_issue_number(task.goal)
        if issue_num is not None:
            bugs.append((issue_num, idx, task))
        else:
            others.append((idx, task))
    # Sort bugs by lowest issue number (ascending); tie-break by
    # original position to preserve FIFO within the same issue.
    bugs.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in bugs] + [t[1] for t in others]
