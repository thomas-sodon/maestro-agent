"""Structured, provenance-stamped SDK conversation transcripts (proposal 104).

Every SDK query currently writes its turns to a per-query free-text debug log at
``logs/sdk/<date>/HH-MM-SS_<hex>.log``: timestamped human-readable lines, content
truncated by ``SessionLogger._truncate``, the streamed answer duplicated across
``Text delta:`` lines, and no record of which phase, persistent session,
iteration, or task produced the conversation. Because the record was born as
opaque text, every consumer re-handles it as an unstructured blob.

This module gives the transcript a first-class, serializable record type before
anything reads or writes it. A :class:`TranscriptRecord` carries a provenance
header plus an ordered list of :class:`TranscriptTurn`s and round-trips through
:func:`to_jsonl_line` / :func:`parse` as one JSONL line. Nothing imports this
module yet — it wires no writer, reader, config, or UI, so it changes zero
behaviour and is safe to merge on its own. Later increments (a
provenance-stamping writer, a single reader owner, the dashboard/CLI views)
build on this record.

Serialization mirrors the repo's JSONL idioms: :func:`to_jsonl_line` returns a
``json.dumps(asdict(...))`` line with no trailing newline (the caller appends it,
matching ``cost_tracking.append_cost_sample`` and ``directive_ledger._row``);
:func:`parse` decodes one line with per-line tolerance — a blank, non-JSON,
non-object, field-missing, mistyped, or unknown-``kind`` line returns ``None``
rather than raising, mirroring ``directive_ledger._load_entries``,
``fileutil._decode_jsonl_line``, and the ``CostSample`` read loop.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import cast

from maestro.types import VALID_TRANSCRIPT_TURN_KINDS, TranscriptTurnKind


@dataclass
class TranscriptTurn:
    """One turn of an SDK conversation.

    ``content`` is unbounded at the type level; a later increment bounds it at
    the writer, not here.
    """

    kind: TranscriptTurnKind
    role: str
    ts: str  # ISO 8601 UTC, matching ``CostSample.timestamp``.
    content: str


@dataclass
class TranscriptRecord:
    """A provenance-stamped SDK conversation: header plus ordered turns.

    The header records which phase (``caller``), persistent session
    (``session_label``), iteration, and task produced the conversation, along
    with the per-query file's short hex (``session_id``) and its start time,
    plus the tool surface the CLI advertised at session init
    (``tools_advertised``) and the MCP server names it advertised
    (``mcp_servers_advertised``), both proposal 142.
    Optional fields default so a label-less or ephemeral session still
    constructs cleanly.
    """

    caller: str
    session_id: str
    started_at: str  # ISO 8601 UTC.
    session_label: str | None = None
    iteration: int | None = None
    task_id: str | None = None
    # The CLI's init-message tool list; ``None`` is *unmeasured* (no init
    # message seen, a pre-field sidecar, or ``cli_paced`` — whose executed
    # calls are still measured from its turns), distinct from ``[]``.
    tools_advertised: list[str] | None = None
    # The init message's ``mcp_servers`` names, with the same ``None`` semantics.
    mcp_servers_advertised: list[str] | None = None
    turns: list[TranscriptTurn] = field(default_factory=list)


def to_jsonl_line(record: TranscriptRecord) -> str:
    """Serialize *record* to one JSONL line, no trailing newline.

    ``asdict`` flattens the nested turns; the ``Literal`` kind is already a bare
    string. The caller appends the newline, matching ``directive_ledger._row``
    and ``cost_tracking.append_cost_sample``.
    """
    return json.dumps(asdict(record))


def _coerce_turn(raw: object) -> TranscriptTurn | None:
    """Rebuild one :class:`TranscriptTurn` from a decoded JSON value.

    Returns ``None`` (never raises) when *raw* is not an object, carries an
    unknown ``kind``, or has a missing / non-string ``role`` / ``ts`` /
    ``content``. :func:`parse` drops the whole record when a turn does not
    coerce.
    """
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in VALID_TRANSCRIPT_TURN_KINDS:
        return None
    role = raw.get("role")
    ts = raw.get("ts")
    content = raw.get("content")
    if not (
        isinstance(role, str)
        and isinstance(ts, str)
        and isinstance(content, str)
    ):
        return None
    return TranscriptTurn(
        kind=cast(TranscriptTurnKind, kind),
        role=role,
        ts=ts,
        content=content,
    )


def parse(line: str) -> TranscriptRecord | None:
    """Decode one JSONL *line* to a :class:`TranscriptRecord`, or ``None``.

    Tolerant per-line contract, mirroring ``directive_ledger._load_entries`` and
    ``fileutil._decode_jsonl_line``: a blank line, a non-JSON line, a non-object
    line, a line missing or mistyping a required header field, a line whose
    ``turns`` is not a list, a line carrying a malformed / unknown-``kind`` turn,
    or a line mistyping an optional header field each return ``None`` rather than
    raising.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    caller = obj.get("caller")
    session_id = obj.get("session_id")
    started_at = obj.get("started_at")
    if not (
        isinstance(caller, str)
        and isinstance(session_id, str)
        and isinstance(started_at, str)
    ):
        return None
    raw_turns = obj.get("turns")
    if not isinstance(raw_turns, list):
        return None
    turns: list[TranscriptTurn] = []
    for raw_turn in raw_turns:
        turn = _coerce_turn(raw_turn)
        if turn is None:
            return None
        turns.append(turn)
    session_label = obj.get("session_label")
    if session_label is not None and not isinstance(session_label, str):
        return None
    iteration = obj.get("iteration")
    if iteration is not None and (
        not isinstance(iteration, int) or isinstance(iteration, bool)
    ):
        return None
    task_id = obj.get("task_id")
    if task_id is not None and not isinstance(task_id, str):
        return None
    tools_advertised = obj.get("tools_advertised")
    if tools_advertised is not None and not (
        isinstance(tools_advertised, list)
        and all(isinstance(tool, str) for tool in tools_advertised)
    ):
        return None
    mcp_servers_advertised = obj.get("mcp_servers_advertised")
    if mcp_servers_advertised is not None and not (
        isinstance(mcp_servers_advertised, list)
        and all(isinstance(name, str) for name in mcp_servers_advertised)
    ):
        return None
    return TranscriptRecord(
        caller=caller,
        session_id=session_id,
        started_at=started_at,
        session_label=session_label,
        iteration=iteration,
        task_id=task_id,
        tools_advertised=cast("list[str] | None", tools_advertised),
        mcp_servers_advertised=cast(
            "list[str] | None", mcp_servers_advertised,
        ),
        turns=turns,
    )
