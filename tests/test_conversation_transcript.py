"""AST guard + writer behaviour for provenance-stamped SDK transcripts.

Proposal 104's first consumer: when ``[logs] structured_transcripts_enabled``
is on, ``SessionLogger`` writes a provenance-stamped ``.jsonl`` transcript
sidecar next to each SDK ``.log``. Two guarantees are pinned here:

1. **Provenance is never silently dropped.** Every ``SessionLogger(...)``
   construction across ``maestro/`` must pass a ``caller`` keyword, mirroring the
   AST-walker guard in ``tests/test_session_attachment.py``. A future call site
   that forgets provenance fails CI.
2. **The writer behaves.** Flag on writes a parseable header carrying all four
   provenance fields under a self-describing filename; a ``None``/unsafe caller
   degrades to the ``"unknown"`` slug without raising; flag off writes no sidecar
   and leaves the ``.log`` output intact.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import patch

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from maestro import transcript
from maestro.sdk import (
    SessionLogger,
    _consume_messages,
    _process_content_blocks,
    _safe_caller,
    query,
)

# Repo-relative path to the package the AST guard walks.
_MAESTRO_ROOT = Path(__file__).resolve().parent.parent / "maestro"


# ---------------------------------------------------------------------------
# AST guard: every SessionLogger construction stamps a caller
# ---------------------------------------------------------------------------


def _session_logger_calls_missing_caller(tree: ast.AST) -> list[int]:
    """Return the line numbers of ``SessionLogger(...)`` calls lacking ``caller=``.

    Walks *tree* for ``ast.Call`` nodes whose callee is the bare name
    ``SessionLogger`` and flags any that do not carry a ``caller`` keyword. The
    class definition is an ``ast.ClassDef``, not a ``Call``, so it is never
    flagged.
    """
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "SessionLogger"):
            continue
        keyword_names = {kw.arg for kw in node.keywords}
        if "caller" not in keyword_names:
            missing.append(node.lineno)
    return missing


# Full-AST scan of every module under ``maestro/``; walking the real AST is the
# test's purpose (mocking it would defeat it). Give it the component budget so
# it stays green deterministically under suite load, matching
# ``test_session_attachment``.
@pytest.mark.timeout(15)
def test_every_session_logger_construction_passes_caller() -> None:
    """Fail when a ``SessionLogger(...)`` lands without a ``caller`` keyword.

    Provenance (the phase label) is the whole point of the sidecar; a writer
    site that forgets it produces an ``"unknown"``-attributed transcript that
    cannot be grouped after the fact. New violators block CI.
    """
    violators: list[str] = []
    for path in sorted(_MAESTRO_ROOT.rglob("*.py")):
        rel = path.relative_to(_MAESTRO_ROOT.parent)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line in _session_logger_calls_missing_caller(tree):
            violators.append(f"{rel}:{line}")

    assert not violators, (
        "SessionLogger(...) constructions missing a ``caller`` keyword:\n  "
        + "\n  ".join(violators)
        + "\n\nEvery SDK session-log writer must stamp provenance. Pass "
        "``caller=`` (the phase label) at construction. See proposal 104."
    )


def test_guard_catches_a_synthetic_missing_caller() -> None:
    """Sanity-check: a bare ``SessionLogger(sdk_dir)`` is flagged.

    Drift in the walker would silently let a real violator pass, which is
    exactly the regression this file exists to prevent.
    """
    tree = ast.parse("SessionLogger(sdk_dir)\n")
    assert _session_logger_calls_missing_caller(tree) == [1]


def test_guard_accepts_a_compliant_construction() -> None:
    """A ``SessionLogger(..., caller=...)`` call is not flagged."""
    tree = ast.parse("SessionLogger(sdk_dir, caller='contemplate')\n")
    assert _session_logger_calls_missing_caller(tree) == []


# ---------------------------------------------------------------------------
# _safe_caller slug helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        (None, "unknown"),
        ("", "unknown"),
        ("!!!", "unknown"),
        ("contemplate", "contemplate"),
        ("execute_step", "execute_step"),  # underscore is preserved
        ("evolve/step:1 weird", "evolve-step-1-weird"),
    ],
)
def test_safe_caller_slugifies(caller: str | None, expected: str) -> None:
    assert _safe_caller(caller) == expected


# ---------------------------------------------------------------------------
# Writer behaviour
# ---------------------------------------------------------------------------


def _find_sidecar(files: dict[str, str]) -> tuple[str, str]:
    """Return the (path, content) of the single ``.jsonl`` sidecar in *files*."""
    sidecars = [(k, v) for k, v in files.items() if k.endswith(".jsonl")]
    assert len(sidecars) == 1, f"expected exactly one .jsonl sidecar, got {sidecars}"
    return sidecars[0]


def test_flag_on_writes_parseable_sidecar_with_all_provenance(
    mem_fs: dict[str, str],
) -> None:
    """Flag on → a sidecar whose header round-trips all four provenance fields."""
    sl = SessionLogger(
        Path("/fake/sdk"),
        caller="contemplate",
        session_label="session-A",
        iteration=7,
        task_id="task-123",
        transcripts_enabled=True,
    )
    sl.close()

    _, content = _find_sidecar(mem_fs)
    record = transcript.parse(content)
    assert record is not None
    assert record.caller == "contemplate"
    assert record.session_label == "session-A"
    assert record.iteration == 7
    assert record.task_id == "task-123"
    # Header only for this slice — no turn content is captured yet.
    assert record.turns == []
    # ``session_id`` is the shared 6-hex stem id the ``.log`` also carries.
    assert record.session_id == sl.path.stem.split("_")[-1]


def test_sidecar_filename_encodes_caller_and_shares_log_stem(
    mem_fs: dict[str, str],
) -> None:
    """The ``.jsonl`` name is ``HH-MM-SS_<caller>_<hex>`` sharing the ``.log`` stem."""
    sl = SessionLogger(
        Path("/fake/sdk"),
        caller="evolve",
        transcripts_enabled=True,
    )
    sl.close()

    # ``.log`` stem: HH-MM-SS_<hex>
    log_time, log_hex = sl.path.stem.split("_")
    sidecar_path, _ = _find_sidecar(mem_fs)
    sidecar_name = Path(sidecar_path).name
    assert sidecar_name.endswith(".jsonl")
    # ``.jsonl`` stem: HH-MM-SS_<caller>_<hex>
    parts = Path(sidecar_name).stem.split("_")
    assert parts[0] == log_time  # same HH-MM-SS as the .log
    assert parts[1] == "evolve"  # caller encoded in the filename
    assert parts[-1] == log_hex  # same <hex> as the .log
    # Both files land in the same date directory.
    assert Path(sidecar_path).parent == sl.path.parent


def test_none_caller_degrades_to_unknown_without_raising(
    mem_fs: dict[str, str],
) -> None:
    """A ``None`` caller writes the ``"unknown"`` slug and header, not an error."""
    sl = SessionLogger(
        Path("/fake/sdk"),
        caller=None,
        transcripts_enabled=True,
    )
    sl.close()  # must not raise

    sidecar_path, content = _find_sidecar(mem_fs)
    assert "_unknown_" in Path(sidecar_path).name
    record = transcript.parse(content)
    assert record is not None
    assert record.caller == "unknown"


def test_set_tools_advertised_lands_in_sidecar(mem_fs: dict[str, str]) -> None:
    """The advertised tool list is stamped into the sidecar header and ``.log``."""
    sl = SessionLogger(
        Path("/fake/sdk"), caller="contemplate", transcripts_enabled=True,
    )
    sl.set_tools_advertised(["Bash"])
    sl.set_tools_advertised(["Read", "Monitor"])
    sl.close()

    _, content = _find_sidecar(mem_fs)
    record = transcript.parse(content)
    assert record is not None
    # Last write wins.
    assert record.tools_advertised == ["Read", "Monitor"]
    assert "Tools advertised: Read, Monitor" in mem_fs[str(sl.path)]


def test_set_tools_advertised_is_noop_with_transcripts_disabled(
    mem_fs: dict[str, str],
) -> None:
    sl = SessionLogger(Path("/fake/sdk"), caller="contemplate")
    sl.set_tools_advertised(["Read"])
    sl.close()

    assert sl._transcript_record is None
    assert [k for k in mem_fs if k.endswith(".jsonl")] == []
    assert "Tools advertised: Read" in mem_fs[str(sl.path)]


def test_set_mcp_servers_advertised_lands_in_sidecar(
    mem_fs: dict[str, str],
) -> None:
    sl = SessionLogger(
        Path("/fake/sdk"), caller="introspect", transcripts_enabled=True,
    )
    sl.set_mcp_servers_advertised(["serena"])
    sl.set_mcp_servers_advertised(["journal", "memory"])
    sl.close()

    _, content = _find_sidecar(mem_fs)
    record = transcript.parse(content)
    assert record is not None
    assert record.mcp_servers_advertised == ["journal", "memory"]
    assert "MCP servers advertised: journal, memory" in mem_fs[str(sl.path)]


def test_set_mcp_servers_advertised_is_noop_with_transcripts_disabled(
    mem_fs: dict[str, str],
) -> None:
    sl = SessionLogger(Path("/fake/sdk"), caller="introspect")
    sl.set_mcp_servers_advertised(["journal"])
    sl.close()

    assert sl._transcript_record is None
    assert [k for k in mem_fs if k.endswith(".jsonl")] == []
    assert "MCP servers advertised: journal" in mem_fs[str(sl.path)]


def test_flag_off_writes_no_sidecar_and_log_is_intact(
    mem_fs: dict[str, str],
) -> None:
    """Flag off (default) → no ``.jsonl``; the ``.log`` is exactly as today."""
    sl = SessionLogger(Path("/fake/sdk"), caller="contemplate")
    sl.log("Prompt: hello")
    sl.close()

    assert [k for k in mem_fs if k.endswith(".jsonl")] == []
    assert sl._transcript_record is None
    assert sl._transcript_path is None

    logs = [k for k in mem_fs if k.endswith(".log")]
    assert len(logs) == 1
    content = mem_fs[logs[0]]
    assert "SDK session started" in content
    assert "Prompt: hello" in content
    assert "SDK session ended" in content


def test_double_close_writes_sidecar_only_once(mem_fs: dict[str, str]) -> None:
    """A second ``close()`` is a no-op — the sidecar is written exactly once."""
    sl = SessionLogger(
        Path("/fake/sdk"),
        caller="contemplate",
        transcripts_enabled=True,
    )
    sl.close()
    sl.close()  # idempotent; must not re-write or raise

    assert len([k for k in mem_fs if k.endswith(".jsonl")]) == 1


def test_close_swallows_sidecar_oserror_and_does_not_raise(
    mem_fs: dict[str, str],
) -> None:
    """A sidecar ``atomic_write`` OSError is swallowed so ``close()`` never raises.

    ``close()`` runs in the ``finally`` of the SDK query impls; a raising
    sidecar write there would mask any in-flight exception and crash an
    otherwise-successful query. The sidecar is best-effort observability, so the
    write is swallowed the same way ``log()`` swallows a ``.log`` OSError.
    Regression for review finding r1.
    """
    sl = SessionLogger(
        Path("/fake/sdk"),
        caller="contemplate",
        transcripts_enabled=True,
    )
    with patch(
        "maestro.sdk.atomic_write", side_effect=OSError("disk full"),
    ) as mock_write:
        sl.close()  # must not raise despite the sidecar write failing

    # The write was attempted (so the swallow branch ran) and no sidecar landed.
    mock_write.assert_called_once()
    assert [k for k in mem_fs if k.endswith(".jsonl")] == []


# ---------------------------------------------------------------------------
# Turn capture: the sidecar mirrors each turn at full content
# ---------------------------------------------------------------------------


def _turns_of_kind(
    record: transcript.TranscriptRecord, kind: str,
) -> list[transcript.TranscriptTurn]:
    """Return the record's turns of one *kind*, in order."""
    return [t for t in record.turns if t.kind == kind]


