"""Tests for the priced-reflection self-prediction loop.

Reflection is made loseable: every reflection must end with a falsifiable
prediction about next-cycle behavior plus a named loss condition, the prior
cycle's bet is injected for confrontation, and the prediction is persisted to
``predictions.jsonl`` and scored by ``measure_prediction_accuracy``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from maestro.audit_checks import (
    CALIBRATION_VERDICT_AUTHORITY_GATE,
    GATE_ACTIVATIONS_FILENAME,
    GateActivation,
)
from maestro.audit_checks_calibration import (
    _CALIBRATION_GATE_WINDOW,
    _CALIBRATION_TASK_WINDOW,
    PREDICTIONS_FILENAME,
    JsonlPredictionLedger,
    _parse_stored_confidence,
    _read_prediction_rows,
    _tail_spans_since,
    banned_stale_loss_conditions,
    calibration_window_ends,
    has_reflection_prediction,
    load_latest_reflection_prediction,
    load_open_reflection_bets,
    load_scored_reflection_bets,
    measure_prediction_accuracy,
    measure_self_calibration,
    record_prediction,
    record_reflection_prediction,
    record_reflection_verdict,
    reflection_bet_cycle,
    stale_repeat_introspect_kwargs,
)
from maestro.consciousness import (
    _MAX_REFLECTION_CHARS,
    SelfModel,
    _introspect_complete,
    _introspect_complete_after_verdict,
)
from maestro.diagnostics import DiagnosticSnapshot
from maestro.memory import EvolutionPlan, SensorReading, TaskRecord
from maestro.prompts.reflection import (
    PREDICTION_REQUIREMENT,
    has_prediction_markers,
    parse_reflection_confidence,
    parse_reflection_prediction,
    parse_reflection_verdict,
)
from maestro.providers.cli_paced import _adapt_complete_on
from maestro.reflection_retrieval import READ_ONLY_REFLECTION_TOOLS
from maestro.sdk import IterationUsage, json_complete_tool_loop_safe, query
from maestro.self_calibration import (
    CalibrationEvidence,
    parse_loss_condition,
    score_loss_condition,
)

# A reflection body that uses the binding marker lines.
_REFLECTION_WITH_MARKERS = (
    "I notice I keep narrating progress I have not landed.\n"
    "PRIOR VERDICT: wrong\n"
    "PREDICTION: Next cycle I will run ruff and mypy on my own change.\n"
    "LOSS CONDITION: I close the cycle without a real gate exit code."
)

# A self-calibration-conforming reflection (proposal 129): it carries a numeric
# CONFIDENCE line and a loss condition in the oracle grammar (GATE_FIRING). Kept
# separate from ``_REFLECTION_WITH_MARKERS`` — whose free-text loss condition the
# grammar deliberately declines — so the unfalsifiable path stays covered too.
_REFLECTION_CALIBRATED = (
    "I keep narrating progress I have not landed.\n"
    "PRIOR VERDICT: wrong\n"
    "CONFIDENCE: 0.75\n"
    "PREDICTION: Next cycle the wip_primacy gate holds me to finishing PR #3725.\n"
    "LOSS CONDITION: gate wip_primacy does not fire within 5 cycles"
)


def _clean_snapshot() -> DiagnosticSnapshot:
    return DiagnosticSnapshot(
        recent_error_count=0,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=0,
        summary="All clear",
        cost_trend="flat",
    )


async def _run_contemplate(
    agent,  # type: ignore[no-untyped-def]
    response_text: str,
) -> str:
    """Drive ``contemplate`` with a mocked SDK; return the captured prompt.

    ``introspect`` runs for real (so the actual prompt is built) but its
    ``query`` SDK call and ``update_self_model`` are mocked.
    """
    captured: list[str] = []

    async def _fake_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(kwargs.get("prompt", ""))
        return MagicMock(response=response_text)

    with patch(
        "maestro.diagnostics.build_diagnostic_snapshot",
        return_value=_clean_snapshot(),
    ), patch("maestro.consciousness.query", side_effect=_fake_query), \
         patch.object(
             agent.consciousness,
             "update_self_model",
             AsyncMock(return_value=SelfModel()),
         ), patch.object(agent.journal, "write_thought"):
        await agent.contemplate(session=None)

    assert len(captured) == 1
    return captured[0]


async def _run_contemplate_streaming(
    agent,  # type: ignore[no-untyped-def]
    chunks: list[str],
) -> None:
    """Drive ``contemplate`` with a fake SDK that HONORS ``complete_on``.

    ``_run_contemplate`` returns the full canned response and so bypasses the
    early-exit predicate entirely — which is exactly the coverage gap defect
    #3471417526 calls out. This helper instead replays the real SDK streaming
    loop (``maestro/sdk.py``): ``complete_on`` is evaluated once per streamed
    ``AssistantMessage``, so ``chunks`` are the per-message text pieces. It
    accumulates them in order, evaluates the predicate after each, and returns
    only the partial buffer captured when it fires — precisely what
    ``introspect`` receives on a genuine early-exit. A realistic reflection
    whose trailing markers arrive in a later message than the 600-char body is
    therefore truncated here just as it would be in production.
    """

    async def _fake_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        # ``introspect`` always supplies the early-exit predicate; replay it.
        complete_on = kwargs["complete_on"]
        buffer = ""
        for chunk in chunks:
            buffer += chunk
            if complete_on(None, buffer):
                break
        return MagicMock(response=buffer)

    with patch(
        "maestro.diagnostics.build_diagnostic_snapshot",
        return_value=_clean_snapshot(),
    ), patch("maestro.consciousness.query", side_effect=_fake_query), \
         patch.object(
             agent.consciousness,
             "update_self_model",
             AsyncMock(return_value=SelfModel()),
         ), patch.object(agent.journal, "write_thought"):
        await agent.contemplate(session=None)


# A realistic full-length reflection streamed as successive AssistantMessages:
# ~900 chars of prose answering the prompt points arrive first (crossing the
# 600-char floor), then the mandatory PREDICTION / LOSS CONDITION markers as
# the final two lines — i.e. they land in later messages, past the floor.
_LONG_REFLECTION_CHUNKS = [
    "I keep narrating progress I have not landed. " * 20,
    "\nPREDICTION: Next cycle I will run ruff and mypy on my own change.",
    "\nLOSS CONDITION: I close the cycle without a real gate exit code.",
]

# The self-calibration variant (proposal 129): the ~900-char body crosses the
# floor first, then CONFIDENCE arrives *before* the two markers. The early-exit
# fires only once PREDICTION and LOSS CONDITION are both present — by which point
# the earlier CONFIDENCE line is already in the buffer, so it is never truncated.
_CALIBRATED_STREAM_CHUNKS = [
    "I keep narrating progress I have not landed. " * 20,
    "\nCONFIDENCE: 0.6",
    "\nPREDICTION: Next cycle the wip_primacy gate holds me to finishing PR #3725.",
    "\nLOSS CONDITION: gate wip_primacy does not fire within 5 cycles",
]


# ---------------------------------------------------------------------------
# Criterion 1 — the prompt names the binding requirement.
# ---------------------------------------------------------------------------


def test_prompt_requires_falsifiable_prediction_and_loss_condition() -> None:
    """The reflection prompt fragment names both binding elements."""
    text = PREDICTION_REQUIREMENT.lower()
    assert "falsifiable" in text
    assert "next-cycle" in text
    assert "named loss condition" in text


# ---------------------------------------------------------------------------
# Marker parsing.
# ---------------------------------------------------------------------------


def test_parse_reflection_prediction_extracts_both() -> None:
    prediction, loss = parse_reflection_prediction(_REFLECTION_WITH_MARKERS)
    assert prediction == "Next cycle I will run ruff and mypy on my own change."
    assert loss == "I close the cycle without a real gate exit code."


def test_parse_reflection_verdict_reads_prior_line() -> None:
    assert parse_reflection_verdict(_REFLECTION_WITH_MARKERS) == "wrong"


# ---------------------------------------------------------------------------
# parse_reflection_confidence (proposal 129) — a probability in [0, 1] or None.
# ---------------------------------------------------------------------------


def test_parse_reflection_confidence_reads_present_value() -> None:
    assert parse_reflection_confidence(_REFLECTION_CALIBRATED) == 0.75


def test_parse_reflection_confidence_absent_is_none() -> None:
    # ``_REFLECTION_WITH_MARKERS`` has no CONFIDENCE line.
    assert parse_reflection_confidence(_REFLECTION_WITH_MARKERS) is None


@pytest.mark.parametrize("raw", ["high", "", "eighty-percent"])
def test_parse_reflection_confidence_non_numeric_is_none(raw: str) -> None:
    text = f"PREDICTION: x\nCONFIDENCE: {raw}"
    assert parse_reflection_confidence(text) is None


@pytest.mark.parametrize("raw", ["1.5", "-0.1", "42"])
def test_parse_reflection_confidence_out_of_range_is_none(raw: str) -> None:
    text = f"CONFIDENCE: {raw}"
    assert parse_reflection_confidence(text) is None


@pytest.mark.parametrize("bound", ["0", "1", "0.0", "1.0"])
def test_parse_reflection_confidence_accepts_inclusive_bounds(bound: str) -> None:
    assert parse_reflection_confidence(f"CONFIDENCE: {bound}") == float(bound)


def test_parse_reflection_confidence_last_line_wins() -> None:
    """The shared last-occurrence rule: a value echoed mid-reflection must not
    shadow the genuine trailing CONFIDENCE line."""
    text = (
        "CONFIDENCE: 0.1 (an earlier draft I discarded)\n"
        "some more prose\n"
        "CONFIDENCE: 0.9"
    )
    assert parse_reflection_confidence(text) == 0.9


# ---------------------------------------------------------------------------
# Streaming early-exit must not truncate the mandatory trailing markers.
# Reproduces defect #3471417523: the 600-char ``complete_on`` early-exit cut
# the reflection before its final PREDICTION / LOSS CONDITION lines, so the
# partial buffer the SDK returns carried no markers and nothing was recorded.
# ---------------------------------------------------------------------------


def _simulate_stream_early_exit(chunks: list[str]) -> str:
    """Replay the SDK ``complete_on`` loop against ``_introspect_complete``.

    Mirrors ``maestro/sdk.py``: accumulate each streamed chunk into the
    buffer, evaluate the predicate after every chunk, and break the instant
    it fires — returning the buffer captured at that point (the partial text
    ``introspect`` would receive as its response on early exit).
    """
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        if _introspect_complete(None, buffer):
            break
    return buffer


def test_adapted_introspect_predicate_ignores_partial_loss_row() -> None:
    """#4112: under cli_paced the driver evaluates the predicate per pty
    snapshot, so a buffer can end at a bare or half-drawn ``LOSS CONDITION:``
    row. The adapted predicate must not fire until that row is complete."""
    body = "x" * 700
    for predicate, head in (
        (_introspect_complete, ""),
        (_introspect_complete_after_verdict, "PRIOR VERDICT: correct\n"),
    ):
        adapted = _adapt_complete_on(predicate)
        assert adapted is not None
        base = head + body + "\nPREDICTION: a\n"
        assert adapted.predicate(base + "LOSS CONDITION:") is False
        assert adapted.predicate(base + "LOSS CONDITION: gate wip") is False
        assert adapted.predicate(base + "LOSS CONDITION: gate wip\n") is True
        assert adapted.fired_prefix is not None
        _prediction, loss = parse_reflection_prediction(adapted.fired_prefix)
        assert loss == "gate wip"


def test_introspect_complete_preserves_trailing_markers() -> None:
    """A >600-char reflection's trailing markers survive the early exit."""
    # ~900 chars of stream-of-consciousness prose answering the prompt points,
    # followed by the mandatory trailing marker lines in later stream chunks.
    body = "I keep narrating progress I have not landed. " * 20
    chunks = [
        body,
        "\nPREDICTION: Next cycle I will run ruff and mypy on my own change.\n",
        "LOSS CONDITION: I close the cycle without a real gate exit code.\n",
    ]
    captured = _simulate_stream_early_exit(chunks)
    prediction, loss = parse_reflection_prediction(captured)
    assert prediction == "Next cycle I will run ruff and mypy on my own change."
    assert loss == "I close the cycle without a real gate exit code."


def test_introspect_complete_preserves_markers_past_old_ceiling() -> None:
    """A long high-effort reflection (>8000-char prose body) keeps its markers.

    Reproduces suggestion #3474762303: a deep reflection can emit more than the
    former 8000-char ceiling of marker-less prose *before* its mandatory
    trailing PREDICTION/LOSS CONDITION lines stream. With the old ceiling the
    early-exit fired at 8000 chars and truncated the markers, dropping the
    prediction row. The body here is ~9000 chars — past the old ceiling but
    under the raised one — so the markers must survive and parse.
    """
    body = "I sit with the gap between narration and landing for a while. " * 150
    assert len(body) > 8000
    assert len(body) < _MAX_REFLECTION_CHARS
    chunks = [
        body,
        "\nPREDICTION: Next cycle I will run ruff and mypy on my own change.\n",
        "LOSS CONDITION: I close the cycle without a real gate exit code.\n",
    ]
    captured = _simulate_stream_early_exit(chunks)
    prediction, loss = parse_reflection_prediction(captured)
    assert prediction == "Next cycle I will run ruff and mypy on my own change."
    assert loss == "I close the cycle without a real gate exit code."


def test_introspect_complete_terminates_without_markers() -> None:
    """The predicate still terminates if markers never arrive (char ceiling)."""
    # An off-prompt reply that never emits the markers must not stream forever.
    chunks = ["x" * 1000 for _ in range(40)]  # 40k chars, no markers
    captured = _simulate_stream_early_exit(chunks)
    assert _introspect_complete(None, captured) is True


# ---------------------------------------------------------------------------
# Echoed-prior-marker early-exit must not truncate the genuine new bet.
# Reproduces defect #3474762297: from cycle 2 onward the prompt injects the
# prior bet as two literal `PREDICTION:` / `LOSS CONDITION:` lines and asks the
# model to confront it. If the model quotes those lines while confronting
# (before it has written `PRIOR VERDICT:` or its genuine new trailing bet) and
# the buffer is already ≥600 chars, the non-verdict-aware predicate fires on the
# echoed markers and truncates the stream — so the parser re-records the prior
# cycle's prediction as this cycle's "new" bet.
# ---------------------------------------------------------------------------

