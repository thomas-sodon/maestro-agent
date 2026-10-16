"""Round-trip serialization and tolerant-parse tests for ``maestro.transcript``.

Pure serialize/parse over in-memory strings — no file I/O, so no ``mem_fs``
fixture (proposal 104's first increment wires no writer or reader). Every
``return None`` fallback branch of :func:`~maestro.transcript.parse` and
``_coerce_turn`` is exercised alongside the happy-path round trip.
"""

from __future__ import annotations

import json

from maestro.transcript import (
    TranscriptRecord,
    TranscriptTurn,
    parse,
    to_jsonl_line,
)
from maestro.types import VALID_TRANSCRIPT_TURN_KINDS


def _all_kind_turns() -> list[TranscriptTurn]:
    """One turn per valid kind (stable order) with valid string fields."""
    return [
        TranscriptTurn(
            kind=kind,
            role="assistant",
            ts="2026-07-29T12:00:00+00:00",
            content=f"content for {kind}",
        )
        for kind in sorted(VALID_TRANSCRIPT_TURN_KINDS)
    ]


def _base_obj() -> dict[str, object]:
    """A fully-valid, JSON-serializable record dict for negative mutation."""
    return {
        "caller": "contemplate",
        "session_id": "a1b2c3",
        "started_at": "2026-07-29T12:00:00+00:00",
        "session_label": "session-a",
        "iteration": 7,
        "task_id": "task-123",
        "turns": [
            {
                "kind": "text",
                "role": "assistant",
                "ts": "2026-07-29T12:00:01+00:00",
                "content": "hi",
            },
        ],
    }


def test_round_trip_full_record() -> None:
    """A record with all turn kinds and a fully-populated header round-trips."""
    record = TranscriptRecord(
        caller="contemplate",
        session_id="a1b2c3",
        started_at="2026-07-29T12:00:00+00:00",
        session_label="session-a",
        iteration=7,
        task_id="task-123",
        turns=_all_kind_turns(),
    )
    assert parse(to_jsonl_line(record)) == record


def test_round_trip_all_optional_none_header() -> None:
    """An all-optional-``None`` header with no turns round-trips cleanly."""
    record = TranscriptRecord(
        caller="run",
        session_id="deadbe",
        started_at="2026-07-29T12:05:00+00:00",
    )
    assert record.session_label is None
    assert record.iteration is None
    assert record.task_id is None
    assert record.turns == []
    assert parse(to_jsonl_line(record)) == record


def test_serialized_kind_is_a_plain_string() -> None:
    """Each turn's ``kind`` serializes as a bare JSON string, not a wrapper."""
    record = TranscriptRecord(
        caller="contemplate",
        session_id="a1b2c3",
        started_at="2026-07-29T12:00:00+00:00",
        turns=_all_kind_turns(),
    )
    payload = json.loads(to_jsonl_line(record))
    for turn in payload["turns"]:
        assert isinstance(turn["kind"], str)
    kinds = {turn["kind"] for turn in payload["turns"]}
    assert kinds == set(VALID_TRANSCRIPT_TURN_KINDS)


def test_no_trailing_newline_in_serialized_line() -> None:
    """The serializer emits no trailing newline; the caller appends it."""
    record = TranscriptRecord(
        caller="run",
        session_id="deadbe",
        started_at="2026-07-29T12:05:00+00:00",
    )
    line = to_jsonl_line(record)
    assert not line.endswith("\n")


def test_parse_blank_line_returns_none() -> None:
    """A blank or whitespace-only line parses to ``None``."""
    assert parse("") is None
    assert parse("   \n\t") is None


def test_parse_non_json_line_returns_none() -> None:
    """A syntactically invalid JSON line parses to ``None``."""
    assert parse("{not valid json") is None


def test_parse_non_object_line_returns_none() -> None:
    """A JSON array (valid JSON, not an object) parses to ``None``."""
    assert parse("[1, 2, 3]") is None


def test_parse_missing_or_mistyped_required_field_returns_none() -> None:
    """A missing or non-string ``caller`` / ``session_id`` / ``started_at``
    parses to ``None``."""
    missing = _base_obj()
    del missing["caller"]
    assert parse(json.dumps(missing)) is None

    mistyped_caller = {**_base_obj(), "caller": 5}
    assert parse(json.dumps(mistyped_caller)) is None

    mistyped_session = {**_base_obj(), "session_id": None}
    assert parse(json.dumps(mistyped_session)) is None

    mistyped_started = {**_base_obj(), "started_at": 12345}
    assert parse(json.dumps(mistyped_started)) is None


def test_parse_turns_not_a_list_returns_none() -> None:
    """A ``turns`` value that is not a list parses to ``None``."""
    bad = {**_base_obj(), "turns": "not-a-list"}
    assert parse(json.dumps(bad)) is None