def test_turn_content_is_full_while_the_log_line_is_truncated(
    mem_fs: dict[str, str],
) -> None:
    """A >500-char thinking/text block is complete in the sidecar, cut in the .log.

    The sidecar cap (default 20000) is a separate bound from the ``.log``'s
    500-char ``_truncate``; a 900-char turn is preserved whole in the ``.jsonl``
    while the matching ``.log`` line ends with the ``[N chars total]`` suffix.
    """
    long_thought = "H" * 900
    long_text = "T" * 900
    sl = SessionLogger(
        Path("/fake/sdk"),
        caller="contemplate",
        transcripts_enabled=True,
    )
    _process_content_blocks(
        [
            ThinkingBlock(thinking=long_thought, signature="sig"),
            TextBlock(text=long_text),
        ],
        [],
        {},
        session_log=sl,
    )
    sl.close()

    # The human ``.log`` is ``_truncate``-bounded: the full content never lands,
    # and the truncated lines carry the chars-total suffix.
    log_path = next(k for k in mem_fs if k.endswith(".log"))
    log_content = mem_fs[log_path]
    assert long_thought not in log_content
    assert long_text not in log_content
    assert "chars total]" in log_content

    # The sidecar carries both turns at full content.
    _, sidecar = _find_sidecar(mem_fs)
    record = transcript.parse(sidecar)
    assert record is not None
    thinking_turns = _turns_of_kind(record, "thinking")
    text_turns = _turns_of_kind(record, "text")
    assert [t.content for t in thinking_turns] == [long_thought]
    assert [t.content for t in text_turns] == [long_text]
    assert thinking_turns[0].role == "assistant"
    assert text_turns[0].role == "assistant"