# A realistic priced reflection: the model first confronts the injected prior
# bet by quoting its two marker lines verbatim, crosses the 600-char floor, and
# only then renders its PRIOR VERDICT and its genuine, new trailing bet.
_PRIOR_ECHO_CHUNKS = [
    "Confronting last cycle's bet, which read:\n"
    "  PREDICTION: Old cycle bet that I must now judge.\n"
    "  LOSS CONDITION: Old loss condition that I must now judge.\n"
    + "I sit with whether that bet won or lost for a long honest while. " * 12,
    "\nPRIOR VERDICT: wrong\n",
    "PREDICTION: Next cycle I will run ruff and mypy on my own change.\n",
    "LOSS CONDITION: I close the cycle without a real gate exit code.\n",
]


def _simulate_stream_after_verdict(chunks: list[str]) -> str:
    """Replay the ``complete_on`` loop against the verdict-aware predicate."""
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        if _introspect_complete_after_verdict(None, buffer):
            break
    return buffer


def test_plain_predicate_truncates_on_echoed_prior_markers() -> None:
    """The non-verdict-aware predicate fires on echoed prior markers (the defect).

    This documents *why* the prior-bet path needs the verdict-aware predicate:
    replaying the plain predicate against an echoed prior bet truncates before
    the genuine trailing markers, so the parser yields the prior cycle's bet.
    """
    # The first chunk alone crosses the 600-char floor and carries both echoed
    # marker lines, so the plain predicate fires immediately.
    assert len(_PRIOR_ECHO_CHUNKS[0]) >= 600
    captured = _simulate_stream_early_exit(_PRIOR_ECHO_CHUNKS)
    prediction, _ = parse_reflection_prediction(captured)
    assert prediction == "Old cycle bet that I must now judge."


def test_after_verdict_predicate_survives_echoed_prior_markers() -> None:
    """The verdict-aware predicate streams past echoed prior markers.

    It only treats the bet as complete once the markers appear AFTER the
    `PRIOR VERDICT:` line, so the genuine new trailing bet is parsed — not the
    echoed prior one.
    """
    captured = _simulate_stream_after_verdict(_PRIOR_ECHO_CHUNKS)
    prediction, loss = parse_reflection_prediction(captured)
    assert prediction == "Next cycle I will run ruff and mypy on my own change."
    assert loss == "I close the cycle without a real gate exit code."


def test_after_verdict_predicate_waits_for_verdict_line() -> None:
    """No `PRIOR VERDICT:` line yet → never fire, even past the floor.

    Until the verdict line streams, any marker lines present are echoes of the
    injected prior bet (the confront section precedes the genuine new bet).
    """
    echoed = (
        "  PREDICTION: echoed prior bet.\n"
        "  LOSS CONDITION: echoed prior loss.\n"
        + "x" * 800
    )
    assert _introspect_complete_after_verdict(None, echoed) is False


def test_after_verdict_predicate_ceiling_still_terminates() -> None:
    """The char ceiling remains a termination fallback for the verdict path."""
    # past the ceiling, no verdict/markers at all
    buffer = "x" * (_MAX_REFLECTION_CHARS + 1000)
    assert _introspect_complete_after_verdict(None, buffer) is True


def test_has_prediction_markers_after_verdict_segment() -> None:
    """`after_prior_verdict` only counts markers following the verdict line."""
    # Echoed markers before the verdict line do not satisfy the predicate.
    before = (
        "PREDICTION: echoed.\nLOSS CONDITION: echoed.\nPRIOR VERDICT: wrong\n"
    )
    assert has_prediction_markers(before, after_prior_verdict=True) is False
    # Genuine markers after the verdict line do.
    after = before + "PREDICTION: new.\nLOSS CONDITION: new.\n"
    assert has_prediction_markers(after, after_prior_verdict=True) is True
    # No verdict line at all → not satisfied under the verdict-aware rule.
    no_verdict = "PREDICTION: echoed.\nLOSS CONDITION: echoed.\n"
    assert has_prediction_markers(no_verdict, after_prior_verdict=True) is False
    # Default behaviour is unchanged (whole-text scan).
    assert has_prediction_markers(no_verdict) is True


# ---------------------------------------------------------------------------
# Tool-loop-safe completion (proposal 132): with grounded reflection on, the
# priced session may stream a *tool-use* assistant turn. The early-exit
# predicates must never break the stream while a tool call is pending — breaking
# mid-tool-loop would leave the shared slot-A session with a dangling call before
# update_self_model's turn. A tool-use message returns False regardless of what
# the accumulated text holds.
# ---------------------------------------------------------------------------


def _tool_use_message(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[
            TextBlock(text=text),
            ToolUseBlock(id="t1", name="read_journal", input={}),
        ],
        model="claude-sonnet-4-20250514",
    )


def test_priced_predicates_false_on_tool_use_even_with_markers() -> None:
    """A tool-use turn never completes the stream, even if the buffer has markers."""
    # A buffer that WOULD complete both predicates: >600 chars, PRIOR VERDICT
    # then both markers after it.
    completing = (
        "x" * 700
        + "\nPRIOR VERDICT: wrong\nPREDICTION: p\nLOSS CONDITION: l\n"
    )
    # Sanity: without a tool-use message the buffer completes both predicates.
    assert _introspect_complete(None, completing) is True
    assert _introspect_complete_after_verdict(None, completing) is True

    message = _tool_use_message("some grounding narration")
    assert _introspect_complete(message, completing) is False
    assert _introspect_complete_after_verdict(message, completing) is False


def test_priced_predicates_false_on_tool_use_past_ceiling() -> None:
    """A tool-use turn short-circuits before the char-ceiling fallback too."""
    huge = "x" * (_MAX_REFLECTION_CHARS + 1000)  # would hit the ceiling → True
    assert _introspect_complete(None, huge) is True

    message = _tool_use_message("still working")
    assert _introspect_complete(message, huge) is False
    assert _introspect_complete_after_verdict(message, huge) is False


def test_json_complete_tool_loop_safe_false_on_tool_use_even_with_json() -> None:
    """update_self_model's session predicate never breaks the stream mid-tool-loop.

    Ledger r1: ``update_self_model``'s session path shares the tool-bearing slot-A
    session with ``introspect``, so its completion predicate must be tool-loop-safe
    too. A tool-use turn returns ``False`` even when the buffer already parses as
    JSON (a bare ``json_complete`` would fire and drop the pending tool call).
    """
    parseable = '{"values": ["x"]}'
    # Sanity: without a tool-use message a parseable buffer completes.
    assert json_complete_tool_loop_safe(None, parseable) is True

    message = _tool_use_message("let me read the journal first")
    assert json_complete_tool_loop_safe(message, parseable) is False


def test_json_complete_tool_loop_safe_delegates_when_no_tool_use() -> None:
    """A non-tool-use message delegates to the shared ``json_complete`` verbatim."""
    from maestro.sdk import json_complete

    text_only = AssistantMessage(
        content=[TextBlock(text='{"values": ["x"]}')],
        model="claude-sonnet-4-20250514",
    )
    # Parseable JSON completes; incomplete text does not — matching json_complete.
    assert json_complete_tool_loop_safe(text_only, '{"values": ["x"]}') is True
    assert json_complete_tool_loop_safe(text_only, "not json yet") is False
    assert json_complete(text_only, "not json yet") is False


@pytest.mark.asyncio
async def test_tool_loop_stream_early_exits_on_trailing_markers_only() -> None:
    """Interleaved tool-use stream: exit on the trailing bet turn, anchoring held.

    Replays the real ``_consume_messages`` loop via ``query`` +
    ``patch("maestro.sdk._query")`` (the ``test_tool_call_flow`` pattern):
    assistant(confront prose + PRIOR VERDICT + ToolUseBlock) →
    user(ToolResultBlock larger than the reflection ceiling) →
    assistant(the genuine trailing markers). The first (tool-use) turn does not
    early-exit; the over-ceiling tool result is a UserMessage so the predicate is
    never evaluated on it and its payload never enters the text buffer; and the
    stream exits on the trailing turn, whose buffer parses to the *new* bet (the
    prior-verdict anchoring holds).
    """
    confront_text = (
        "Confronting last cycle's bet.\n"
        "PRIOR VERDICT: wrong\n"
        + "I sit with whether that bet won or lost for a long honest while. " * 12
    )
    final_text = (
        "\nPREDICTION: Next cycle I will run ruff and mypy on my own change.\n"
        "LOSS CONDITION: I close the cycle without a real gate exit code.\n"
    )
    huge_payload = "z" * (_MAX_REFLECTION_CHARS + 5000)

    async def gen(**kwargs: Any) -> Any:
        yield _tool_use_message(confront_text)
        yield UserMessage(
            content=[ToolResultBlock(tool_use_id="t1", content=huge_payload)],
        )
        yield AssistantMessage(
            content=[TextBlock(text=final_text)],
            model="claude-sonnet-4-20250514",
        )

    with patch("maestro.sdk._query", side_effect=gen):
        result = await query(
            prompt="reflect",
            complete_on=_introspect_complete_after_verdict,
        )

    # Exited on the trailing turn: the buffer is exactly the two assistant
    # texts (a first-turn exit would carry only ``confront_text``).
    assert result.response == confront_text + final_text
    # The over-ceiling tool result never entered the buffer.
    assert huge_payload not in result.response
    # Prior-verdict anchoring holds: the genuine new bet is parsed, not the echo.
    prediction, loss = parse_reflection_prediction(result.response)
    assert prediction == "Next cycle I will run ruff and mypy on my own change."
    assert loss == "I close the cycle without a real gate exit code."


@pytest.mark.asyncio
async def test_contemplate_records_prediction_through_streaming_path(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """End-to-end: a full-length reflection's late markers survive the stream.

    Defect #3471417526 — the original ``_run_contemplate`` fake returned the
    whole canned response, bypassing ``complete_on`` so the truncation was
    invisible. Driving ``contemplate`` through a ``complete_on``-honouring fake
    with a >600-char reflection (markers as the final two lines) reproduces the
    real early-exit: pre-fix the markers are truncated and NO prediction row is
    written; post-fix the full reflection streams and exactly one row lands.
    """
    agent = make_agent()
    await _run_contemplate_streaming(agent, _LONG_REFLECTION_CHUNKS)

    path_str = str(Path("/fake/agent") / PREDICTIONS_FILENAME)
    # Pre-fix this file is never created (truncated reflection → empty parse).
    assert path_str in mem_fs
    rows = [
        json.loads(line)
        for line in mem_fs[path_str].strip().splitlines()
        if line.strip()
    ]
    prediction_rows = [r for r in rows if r.get("source") == "reflection"]
    assert len(prediction_rows) == 1
    row = prediction_rows[0]
    assert row["prediction"] == (
        "Next cycle I will run ruff and mypy on my own change."
    )
    assert row["loss_condition"] == (
        "I close the cycle without a real gate exit code."
    )


def test_parse_reflection_verdict_absent_returns_empty() -> None:
    assert parse_reflection_verdict("no verdict line here") == ""


def test_parse_reflection_verdict_correct() -> None:
    assert parse_reflection_verdict("PRIOR VERDICT: correct\n") == "correct"


def test_parse_reflection_verdict_incorrect_is_wrong() -> None:
    # "incorrect" contains "correct" as a substring — it must score as a loss,
    # not be silently inverted into a win.
    assert parse_reflection_verdict("PRIOR VERDICT: incorrect\n") == "wrong"


# ---------------------------------------------------------------------------
# parse_reflection_verdict must not flip negations to 'correct'.
# Reproduces the bug where 'correct' (a substring of 'incorrect' / 'not
# correct') was checked first and returned for the most natural negation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("PRIOR VERDICT: incorrect", "wrong"),
        ("PRIOR VERDICT: wrong, my prediction was not correct", "wrong"),
        ("PRIOR VERDICT: not correct", "wrong"),
        ("PRIOR VERDICT: that was incorrect", "wrong"),
        ("PRIOR VERDICT: correct", "correct"),
        ("PRIOR VERDICT: that was correct", "correct"),
        ("PRIOR VERDICT: undecided", ""),
    ],
)
def test_parse_reflection_verdict_does_not_flip_negation(
    line: str, expected: str,
) -> None:
    assert parse_reflection_verdict(line) == expected


# ---------------------------------------------------------------------------
# parse_reflection_verdict must not flip a 'correct' verdict whose explanatory
# prose merely *mentions* 'wrong'. The leftmost whole-word verdict token wins,
# so a leading 'correct' beats a trailing prose 'wrong' (defect 3475221248).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "PRIOR VERDICT: correct — my worry it would go wrong was unfounded",
            "correct",
        ),
        ("PRIOR VERDICT: correct, not wrong this time", "correct"),
        ("PRIOR VERDICT: wrong — I had hoped to be correct", "wrong"),
    ],
)
def test_parse_reflection_verdict_prose_does_not_invert(
    line: str, expected: str,
) -> None:
    assert parse_reflection_verdict(line) == expected


# ---------------------------------------------------------------------------
# has_prediction_markers requires a line *prefix*, matching the parser, so the
# early-exit predicate cannot fire on a mid-prose mention of the marker words.
# Reproduces defect #3474336958 (predicate used substring `in`, parser used
# `startswith`).
# ---------------------------------------------------------------------------


def test_has_prediction_markers_true_for_line_prefixes() -> None:
    text = (
        "Some reflection prose.\n"
        "PREDICTION: I will run the gate.\n"
        "LOSS CONDITION: I skip the gate.\n"
    )
    assert has_prediction_markers(text) is True


def test_has_prediction_markers_false_for_midprose_mentions() -> None:
    # The model talks *about* a prediction / loss condition without emitting
    # the binding marker lines — must NOT satisfy the predicate.
    text = (
        "Let me state my prediction: I think I will improve, and the "
        "loss condition: would be if I do not.\n"
        "I keep going for a while longer here.\n"
    )
    assert has_prediction_markers(text) is False