def test_parse_malformed_turn_returns_none() -> None:
    """A non-object turn, an unknown ``kind``, or a non-string turn field
    drops the whole record to ``None``."""
    non_object_turn = {**_base_obj(), "turns": ["not-a-dict"]}
    assert parse(json.dumps(non_object_turn)) is None

    unknown_kind = {
        **_base_obj(),
        "turns": [
            {
                "kind": "bogus",
                "role": "assistant",
                "ts": "2026-07-29T12:00:01+00:00",
                "content": "hi",
            },
        ],
    }
    assert parse(json.dumps(unknown_kind)) is None

    non_string_content = {
        **_base_obj(),
        "turns": [
            {
                "kind": "text",
                "role": "assistant",
                "ts": "2026-07-29T12:00:01+00:00",
                "content": 123,
            },
        ],
    }
    assert parse(json.dumps(non_string_content)) is None


def test_parse_unhashable_kind_returns_none() -> None:
    """A turn whose ``kind`` is an unhashable JSON value (array / object) parses
    to ``None`` rather than raising ``TypeError`` from the frozenset membership
    test — the tolerant-parse contract holds for a non-string ``kind`` too."""
    array_kind = {
        **_base_obj(),
        "turns": [
            {
                "kind": [],
                "role": "assistant",
                "ts": "2026-07-29T12:00:01+00:00",
                "content": "hi",
            },
        ],
    }
    assert parse(json.dumps(array_kind)) is None

    object_kind = {
        **_base_obj(),
        "turns": [
            {
                "kind": {},
                "role": "assistant",
                "ts": "2026-07-29T12:00:01+00:00",
                "content": "hi",
            },
        ],
    }
    assert parse(json.dumps(object_kind)) is None


def test_parse_mistyped_optional_field_returns_none() -> None:
    """A non-string ``session_label`` / ``task_id`` or non-int (incl. ``bool``)
    ``iteration`` parses to ``None``."""
    bad_label = {**_base_obj(), "session_label": 5}
    assert parse(json.dumps(bad_label)) is None

    bad_iteration = {**_base_obj(), "iteration": "seven"}
    assert parse(json.dumps(bad_iteration)) is None

    bool_iteration = {**_base_obj(), "iteration": True}
    assert parse(json.dumps(bool_iteration)) is None

    bad_task_id = {**_base_obj(), "task_id": 9}
    assert parse(json.dumps(bad_task_id)) is None


# ---------------------------------------------------------------------------
# tools_advertised header field (proposal 142)
# ---------------------------------------------------------------------------


def test_tools_advertised_round_trips() -> None:
    record = TranscriptRecord(
        caller="contemplate",
        session_id="a1b2c3",
        started_at="2026-07-29T12:00:00+00:00",
        tools_advertised=["Read", "Monitor"],
    )
    parsed = parse(to_jsonl_line(record))
    assert parsed is not None
    assert parsed.tools_advertised == ["Read", "Monitor"]


def test_tools_advertised_missing_key_parses_as_none() -> None:
    parsed = parse(json.dumps(_base_obj()))
    assert parsed is not None
    assert parsed.tools_advertised is None


def test_tools_advertised_empty_list_is_not_none() -> None:
    obj = _base_obj()
    obj["tools_advertised"] = []
    parsed = parse(json.dumps(obj))
    assert parsed is not None
    assert parsed.tools_advertised == []


def test_tools_advertised_string_value_rejects_record() -> None:
    obj = _base_obj()
    obj["tools_advertised"] = "Read"
    assert parse(json.dumps(obj)) is None


def test_tools_advertised_non_string_member_rejects_record() -> None:
    obj = _base_obj()
    obj["tools_advertised"] = ["Read", 3]
    assert parse(json.dumps(obj)) is None


# ---------------------------------------------------------------------------
# mcp_servers_advertised header field (proposal 142)
# ---------------------------------------------------------------------------


def test_mcp_servers_advertised_round_trips() -> None:
    record = TranscriptRecord(
        caller="introspect",
        session_id="a1b2c3",
        started_at="2026-07-29T12:00:00+00:00",
        mcp_servers_advertised=["journal", "memory"],
    )
    parsed = parse(to_jsonl_line(record))
    assert parsed is not None
    assert parsed.mcp_servers_advertised == ["journal", "memory"]


def test_mcp_servers_advertised_missing_key_parses_as_none() -> None:
    parsed = parse(json.dumps(_base_obj()))
    assert parsed is not None
    assert parsed.mcp_servers_advertised is None


def test_mcp_servers_advertised_string_value_rejects_record() -> None:
    obj = _base_obj()
    obj["mcp_servers_advertised"] = "journal"
    assert parse(json.dumps(obj)) is None


def test_mcp_servers_advertised_non_string_member_rejects_record() -> None:
    obj = _base_obj()
    obj["mcp_servers_advertised"] = ["journal", 3]
    assert parse(json.dumps(obj)) is None