@pytest.mark.asyncio
async def test_streamed_answer_collapses_to_one_text_turn(
    mem_fs: dict[str, str],
) -> None:
    """A ``text_delta`` stream event adds no turn — only the final block does.

    Streaming deltas are left uncaptured, so a streamed answer (many per-chunk
    deltas plus one final ``TextBlock``) collapses to a single ``text`` turn
    rather than a wall of fragments.
    """
    delta = StreamEvent(
        uuid="u1",
        session_id="s1",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "partial "},
        },
    )
    final = AssistantMessage(
        content=[TextBlock(text="partial answer")],
        model="claude-sonnet-4-20250514",
    )
    sl = SessionLogger(
        Path("/fake/sdk"),
        caller="contemplate",
        transcripts_enabled=True,
    )

    async def gen() -> Any:
        yield delta
        yield final

    # ``stream_idle_timeout_seconds=0`` disables the idle wait_for so the test
    # never arms a real asyncio timer.
    await _consume_messages(
        gen(), "contemplate", sl, stream_idle_timeout_seconds=0,
    )
    sl.close()

    _, sidecar = _find_sidecar(mem_fs)
    record = transcript.parse(sidecar)
    assert record is not None
    text_turns = _turns_of_kind(record, "text")
    assert len(text_turns) == 1
    assert text_turns[0].content == "partial answer"