def test_introspect_complete_ignores_midprose_marker_words() -> None:
    """A >600-char buffer with only mid-prose marker words must not exit."""
    prose = (
        "Let me state my prediction: I will improve, and the loss "
        "condition: is failure. " * 20
    )
    assert len(prose) >= 600
    assert _introspect_complete(None, prose) is False


def test_introspect_complete_warns_on_ceiling_without_markers(
    caplog,  # type: ignore[no-untyped-def]
) -> None:
    """Hitting the char ceiling without markers logs a WARNING and terminates."""
    import logging

    buffer = "x" * (_MAX_REFLECTION_CHARS + 1000)  # past ceiling, no markers
    with caplog.at_level(logging.WARNING, logger="maestro"):
        assert _introspect_complete(None, buffer) is True
    assert any(
        "ceiling without both" in rec.message.lower()
        or "ceiling" in rec.message.lower()
        for rec in caplog.records
    )


def test_introspect_complete_no_warning_when_markers_present(
    caplog,  # type: ignore[no-untyped-def]
) -> None:
    """A long buffer that DOES carry the markers terminates without warning."""
    import logging

    buffer = (
        "x" * (_MAX_REFLECTION_CHARS + 1000)
        + "\nPREDICTION: do the thing\nLOSS CONDITION: skip the thing\n"
    )
    with caplog.at_level(logging.WARNING, logger="maestro"):
        assert _introspect_complete(None, buffer) is True
    assert not [r for r in caplog.records if "ceiling" in r.message.lower()]


# ---------------------------------------------------------------------------
# Unpriced introspect (evolve path) early-exits on the character floor and the
# evolve caller omits PREDICTION_REQUIREMENT — it must not manufacture a bet
# that is never recorded or confronted (#3471681804).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_introspect_priced_prompt_appends_requirement(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """The reflection appends the requirement and the priced predicate."""
    agent = make_agent()
    captured: dict[str, object] = {}

    async def _fake_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return MagicMock(response=_REFLECTION_WITH_MARKERS)

    with patch("maestro.consciousness.query", side_effect=_fake_query):
        await agent.consciousness.introspect(
            [], agent.journal,
        )

    assert PREDICTION_REQUIREMENT in captured["prompt"]  # type: ignore[operator]
    assert captured["complete_on"] is _introspect_complete