def test_turn_content_is_sliced_to_the_cap(mem_fs: dict[str, str]) -> None:
    """Content longer than ``transcript_content_max`` is sliced to the cap."""
    cap = 50
    sl = SessionLogger(
        Path("/fake/sdk"),
        caller="contemplate",
        transcripts_enabled=True,
        transcript_content_max=cap,
    )
    _process_content_blocks(
        [TextBlock(text="Z" * 500)], [], {}, session_log=sl,
    )
    sl.close()

    _, sidecar = _find_sidecar(mem_fs)
    record = transcript.parse(sidecar)
    assert record is not None
    text_turns = _turns_of_kind(record, "text")
    assert len(text_turns) == 1
    assert text_turns[0].content == "Z" * cap
    assert len(text_turns[0].content) == cap


def test_tool_call_and_tool_result_turns_capture_expected_roles(
    mem_fs: dict[str, str],
) -> None:
    """A tool-use / tool-result pair captures ``tool_call`` and ``tool_result``.

    The ``tool_call`` turn is ``assistant``-role and carries the tool name plus
    input; the ``tool_result`` turn is ``tool``-role and carries the result.
    """
    sl = SessionLogger(
        Path("/fake/sdk"),
        caller="contemplate",
        transcripts_enabled=True,
    )
    _process_content_blocks(
        [
            ToolUseBlock(id="tu1", name="Bash", input={"command": "ls"}),
            ToolResultBlock(tool_use_id="tu1", content="file.txt"),
        ],
        [],
        {},
        session_log=sl,
    )
    sl.close()

    _, sidecar = _find_sidecar(mem_fs)
    record = transcript.parse(sidecar)
    assert record is not None

    call_turns = _turns_of_kind(record, "tool_call")
    assert len(call_turns) == 1
    assert call_turns[0].role == "assistant"
    assert call_turns[0].content.startswith("Bash input=")
    assert "ls" in call_turns[0].content

    result_turns = _turns_of_kind(record, "tool_result")
    assert len(result_turns) == 1
    assert result_turns[0].role == "tool"
    assert result_turns[0].content == "file.txt"


def test_error_tool_result_turn_is_error_prefixed(
    mem_fs: dict[str, str],
) -> None:
    """A failed tool result captures the ``ERROR ``-prefixed content."""
    sl = SessionLogger(
        Path("/fake/sdk"),
        caller="contemplate",
        transcripts_enabled=True,
    )
    _process_content_blocks(
        [ToolResultBlock(tool_use_id="tu1", content="boom", is_error=True)],
        [],
        {},
        session_log=sl,
    )
    sl.close()

    _, sidecar = _find_sidecar(mem_fs)
    record = transcript.parse(sidecar)
    assert record is not None
    result_turns = _turns_of_kind(record, "tool_result")
    assert len(result_turns) == 1
    assert result_turns[0].content == "ERROR boom"


@pytest.mark.asyncio
async def test_prompt_turn_captured_with_user_role_through_query(
    mem_fs: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving ``query()`` with transcripts on captures a ``user`` prompt turn."""
    import maestro.sdk as sdk_mod

    monkeypatch.setattr(sdk_mod, "_sdk_log_dir", Path("/fake/sdk"))
    monkeypatch.setattr(sdk_mod, "_structured_transcripts_enabled", True)
    monkeypatch.setattr(sdk_mod, "_transcript_content_max", 20000)

    result_msg = ResultMessage(
        subtype="result",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s1",
        result="done",
    )

    async def gen(**kwargs: Any) -> Any:
        yield result_msg

    with patch("maestro.sdk._query", side_effect=gen):
        await query(prompt="hello world")

    _, sidecar = _find_sidecar(mem_fs)
    record = transcript.parse(sidecar)
    assert record is not None
    prompt_turns = _turns_of_kind(record, "prompt")
    assert len(prompt_turns) == 1
    assert prompt_turns[0].role == "user"
    assert prompt_turns[0].content == "hello world"


def test_add_turn_noops_when_transcripts_disabled(
    mem_fs: dict[str, str],
) -> None:
    """Flag off → ``add_turn`` no-ops (record is ``None``); the ``.log`` is intact.

    ``_process_content_blocks`` calls ``add_turn`` unconditionally beside its
    ``.log`` writes; with transcripts off the record is ``None`` so no turn is
    recorded and no sidecar is written, while the human ``.log`` is unchanged.
    """
    sl = SessionLogger(Path("/fake/sdk"), caller="contemplate")
    _process_content_blocks(
        [TextBlock(text="visible answer")], [], {}, session_log=sl,
    )
    # A direct call is a no-op too when the record is absent.
    sl.add_turn("thinking", "assistant", "should be dropped")
    assert sl._transcript_record is None
    sl.close()

    assert [k for k in mem_fs if k.endswith(".jsonl")] == []
    log_path = next(k for k in mem_fs if k.endswith(".log"))
    log_content = mem_fs[log_path]
    assert "Text: visible answer" in log_content


# ---------------------------------------------------------------------------
# AST guard: transcript-sidecar parsing has one reader owner (logs.py)
# ---------------------------------------------------------------------------
#
# Proposal 104. Parsing a ``.jsonl`` transcript sidecar back into a
# ``TranscriptRecord`` is a fact the package must read through a *single*
# module, ``maestro.logs`` (its ``read_transcript_summaries`` /
# ``read_transcript_detail`` readers), never re-derive with a hand-mirrored
# ``transcript.parse`` call elsewhere — the same single-owner discipline the
# merge-truth and CI-truth chokepoints already enforce. This guard fails the
# build when the owner's parse call appears outside ``logs.py`` — in *any*
# import spelling: the qualified ``transcript.parse(...)`` attribute call (under
# the bare module name, an ``as``-renamed module alias, or the dotted
# ``maestro.transcript.parse(...)``), or a bare ``parse(...)`` whose name was
# bound by ``from maestro.transcript import parse`` (alias included). The
# from-import spelling is the house pattern for this module (``maestro/sdk.py``
# imports its siblings that way), so covering only the attribute form would
# leave the fence open on the more likely shape.


# The single owner of transcript-sidecar parsing. Its own ``transcript.parse``
# call is the point, so it is scanned past entirely.
_PARSE_OWNER = "logs.py"

# Whole-function exemptions: ``(module_basename, function_name)``. Starts empty —
# nothing outside the owner parses a transcript sidecar today. A future non-owner
# parse site must route through the ``logs.py`` readers or justify an entry here.
_FUNCTION_ALLOWLIST: frozenset[tuple[str, str]] = frozenset()


class _TranscriptBindings(NamedTuple):
    """The local names a module bound to the transcript parse owner.

    ``parse_names`` are names that *are* the function (bound by
    ``from maestro.transcript import parse``, ``as``-alias included);
    ``module_names`` are names that are the *module* (the bare ``transcript``
    from ``from maestro import transcript``, plus any ``as``-renamed module
    alias), whose ``.parse`` attribute is the guarded call.
    """

    parse_names: frozenset[str]
    module_names: frozenset[str]


def _transcript_bindings(tree: ast.AST) -> _TranscriptBindings:
    """Collect the names *tree* bound to ``maestro.transcript`` / its ``parse``.

    Walks every import statement so the detector is import-style agnostic:

    * ``from maestro.transcript import parse`` (and the intra-package relative
      ``from .transcript import parse``) binds a *function* name;
    * ``from maestro import transcript as t`` (and the relative
      ``from . import transcript as t``) and ``import maestro.transcript as t``
      bind a *module* name.

    The relative spellings match at *any* depth (``node.level >= 1``), so a
    reader added under a subpackage (``maestro/loop/``, ``maestro/providers/``,
    ``maestro/dashboard/`` — all scanned by ``rglob``) that reaches the owner as
    ``from ..transcript import parse`` or ``from .. import transcript as t`` is
    armed exactly like its top-level sibling.

    Each ``alias.asname or alias.name`` is recorded, so an ``as``-renamed import
    is covered. The bare name ``transcript`` is always a module name — it is the
    house spelling and needs no import to be worth flagging. Mirrors the
    ``ImportFrom`` matching style of
    ``structural_guards._vendor_sdk_import_detector``.
    """
    parse_names: set[str] = set()
    module_names: set[str] = {"transcript"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "maestro.transcript" or (
                node.level >= 1 and node.module == "transcript"
            ):
                parse_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "parse"
                )
            elif node.module == "maestro" or (
                node.level >= 1 and node.module is None
            ):
                module_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "transcript"
                )
        elif isinstance(node, ast.Import):
            module_names.update(
                alias.asname
                for alias in node.names
                if alias.name == "maestro.transcript" and alias.asname
            )
    return _TranscriptBindings(frozenset(parse_names), frozenset(module_names))


def _transcript_parse_call_spelling(
    node: ast.AST, bindings: _TranscriptBindings
) -> str | None:
    """Return the matched call spelling when *node* is the owner's parse call.

    Three spellings fork the owner identically and all are flagged: a ``parse``
    attribute on a module name in ``bindings.module_names``, the dotted
    ``maestro.transcript.parse(...)`` (an attribute chain, as left by a plain
    ``import maestro.transcript``), and a bare ``name(...)`` whose ``name`` is in
    ``bindings.parse_names``. A bare ``parse(...)`` in a module carrying no such
    import is a coincidental local, not the guarded call, and stays unflagged;
    so does a ``parse`` attribute on any other object. Returns the spelling
    actually matched (for the violator message), or ``None``.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "parse":
        value = func.value
        if isinstance(value, ast.Name) and value.id in bindings.module_names:
            return f"{value.id}.parse"
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "transcript"
            and isinstance(value.value, ast.Name)
            and value.value.id == "maestro"
        ):
            return "maestro.transcript.parse"
    if isinstance(func, ast.Name) and func.id in bindings.parse_names:
        return func.id
    return None