@pytest.mark.asyncio
async def test_introspect_priced_with_prior_bet_uses_verdict_aware_predicate(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """When the prior bet is injected, introspect uses the verdict-aware predicate.

    The prior-bet block carries a `PRIOR VERDICT:` marker; its presence in the
    measured context switches the early-exit to the variant that survives echoed prior
    markers (defect #3474762297).
    """
    from maestro.prompts.reflection import format_prior_prediction_block

    agent = make_agent()
    captured: dict[str, object] = {}

    async def _fake_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return MagicMock(response=_REFLECTION_WITH_MARKERS)

    prior_block = format_prior_prediction_block(
        "old prediction", "old loss", "unscored",
    )
    with patch("maestro.consciousness.query", side_effect=_fake_query):
        await agent.consciousness.introspect(
            [], agent.journal, measured_context=prior_block,
        )

    assert captured["complete_on"] is _introspect_complete_after_verdict


def test_banned_stale_loss_conditions_folds_full_ledger(
    mem_fs: dict[str, str],
) -> None:
    """The composer bans a loss condition staked twice, none scoring 'correct'.

    It folds open (unscored/pending) and scored bets together: two prediction
    rows carry the same loss condition (modulo whitespace/case) and neither
    scores 'correct' (one is scored 'wrong', one left unscored), so the composer
    returns the normalised string. A distinct once-staked loser stays below
    threshold and is excluded.
    """
    backend = JsonlPredictionLedger(Path("/fake/pred"))
    record_reflection_prediction(backend, "reflection-1", "p", "pr #7 settles")
    record_reflection_verdict(backend, "reflection-1", "wrong")
    record_reflection_prediction(backend, "reflection-2", "p", "PR #7  settles")
    record_reflection_prediction(backend, "reflection-3", "p", "gate g fires")
    assert banned_stale_loss_conditions(backend) == frozenset({"pr #7 settles"})


def test_banned_stale_loss_conditions_excludes_ever_correct(
    mem_fs: dict[str, str],
) -> None:
    """One 'correct' scoring excludes the loss condition regardless of repeats."""
    backend = JsonlPredictionLedger(Path("/fake/pred"))
    record_reflection_prediction(backend, "reflection-1", "p", "pr #7 settles")
    record_reflection_verdict(backend, "reflection-1", "correct")
    record_reflection_prediction(backend, "reflection-2", "p", "pr #7 settles")
    assert banned_stale_loss_conditions(backend) == frozenset()


def test_stale_repeat_introspect_kwargs_off_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """Guard off: composer returns ``{}`` without folding the ledger."""
    backend = JsonlPredictionLedger(Path("/fake/pred"))
    record_reflection_prediction(backend, "reflection-1", "p", "pr #7 settles")
    record_reflection_prediction(backend, "reflection-2", "p", "pr #7 settles")
    assert stale_repeat_introspect_kwargs(backend, enabled=False) == {}


def test_stale_repeat_introspect_kwargs_on_no_banned_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """Guard on but nothing stale-repeat-banned: composer still returns ``{}``.

    A single once-staked loser stays below threshold, so the priced prompt stays
    byte-identical to the pre-guard path.
    """
    backend = JsonlPredictionLedger(Path("/fake/pred"))
    record_reflection_prediction(backend, "reflection-1", "p", "pr #7 settles")
    assert stale_repeat_introspect_kwargs(backend, enabled=True) == {}


def test_stale_repeat_introspect_kwargs_on_banned_returns_set(
    mem_fs: dict[str, str],
) -> None:
    """Guard on with a twice-staled loser: composer keys the banned set."""
    backend = JsonlPredictionLedger(Path("/fake/pred"))
    record_reflection_prediction(backend, "reflection-1", "p", "pr #7 settles")
    record_reflection_prediction(backend, "reflection-2", "p", "PR #7  settles")
    assert stale_repeat_introspect_kwargs(backend, enabled=True) == {
        "banned_loss_conditions": frozenset({"pr #7 settles"}),
    }


@pytest.mark.asyncio
async def test_contemplate_threads_banned_loss_conditions_when_flag_on(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Stale-repeat guard on: a twice-staled loss condition reaches the ban block.

    Two 'unscored' prediction rows carry the same loss condition (the comfort-bet
    signature), so ``contemplate`` computes it as banned and threads it into the
    priced prompt, which names it under a "do not reuse" block.
    """
    agent = make_agent()
    agent.config.stale_repeat_bet_guard_enabled = True
    loss = "pr #10 settles within 5 cycles"
    record_reflection_prediction(agent.predictions, "reflection-1", "p", loss)
    record_reflection_prediction(agent.predictions, "reflection-2", "p", loss)

    prompt = await _run_contemplate(agent, _REFLECTION_WITH_MARKERS)

    assert "do not reuse" in prompt.lower()
    assert loss in prompt


@pytest.mark.asyncio
async def test_contemplate_omits_ban_block_when_flag_off(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Stale-repeat guard off (default): no ban block even with a staled loser.

    The banned set is never computed, so the priced prompt carries no "do not
    reuse" block — byte-for-byte the pre-guard behaviour.
    """
    agent = make_agent()
    assert agent.config.stale_repeat_bet_guard_enabled is False
    loss = "pr #10 settles within 5 cycles"
    record_reflection_prediction(agent.predictions, "reflection-1", "p", loss)
    record_reflection_prediction(agent.predictions, "reflection-2", "p", loss)

    prompt = await _run_contemplate(agent, _REFLECTION_WITH_MARKERS)

    assert "do not reuse" not in prompt.lower()


# NOTE: ``test_evolve_introspection_is_unpriced`` was removed — evolve()'s
# standalone introspect call was deleted (follow-up to 823d) and proposal 149
# Step 1 then deleted the unpriced introspect branch itself. evolve() now
# journals the reflection from ``evolution.reflect``; the "no introspect on any
# path" contract is covered by tests/test_agent_evolve_gating.py.


# ---------------------------------------------------------------------------
# Criterion 2 — exactly one prediction row with a non-empty loss_condition.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contemplate_writes_one_prediction_row(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """One reflection cycle appends exactly one prediction row."""
    agent = make_agent()
    await _run_contemplate(agent, _REFLECTION_WITH_MARKERS)

    path_str = str(Path("/fake/agent") / PREDICTIONS_FILENAME)
    assert path_str in mem_fs
    rows = [
        json.loads(line)
        for line in mem_fs[path_str].strip().splitlines()
    ]
    prediction_rows = [r for r in rows if r.get("source") == "reflection"]
    assert len(prediction_rows) == 1
    row = prediction_rows[0]
    assert row["loss_condition"]  # non-empty
    assert row["prediction"].startswith("Next cycle I will run ruff")
    assert row["verdict"] == "unscored"
    # Self-calibration is off by default (make_agent), so the row is byte-identical
    # to before proposal 129 — neither additive field is written.
    assert "confidence" not in row
    assert "loss_condition_kind" not in row


@pytest.mark.asyncio
async def test_contemplate_calibrated_row_carries_confidence_and_kind(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Flag on + a grammar-conforming bet: the row gains the stated confidence and
    the parsed ProbeKind."""
    agent = make_agent()
    agent.config.audit_self_calibration_enabled = True
    await _run_contemplate(agent, _REFLECTION_CALIBRATED)

    path_str = str(Path("/fake/agent") / PREDICTIONS_FILENAME)
    rows = [
        json.loads(line)
        for line in mem_fs[path_str].strip().splitlines()
    ]
    prediction_rows = [r for r in rows if r.get("source") == "reflection"]
    assert len(prediction_rows) == 1
    row = prediction_rows[0]
    assert row["confidence"] == 0.75
    assert row["loss_condition_kind"] == "GATE_FIRING"


@pytest.mark.asyncio
async def test_contemplate_calibrated_confidence_survives_streaming_early_exit(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Under a real streaming early-exit, the CONFIDENCE line — placed first —
    reaches the recorded row (proposal 129 ordering guard).

    ``_run_contemplate_streaming`` honours ``complete_on``, so the buffer is cut
    the instant PREDICTION and LOSS CONDITION are both present. Because CONFIDENCE
    streams before them, it is captured and stamped; a CONFIDENCE line placed
    after the markers would be truncated away (the defect the ordering avoids).
    """
    agent = make_agent()
    agent.config.audit_self_calibration_enabled = True
    await _run_contemplate_streaming(agent, _CALIBRATED_STREAM_CHUNKS)

    path_str = str(Path("/fake/agent") / PREDICTIONS_FILENAME)
    rows = [
        json.loads(line)
        for line in mem_fs[path_str].strip().splitlines()
    ]
    prediction_rows = [r for r in rows if r.get("source") == "reflection"]
    assert len(prediction_rows) == 1
    row = prediction_rows[0]
    assert row["confidence"] == 0.6
    assert row["loss_condition_kind"] == "GATE_FIRING"


@pytest.mark.asyncio
async def test_contemplate_flag_on_but_unparseable_omits_both_keys(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Flag on, but the loss condition is free text the grammar declines and no
    CONFIDENCE line is stated: both keys are omitted (the unfalsifiable signal),
    so the row stays byte-identical to the flag-off row."""
    agent = make_agent()
    agent.config.audit_self_calibration_enabled = True
    await _run_contemplate(agent, _REFLECTION_WITH_MARKERS)

    path_str = str(Path("/fake/agent") / PREDICTIONS_FILENAME)
    rows = [
        json.loads(line)
        for line in mem_fs[path_str].strip().splitlines()
    ]
    prediction_rows = [r for r in rows if r.get("source") == "reflection"]
    assert len(prediction_rows) == 1
    row = prediction_rows[0]
    assert "confidence" not in row
    assert "loss_condition_kind" not in row


@pytest.mark.asyncio
async def test_contemplate_no_markers_writes_no_row(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A reflection lacking the markers writes no prediction row."""
    agent = make_agent()
    await _run_contemplate(agent, "Just musing, no structured prediction.")

    path_str = str(Path("/fake/agent") / PREDICTIONS_FILENAME)
    rows = []
    if path_str in mem_fs:
        rows = [
            json.loads(line)
            for line in mem_fs[path_str].strip().splitlines()
            if line.strip()
        ]
    assert [r for r in rows if r.get("source") == "reflection"] == []


# ---------------------------------------------------------------------------
# Criterion 3 — prior prediction + scored label injected into the SDK prompt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contemplate_injects_prior_unscored_prediction(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """An unscored prior bet appears in the prompt labelled 'unscored'."""
    agent = make_agent()
    record_reflection_prediction(
        agent.predictions,
        "reflection-1",
        "I will land a change on main.",
        "No commit reaches origin/main.",
    )
    prompt = await _run_contemplate(agent, _REFLECTION_WITH_MARKERS)

    assert "I will land a change on main." in prompt
    assert "No commit reaches origin/main." in prompt
    assert "unscored" in prompt


@pytest.mark.asyncio
async def test_contemplate_injects_prior_wrong_verdict(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A prior bet already scored 'wrong' is injected with that label."""
    agent = make_agent()
    record_reflection_prediction(
        agent.predictions,
        "reflection-1",
        "I will land a change on main.",
        "No commit reaches origin/main.",
    )
    record_reflection_verdict(
        agent.predictions, "reflection-1", "wrong",
    )
    prompt = await _run_contemplate(agent, _REFLECTION_WITH_MARKERS)

    assert "I will land a change on main." in prompt
    assert "Its scored outcome so far: wrong." in prompt


@pytest.mark.asyncio
async def test_contemplate_omits_block_when_no_prior(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """With no prior prediction the confront-block is absent from the prompt."""
    agent = make_agent()
    prompt = await _run_contemplate(agent, _REFLECTION_WITH_MARKERS)
    assert "Confront Your Last Bet" not in prompt


@pytest.mark.asyncio
async def test_contemplate_scores_prior_with_parsed_verdict(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """The prior unscored bet is scored from the new reflection's verdict."""
    agent = make_agent()
    record_reflection_prediction(
        agent.predictions,
        "reflection-1",
        "I will land a change on main.",
        "No commit reaches origin/main.",
    )
    await _run_contemplate(agent, _REFLECTION_WITH_MARKERS)

    # The new reflection said "PRIOR VERDICT: wrong"; a verdict row keyed to
    # reflection-1 must now exist.
    path_str = str(Path("/fake/agent") / PREDICTIONS_FILENAME)
    rows = [
        json.loads(line)
        for line in mem_fs[path_str].strip().splitlines()
        if line.strip()
    ]
    verdicts = [r for r in rows if r.get("source") == "reflection_verdict"]
    assert len(verdicts) == 1
    assert verdicts[0]["prediction_id"] == "reflection-1"
    assert verdicts[0]["verdict"] == "wrong"

    # And the prior bet now resolves as 'wrong'.
    latest = load_latest_reflection_prediction(agent.predictions)
    assert latest is not None


# ---------------------------------------------------------------------------
# Proposal 132 step 5 — grounded-reflection prompt steering threads through
# introspect and contemplate under the same predicate.
# ---------------------------------------------------------------------------


async def _capture_introspect_prompt(
    agent,  # type: ignore[no-untyped-def]
    *,
    grounded_reflection_active: bool,
) -> str:
    """Drive a priced ``introspect`` with a mocked SDK; return the built prompt."""
    captured: list[str] = []

    async def _fake_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(kwargs.get("prompt", ""))
        return MagicMock(response=_REFLECTION_WITH_MARKERS)

    with patch("maestro.consciousness.query", side_effect=_fake_query):
        await agent.consciousness.introspect(
            [],
            agent.journal,
            grounded_reflection_active=grounded_reflection_active,
        )
    assert len(captured) == 1
    return captured[0]


@pytest.mark.asyncio
async def test_introspect_renders_grounded_steering_when_active(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """``grounded_reflection_active=True`` renders the read-only tool steering into
    the priced prompt: every allowlisted tool is named and ``Bash`` is forbidden."""
    agent = make_agent()
    prompt = await _capture_introspect_prompt(agent, grounded_reflection_active=True)
    assert "Bash" in prompt
    for tool in READ_ONLY_REFLECTION_TOOLS:
        assert tool in prompt


@pytest.mark.asyncio
async def test_introspect_omits_grounded_steering_when_inactive(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """``grounded_reflection_active=False`` (the default) leaves the priced prompt
    free of tool-surface steering — no tool names, no ``Bash``/shell language."""
    agent = make_agent()
    prompt = await _capture_introspect_prompt(agent, grounded_reflection_active=False)
    assert "Bash" not in prompt
    for tool in READ_ONLY_REFLECTION_TOOLS:
        assert tool not in prompt


@pytest.mark.asyncio
async def test_contemplate_threads_grounded_predicate_when_active(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """``contemplate`` passes ``_grounded_reflection_active()`` into ``introspect``:
    when the predicate is true the steering is present in the built prompt."""
    agent = make_agent()
    with patch.object(agent, "_grounded_reflection_active", return_value=True):
        prompt = await _run_contemplate(agent, _REFLECTION_WITH_MARKERS)
    assert "Bash" in prompt
    for tool in READ_ONLY_REFLECTION_TOOLS:
        assert tool in prompt


@pytest.mark.asyncio
async def test_contemplate_omits_grounded_steering_when_predicate_false(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """When ``_grounded_reflection_active()`` is false, ``contemplate`` renders no
    tool-surface steering — the flag-off/throttled path stays byte-clean."""
    agent = make_agent()
    with patch.object(agent, "_grounded_reflection_active", return_value=False):
        prompt = await _run_contemplate(agent, _REFLECTION_WITH_MARKERS)
    assert "Bash" not in prompt
    for tool in READ_ONLY_REFLECTION_TOOLS:
        assert tool not in prompt


# ---------------------------------------------------------------------------
# Criterion 4 — measure_prediction_accuracy counts reflection rows.
# ---------------------------------------------------------------------------


def test_measure_counts_reflection_row_with_override(
    mem_fs: dict[str, str],
) -> None:
    """A reflection prediction scored by a verdict row is counted correctly."""
    d = Path("/fake/measure")
    record_reflection_prediction(JsonlPredictionLedger(d), "reflection-7", "p", "loss")
    record_reflection_verdict(JsonlPredictionLedger(d), "reflection-7", "correct")

    result = measure_prediction_accuracy(JsonlPredictionLedger(d), [])
    assert result["total"] == 1
    assert result["correct"] == 1
    by_shape = result["by_shape"]
    assert isinstance(by_shape, dict)
    assert by_shape["reflection"] == {"total": 1, "correct": 1}


def test_measure_counts_self_contained_reflection_row(
    mem_fs: dict[str, str],
) -> None:
    """A reflection row whose own verdict is set is counted without override."""
    d = Path("/fake/measure")
    path = d / PREDICTIONS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "source": "reflection",
        "prediction_id": "reflection-3",
        "prediction": "p",
        "loss_condition": "loss",
        "verdict": "wrong",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")

    result = measure_prediction_accuracy(JsonlPredictionLedger(d), [])
    assert result["total"] == 1
    assert result["correct"] == 0


def test_measure_excludes_unscored_reflection_row(
    mem_fs: dict[str, str],
) -> None:
    """An unscored reflection prediction is excluded from the count."""
    d = Path("/fake/measure")
    record_reflection_prediction(JsonlPredictionLedger(d), "reflection-9", "p", "loss")
    result = measure_prediction_accuracy(JsonlPredictionLedger(d), [])
    assert result["total"] == 0


def test_measure_mixes_reflection_and_task_rows(
    mem_fs: dict[str, str],
) -> None:
    """Reflection rows add to task-prediction counts, not replace them."""
    d = Path("/fake/measure")
    record_prediction(JsonlPredictionLedger(d), "t1", "success")
    record_reflection_prediction(JsonlPredictionLedger(d), "reflection-1", "p", "loss")
    record_reflection_verdict(JsonlPredictionLedger(d), "reflection-1", "correct")

    task_history: list[dict[str, object]] = [
        {"task_id": "t1", "outcome": "success", "goal": "fix #1"},
    ]
    result = measure_prediction_accuracy(JsonlPredictionLedger(d), task_history)
    assert result["total"] == 2
    assert result["correct"] == 2


# ---------------------------------------------------------------------------
# _read_prediction_rows guard branches.
# ---------------------------------------------------------------------------


def test_read_prediction_rows_skips_blank_malformed_and_nondict(
    mem_fs: dict[str, str],
) -> None:
    """Blank, non-JSON, and non-dict lines are skipped; dict rows kept."""
    d = Path("/fake/measure")
    path = d / PREDICTIONS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n")  # blank
        f.write("not valid json\n")  # malformed
        f.write("123\n")  # valid JSON but not a dict
        f.write(json.dumps({"source": "reflection", "ok": True}) + "\n")

    rows = _read_prediction_rows(path)
    assert len(rows) == 1
    assert rows[0]["ok"] is True


def test_read_prediction_rows_missing_file_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """A missing predictions file yields an empty list."""
    assert _read_prediction_rows(Path("/fake/missing") / PREDICTIONS_FILENAME) == []


def test_read_prediction_rows_os_error_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """An OSError while reading yields an empty list."""
    d = Path("/fake/measure")
    record_reflection_prediction(JsonlPredictionLedger(d), "reflection-1", "p", "loss")
    path = d / PREDICTIONS_FILENAME
    path_str = str(path)
    original_open = (
        __builtins__["open"]
        if isinstance(__builtins__, dict)
        else __builtins__.open  # type: ignore[union-attr]
    )

    def error_open(
        file: object, mode: str = "r", *a: object, **kw: object,
    ) -> object:
        if str(file) == path_str and "r" in mode:
            raise OSError("simulated read error")
        return original_open(file, mode, *a, **kw)  # type: ignore[arg-type]

    with patch("builtins.open", side_effect=error_open):
        assert _read_prediction_rows(path) == []


# ---------------------------------------------------------------------------
# contemplate recording is best-effort.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contemplate_recording_failure_is_swallowed(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A failure persisting the prediction must not crash contemplation."""
    agent = make_agent()

    async def _fake_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        return MagicMock(response=_REFLECTION_WITH_MARKERS)

    with patch(
        "maestro.diagnostics.build_diagnostic_snapshot",
        return_value=_clean_snapshot(),
    ), patch("maestro.consciousness.query", side_effect=_fake_query), \
         patch.object(
             agent.consciousness,
             "update_self_model",
             AsyncMock(return_value=SelfModel()),
         ), patch.object(agent.journal, "write_thought"), patch(
             "maestro.loop.contemplation.record_reflection_prediction",
             side_effect=RuntimeError("disk full"),
         ):
        result = await agent.contemplate(session=None)

    assert result == _REFLECTION_WITH_MARKERS


# ---------------------------------------------------------------------------
# Proposal 129 — calibration verdict authority: the oracle authors the verdict.
# ---------------------------------------------------------------------------


async def _run_contemplate_at(
    agent,  # type: ignore[no-untyped-def]
    response_text: str,
    iteration: int,
) -> None:
    """Drive ``contemplate`` at an explicit *iteration* (mocked SDK)."""

    async def _fake_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        return MagicMock(response=response_text)

    with patch(
        "maestro.diagnostics.build_diagnostic_snapshot",
        return_value=_clean_snapshot(),
    ), patch("maestro.consciousness.query", side_effect=_fake_query), \
         patch.object(
             agent.consciousness,
             "update_self_model",
             AsyncMock(return_value=SelfModel()),
         ), patch.object(agent.journal, "write_thought"):
        await agent.contemplate(session=None, iteration=iteration)


def _reflection_verdict_rows(
    mem_fs: dict[str, str], prediction_id: str | None = None,
) -> list[dict[str, object]]:
    """Return the ``reflection_verdict`` rows persisted for the fixture agent."""
    path_str = str(Path("/fake/agent") / PREDICTIONS_FILENAME)
    if path_str not in mem_fs:
        return []
    rows = [
        json.loads(line)
        for line in mem_fs[path_str].strip().splitlines()
        if line.strip()
    ]
    verdicts = [r for r in rows if r.get("source") == "reflection_verdict"]
    if prediction_id is not None:
        verdicts = [r for r in verdicts if r.get("prediction_id") == prediction_id]
    return verdicts


def _calibration_gate_rows(mem_fs: dict[str, str]) -> list[dict[str, object]]:
    """Return the calibration-authority gate-activation rows for the fixture agent."""
    path_str = str(Path("/fake/agent") / GATE_ACTIVATIONS_FILENAME)
    if path_str not in mem_fs:
        return []
    rows = [
        json.loads(line)
        for line in mem_fs[path_str].strip().splitlines()
        if line.strip()
    ]
    return [r for r in rows if r.get("gate") == CALIBRATION_VERDICT_AUTHORITY_GATE]


@pytest.mark.asyncio
async def test_contemplate_flag_off_verdict_row_byte_identical(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Flag off: the verdict row is byte-identical to before proposal 129 — the
    self-grade is recorded with neither additive field written."""
    agent = make_agent()
    record_reflection_prediction(
        agent.predictions, "reflection-1", "p", "No commit reaches origin/main.",
    )
    await _run_contemplate_at(agent, _REFLECTION_WITH_MARKERS, iteration=2)

    rows = _reflection_verdict_rows(mem_fs, "reflection-1")
    assert len(rows) == 1
    assert rows[0]["verdict"] == "wrong"
    assert "verdict_source" not in rows[0]
    assert "self_verdict" not in rows[0]
    assert _calibration_gate_rows(mem_fs) == []


@pytest.mark.asyncio
async def test_contemplate_flag_off_no_self_grade_writes_no_verdict(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Flag off + a prior unscored bet but no ``PRIOR VERDICT`` line: the author
    has nothing to record, so no verdict row is written."""
    agent = make_agent()
    record_reflection_prediction(
        agent.predictions, "reflection-1", "p", "No commit reaches origin/main.",
    )
    await _run_contemplate_at(
        agent,
        "Just musing.\nPREDICTION: I will act.\nLOSS CONDITION: I do not.",
        iteration=2,
    )
    assert _reflection_verdict_rows(mem_fs, "reflection-1") == []


@pytest.mark.asyncio
async def test_contemplate_flag_on_measured_verdict_diverges(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Flag on + a parseable, mature loss condition + a contradicting self-grade:
    the oracle's measured verdict is persisted (source measured, self-grade
    retained) and one diverged gate row is stamped."""
    agent = make_agent()
    agent.config.audit_calibration_verdict_authority_enabled = True
    # A GATE_FIRING OCCURS bet with a one-cycle window, closed once iteration 3
    # begins; no gate fires, so the measured verdict is ``correct`` —
    # contradicting the reflection's ``PRIOR VERDICT: wrong`` self-grade. A
    # pre-bet anchor row proves the gate tail spans the window start, so
    # absence is covered evidence (#4575).
    agent.gate_activations.append(
        GateActivation("other", "x", "2020-01-01T00:00:00+00:00", "")
    )
    record_reflection_prediction(
        agent.predictions,
        "reflection-1",
        "The wip_primacy gate will fire.",
        "gate wip_primacy fires within 1 cycles",
    )
    # Iteration 3 is the window's exclusive bound; its in-flight start bounds
    # the evidence window.
    agent._iteration_usage = IterationUsage(
        iteration=3, started_at=datetime.now(UTC).isoformat(),
    )
    await _run_contemplate_at(agent, _REFLECTION_CALIBRATED, iteration=3)

    rows = _reflection_verdict_rows(mem_fs, "reflection-1")
    assert len(rows) == 1
    assert rows[0]["verdict"] == "correct"
    assert rows[0]["verdict_source"] == "measured"
    assert rows[0]["self_verdict"] == "wrong"
    gate_rows = _calibration_gate_rows(mem_fs)
    assert len(gate_rows) == 1
    assert gate_rows[0]["action"] == "diverged"


@pytest.mark.asyncio
async def test_contemplate_five_cycle_bet_pending_then_resolves_once(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A five-cycle bet records ``pending`` next cycle and resolves at maturity,
    with no duplicate row on the intervening cycle."""
    agent = make_agent()
    agent.gate_activations.append(
        GateActivation("other", "x", "2020-01-01T00:00:00+00:00", "")
    )  # pre-bet anchor: the gate tail spans the window start (#4575)
    agent.config.audit_calibration_verdict_authority_enabled = True
    record_reflection_prediction(
        agent.predictions,
        "reflection-1",
        "The gate will fire.",
        "gate some_gate fires within 5 cycles",
    )
    # A reflection with only a PRIOR VERDICT line — no new bet is minted, so
    # reflection-1 stays the sole open bet across all three cycles.
    quiet = "Reflecting quietly.\nPRIOR VERDICT: correct"

    await _run_contemplate_at(agent, quiet, iteration=2)  # elapsed 1 < 5 → pending
    pending = _reflection_verdict_rows(mem_fs, "reflection-1")
    assert len(pending) == 1
    assert pending[0]["verdict"] == "pending"

    await _run_contemplate_at(agent, quiet, iteration=3)  # elapsed 2 < 5 → no change
    assert len(_reflection_verdict_rows(mem_fs, "reflection-1")) == 1

    # Iteration 7 is the window's exclusive bound; the loop stamps its in-flight
    # start before contemplate runs, which bounds the evidence window (#4156).
    agent._iteration_usage = IterationUsage(
        iteration=7, started_at=datetime.now(UTC).isoformat(),
    )
    await _run_contemplate_at(agent, quiet, iteration=7)  # elapsed 6 ≥ 5 → correct
    rows = _reflection_verdict_rows(mem_fs, "reflection-1")
    assert len(rows) == 2
    assert [r["verdict"] for r in rows] == ["pending", "correct"]
    # The measured ``correct`` agrees with the ``PRIOR VERDICT: correct`` self-grade,
    # so exactly one gate row fires and its action is ``agree`` (the pending cycles
    # stamp no row). Asserting the action pins the agree branch: a swapped
    # AGREE/DIVERGED constant would flip this and fail.
    gate_rows = _calibration_gate_rows(mem_fs)
    assert len(gate_rows) == 1
    assert gate_rows[0]["action"] == "agree"


@pytest.mark.asyncio
async def test_contemplate_divergence_trace_fires_for_displaced_multicycle_bet(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A held-through multi-cycle bet displaced as "latest" by a newer bet still
    stamps a divergence row at maturity, recovering the self-grade persisted on its
    earlier ``pending`` row (r1 regression).

    Without recovering the persisted self-grade the mature verdict would carry
    ``self_verdict=None`` and no ``agree``/``diverged`` row would ever fire for the
    multi-cycle bets the grammar teaches — the trace would only fire when a bet
    resolves within one cycle of minting.
    """
    agent = make_agent()
    agent.gate_activations.append(
        GateActivation("other", "x", "2020-01-01T00:00:00+00:00", "")
    )  # pre-bet anchor: the gate tail spans the window start (#4575)
    agent.config.audit_calibration_verdict_authority_enabled = True
    # reflection-1: GATE_FIRING OCCURS, window 2. No matching gate fires, so it
    # matures ``correct`` — contradicting the ``wrong`` self-grade stated next cycle.
    record_reflection_prediction(
        agent.predictions,
        "reflection-1",
        "The gate will fire.",
        "gate some_gate fires within 2 cycles",
    )
    # Cycle 2: self-grade reflection-1 (wrong) *and* mint reflection-2, so from
    # cycle 3 on reflection-1 is no longer the latest bet. elapsed 1 < 2 → pending,
    # and the ``wrong`` self-grade is persisted on that pending row.
    mint_two = (
        "Confronting.\nPRIOR VERDICT: wrong\n"
        "PREDICTION: The next gate holds me.\n"
        "LOSS CONDITION: gate other_gate fires within 2 cycles"
    )
    await _run_contemplate_at(agent, mint_two, iteration=2)
    pending = _reflection_verdict_rows(mem_fs, "reflection-1")
    assert len(pending) == 1
    assert pending[0]["verdict"] == "pending"
    assert pending[0]["self_verdict"] == "wrong"

    # Cycle 4: reflection-2 is now latest, so reflection-1 gets no fresh self-grade —
    # yet its window has closed here (elapsed 3 > 2). The trace must still fire, recovering the
    # persisted ``wrong`` self-grade against the measured ``correct``.
    mint_three = (
        "Confronting.\nPRIOR VERDICT: correct\n"
        "PREDICTION: And the one after.\n"
        "LOSS CONDITION: gate third_gate fires within 2 cycles"
    )
    agent._iteration_usage = IterationUsage(
        iteration=4, started_at=datetime.now(UTC).isoformat(),
    )
    await _run_contemplate_at(agent, mint_three, iteration=4)

    rows = _reflection_verdict_rows(mem_fs, "reflection-1")
    assert [r["verdict"] for r in rows] == ["pending", "correct"]
    matured = rows[-1]
    assert matured["verdict_source"] == "measured"
    assert matured["self_verdict"] == "wrong"
    diverged = [
        r
        for r in _calibration_gate_rows(mem_fs)
        if "reflection-1" in str(r.get("detail", ""))
    ]
    assert len(diverged) == 1
    assert diverged[0]["action"] == "diverged"


@pytest.mark.asyncio
async def test_contemplate_flag_on_unparseable_keeps_narrated_verdict(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Flag on but the loss condition is free text the grammar declines: the
    self-grade is kept, stamped ``narrated``, and no measured gate row fires."""
    agent = make_agent()
    agent.config.audit_calibration_verdict_authority_enabled = True
    record_reflection_prediction(
        agent.predictions, "reflection-1", "p", "No commit reaches origin/main.",
    )
    await _run_contemplate_at(agent, _REFLECTION_WITH_MARKERS, iteration=2)

    rows = _reflection_verdict_rows(mem_fs, "reflection-1")
    assert len(rows) == 1
    assert rows[0]["verdict"] == "wrong"
    assert rows[0]["verdict_source"] == "narrated"
    assert "self_verdict" not in rows[0]
    assert _calibration_gate_rows(mem_fs) == []


@pytest.mark.asyncio
async def test_contemplate_flag_on_ancient_bet_gets_no_verdict(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A bet older than ``MAX_SCOREABLE_AGE_CYCLES`` is not re-scored, so enabling
    the flag cannot flood the ledger with verdicts for ancient pre-flag bets."""
    from maestro.self_calibration import MAX_SCOREABLE_AGE_CYCLES

    agent = make_agent()
    agent.config.audit_calibration_verdict_authority_enabled = True
    record_reflection_prediction(
        agent.predictions,
        "reflection-1",
        "The gate will fire.",
        "gate some_gate fires within 3 cycles",
    )
    await _run_contemplate_at(
        agent, "Quiet.\nPRIOR VERDICT: wrong",
        iteration=1 + MAX_SCOREABLE_AGE_CYCLES + 1,
    )
    assert _reflection_verdict_rows(mem_fs, "reflection-1") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("every", "scored"), [(5, 1), (1, 0)])
async def test_contemplate_cadence_keeps_matured_pass_reachable(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
    every: int,
    scored: int,
) -> None:
    """With ``contemplate_every = 5`` a 50-cycle bet's first pass past its close
    lands at elapsed 55; the cadence-widened age cap still scores it, and with
    every iteration start known it settles a measured verdict (an unfired
    ``OCCURS`` loss condition is ``correct``), not ``unresolved``."""
    agent = make_agent()
    with open(agent.memory._usage_path, "a", encoding="utf-8") as f:
        for it in range(1, 57):
            stamp = f"2099-01-01T{it // 60:02d}:{it % 60:02d}:00+00:00"
            f.write(json.dumps({"iteration": it, "started_at": stamp}) + "\n")
    agent.gate_activations.append(
        GateActivation("other", "x", "2020-01-01T00:00:00+00:00", "")
    )  # pre-bet anchor: the gate tail spans the window start (#4575)
    agent.config.audit_calibration_verdict_authority_enabled = True
    agent.config.loop_contemplate_every = every
    record_reflection_prediction(
        agent.predictions,
        "reflection-1",
        "The gate will fire.",
        "gate some_gate fires within 50 cycles",
    )
    await _run_contemplate_at(agent, "Quiet.\nPRIOR VERDICT: wrong", iteration=56)
    rows = _reflection_verdict_rows(mem_fs, "reflection-1")
    assert len(rows) == scored
    assert [r["verdict"] for r in rows] == ["correct"] * scored


@pytest.mark.asyncio
async def test_contemplate_flag_on_skips_older_unparseable_bet(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """An older unparseable bet (no self-grade to fall back to) records nothing,
    while the newest unparseable bet keeps its narrated verdict."""
    agent = make_agent()
    agent.config.audit_calibration_verdict_authority_enabled = True
    record_reflection_prediction(
        agent.predictions, "reflection-1", "p", "free text one",
    )
    record_reflection_prediction(
        agent.predictions, "reflection-2", "p", "free text two",
    )
    await _run_contemplate_at(agent, _REFLECTION_WITH_MARKERS, iteration=3)

    # reflection-1 is not the newest bet, so no self-grade attaches; its
    # unparseable loss condition leaves nothing to record.
    assert _reflection_verdict_rows(mem_fs, "reflection-1") == []
    # reflection-2 is the newest bet, so the self-grade is adopted (narrated).
    latest = _reflection_verdict_rows(mem_fs, "reflection-2")
    assert len(latest) == 1
    assert latest[0]["verdict"] == "wrong"
    assert latest[0]["verdict_source"] == "narrated"


@pytest.mark.asyncio
async def test_contemplate_flag_on_skips_bet_with_unparseable_cycle_id(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A bet whose id is not ``reflection-{iteration}`` cannot be aged, so it is
    skipped rather than mis-scored."""
    agent = make_agent()
    agent.config.audit_calibration_verdict_authority_enabled = True
    record_reflection_prediction(
        agent.predictions,
        "legacy-bet",
        "The gate will fire.",
        "gate some_gate fires within 3 cycles",
    )
    await _run_contemplate_at(agent, "Quiet.\nPRIOR VERDICT: wrong", iteration=2)
    assert _reflection_verdict_rows(mem_fs, "legacy-bet") == []


# ── reflection_bet_cycle ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prediction_id", "expected"),
    [
        ("reflection-5", 5),
        ("reflection-007", 7),
        ("reflection-0", 0),
        ("reflection-", None),
        ("reflection-abc", None),
        ("reflection-1x", None),
        ("legacy-bet", None),
        ("", None),
    ],
)
def test_reflection_bet_cycle(prediction_id: str, expected: int | None) -> None:
    """The id-shape owner recovers the cycle or declines a foreign/malformed id."""
    assert reflection_bet_cycle(prediction_id) == expected


# ── load_open_reflection_bets ────────────────────────────────────────────────────


def test_load_open_reflection_bets_returns_open_only(
    mem_fs: dict[str, str],
) -> None:
    """Every prediction whose effective verdict is unscored/pending is returned,
    oldest-first, carrying the row's timestamp; scored bets are excluded."""
    d = Path("/fake/open")
    ledger = JsonlPredictionLedger(d)
    record_reflection_prediction(ledger, "reflection-1", "p1", "loss1")
    record_reflection_prediction(ledger, "reflection-2", "p2", "loss2")
    record_reflection_prediction(ledger, "reflection-3", "p3", "loss3")
    # reflection-2 goes pending (still open); reflection-3 resolves (excluded).
    record_reflection_verdict(ledger, "reflection-2", "pending")
    record_reflection_verdict(ledger, "reflection-3", "correct")

    open_bets = load_open_reflection_bets(ledger)
    ids = [bet["prediction_id"] for bet in open_bets]
    assert ids == ["reflection-1", "reflection-2"]
    assert all(bet["timestamp"] for bet in open_bets)
    verdicts = {bet["prediction_id"]: bet["verdict"] for bet in open_bets}
    assert verdicts == {"reflection-1": "unscored", "reflection-2": "pending"}


def test_load_scored_reflection_bets_returns_scored_only(
    mem_fs: dict[str, str],
) -> None:
    """The complement of the open reader: every bet whose effective verdict is no
    longer unscored/pending is returned oldest-first, carrying the oracle's
    ``verdict_source`` stamp."""
    d = Path("/fake/scored")
    ledger = JsonlPredictionLedger(d)
    record_reflection_prediction(ledger, "reflection-1", "p1", "loss1")
    record_reflection_prediction(ledger, "reflection-2", "p2", "loss2")
    record_reflection_prediction(ledger, "reflection-3", "p3", "loss3")
    # reflection-2 stays pending (open); the other two are scored.
    record_reflection_verdict(ledger, "reflection-2", "pending")
    record_reflection_verdict(ledger, "reflection-1", "correct")
    record_reflection_verdict(ledger, "reflection-3", "wrong")

    scored = load_scored_reflection_bets(ledger)
    assert [bet["prediction_id"] for bet in scored] == [
        "reflection-1", "reflection-3",
    ]
    assert scored[0]["verdict"] == "correct"
    assert scored[1]["verdict"] == "wrong"
    assert all(bet["timestamp"] for bet in scored)
    assert all(bet["loss_condition"] for bet in scored)


def test_load_scored_reflection_bets_carries_the_verdict_source_stamp(
    mem_fs: dict[str, str],
) -> None:
    """``verdict_source`` is surfaced verbatim, and an unstamped verdict reads as
    the empty string rather than ``None`` — the adapter's admission filter keys
    on exactly this field."""
    d = Path("/fake/scored2")
    ledger = JsonlPredictionLedger(d)
    record_reflection_prediction(ledger, "reflection-1", "p1", "loss1")
    record_reflection_prediction(ledger, "reflection-2", "p2", "loss2")
    ledger.append({
        "source": "reflection_verdict", "prediction_id": "reflection-1",
        "verdict": "wrong", "verdict_source": "measured",
        "self_verdict": "right",
    })
    record_reflection_verdict(ledger, "reflection-2", "correct")

    by_id = {
        bet["prediction_id"]: bet for bet in load_scored_reflection_bets(ledger)
    }
    assert by_id["reflection-1"]["verdict_source"] == "measured"
    assert by_id["reflection-1"]["self_verdict"] == "right"
    assert by_id["reflection-2"]["verdict_source"] == ""


def test_load_scored_reflection_bets_windows_on_the_result_tail(
    mem_fs: dict[str, str],
) -> None:
    """*n* bounds the *scored bets* returned, not the raw ledger rows read; a
    non-positive *n* returns them all rather than an empty window."""
    d = Path("/fake/scored3")
    ledger = JsonlPredictionLedger(d)
    for cycle in (1, 2, 3):
        record_reflection_prediction(
            ledger, f"reflection-{cycle}", f"p{cycle}", f"loss{cycle}",
        )
        record_reflection_verdict(ledger, f"reflection-{cycle}", "correct")

    assert [
        bet["prediction_id"] for bet in load_scored_reflection_bets(ledger, n=2)
    ] == ["reflection-2", "reflection-3"]
    assert [
        bet["prediction_id"] for bet in load_scored_reflection_bets(ledger, n=0)
    ] == ["reflection-1", "reflection-2", "reflection-3"]


def test_load_scored_reflection_bets_keeps_a_bet_whose_prediction_row_aged_out(
    mem_fs: dict[str, str],
) -> None:
    """Regression: windowing the *raw* rows dropped a just-scored bet whose
    prediction row had fallen out of the tail even though its verdict row was
    inside it — so the join runs over the whole ledger and *n* bounds the result.
    """
    d = Path("/fake/scored5")
    ledger = JsonlPredictionLedger(d)
    record_reflection_prediction(ledger, "reflection-old", "p-old", "loss-old")
    # Enough intervening rows to push the prediction row past any small tail.
    for filler in range(6):
        record_reflection_prediction(
            ledger, f"filler-{filler}", f"pf{filler}", f"lf{filler}",
        )
    record_reflection_verdict(ledger, "reflection-old", "correct")

    scored = load_scored_reflection_bets(ledger, n=2)
    assert [bet["prediction_id"] for bet in scored] == ["reflection-old"]
    assert scored[0]["prediction"] == "p-old"


def test_load_scored_reflection_bets_empty_ledger_returns_nothing(
    mem_fs: dict[str, str],
) -> None:
    ledger = JsonlPredictionLedger(Path("/fake/scored4"))
    assert load_scored_reflection_bets(ledger) == []


def test_load_open_reflection_bets_tolerates_malformed_rows(
    mem_fs: dict[str, str],
) -> None:
    """Non-string ids/fields default to empty strings; a non-string verdict-row id
    is ignored for override; a prediction row with no own verdict still reads
    unscored."""
    d = Path("/fake/open2")
    path = d / PREDICTIONS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        # A well-formed open bet.
        {"source": "reflection", "prediction_id": "reflection-1",
         "prediction": "p", "loss_condition": "loss",
         "verdict": "unscored", "timestamp": "2026-01-01T00:00:00+00:00"},
        # A verdict row with a non-string prediction_id — ignored for override.
        {"source": "reflection_verdict", "prediction_id": 5, "verdict": "correct"},
        # A prediction row whose fields are non-strings / missing own verdict.
        {"source": "reflection", "prediction_id": 9, "prediction": 1,
         "loss_condition": None, "timestamp": 42},
    ]
    with open(path, "w", encoding="utf-8") as f:
        for row in lines:
            f.write(json.dumps(row) + "\n")

    open_bets = load_open_reflection_bets(JsonlPredictionLedger(d))
    assert len(open_bets) == 2
    first = open_bets[0]
    assert first["prediction_id"] == "reflection-1"
    # The malformed prediction row: non-string fields collapse to "" and, with no
    # own verdict, it still reads as an open (unscored) bet.
    second = open_bets[1]
    assert second["prediction_id"] == ""
    assert second["prediction"] == ""
    assert second["loss_condition"] == ""
    assert second["timestamp"] == ""
    assert second["verdict"] == "unscored"
    # No verdict row carried a self_verdict, so both bets default it to "".
    assert first["self_verdict"] == ""
    assert second["self_verdict"] == ""


def test_load_open_reflection_bets_carries_persisted_self_verdict(
    mem_fs: dict[str, str],
) -> None:
    """A ``self_verdict`` persisted on a ``pending`` override row is carried on the
    still-open bet, so a bet displaced as "latest" can recover its self-grade at
    maturity (r1 regression); a bet with no such override defaults it to ``""``."""
    d = Path("/fake/open3")
    ledger = JsonlPredictionLedger(d)
    record_reflection_prediction(ledger, "reflection-1", "p1", "loss1")
    record_reflection_prediction(ledger, "reflection-2", "p2", "loss2")
    # reflection-1 goes pending with a retained self-grade; reflection-2 stays bare.
    record_reflection_verdict(
        ledger, "reflection-1", "pending",
        verdict_source="measured", self_verdict="wrong",
    )

    by_id = {bet["prediction_id"]: bet for bet in load_open_reflection_bets(ledger)}
    assert by_id["reflection-1"]["verdict"] == "pending"
    assert by_id["reflection-1"]["self_verdict"] == "wrong"
    assert by_id["reflection-2"]["self_verdict"] == ""


# ── record_reflection_verdict additive fields ────────────────────────────────────


def test_record_reflection_verdict_additive_fields(
    mem_fs: dict[str, str],
) -> None:
    """The two calibration fields are written only when supplied; omitting them
    yields a byte-identical row."""
    d = Path("/fake/verdict")
    ledger = JsonlPredictionLedger(d)
    record_reflection_verdict(ledger, "reflection-1", "wrong")
    record_reflection_verdict(
        ledger, "reflection-2", "correct",
        verdict_source="measured", self_verdict="wrong",
    )
    rows = [
        json.loads(line)
        for line in mem_fs[str(d / PREDICTIONS_FILENAME)].strip().splitlines()
    ]
    bare = next(r for r in rows if r["prediction_id"] == "reflection-1")
    assert "verdict_source" not in bare
    assert "self_verdict" not in bare
    stamped = next(r for r in rows if r["prediction_id"] == "reflection-2")
    assert stamped["verdict_source"] == "measured"
    assert stamped["self_verdict"] == "wrong"


# ── _collect_calibration_evidence ────────────────────────────────────────────────


def _task_record(task_id: str, goal: str, outcome: str, timestamp: str) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        goal=goal,
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary="",
        errors=[],
        duration_secs=0.0,
        timestamp=timestamp,
    )


def _plan(plan_id: str, status: str) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title="t",
        category="capability",
        rationale="r",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        status=status,  # type: ignore[arg-type]
    )


def test_collect_calibration_evidence_bundles_by_kind(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """One bundle per bet: task/gate rows filtered to the bet's window and marked
    covered, the plan snapshot injected, PR/unparseable bets empty."""
    agent = make_agent()
    since = "2026-06-01T00:00:00+00:00"
    before = "2026-01-01T00:00:00+00:00"
    after = "2026-12-01T00:00:00+00:00"
    # Task history: one before the window (excluded), one inside (included).
    agent.memory.record_task(_task_record("t-old", "remediation A", "failure", before))
    agent.memory.record_task(_task_record("t-new", "remediation B", "success", after))
    # Gate trace: one before, one inside the window.
    agent.gate_activations.append(GateActivation("old_gate", "x", before, ""))
    agent.gate_activations.append(GateActivation("wip_primacy", "x", after, ""))
    # Plans: the current snapshot.
    agent.memory.save_evolution_plan(_plan("p-1", "implemented"))

    open_bets = [
        {"prediction_id": "reflection-1",
         "loss_condition": "task remediation outcome is success within 3 cycles",
         "timestamp": since, "verdict": "unscored"},
        {"prediction_id": "reflection-2",
         "loss_condition": "gate wip_primacy fires within 3 cycles",
         "timestamp": since, "verdict": "unscored"},
        {"prediction_id": "reflection-3",
         "loss_condition": "plan p-1 reaches implemented within 3 cycles",
         "timestamp": since, "verdict": "unscored"},
        {"prediction_id": "reflection-4",
         "loss_condition": "pr #5 settles within 3 cycles",
         "timestamp": since, "verdict": "unscored"},
        {"prediction_id": "reflection-5",
         "loss_condition": "free text the grammar declines",
         "timestamp": since, "verdict": "unscored"},
    ]
    evidence = agent._collect_calibration_evidence(open_bets, 1)

    task_ev = evidence["reflection-1"]
    assert task_ev.task_window_covered is True
    assert task_ev.task_outcomes == (("remediation", "success"),)
    gate_ev = evidence["reflection-2"]
    assert gate_ev.gate_window_covered is True
    assert gate_ev.fired_gates == ("wip_primacy",)
    plan_ev = evidence["reflection-3"]
    assert plan_ev.plan_statuses == (("p-1", "implemented"),)
    assert evidence["reflection-4"] == CalibrationEvidence()
    assert evidence["reflection-5"] == CalibrationEvidence()


def test_collect_calibration_evidence_no_bets_reads_nothing(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """With no open bets (or none of a kind) each source read is skipped entirely."""
    agent = make_agent()
    with patch.object(
        agent.memory, "get_recent_tasks", side_effect=AssertionError("read"),
    ), patch.object(
        agent.gate_activations, "read_tail", side_effect=AssertionError("read"),
    ), patch.object(
        agent.memory, "get_all_plans", side_effect=AssertionError("read"),
    ):
        assert agent._collect_calibration_evidence([], 1) == {}


def test_collect_calibration_evidence_empty_since_not_covered(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A windowed bet whose timestamp collapsed to "" (a corrupt row) is not scored
    against the whole tail: its evidence is left uncovered rather than admitting
    every task/gate row as if inside the window (r3 regression).

    Rows that *would* match are present, so before the guard the task/gate windows
    would read as covered with the full tail; the guard must leave both empty.
    """
    agent = make_agent()
    after = "2026-12-01T00:00:00+00:00"
    agent.memory.record_task(_task_record("t", "remediation", "success", after))
    agent.gate_activations.append(GateActivation("wip_primacy", "x", after, ""))

    open_bets = [
        {"prediction_id": "reflection-1",
         "loss_condition": "task remediation outcome is success within 3 cycles",
         "timestamp": "", "verdict": "unscored"},
        {"prediction_id": "reflection-2",
         "loss_condition": "gate wip_primacy fires within 3 cycles",
         "timestamp": "", "verdict": "unscored"},
    ]
    evidence = agent._collect_calibration_evidence(open_bets, 1)
    # Empty ``since`` ⇒ no covered windowed evidence, so neither bet is mis-scored.
    assert evidence["reflection-1"] == CalibrationEvidence()
    assert evidence["reflection-2"] == CalibrationEvidence()


# ---------------------------------------------------------------------------
# Two-ended evidence window (#4156) — a bet scored late sees only rows stamped
# before the iteration after its window closes, so it scores as if on time.
# ---------------------------------------------------------------------------

_WIN_SINCE = "2026-06-01T00:00:00+00:00"
_PRE_SINCE = "2026-01-01T00:00:00+00:00"


def _iter_start(iteration: int) -> str:
    """A distinct, ordered ``started_at`` stamp for *iteration* (100 → day 01)."""
    return f"2026-07-{iteration - 99:02d}T00:00:00+00:00"


def _seed_usage(agent, iterations: range) -> None:  # type: ignore[no-untyped-def]
    """Append one ``usage.jsonl`` row per iteration to the mem_fs sidecar."""
    with open(agent.memory._usage_path, "a", encoding="utf-8") as f:
        for it in iterations:
            f.write(json.dumps({"iteration": it, "started_at": _iter_start(it)}) + "\n")


def _in_iteration(iteration: int) -> str:
    """A timestamp inside *iteration* (noon of its day)."""
    return f"2026-07-{iteration - 99:02d}T12:00:00+00:00"


def _score_bet(agent, loss: str, iteration: int) -> str:  # type: ignore[no-untyped-def]
    """Score a ``reflection-100`` bet through the real evidence feed + oracle."""
    bet = {"prediction_id": "reflection-100", "loss_condition": loss,
           "timestamp": _WIN_SINCE, "verdict": "unscored"}
    evidence = agent._collect_calibration_evidence([bet], iteration)
    score = score_loss_condition(
        parse_loss_condition(loss),
        evidence["reflection-100"],
        cycles_elapsed=iteration - 100,
    )
    return score.verdict


@pytest.mark.parametrize(
    ("loss", "expected"),
    [
        ("gate wip_primacy does not fire within 3 cycles", "wrong"),
        ("gate wip_primacy fires within 3 cycles", "correct"),
    ],
)
def test_calibration_evidence_excludes_rows_after_window_end(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
    loss: str,
    expected: str,
) -> None:
    """A 3-cycle bet placed at 100 and scored at 105 must not see a gate that
    fired in iteration 104 — after its window closed with iteration 103."""
    agent = make_agent()
    _seed_usage(agent, range(100, 105))
    agent.gate_activations.append(GateActivation("other", "x", _PRE_SINCE, ""))
    agent.gate_activations.append(
        GateActivation("wip_primacy", "x", _in_iteration(104), "")
    )
    assert _score_bet(agent, loss, 105) == expected


def test_calibration_evidence_keeps_rows_inside_window_when_late(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """The upper bound is exclusive of post-window rows only: a gate firing in
    iteration 103 (the window's last) still realises the loss when scored late."""
    agent = make_agent()
    _seed_usage(agent, range(100, 105))
    agent.gate_activations.append(
        GateActivation("wip_primacy", "x", _in_iteration(103), "")
    )
    assert _score_bet(agent, "gate wip_primacy fires within 3 cycles", 105) == "wrong"


@pytest.mark.parametrize(
    "loss",
    [
        "gate wip_primacy fires within 3 cycles",
        "gate wip_primacy does not fire within 3 cycles",
    ],
)
def test_calibration_bet_pending_at_elapsed_n_while_window_open(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
    loss: str,
) -> None:
    """At elapsed N (iteration c+N, still inside the window) a non-occurrence is
    not yet final: the rest of that iteration could still fire the gate."""
    agent = make_agent()
    _seed_usage(agent, range(100, 104))
    assert _score_bet(agent, loss, 103) == "pending"


@pytest.mark.parametrize("late", [104, 106])
@pytest.mark.parametrize(
    ("loss", "expected"),
    [
        ("gate wip_primacy fires within 3 cycles", "wrong"),
        ("gate wip_primacy does not fire within 3 cycles", "correct"),
    ],
)
def test_calibration_on_time_and_late_scoring_agree(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
    loss: str,
    expected: str,
    late: int,
) -> None:
    """A firing in the window's last iteration (103) decides the bet the same
    way on the first matured pass (104) and on a late pass (106)."""
    agent = make_agent()
    _seed_usage(agent, range(100, 107))
    assert _score_bet(agent, loss, 103) == "pending"
    agent.gate_activations.append(
        GateActivation("wip_primacy", "x", _in_iteration(103), "")
    )
    assert _score_bet(agent, loss, late) == expected


def test_calibration_evidence_task_and_sensor_bounded(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """TASK_OUTCOME and SENSOR_THRESHOLD rows after the window end are excluded."""
    agent = make_agent()
    _seed_usage(agent, range(100, 105))
    agent.memory.record_task(_task_record("t0", "other", "failure", _PRE_SINCE))
    agent.memory.record_task(
        _task_record("t", "remediation", "success", _in_iteration(104))
    )
    agent.memory.record_sensor_readings(
        [SensorReading(timestamp=_in_iteration(104), tag="PR-LAPS", value=9.0,
                       crossed=True)]
    )
    bets = [
        {"prediction_id": "reflection-100",
         "loss_condition": "task remediation outcome is success within 3 cycles",
         "timestamp": _WIN_SINCE, "verdict": "unscored"},
        {"prediction_id": "reflection-101",
         "loss_condition": "sensor PR-LAPS goes above 5 within 2 cycles",
         "timestamp": _WIN_SINCE, "verdict": "unscored"},
    ]
    evidence = agent._collect_calibration_evidence(bets, 105)
    assert evidence["reflection-100"].task_outcomes == ()
    assert evidence["reflection-100"].task_window_covered is True
    assert evidence["reflection-101"].sensor_readings == ()


def test_calibration_evidence_open_window_unbounded(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A bet whose window is still open keeps the start-only filter (the latest
    row is visible) and reads no iteration starts at all."""
    agent = make_agent()
    agent.gate_activations.append(
        GateActivation("wip_primacy", "x", _in_iteration(102), "")
    )
    bet = {"prediction_id": "reflection-100",
           "loss_condition": "gate wip_primacy fires within 3 cycles",
           "timestamp": _WIN_SINCE, "verdict": "unscored"}
    with patch.object(
        agent.memory, "get_iteration_starts", side_effect=AssertionError("read"),
    ):
        evidence = agent._collect_calibration_evidence([bet], 102)
    assert evidence["reflection-100"].fired_gates == ("wip_primacy",)


def test_calibration_evidence_in_flight_bound_uses_current_start(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """When the bound iteration is the in-flight one its start comes from the
    live ``IterationUsage`` (no row written yet); unknown → no evidence."""
    agent = make_agent()
    agent.gate_activations.append(
        GateActivation("wip_primacy", "x", _in_iteration(104), "")
    )
    agent.gate_activations.append(
        GateActivation("wip_primacy", "x", _in_iteration(103), "")
    )
    bet = {"prediction_id": "reflection-100",
           "loss_condition": "gate wip_primacy fires within 3 cycles",
           "timestamp": _WIN_SINCE, "verdict": "unscored"}
    agent._iteration_usage = IterationUsage(
        iteration=104, started_at=_iter_start(104),
    )
    evidence = agent._collect_calibration_evidence([bet], 104)
    assert evidence["reflection-100"].fired_gates == ("wip_primacy",)
    agent._iteration_usage = None
    assert agent._collect_calibration_evidence([bet], 104)["reflection-100"] == (
        CalibrationEvidence()
    )


def test_calibration_evidence_missing_bound_row_not_covered(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A matured bet whose bound iteration has no usage row (archived, frozen
    counter) carries no covered evidence, so it resolves ``unresolved`` rather than
    being mis-scored ``correct`` on an unbounded tail."""
    agent = make_agent()
    _seed_usage(agent, range(100, 103))  # iteration 104's row is missing
    loss = "gate wip_primacy does not fire within 3 cycles"
    bet = {"prediction_id": "reflection-100", "loss_condition": loss,
           "timestamp": _WIN_SINCE, "verdict": "unscored"}
    evidence = agent._collect_calibration_evidence([bet], 106)
    assert evidence["reflection-100"] == CalibrationEvidence()
    score = score_loss_condition(
        parse_loss_condition(loss), evidence["reflection-100"], cycles_elapsed=6,
    )
    assert score.verdict == "unresolved"


def test_calibration_evidence_inverted_window_raises(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """An ``until`` earlier than ``since`` is a corrupt window and fails loudly."""
    agent = make_agent()
    _seed_usage(agent, range(100, 105))
    bet = {"prediction_id": "reflection-100",
           "loss_condition": "gate wip_primacy fires within 3 cycles",
           "timestamp": "2027-01-01T00:00:00+00:00", "verdict": "unscored"}
    with pytest.raises(ValueError, match="inverted calibration window"):
        agent._collect_calibration_evidence([bet], 105)


# ---------------------------------------------------------------------------
# Measured coverage (#4575) — a bounded tail is covered only when its oldest row
# predates the bet's ``since``; a crowded tail resolves ``unresolved``.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "loss",
    [
        "gate wip_primacy does not fire within 3 cycles",
        "gate wip_primacy fires within 3 cycles",
    ],
)
def test_calibration_evidence_gate_tail_crowded_past_since_not_covered(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
    loss: str,
) -> None:
    """In-window noise pushing the target row out of the tail leaves the bet
    uncovered: neither ``wrong`` by absence nor ``correct``."""
    agent = make_agent()
    _seed_usage(agent, range(100, 105))
    agent.gate_activations.append(
        GateActivation("wip_primacy", "x", _in_iteration(100), "")
    )
    for i in range(_CALIBRATION_GATE_WINDOW + 1):
        agent.gate_activations.append(GateActivation(
            "tend_cooldown_skip", "x", _in_iteration(101 + i % 3), "",
        ))
    bet = {"prediction_id": "reflection-100", "loss_condition": loss,
           "timestamp": _WIN_SINCE, "verdict": "unscored"}
    ev = agent._collect_calibration_evidence([bet], 105)["reflection-100"]
    assert ev.gate_window_covered is False
    assert "wip_primacy" not in ev.fired_gates
    # Elapsed 6 is the 3-cycle window's cutoff (close N + 1 plus the grace).
    score = score_loss_condition(parse_loss_condition(loss), ev, cycles_elapsed=6)
    assert score.verdict == "unresolved"


def test_calibration_evidence_task_tail_crowded_past_since_not_covered(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """The task tail crowded past ``since`` is not covered either."""
    agent = make_agent()
    _seed_usage(agent, range(100, 105))
    agent.memory.record_task(
        _task_record("t-r", "remediation", "success", _in_iteration(100))
    )
    for i in range(_CALIBRATION_TASK_WINDOW + 1):
        agent.memory.record_task(
            _task_record(f"t-{i}", "other", "failure", _in_iteration(101))
        )
    loss = "task remediation outcome is success within 3 cycles"
    bet = {"prediction_id": "reflection-100", "loss_condition": loss,
           "timestamp": _WIN_SINCE, "verdict": "unscored"}
    ev = agent._collect_calibration_evidence([bet], 105)["reflection-100"]
    assert ev.task_window_covered is False
    score = score_loss_condition(parse_loss_condition(loss), ev, cycles_elapsed=6)
    assert score.verdict == "unresolved"


def test_calibration_evidence_tail_reaching_before_since_stays_covered(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A tail whose oldest row predates ``since`` is covered; absence is negative."""
    agent = make_agent()
    _seed_usage(agent, range(100, 105))
    agent.gate_activations.append(GateActivation("other", "x", _PRE_SINCE, ""))
    agent.gate_activations.append(
        GateActivation("other", "x", _in_iteration(101), "")
    )
    agent.memory.record_task(_task_record("t0", "other", "failure", _PRE_SINCE))
    agent.memory.record_task(
        _task_record("t1", "other", "failure", _in_iteration(101))
    )
    bets = [
        {"prediction_id": "reflection-100",
         "loss_condition": "gate wip_primacy does not fire within 3 cycles",
         "timestamp": _WIN_SINCE, "verdict": "unscored"},
        {"prediction_id": "reflection-100",
         "loss_condition": "task remediation outcome is success within 3 cycles",
         "timestamp": _WIN_SINCE, "verdict": "unscored"},
    ]
    gate_ev = agent._collect_calibration_evidence(bets[:1], 105)
    task_ev = agent._collect_calibration_evidence(bets[1:], 105)
    assert gate_ev["reflection-100"].gate_window_covered is True
    assert task_ev["reflection-100"].task_window_covered is True
    loss = "gate wip_primacy does not fire within 3 cycles"
    assert _score_bet(agent, loss, 105) == "wrong"


@pytest.mark.parametrize(
    "oldest",
    [None, _WIN_SINCE, "2026-06-02T00:00:00+00:00"],
)
def test_tail_spans_since_not_covered_edges(oldest: str | None) -> None:
    """Empty tail, oldest equal to ``since`` (strict ``<``), or newer: uncovered."""
    assert _tail_spans_since(oldest, _WIN_SINCE) is False


def test_tail_spans_since_covered() -> None:
    """An oldest row strictly before ``since`` spans the window start."""
    assert _tail_spans_since(_PRE_SINCE, _WIN_SINCE) is True


def test_calibration_window_ends_skips_non_windowed() -> None:
    """Plan/PR/unparseable probes and foreign ids get no window end."""
    probes = {
        "reflection-1": parse_loss_condition("plan p-1 reaches implemented within 3 cycles"),
        "reflection-2": None,
        "foreign": parse_loss_condition("gate g fires within 1 cycles"),
    }
    assert calibration_window_ends(
        probes, {}, current_iteration=50, current_started_at="x",
    ) == {}


def test_get_iteration_starts_tail_and_corrupt_rows(mem_fs: dict[str, str]) -> None:
    """The reader maps iteration → started_at, skips corrupt rows under a shared
    budget, stops at *n*, and returns ``{}`` for ``n <= 0``."""
    from maestro.memory import Memory

    mem = Memory(Path("/fake/usage-mem"))
    with open(mem._usage_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"iteration": 1, "started_at": "a"}) + "\n")
        f.write(json.dumps({"iteration": 2, "started_at": "b"}) + "\n")
        f.write(json.dumps({"iteration": True, "started_at": "c"}) + "\n")
        f.write(json.dumps({"iteration": 3, "started_at": ""}) + "\n")
    assert mem.get_iteration_starts(0) == {}
    assert mem.get_iteration_starts(5) == {1: "a", 2: "b"}
    # Skip budget of 2 is spent by the two corrupt tail rows before any good row.
    assert mem.get_iteration_starts(2) == {}
    clean = Memory(Path("/fake/usage-clean"))
    with open(clean._usage_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"iteration": 1, "started_at": "a"}) + "\n")
        f.write(json.dumps({"iteration": 2, "started_at": "b"}) + "\n")
    assert clean.get_iteration_starts(1) == {2: "b"}


def test_get_iteration_starts_frozen_iteration_keeps_earliest(
    mem_fs: dict[str, str],
) -> None:
    """A frozen counter's duplicate rows map to the iteration's earliest start
    and count once toward *n*, so they cannot crowd older iterations out."""
    from maestro.memory import Memory

    mem = Memory(Path("/fake/usage-frozen"))
    with open(mem._usage_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"iteration": 1, "started_at": "a"}) + "\n")
        for ts in ("b1", "b2", "b3"):
            f.write(json.dumps({"iteration": 2, "started_at": ts}) + "\n")
    assert mem.get_iteration_starts(2) == {1: "a", 2: "b1"}
    assert mem.get_iteration_starts(1) == {2: "b1"}


def test_calibration_window_ends_in_flight_prefers_earliest_recorded_start() -> None:
    """When a frozen in-flight bound iteration already has a recorded row, the
    earlier of that row and the live start bounds the window."""
    probes = {"reflection-1": parse_loss_condition("gate g fires within 1 cycles")}
    bound = 1 + 1 + 1
    assert calibration_window_ends(
        probes, {bound: "2025-01-01"}, current_iteration=bound,
        current_started_at="2025-02-01",
    ) == {"reflection-1": "2025-01-01"}
    assert calibration_window_ends(
        probes, {}, current_iteration=bound, current_started_at="",
    ) == {"reflection-1": None}


@pytest.mark.asyncio
async def test_contemplate_late_scored_bet_ignores_post_window_gate(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """End-to-end: a 3-cycle DOES_NOT_OCCUR bet placed at 100 and first scored at
    105 is ``wrong`` when the gate fired only in iteration 104 — was ``correct``."""
    agent = make_agent()
    agent.gate_activations.append(
        GateActivation("other", "x", "2020-01-01T00:00:00+00:00", "")
    )  # pre-bet anchor: the gate tail spans the window start (#4575)
    agent.config.audit_calibration_verdict_authority_enabled = True
    record_reflection_prediction(
        agent.predictions,
        "reflection-100",
        "The wip_primacy gate will fire.",
        "gate wip_primacy does not fire within 3 cycles",
    )
    since = load_open_reflection_bets(agent.predictions)[0]["timestamp"]
    with open(agent.memory._usage_path, "a", encoding="utf-8") as f:
        for it in range(100, 105):
            f.write(json.dumps(
                {"iteration": it, "started_at": f"{since}~{it}"}
            ) + "\n")
    agent.gate_activations.append(
        GateActivation("wip_primacy", "x", f"{since}~104~mid", "")
    )
    await _run_contemplate_at(agent, "Quiet.\nPRIOR VERDICT: correct", iteration=105)
    rows = _reflection_verdict_rows(mem_fs, "reflection-100")
    assert len(rows) == 1
    assert rows[0]["verdict"] == "wrong"
    assert rows[0]["verdict_source"] == "measured"


# ---------------------------------------------------------------------------
# by_verdict_source split (proposal 129) — measure_prediction_accuracy folds the
# reflection rows and additionally buckets them by their verdict override's stamp.
# ---------------------------------------------------------------------------


def test_measure_by_verdict_source_split(mem_fs: dict[str, str]) -> None:
    """Scored reflection rows bucket measured / narrated / unstamped without
    disturbing the existing total / correct / by_shape counts."""
    d = Path("/fake/bvs")
    b = JsonlPredictionLedger(d)
    # measured: one correct, one wrong.
    record_reflection_prediction(b, "reflection-1", "p", "loss")
    record_reflection_verdict(b, "reflection-1", "correct", verdict_source="measured")
    record_reflection_prediction(b, "reflection-2", "p", "loss")
    record_reflection_verdict(b, "reflection-2", "wrong", verdict_source="measured")
    # narrated: one correct.
    record_reflection_prediction(b, "reflection-3", "p", "loss")
    record_reflection_verdict(b, "reflection-3", "correct", verdict_source="narrated")
    # unstamped: a verdict override with no source stamp.
    record_reflection_prediction(b, "reflection-4", "p", "loss")
    record_reflection_verdict(b, "reflection-4", "wrong")

    result = measure_prediction_accuracy(b, [])
    assert result["total"] == 4
    assert result["correct"] == 2
    by_shape = result["by_shape"]
    assert isinstance(by_shape, dict)
    assert by_shape["reflection"] == {"total": 4, "correct": 2}
    assert result["by_verdict_source"] == {
        "measured": {"total": 2, "correct": 1},
        "narrated": {"total": 1, "correct": 1},
        "unstamped": {"total": 1, "correct": 0},
    }


def test_measure_by_verdict_source_empty_for_task_only(
    mem_fs: dict[str, str],
) -> None:
    """A task-outcome-only ledger yields an empty by_verdict_source split."""
    d = Path("/fake/bvs2")
    b = JsonlPredictionLedger(d)
    record_prediction(b, "t1", "success")
    task_history: list[dict[str, object]] = [
        {"task_id": "t1", "outcome": "success", "goal": "fix #1"},
    ]
    result = measure_prediction_accuracy(b, task_history)
    assert result["total"] == 1
    assert result["by_verdict_source"] == {}


# ---------------------------------------------------------------------------
# _parse_stored_confidence (proposal 129) — untrusted-numeric read.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        (0.8, 0.8),
        ("0.8", 0.8),
        ("not-a-number", None),
        (float("nan"), None),
        (float("inf"), None),
        (1.5, 1.0),
        (-0.5, 0.0),
        ([1, 2], None),
    ],
)
def test_parse_stored_confidence(raw: object, expected: float | None) -> None:
    """Coerce with ``float``, reject NaN/inf, clamp to ``[0, 1]``; a null or
    unparseable value reads as absent (``None``)."""
    assert _parse_stored_confidence(raw) == expected


# ---------------------------------------------------------------------------
# measure_self_calibration (proposal 129) — read the ledger back on itself.
# ---------------------------------------------------------------------------


def test_measure_self_calibration_empty_ledger(mem_fs: dict[str, str]) -> None:
    """An empty ledger reads all zeros and suppresses the bins."""
    d = Path("/fake/cal-empty")
    reading = measure_self_calibration(JsonlPredictionLedger(d))
    assert reading.stamped_total == 0
    assert reading.resolved == 0
    assert reading.hits == 0
    assert reading.hit_rate == 0.0
    assert reading.agreement_total == 0
    assert reading.agreements == 0
    assert reading.agreement_rate == 0.0
    assert reading.narrated == 0
    assert reading.pending == 0
    assert reading.unresolved == 0
    assert reading.brier_total == 0
    assert reading.brier_score == 0.0
    assert reading.reliability_bins == {}


def test_measure_self_calibration_hit_rate_agreement_shares(
    mem_fs: dict[str, str],
) -> None:
    """Hit rate, self-vs-measured agreement, and the narrated / pending /
    unresolved shares over a mixed ledger; a garbage-verdict measured row is
    counted as stamped but excluded from the metric domain."""
    d = Path("/fake/cal-mix")
    b = JsonlPredictionLedger(d)
    # measured, resolved: correct (self agrees) + wrong (self diverges).
    record_reflection_prediction(b, "reflection-1", "p", "loss")
    record_reflection_verdict(
        b, "reflection-1", "correct",
        verdict_source="measured", self_verdict="correct",
    )
    record_reflection_prediction(b, "reflection-2", "p", "loss")
    record_reflection_verdict(
        b, "reflection-2", "wrong",
        verdict_source="measured", self_verdict="correct",
    )
    # measured, open: pending + unresolved (no self grade).
    record_reflection_prediction(b, "reflection-3", "p", "loss")
    record_reflection_verdict(
        b, "reflection-3", "pending", verdict_source="measured",
    )
    record_reflection_prediction(b, "reflection-4", "p", "loss")
    record_reflection_verdict(
        b, "reflection-4", "unresolved", verdict_source="measured",
    )
    # narrated (the grammar could not score the bet).
    record_reflection_prediction(b, "reflection-5", "p", "loss")
    record_reflection_verdict(
        b, "reflection-5", "correct",
        verdict_source="narrated", self_verdict="correct",
    )
    # garbage-verdict measured row: stamped, but outside the metric domain.
    record_reflection_prediction(b, "reflection-6", "p", "loss")
    record_reflection_verdict(
        b, "reflection-6", "bogus", verdict_source="measured",
    )

    reading = measure_self_calibration(b)
    assert reading.stamped_total == 6  # 5 measured-stamped + 1 narrated
    assert reading.resolved == 2
    assert reading.hits == 1
    assert reading.hit_rate == 0.5
    assert reading.agreement_total == 2  # reflection-1 / reflection-2 carry a grade
    assert reading.agreements == 1
    assert reading.agreement_rate == 0.5
    assert reading.narrated == 1
    assert reading.pending == 1
    assert reading.unresolved == 1
    # No confidence on any row ⇒ no Brier population, bins suppressed.
    assert reading.brier_total == 0
    assert reading.reliability_bins == {}


def test_measure_self_calibration_brier_and_reliability_bins(
    mem_fs: dict[str, str],
) -> None:
    """Table-tested Brier score and coarse reliability bins over the
    confidence-bearing measured-and-resolved bets, once the threshold is met."""
    d = Path("/fake/cal-brier")
    b = JsonlPredictionLedger(d)
    rows = [
        ("reflection-1", 0.8, "correct"),
        ("reflection-2", 0.9, "correct"),
        ("reflection-3", 0.7, "correct"),
        ("reflection-4", 0.2, "wrong"),
        ("reflection-5", 0.5, "wrong"),
        ("reflection-6", 0.1, "wrong"),
    ]
    for pid, conf, verdict in rows:
        record_reflection_prediction(b, pid, "p", "loss", confidence=conf)
        record_reflection_verdict(b, pid, verdict, verdict_source="measured")

    reading = measure_self_calibration(b)
    assert reading.resolved == 6
    assert reading.hits == 3
    assert reading.hit_rate == 0.5
    assert reading.brier_total == 6
    # 0.04 + 0.01 + 0.09 + 0.04 + 0.25 + 0.01 = 0.44 over 6 bets.
    assert reading.brier_score == pytest.approx(0.44 / 6)
    assert reading.reliability_bins == {
        "0.67-1.00": {"total": 3, "hits": 3},
        "0.34-0.66": {"total": 1, "hits": 0},
        "0.00-0.33": {"total": 2, "hits": 0},
    }


def test_measure_self_calibration_below_brier_threshold_suppresses_bins(
    mem_fs: dict[str, str],
) -> None:
    """Fewer than the minimum confidence-bearing bets ⇒ a Brier score but no bins."""
    d = Path("/fake/cal-thin")
    b = JsonlPredictionLedger(d)
    thin = [(0.8, "correct"), (0.2, "wrong"), (0.9, "correct"), (0.4, "wrong")]
    for i, (conf, verdict) in enumerate(thin, start=1):
        record_reflection_prediction(b, f"reflection-{i}", "p", "loss", confidence=conf)
        record_reflection_verdict(b, f"reflection-{i}", verdict, verdict_source="measured")

    reading = measure_self_calibration(b)
    assert reading.brier_total == 4  # below the minimum-rows threshold
    assert reading.brier_score == pytest.approx((0.04 + 0.04 + 0.01 + 0.16) / 4)
    assert reading.reliability_bins == {}


def test_measure_self_calibration_defensive_confidence(
    mem_fs: dict[str, str],
) -> None:
    """A numeric-string, a null, and an unparseable confidence read defensively —
    only the parseable numeric string feeds the Brier numerator."""
    d = Path("/fake/cal-def")
    path = d / PREDICTIONS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"source": "reflection", "prediction_id": "reflection-1", "prediction": "p",
         "loss_condition": "loss", "verdict": "unscored", "confidence": "0.8",
         "timestamp": "2026-01-01T00:00:00+00:00"},
        {"source": "reflection_verdict", "prediction_id": "reflection-1",
         "verdict": "correct", "verdict_source": "measured"},
        {"source": "reflection", "prediction_id": "reflection-2", "prediction": "p",
         "loss_condition": "loss", "verdict": "unscored", "confidence": None,
         "timestamp": "2026-01-01T00:00:00+00:00"},
        {"source": "reflection_verdict", "prediction_id": "reflection-2",
         "verdict": "wrong", "verdict_source": "measured"},
        {"source": "reflection", "prediction_id": "reflection-3", "prediction": "p",
         "loss_condition": "loss", "verdict": "unscored", "confidence": "not-a-number",
         "timestamp": "2026-01-01T00:00:00+00:00"},
        {"source": "reflection_verdict", "prediction_id": "reflection-3",
         "verdict": "correct", "verdict_source": "measured"},
    ]
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    reading = measure_self_calibration(JsonlPredictionLedger(d))
    assert reading.resolved == 3
    assert reading.brier_total == 1  # only the "0.8" numeric string parses
    assert reading.brier_score == pytest.approx((0.8 - 1) ** 2)


def test_measure_self_calibration_windows_the_tail(mem_fs: dict[str, str]) -> None:
    """``n <= 0`` reads nothing; a small ``n`` windows to the trace tail."""
    d = Path("/fake/cal-win")
    b = JsonlPredictionLedger(d)
    record_reflection_prediction(b, "reflection-1", "p", "loss")
    record_reflection_verdict(b, "reflection-1", "correct", verdict_source="measured")
    assert measure_self_calibration(b, n=0).stamped_total == 0
    assert measure_self_calibration(b, n=10).stamped_total == 1


def test_measure_self_calibration_pending_grade_excluded_from_agreement(
    mem_fs: dict[str, str],
) -> None:
    """A still-``pending`` measured bet carrying a self-grade is excluded from the
    agreement domain — its ``correct``/``wrong`` grade can never equal a ``pending``
    verdict, so counting it would deflate agreement_rate with immaturity rather than
    genuine miscalibration. Only the resolved graded bet is scored."""
    d = Path("/fake/cal-pending-grade")
    b = JsonlPredictionLedger(d)
    # A resolved measured bet whose self-grade agrees.
    record_reflection_prediction(b, "reflection-1", "p", "loss")
    record_reflection_verdict(
        b, "reflection-1", "correct",
        verdict_source="measured", self_verdict="correct",
    )
    # A pending measured bet still carrying the model's self-grade — the real ledger
    # shape: ``contemplate`` stamps the self-grade on the first ``pending`` re-score.
    record_reflection_prediction(b, "reflection-2", "p", "loss")
    record_reflection_verdict(
        b, "reflection-2", "pending",
        verdict_source="measured", self_verdict="correct",
    )

    reading = measure_self_calibration(b)
    # Only the resolved bet is in the agreement domain, not the pending one, so the
    # rate stays 1/1 (100%) rather than being deflated to 1/2 by the immature grade.
    assert reading.agreement_total == 1
    assert reading.agreements == 1
    assert reading.agreement_rate == 1.0
    assert reading.pending == 1


# ---------------------------------------------------------------------------
# #4106 — same-iteration retry guards.
# ---------------------------------------------------------------------------


def test_load_latest_before_cycle_skips_same_and_later_bets(
    mem_fs: dict[str, str],
) -> None:
    backend = JsonlPredictionLedger(Path("/fake/agent"))
    record_reflection_prediction(backend, "reflection-4", "old", "lc4")
    record_reflection_prediction(backend, "reflection-5", "new", "lc5")
    latest = load_latest_reflection_prediction(backend, before_cycle=5)
    assert latest is not None
    assert latest["prediction_id"] == "reflection-4"


def test_load_latest_before_cycle_keeps_unparseable_ids(
    mem_fs: dict[str, str],
) -> None:
    backend = JsonlPredictionLedger(Path("/fake/agent"))
    record_reflection_prediction(backend, "reflection-9", "later", "lc")
    record_reflection_prediction(backend, "legacy-bet", "foreign", "lc")
    latest = load_latest_reflection_prediction(backend, before_cycle=5)
    assert latest is not None
    assert latest["prediction_id"] == "legacy-bet"


def test_load_latest_before_cycle_none_is_legacy(
    mem_fs: dict[str, str],
) -> None:
    backend = JsonlPredictionLedger(Path("/fake/agent"))
    record_reflection_prediction(backend, "reflection-5", "new", "lc5")
    latest = load_latest_reflection_prediction(backend)
    assert latest is not None
    assert latest["prediction_id"] == "reflection-5"


def test_has_reflection_prediction(mem_fs: dict[str, str]) -> None:
    backend = JsonlPredictionLedger(Path("/fake/agent"))
    assert has_reflection_prediction(backend, "reflection-5") is False
    record_reflection_prediction(backend, "reflection-5", "p", "lc")
    record_reflection_verdict(backend, "reflection-6", "wrong")
    assert has_reflection_prediction(backend, "reflection-5") is True
    assert has_reflection_prediction(backend, "reflection-6") is False


@pytest.mark.asyncio
async def test_contemplate_without_loop_iteration_keeps_appending(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Iteration 0 (non-loop caller) keeps the legacy append — no dedup."""
    agent = make_agent()
    await _run_contemplate(agent, _REFLECTION_WITH_MARKERS)
    await _run_contemplate(agent, _REFLECTION_WITH_MARKERS)
    rows = [
        json.loads(line)
        for line in mem_fs[str(Path("/fake/agent") / PREDICTIONS_FILENAME)].splitlines()
        if line.strip()
    ]
    assert len([r for r in rows if r.get("source") == "reflection"]) == 2