def _is_transcript_parse_call(node: ast.AST, bindings: _TranscriptBindings) -> bool:
    """Return ``True`` when *node* is the owner's parse call in any spelling.

    Thin predicate over :func:`_transcript_parse_call_spelling`; see its
    docstring for the matching rule.
    """
    return _transcript_parse_call_spelling(node, bindings) is not None


def _line_to_function(tree: ast.AST) -> dict[int, str]:
    """Map ``lineno`` → enclosing function name within *tree* (methods included).

    Inner closures resolve to their outer named function (``ast.walk`` is
    breadth-first, so the outer ``FunctionDef`` is seen first and ``setdefault``
    keeps it) — copied verbatim from ``tests/test_ci_truth_chokepoint.py``.
    """
    line_to_func: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            line = getattr(child, "lineno", None)
            if line is not None:
                line_to_func.setdefault(line, node.name)
    return line_to_func


def _parse_scanned_files() -> list[Path]:
    """Every ``maestro/`` source file except the parse owner, deterministically
    ordered."""
    return sorted(p for p in _MAESTRO_ROOT.rglob("*.py") if p.name != _PARSE_OWNER)


def _functions_by_module() -> dict[str, set[str]]:
    """Map ``module_basename`` → the set of function names it defines (scanned
    files only)."""
    by_module: dict[str, set[str]] = {}
    for path in _parse_scanned_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        by_module.setdefault(path.name, set()).update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
    return by_module


def _stale_allowlist_entries(
    allowlist: frozenset[tuple[str, str]],
    by_module: dict[str, set[str]],
) -> list[str]:
    """Return ``module::func`` for each allowlist entry naming no real function."""
    stale: list[str] = []
    for module_base, func_name in sorted(allowlist):
        if module_base not in by_module or func_name not in by_module[module_base]:
            stale.append(f"{module_base}::{func_name}")
    return stale


# Full-AST scan of the whole package; mocking is impossible — walking the real
# AST is the test's purpose. Raise to the component budget so it stays green
# deterministically under suite load (mirrors test_ci_truth_chokepoint.py).
@pytest.mark.timeout(15)
def test_transcript_parse_owned_by_logs() -> None:
    """Fail when the owner's parse call appears outside ``logs.py``.

    Either spelling counts: the qualified ``transcript.parse(...)`` and a bare
    ``parse(...)`` bound by ``from maestro.transcript import parse``.

    The architectural rule: parsing a transcript sidecar lives only in
    ``maestro.logs``. A new site that re-parses one must route through the
    ``read_transcript_summaries`` / ``read_transcript_detail`` readers or justify
    an ``_FUNCTION_ALLOWLIST`` entry — otherwise the label-less blob re-handling
    proposal 104 consolidated returns.
    """
    violators: list[str] = []

    for path in _parse_scanned_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        line_to_func = _line_to_function(tree)
        bindings = _transcript_bindings(tree)
        module_base = path.name
        rel = path.relative_to(_MAESTRO_ROOT).as_posix()

        for node in ast.walk(tree):
            spelling = _transcript_parse_call_spelling(node, bindings)
            if spelling is None:
                continue
            lineno = getattr(node, "lineno", -1)
            func_name = line_to_func.get(lineno, "<module>")
            if (module_base, func_name) in _FUNCTION_ALLOWLIST:
                continue
            violators.append(f"{rel}:{lineno} — {spelling} in {func_name}")

    assert not violators, (
        "transcript-sidecar parsing made outside the ``logs.py`` owner:\n  "
        + "\n  ".join(violators)
        + "\n\n``transcript.parse`` has one reader owner, ``maestro.logs`` "
        "(proposal 104). Route the new site through "
        "``logs.read_transcript_summaries`` / ``read_transcript_detail``, or add "
        "it to ``_FUNCTION_ALLOWLIST`` with a justifying comment."
    )


def test_transcript_guard_flags_a_synthetic_bypass() -> None:
    """A synthetic reader that re-introduces ``transcript.parse`` is caught.

    Drift in the AST helper would silently make the guard above pass on a real
    violator — the regression this pin prevents. A different ``transcript``
    function, a ``parse`` on another object, and a bare ``parse`` are all
    correctly *not* flagged, pinning the detector's precision.
    """
    source = (
        "def some_new_reader(text):\n"
        "    record = transcript.parse(text)\n"
        "    return record\n"
    )
    tree = ast.parse(source)
    line_to_func = _line_to_function(tree)

    bindings = _transcript_bindings(tree)

    flagged: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not _is_transcript_parse_call(node, bindings):
            continue
        lineno = getattr(node, "lineno", -1)
        flagged.append((lineno, line_to_func.get(lineno, "<module>")))

    assert flagged == [(2, "some_new_reader")]

    # Precision: none of these three shapes is the guarded call. The bare
    # ``parse(t)`` is unflagged because this snippet imports nothing from the
    # owner module — the from-import case is pinned in its own test below.
    others = ast.parse(
        "def r(t):\n"
        "    a = transcript.to_jsonl_line(t)\n"
        "    b = other.parse(t)\n"
        "    c = parse(t)\n"
        "    return a, b, c\n"
    )
    other_bindings = _transcript_bindings(others)
    assert other_bindings.parse_names == frozenset()
    assert [
        n for n in ast.walk(others) if _is_transcript_parse_call(n, other_bindings)
    ] == []


def test_transcript_guard_flags_a_from_import_bypass() -> None:
    """A from-imported ``parse`` forks the owner just as the attribute call does.

    ``maestro.transcript.parse`` is a module-level function, and the house
    pattern for this module is ``from maestro.transcript import ...`` — so a
    bypass is at least as likely to be spelled ``parse(line)`` after a
    from-import as ``transcript.parse(line)``. Both must fail the build; this
    pins the from-import half, including an ``as`` alias and the relative
    import spelling.
    """
    for source, expected_alias, expected_line in (
        (
            "from maestro.transcript import parse\n"
            "\n"
            "def some_new_reader(text):\n"
            "    return parse(text)\n",
            "parse",
            4,
        ),
        (
            "from maestro.transcript import parse as _p\n"
            "\n"
            "def some_new_reader(text):\n"
            "    return _p(text)\n",
            "_p",
            4,
        ),
        (
            "from .transcript import parse\n"
            "\n"
            "def some_new_reader(text):\n"
            "    return parse(text)\n",
            "parse",
            4,
        ),
        (
            # A subpackage reader (``maestro/loop/``) reaching the sibling owner.
            "from ..transcript import parse\n"
            "\n"
            "def some_new_reader(text):\n"
            "    return parse(text)\n",
            "parse",
            4,
        ),
    ):
        tree = ast.parse(source)
        bindings = _transcript_bindings(tree)
        assert bindings.parse_names == frozenset({expected_alias})

        line_to_func = _line_to_function(tree)
        flagged = [
            (getattr(n, "lineno", -1), line_to_func.get(getattr(n, "lineno", -1), ""))
            for n in ast.walk(tree)
            if _is_transcript_parse_call(n, bindings)
        ]
        assert flagged == [(expected_line, "some_new_reader")]


def test_transcript_guard_flags_module_alias_spellings() -> None:
    """An ``as``-renamed module and the dotted call fork the owner too.

    ``func.value.id == "transcript"`` alone would miss
    ``from maestro import transcript as t; t.parse(x)`` and the plain
    ``import maestro.transcript; maestro.transcript.parse(x)`` attribute chain —
    both reach the same function, so both must fail the build.
    """
    for source, expected_line in (
        (
            "from maestro import transcript as t\n"
            "\n"
            "def some_new_reader(text):\n"
            "    return t.parse(text)\n",
            4,
        ),
        (
            "from . import transcript as t\n"
            "\n"
            "def some_new_reader(text):\n"
            "    return t.parse(text)\n",
            4,
        ),
        (
            # A subpackage reader (``maestro/loop/``) reaching the sibling owner.
            "from .. import transcript as t\n"
            "\n"
            "def some_new_reader(text):\n"
            "    return t.parse(text)\n",
            4,
        ),
        (
            "import maestro.transcript as t\n"
            "\n"
            "def some_new_reader(text):\n"
            "    return t.parse(text)\n",
            4,
        ),
        (
            "import maestro.transcript\n"
            "\n"
            "def some_new_reader(text):\n"
            "    return maestro.transcript.parse(text)\n",
            4,
        ),
    ):
        tree = ast.parse(source)
        bindings = _transcript_bindings(tree)
        line_to_func = _line_to_function(tree)
        flagged = [
            (getattr(n, "lineno", -1), line_to_func.get(getattr(n, "lineno", -1), ""))
            for n in ast.walk(tree)
            if _is_transcript_parse_call(n, bindings)
        ]
        assert flagged == [(expected_line, "some_new_reader")]

    # Precision: an unrelated module bound under the same shape is not the owner.
    unrelated = ast.parse(
        "from maestro import journal as t\n"
        "\n"
        "def r(text):\n"
        "    return t.parse(text)\n"
    )
    unrelated_bindings = _transcript_bindings(unrelated)
    assert unrelated_bindings.module_names == frozenset({"transcript"})
    assert [
        n for n in ast.walk(unrelated) if _is_transcript_parse_call(n, unrelated_bindings)
    ] == []


def test_transcript_parse_spelling_names_the_matched_shape() -> None:
    """The violator message names the spelling that actually matched.

    A future failure must tell the implementer which call shape tripped the
    fence, so each spelling reports itself rather than a fixed label.
    """
    cases = (
        (
            "from maestro.transcript import parse as _p\n\ndef r(t):\n    return _p(t)\n",
            "_p",
        ),
        ("def r(t):\n    return transcript.parse(t)\n", "transcript.parse"),
        (
            "from maestro import transcript as _t\n\ndef r(t):\n    return _t.parse(t)\n",
            "_t.parse",
        ),
        (
            "import maestro.transcript\n\ndef r(t):\n"
            "    return maestro.transcript.parse(t)\n",
            "maestro.transcript.parse",
        ),
    )
    for source, expected in cases:
        tree = ast.parse(source)
        bindings = _transcript_bindings(tree)
        spellings = [
            _transcript_parse_call_spelling(n, bindings) for n in ast.walk(tree)
        ]
        assert [s for s in spellings if s is not None] == [expected]


def test_transcript_guard_ignores_a_coincidental_bare_parse() -> None:
    """A bare ``parse(t)`` with no owner from-import binds no name and is safe.

    Importing an unrelated symbol from the owner module must not arm the bare
    spelling — otherwise the guard would flag any local helper named ``parse``.
    """
    unrelated = ast.parse(
        "from maestro.transcript import to_jsonl_line\n"
        "\n"
        "def r(t):\n"
        "    return parse(t)\n"
    )
    bindings = _transcript_bindings(unrelated)
    assert bindings.parse_names == frozenset()
    assert [
        n for n in ast.walk(unrelated) if _is_transcript_parse_call(n, bindings)
    ] == []


def test_transcript_parse_from_owner_is_not_flagged() -> None:
    """The owner ``logs.py`` is scanned past, so its sanctioned parse is exempt.

    The exclusion must be load-bearing, not vacuous: ``logs.py`` genuinely
    contains the single sanctioned ``transcript.parse`` call, and it is absent
    from the scanned set — so it can never be a violator.
    """
    scanned = {p.name for p in _parse_scanned_files()}
    assert _PARSE_OWNER not in scanned

    owner_tree = ast.parse((_MAESTRO_ROOT / _PARSE_OWNER).read_text(encoding="utf-8"))
    owner_bindings = _transcript_bindings(owner_tree)
    assert any(
        _is_transcript_parse_call(node, owner_bindings)
        for node in ast.walk(owner_tree)
    )


def test_transcript_parse_allowlist_entries_name_real_functions() -> None:
    """Every allowlist entry must name a function that exists in its module.

    The real allowlist starts empty (nothing outside the owner parses a
    sidecar), so the checker is exercised against synthetic entries to keep the
    guard meaningful: a real entry validates, a bogus one is caught. Guards
    against a stale exemption surviving a rename/removal and silently widening
    the hole (mirrors ``test_ci_truth_chokepoint.py``).
    """
    by_module = _functions_by_module()

    # The real allowlist has no stale entries (it is empty today).
    assert _stale_allowlist_entries(_FUNCTION_ALLOWLIST, by_module) == []
    # A synthetic entry naming a real scanned function validates; bogus entries
    # (unknown module, and known module / unknown function) are both flagged.
    assert _stale_allowlist_entries(
        frozenset({("transcript.py", "parse")}), by_module,
    ) == []
    assert _stale_allowlist_entries(
        frozenset({("transcript.py", "nope"), ("ghost.py", "x")}), by_module,
    ) == ["ghost.py::x", "transcript.py::nope"]
