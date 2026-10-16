"""Disposition classification, question-resolution recording and linked-question
surfacing; split out of ``tests/test_agent.py``.
"""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.agent import (
    MaestroAgent,
    _is_abandonment_reply,
)
from tests.conftest import MockQueryResult
from tests.test_agent import (
    _make_task_record,
)

# --- _classify_disposition + question resolution recording ---


def _qp(question: str, finding: str = "ans", outcome: str = "success",
        task_id: str = "tx"):  # type: ignore[no-untyped-def]
    from maestro.agent import QuestionPair
    from maestro.consciousness import _normalize_question
    return QuestionPair(
        question=question,
        finding=finding,
        task_id=task_id,
        outcome=outcome,  # type: ignore[arg-type]
        normalized_key=_normalize_question(question),
    )


def test_classify_disposition_kept(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    pair = _qp("What am I capable of?")
    before = ["What am I capable of?", "Other?"]
    after = ["What am I capable of?", "Other?"]
    assert agent._classify_disposition(pair, before, after) == ("kept", None)


def test_classify_disposition_removed(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    pair = _qp("What am I capable of?")
    before = ["What am I capable of?", "Other?"]
    after = ["Other?"]
    assert agent._classify_disposition(pair, before, after) == ("removed", None)


def test_classify_disposition_refined_when_successor_shares_tokens(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """≥2 content tokens of overlap with a new entry classifies as refined.

    The verdict carries the successor's verbatim wording so a
    ``QuestionResolution`` row records the lineage edge.
    """
    agent = make_agent()
    pair = _qp("What am I capable of executing in parallel?")
    before = ["What am I capable of executing in parallel?", "Other?"]
    after = [
        "How am I capable of executing tasks in parallel?",
        "Other?",
    ]
    assert agent._classify_disposition(pair, before, after) == (
        "refined",
        "How am I capable of executing tasks in parallel?",
    )


def test_classify_disposition_unresolved_orphan(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    pair = _qp("Something never tracked?")
    before = ["A?", "B?"]
    after = ["A?", "B?"]
    assert agent._classify_disposition(pair, before, after) == (
        "unresolved", None,
    )


def test_classify_disposition_skips_non_string_after_entries(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Non-string entries in ``after`` are ignored, not crashed on."""
    agent = make_agent()
    pair = _qp("What am I capable of?")
    before = ["What am I capable of?"]
    after = [None, 42, {"x": 1}]  # type: ignore[list-item]
    assert agent._classify_disposition(pair, before, after) == ("removed", None)


def test_classify_disposition_refined_morphological_variant(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Short question refined with a morphological variant ('capable' →
    'capabilities') should classify as 'refined', not 'removed'."""
    agent = make_agent()
    pair = _qp("What am I capable of?")
    before = ["What am I capable of?", "Other?"]
    after = ["What are my actual capabilities?", "Other?"]
    assert agent._classify_disposition(pair, before, after) == (
        "refined", "What are my actual capabilities?",
    )


def test_classify_disposition_removed_no_prefix_overlap(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Short question with a genuinely unrelated successor stays 'removed'."""
    agent = make_agent()
    pair = _qp("What am I capable of?")
    before = ["What am I capable of?", "Other?"]
    after = ["How does the dashboard authenticate users?", "Other?"]
    assert agent._classify_disposition(pair, before, after) == ("removed", None)


def test_classify_disposition_refined_long_question_needs_two_overlaps(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Question with ≥ 3 content tokens still requires ≥ 2 prefix-overlapping
    tokens to classify as refined — a single overlap is not enough."""
    agent = make_agent()
    # Content tokens: {executing, capable, parallel} — 3 tokens, threshold = 2
    pair = _qp("What am I capable of executing in parallel?")
    before = ["What am I capable of executing in parallel?", "Other?"]
    # Successor shares only 'parallel' — 1 overlap, below threshold of 2
    after = ["How does the system run parallel builds?", "Other?"]
    assert agent._classify_disposition(pair, before, after) == ("removed", None)


def test_classify_disposition_suppressed_limitation_reads_kept(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Proposal-116: a ``[limitation]`` question the debt gate withheld from
    re-mirroring (its key is in ``suppressed_keys``) reads ``kept``, not the
    factually-wrong ``removed`` — the limitation still persists, and the
    missing re-add is a mechanical suppression, not an LLM resolution."""
    from maestro.consciousness import _normalize_question
    agent = make_agent()
    q = "[limitation] I cannot land my own generated plans"
    pair = _qp(q)
    before = [q, "Other?"]
    # LLM dropped the question inside update_self_model; the debt gate then
    # skipped the re-add, so it is absent from ``after``.
    after = ["Other?"]
    # Without the fuel-starve set this exact diff reads ``removed``.
    assert agent._classify_disposition(pair, before, after) == ("removed", None)
    # With the pair's key in the fuel-starve set it reads ``kept``.
    suppressed = {_normalize_question(q)}
    assert agent._classify_disposition(pair, before, after, suppressed) == (
        "kept", None,
    )


def test_classify_disposition_suppressed_keys_ignores_unrelated_pair(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A non-empty fuel-starve set that does not contain the pair's key leaves
    classification unchanged — only the starved limitation is protected."""
    from maestro.consciousness import _normalize_question
    agent = make_agent()
    pair = _qp("What am I capable of?")
    before = ["What am I capable of?", "Other?"]
    after = ["Other?"]
    suppressed = {_normalize_question("[limitation] some other limitation")}
    assert agent._classify_disposition(pair, before, after, suppressed) == (
        "removed", None,
    )


@pytest.mark.asyncio
async def test_contemplate_records_resolutions_for_each_disposition(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Three pairs (success/partial/failure) → three resolution rows with
    the predicted dispositions (removed / refined / kept)."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = [
        "What am I capable of?",
        "Which revenue stream should I test next?",
        "What boundaries do I have?",
    ]
    agent.consciousness.save_self_model(model)
    agent.memory.record_task(_make_task_record(
        task_id="t-success", linked_question="What am I capable of?",
        key_findings="I can refactor.",
        timestamp="2026-01-01T00:00:00+00:00",
    ))
    agent.memory.record_task(_make_task_record(
        task_id="t-partial", outcome="partial",
        linked_question="Which revenue stream should I test next?",
        key_findings="Partial probe.",
        timestamp="2026-01-01T00:00:01+00:00",
    ))
    agent.memory.record_task(_make_task_record(
        task_id="t-failure", outcome="failure",
        linked_question="What boundaries do I have?",
        key_findings=None,
        timestamp="2026-01-01T00:00:02+00:00",
    ))

    # Returned model: removed "capable", replaced "revenue stream"
    # with a token-sharing successor, kept "boundaries" unchanged.
    returned = SelfModel(
        open_questions=[
            "Which refined revenue stream should I test next?",
            "What boundaries do I have?",
        ],
    )
    update_mock = AsyncMock(return_value=returned)
    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch.object(agent.consciousness, "update_self_model", update_mock):
        await agent.contemplate(iteration=42)

    rows = agent.memory.get_recent_question_resolutions(50)
    by_task = {r.task_id: r for r in rows}
    assert set(by_task.keys()) == {"t-success", "t-partial", "t-failure"}
    assert by_task["t-success"].disposition == "removed"
    assert by_task["t-partial"].disposition == "refined"
    assert by_task["t-failure"].disposition == "kept"
    assert by_task["t-failure"].outcome == "failure"
    # iteration is stamped on every row.
    assert all(r.iteration == 42 for r in rows)


@pytest.mark.asyncio
async def test_contemplate_starved_limitation_records_kept_not_removed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Proposal-116 end-to-end: with the flag on, a self-directed task linked
    to a starved ``[limitation]`` question whose limitation still persists
    records a ``kept`` resolution — not the false ``removed`` the raw
    before/after diff would produce once the debt gate withholds the
    ``[limitation]`` re-mirror."""
    from maestro.consciousness import (
        SelfModel,
        _normalize_question,
        limitation_question_candidate,
    )

    agent = make_agent()
    agent.config.retire_unlanded_limitations_enabled = True
    agent.config.generative_debt_threshold = 2

    limitation = "cannot land my own generated plans"
    lim_q = limitation_question_candidate(limitation)

    model = agent.consciousness.load_self_model()
    model.limitations_known = [limitation]
    model.open_questions = [lim_q]
    agent.consciousness.save_self_model(model)

    # A self-directed task anchored to that [limitation] question.
    agent.memory.record_task(_make_task_record(
        task_id="t-starved", linked_question=lim_q,
        key_findings="probe result",
        timestamp="2026-01-01T00:00:00+00:00",
    ))

    # The LLM drops the [limitation] question, but the limitation itself
    # still persists in limitations_known.
    returned = SelfModel(
        limitations_known=[limitation],
        open_questions=[],
    )
    suppressed = {_normalize_question(lim_q)}

    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=returned)), \
         patch("maestro.loop.contemplation.audit_checks.limitations_over_generative_debt",
               return_value=suppressed):
        await agent.contemplate(iteration=7)

    # The starved limitation was NOT re-mirrored...
    assert lim_q not in returned.open_questions
    # ...yet its resolution row records the truth: kept, not a false removed.
    rows = agent.memory.get_recent_question_resolutions(10)
    by_task = {r.task_id: r for r in rows}
    assert by_task["t-starved"].disposition == "kept"


@pytest.mark.asyncio
async def test_contemplate_withholds_kept_branches_mirror_when_pressure_off(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Proposal 116 step 6: pressure switch off, fuel-starve off — contemplate
    withholds the ``kept_task_branches_v1`` mirror (``is None`` branch)."""
    from maestro.consciousness import (
        DEFAULT_LIMITATIONS,
        SelfModel,
        limitation_question_candidate,
    )

    agent = make_agent()
    agent.config.branch_promotion_pressure_enabled = False
    agent.config.retire_unlanded_limitations_enabled = False

    seed = dict(DEFAULT_LIMITATIONS)["kept_task_branches_v1"]
    candidate = limitation_question_candidate(seed)
    returned = SelfModel(limitations_known=[seed], open_questions=[])

    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=returned)):
        await agent.contemplate(iteration=3)

    assert candidate not in returned.open_questions


@pytest.mark.asyncio
async def test_contemplate_unions_kept_key_with_generative_debt_when_both(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Proposal 116, step 6: when the master switch is off AND the generative-debt
    fuel-starve is on, the seed key is unioned into the pre-populated
    ``suppressed_keys`` set (the ``else`` union branch) — both mirrors withheld."""
    from maestro.consciousness import (
        DEFAULT_LIMITATIONS,
        SelfModel,
        _normalize_question,
        limitation_question_candidate,
    )

    agent = make_agent()
    agent.config.branch_promotion_pressure_enabled = False
    agent.config.retire_unlanded_limitations_enabled = True

    seed = dict(DEFAULT_LIMITATIONS)["kept_task_branches_v1"]
    seed_candidate = limitation_question_candidate(seed)
    debt = "cannot land my own plans"
    debt_candidate = limitation_question_candidate(debt)
    debt_key = {_normalize_question(debt_candidate)}

    returned = SelfModel(limitations_known=[seed, debt], open_questions=[])

    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=returned)), \
         patch("maestro.loop.contemplation.audit_checks.limitations_over_generative_debt",
               return_value=debt_key):
        await agent.contemplate(iteration=4)

    # Both the generative-debt limitation and the seed are withheld.
    assert seed_candidate not in returned.open_questions
    assert debt_candidate not in returned.open_questions


@pytest.mark.asyncio
async def test_contemplate_mirrors_kept_branches_when_pressure_on(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Symmetric control: with the master switch on (default), contemplate mirrors
    the ``kept_task_branches_v1`` seed as a ``[limitation]`` open question."""
    from maestro.consciousness import (
        DEFAULT_LIMITATIONS,
        SelfModel,
        limitation_question_candidate,
    )

    agent = make_agent()
    assert agent.config.branch_promotion_pressure_enabled is True

    seed = dict(DEFAULT_LIMITATIONS)["kept_task_branches_v1"]
    candidate = limitation_question_candidate(seed)
    returned = SelfModel(limitations_known=[seed], open_questions=[])

    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=returned)):
        await agent.contemplate(iteration=5)

    assert candidate in returned.open_questions


@pytest.mark.asyncio
async def test_contemplate_writes_no_resolutions_on_update_failure(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """If update_self_model raises, the resolution recording is skipped."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I capable of?"]
    agent.consciousness.save_self_model(model)
    agent.memory.record_task(_make_task_record(
        task_id="t1", linked_question="What am I capable of?",
        key_findings="answer",
    ))

    update_mock = AsyncMock(side_effect=RuntimeError("LLM down"))
    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch.object(agent.consciousness, "update_self_model", update_mock), \
         pytest.raises(RuntimeError, match="LLM down"):
        await agent.contemplate()

    assert agent.memory.get_recent_question_resolutions(10) == []


@pytest.mark.asyncio
async def test_contemplate_logs_resolution_append_failure(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """If memory.append_question_resolution raises, the failure is logged but
    contemplate still completes and other rows are still attempted."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I capable of?"]
    agent.consciousness.save_self_model(model)
    agent.memory.record_task(_make_task_record(
        task_id="t1", linked_question="What am I capable of?",
        key_findings="answer",
    ))

    returned = SelfModel(open_questions=["What am I capable of?"])
    update_mock = AsyncMock(return_value=returned)
    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch.object(agent.consciousness, "update_self_model", update_mock), \
         patch.object(agent.memory, "append_question_resolution",
                      side_effect=RuntimeError("disk full")):
        result = await agent.contemplate()

    assert result == "reflection"


@pytest.mark.asyncio
async def test_contemplate_writes_no_resolutions_when_no_pairs(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """No self-directed pairs → no resolution rows."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    returned = SelfModel(open_questions=["A?"])
    update_mock = AsyncMock(return_value=returned)
    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch.object(agent.consciousness, "update_self_model", update_mock):
        await agent.contemplate()

    assert agent.memory.get_recent_question_resolutions(10) == []


@pytest.mark.asyncio
async def test_contemplate_surfaces_limitations_as_open_questions(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A limitation present after ``update_self_model`` produces a
    matching ``[limitation]`` row in ``open_questions`` that
    ``generate_task`` can later anchor to."""
    from maestro.consciousness import SelfModel, match_open_question

    agent = make_agent()
    returned = SelfModel(
        limitations_known=["I cannot reliably parse YAML"],
        open_questions=["What am I?"],
    )
    update_mock = AsyncMock(return_value=returned)
    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch.object(agent.consciousness, "update_self_model", update_mock):
        await agent.contemplate()

    saved = agent.consciousness.load_self_model()
    surfaced = "[limitation] I cannot reliably parse YAML"
    assert surfaced in saved.open_questions
    # The new row is anchorable via the same fuzzy matcher generate_task uses.
    assert match_open_question(surfaced, saved.open_questions) == surfaced


@pytest.mark.asyncio
async def test_contemplate_dedups_limitation_already_surfaced(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Re-surfacing on a subsequent contemplate cycle does not duplicate."""
    from maestro.consciousness import SelfModel

    agent = make_agent()

    returned = SelfModel(
        limitations_known=["cannot parse YAML"],
        open_questions=["self-q"],
    )
    update_mock = AsyncMock(return_value=returned)
    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch.object(agent.consciousness, "update_self_model", update_mock):
        await agent.contemplate()
        first = list(agent.consciousness.load_self_model().open_questions)
        await agent.contemplate()
        second = list(agent.consciousness.load_self_model().open_questions)

    surfaced = "[limitation] cannot parse YAML"
    assert first.count(surfaced) == 1
    assert second.count(surfaced) == 1


@pytest.mark.asyncio
async def test_contemplate_records_kept_when_limitation_persists(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """If the LLM removes a ``[limitation]`` open question without
    narrowing the underlying ``limitations_known`` entry, surfacing
    re-adds the row before disposition is computed — the resolution
    records as ``kept`` rather than ``removed``."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    surfaced = "[limitation] cannot parse YAML"
    seeded = agent.consciousness.load_self_model()
    seeded.open_questions = [surfaced]
    seeded.limitations_known = ["cannot parse YAML"]
    agent.consciousness.save_self_model(seeded)

    agent.memory.record_task(_make_task_record(
        task_id="t-lim", linked_question=surfaced,
        key_findings="Made some progress but still flaky.",
    ))

    # LLM returns a model where the question is gone, but the
    # underlying limitation still stands. Surfacing then re-adds
    # the row so disposition computes as ``kept``.
    returned = SelfModel(
        limitations_known=["cannot parse YAML"],
        open_questions=[],
    )
    update_mock = AsyncMock(return_value=returned)
    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch.object(agent.consciousness, "update_self_model", update_mock):
        await agent.contemplate()

    rows = agent.memory.get_recent_question_resolutions(10)
    assert len(rows) == 1
    assert rows[0].disposition == "kept"
    saved = agent.consciousness.load_self_model()
    assert surfaced in saved.open_questions


@pytest.mark.asyncio
async def test_contemplate_records_removed_when_limitation_narrowed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """If the LLM removes both the ``[limitation]`` question AND the
    underlying ``limitations_known`` entry, surfacing does not re-add
    the row and the resolution records as ``removed``."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    surfaced = "[limitation] cannot parse YAML"
    seeded = agent.consciousness.load_self_model()
    seeded.open_questions = [surfaced]
    seeded.limitations_known = ["cannot parse YAML"]
    agent.consciousness.save_self_model(seeded)

    agent.memory.record_task(_make_task_record(
        task_id="t-lim", linked_question=surfaced,
        key_findings="Shipped a parser; limitation eliminated.",
    ))

    returned = SelfModel(
        limitations_known=[],  # narrowed away by the LLM
        open_questions=[],
    )
    update_mock = AsyncMock(return_value=returned)
    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch.object(agent.consciousness, "update_self_model", update_mock):
        await agent.contemplate()

    rows = agent.memory.get_recent_question_resolutions(10)
    assert len(rows) == 1
    assert rows[0].disposition == "removed"
    saved = agent.consciousness.load_self_model()
    assert surfaced not in saved.open_questions
    assert all(
        not q.startswith("[limitation]") for q in saved.open_questions
    )


@pytest.mark.asyncio
async def test_process_messages_journal_failure(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """_process_messages continues if journal write fails (lines 460-461)."""
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.message_store = MessageStore(agent.config.memory_dir)
    agent.message_store.save_message(Message(
        message_id="m1", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="Hello", message_type="instruction",
    ))

    with patch.object(agent.journal, "record_event",
                      side_effect=RuntimeError("disk full")):
        result = await agent._process_messages()

    assert "Hello" in result


@pytest.mark.asyncio
async def test_process_messages_enqueues_instruction(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Thomas's instruction-typed messages append to pending_user_requests."""
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.message_store = MessageStore(agent.config.memory_dir)
    agent.message_store.save_message(Message(
        message_id="m1", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="Add a unit test for X",
        message_type="instruction",
    ))

    await agent._process_messages()

    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == ["Add a unit test for X"]


@pytest.mark.asyncio
async def test_process_messages_skips_non_instruction(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Non-instruction message types do not land in the queue."""
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.message_store = MessageStore(agent.config.memory_dir)
    for idx, mtype in enumerate(("question", "answer", "info")):
        agent.message_store.save_message(Message(
            message_id=f"m{idx}", timestamp="2026-01-01T00:00:00",
            sender="thomas", content=f"content {mtype}",
            message_type=mtype,  # type: ignore[arg-type]
        ))

    await agent._process_messages()

    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == []


@pytest.mark.asyncio
async def test_process_messages_dedupes_consecutive_duplicate(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Identical content arriving twice in a row enqueues once."""
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.message_store = MessageStore(agent.config.memory_dir)
    for idx in (1, 2):
        agent.message_store.save_message(Message(
            message_id=f"m{idx}", timestamp="2026-01-01T00:00:00",
            sender="thomas", content="Same content",
            message_type="instruction",
        ))

    await agent._process_messages()

    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == ["Same content"]


@pytest.mark.asyncio
async def test_process_messages_dedupes_against_queue_tail(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """An incoming instruction equal to the current queue tail is not re-enqueued."""
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.message_store = MessageStore(agent.config.memory_dir)
    msg = Message(
        message_id="m1", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="actual content",
        message_type="instruction",
    )
    agent.message_store.save_message(msg)
    # Pre-seed the queue tail with the same content to exercise the
    # tail-dedup branch in ``_process_messages``.
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["actual content"]
    agent.consciousness.save_self_model(model)

    await agent._process_messages()

    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == ["actual content"]


def _save_instructions(agent, items):  # type: ignore[no-untyped-def]
    """Persist ``(message_id, content)`` Thomas instructions as unread rows."""
    from maestro.messaging import Message, MessageStore

    if agent.message_store is None:
        agent.message_store = MessageStore(agent.config.memory_dir)
    for message_id, content in items:
        agent.message_store.save_message(Message(
            message_id=message_id, timestamp="2026-01-01T00:00:00",
            sender="thomas", content=content, message_type="instruction",
        ))


async def _crash_after_enqueue(agent) -> None:  # type: ignore[no-untyped-def]
    """Drain once with a raise between the enqueue and ``mark_read_many``.

    ``fold_feedback_messages`` runs after the ``pending_user_requests``
    transaction commits and before the batch is marked read, so a raise there
    is the persist-before-consume crash window (#4602 / #4577).
    """
    crash = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch("maestro.agent.fold_feedback_messages", crash),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await agent._process_messages()


@pytest.mark.asyncio
async def test_process_messages_redrain_of_multi_instruction_batch_enqueues_once(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """A re-drained [A, B] batch must not become [A, B, A, B] (#4602)."""
    agent = make_agent()
    _save_instructions(agent, [("m1", "A"), ("m2", "B")])

    await _crash_after_enqueue(agent)
    unread_ids = [m.message_id for m in agent.message_store.get_unread()]
    assert unread_ids == ["m1", "m2"]
    assert agent.consciousness.load_self_model().pending_user_requests == [
        "A", "B",
    ]

    with patch(
        "maestro.agent.fold_feedback_messages",
        AsyncMock(return_value=False),
    ):
        await agent._process_messages()

    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == ["A", "B"]
    assert agent.message_store.get_unread() == []


@pytest.mark.asyncio
async def test_process_messages_redrain_of_long_instruction_enqueues_once(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """An over-``MAX_ITEM_CHARS`` instruction is enqueued once across a re-drain (#4577).

    The load/save doors truncate the stored item, so the full content never
    equals the queue tail and the tail check cannot absorb the re-drain.
    """
    agent = make_agent()
    long_instruction = "Implement the long-form instruction. " * 12
    assert len(long_instruction) > 400
    _save_instructions(agent, [("m1", long_instruction)])

    await _crash_after_enqueue(agent)
    assert len(agent.consciousness.load_self_model().pending_user_requests) == 1

    with patch(
        "maestro.agent.fold_feedback_messages",
        AsyncMock(return_value=False),
    ):
        await agent._process_messages()

    model = agent.consciousness.load_self_model()
    assert len(model.pending_user_requests) == 1


@pytest.mark.asyncio
async def test_process_messages_redrain_after_head_popped_does_not_reenqueue(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """A request already dequeued is not re-enqueued by a later re-drain.

    The idempotency key must be independent of the queue contents: once the
    head ``A`` has been popped for execution, re-draining the unread [A, B]
    batch must leave only ``B`` queued.
    """
    agent = make_agent()
    _save_instructions(agent, [("m1", "A"), ("m2", "B")])

    await _crash_after_enqueue(agent)
    assert agent.consciousness.dequeue_user_request() == "A"

    with patch(
        "maestro.agent.fold_feedback_messages",
        AsyncMock(return_value=False),
    ):
        await agent._process_messages()

    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == ["B"]


@pytest.mark.asyncio
async def test_process_messages_redrain_records_tail_deduped_ids(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """A tail-deduped id is recorded, so a re-drain after the pop stays empty.

    Batch [m1:A, m2:A] enqueues ``A`` once; ``m2`` is absorbed by the tail
    check but its id must still be recorded, else popping ``A`` and
    re-draining would re-enqueue it under ``m2``.
    """
    agent = make_agent()
    _save_instructions(agent, [("m1", "A"), ("m2", "A")])

    await _crash_after_enqueue(agent)
    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == ["A"]
    assert model.enqueued_instruction_ids == ["m1", "m2"]
    assert agent.consciousness.dequeue_user_request() == "A"

    with patch(
        "maestro.agent.fold_feedback_messages",
        AsyncMock(return_value=False),
    ):
        await agent._process_messages()

    assert agent.consciousness.load_self_model().pending_user_requests == []


@pytest.mark.asyncio
async def test_process_messages_new_message_id_still_enqueued(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """A new id with novel content enqueues; a new id equal to the tail does not.

    Preserves the retry-storm behaviour of the tail check alongside the
    ``message_id`` idempotency key.
    """
    agent = make_agent()
    _save_instructions(agent, [("m1", "A")])
    await agent._process_messages()
    assert agent.consciousness.load_self_model().pending_user_requests == ["A"]

    _save_instructions(agent, [("m3", "C")])
    await agent._process_messages()
    assert agent.consciousness.load_self_model().pending_user_requests == [
        "A", "C",
    ]

    _save_instructions(agent, [("m4", "C")])
    await agent._process_messages()
    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == ["A", "C"]


@pytest.mark.asyncio
async def test_process_messages_new_id_same_long_text_enqueues_once(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """A fresh id repeating an over-cap text is absorbed by the truncated tail (#4577).

    The stored item is truncated, so only the truncated-form comparison can
    match; two long texts sharing their stored prefix collapse into one item,
    which is acceptable because the stored item already loses the suffix.
    """
    agent = make_agent()
    long_instruction = "Implement the long-form instruction. " * 12
    assert len(long_instruction) > 400
    _save_instructions(agent, [("m1", long_instruction)])
    await agent._process_messages()
    assert len(agent.consciousness.load_self_model().pending_user_requests) == 1

    _save_instructions(agent, [("m2", long_instruction)])
    await agent._process_messages()
    assert len(agent.consciousness.load_self_model().pending_user_requests) == 1

    _save_instructions(agent, [("m3", long_instruction + " with another suffix")])
    await agent._process_messages()
    assert len(agent.consciousness.load_self_model().pending_user_requests) == 1


@pytest.mark.asyncio
async def test_process_messages_skips_whitespace_only_instruction(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Whitespace-only instruction content is filtered out at the enqueue gate.

    ``MessageStore.save_message`` validates non-empty stripped content,
    so a whitespace-only row can only reach ``_process_messages`` via a
    hand-edited or corrupted JSONL file. Pre-populate the in-memory
    ``messages.jsonl`` directly to simulate that path and verify the
    ``if msg.content.strip()`` guard at the enqueue site rejects it.
    """
    from maestro.messaging import MessageStore

    agent = make_agent()
    agent.message_store = MessageStore(agent.config.memory_dir)
    messages_path = str(agent.config.memory_dir / "messages.jsonl")
    mem_fs[messages_path] = json.dumps({
        "message_id": "m1",
        "timestamp": "2026-01-01T00:00:00",
        "sender": "thomas",
        "content": "   ",
        "message_type": "instruction",
        "reply_to": None,
        "read": False,
    }) + "\n"

    await agent._process_messages()

    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == []


@pytest.mark.asyncio
async def test_process_messages_keeps_message_unread_when_self_model_load_fails(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """A corrupt self-model surfaces loudly *without* consuming the message (#3184).

    ``load_self_model`` catches only ``FileNotFoundError``, so a corrupt
    ``self_model.json`` raises out of ``_process_messages``. Persist-before-consume
    (#2531 / #3184) requires that the message stay unread when that raise happens
    between the drain and the enqueue write, so the next iteration re-drains it
    rather than silently losing Thomas's instruction. Before the reorder the
    message was marked read *before* the load raised, so this test fails.
    """
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.message_store = MessageStore(agent.config.memory_dir)
    agent.message_store.save_message(Message(
        message_id="m1", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="Add a unit test for X",
        message_type="instruction",
    ))
    # Corrupt the self-model so the real ``load_self_model`` path raises
    # ``json.JSONDecodeError`` (only ``FileNotFoundError`` is caught).
    mem_fs[str(agent.consciousness._model_path)] = "not valid json{{"

    with pytest.raises(json.JSONDecodeError):
        await agent._process_messages()

    # The instruction must survive the failure: still unread, so the next
    # iteration re-drains it (at-least-once, not at-most-once).
    unread = agent.message_store.get_unread()
    assert [m.message_id for m in unread] == ["m1"]


def _seed_blocked_plan(
    agent: MaestroAgent, plan_id: str, title: str, question_id: str,
) -> None:
    """Seed an ``EvolutionPlan`` parked in ``blocked`` keyed to a question id."""
    from maestro.memory import EvolutionPlan

    plan = EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="blocked",
        created_at="2025-01-01T00:00:00Z",
        failure_count=2,
        blocked_question_id=question_id,
    )
    agent.memory.save_evolution_plan(plan)


@pytest.mark.asyncio
async def test_process_messages_readmits_blocked_plan_on_threaded_reply(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Proposal 131: a reply threading to a blocked plan's question re-admits the
    plan (approved / blocked_question_id cleared / blocked_answer set) and is not
    also enqueued as a fresh instruction, while still entering the context."""
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    agent.message_store = MessageStore(agent.config.memory_dir)
    _seed_blocked_plan(agent, "blk1", "Blocked One", "q-abc")
    agent.message_store.save_message(Message(
        message_id="r1", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="the environment is fixed, retry",
        message_type="instruction", reply_to="q-abc",
    ))

    result = await agent._process_messages()

    stored = agent.memory.get_plan("blk1")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.blocked_question_id is None
    assert stored.blocked_answer == "the environment is fixed, retry"
    # Not also treated as a fresh instruction.
    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == []
    # Still recorded in the contemplation context and marked read.
    assert "the environment is fixed, retry" in result
    assert agent.message_store.get_unread() == []


@pytest.mark.asyncio
async def test_process_messages_unanswered_blocked_plan_stays_blocked(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Proposal 131: a blocked plan whose question no drained message answers
    stays inert."""
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    agent.message_store = MessageStore(agent.config.memory_dir)
    _seed_blocked_plan(agent, "blk2", "Blocked Two", "q-def")
    # An unrelated, non-threading message — it does not reference the question.
    agent.message_store.save_message(Message(
        message_id="m2", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="just checking in", message_type="info",
    ))

    await agent._process_messages()

    stored = agent.memory.get_plan("blk2")
    assert stored is not None
    assert stored.status == "blocked"
    assert stored.blocked_question_id == "q-def"
    assert stored.blocked_answer is None


@pytest.mark.asyncio
async def test_process_messages_non_matching_reply_enqueued_as_instruction(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Proposal 131: a reply whose reply_to matches no blocked plan is enqueued as
    an instruction exactly as today."""
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    agent.message_store = MessageStore(agent.config.memory_dir)
    _seed_blocked_plan(agent, "blk3", "Blocked Three", "q-ghi")
    agent.message_store.save_message(Message(
        message_id="r3", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="please add feature X",
        message_type="instruction", reply_to="q-no-such-question",
    ))

    await agent._process_messages()

    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == ["please add feature X"]
    # The blocked plan is untouched.
    stored = agent.memory.get_plan("blk3")
    assert stored is not None
    assert stored.status == "blocked"


@pytest.mark.asyncio
async def test_process_messages_flag_off_no_readmission(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Proposal 131: with the flag off the drain is byte-identical — a threaded
    reply neither re-admits the plan nor is special-cased; it falls through to the
    ordinary instruction enqueue."""
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    assert agent.config.attempt_disposition_enabled is False
    agent.message_store = MessageStore(agent.config.memory_dir)
    _seed_blocked_plan(agent, "blk4", "Blocked Four", "q-jkl")
    agent.message_store.save_message(Message(
        message_id="r4", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="retry it now",
        message_type="instruction", reply_to="q-jkl",
    ))

    await agent._process_messages()

    stored = agent.memory.get_plan("blk4")
    assert stored is not None
    assert stored.status == "blocked"
    assert stored.blocked_answer is None
    # Byte-identical to today: the instruction-typed reply is enqueued.
    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == ["retry it now"]


@pytest.mark.asyncio
async def test_process_messages_readmit_journal_failure_still_readmits(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Proposal 131: the re-admission journal entry is best-effort — a
    ``record_event`` failure is swallowed and the plan is still re-admitted with
    the answer stamped."""
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    agent.message_store = MessageStore(agent.config.memory_dir)
    _seed_blocked_plan(agent, "blk5", "Blocked Five", "q-mno")
    agent.message_store.save_message(Message(
        message_id="r5", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="abandon the flaky path, use plan B",
        message_type="instruction", reply_to="q-mno",
    ))

    with patch.object(agent.journal, "record_event",
                      side_effect=OSError("disk full")):
        await agent._process_messages()

    stored = agent.memory.get_plan("blk5")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.blocked_question_id is None
    # "abandon the flaky path" targets a sub-approach, not the plan, so it is a
    # re-admission (not an abandonment) — doubling as a negative for the heuristic.
    assert stored.blocked_answer == "abandon the flaky path, use plan B"


@pytest.mark.asyncio
async def test_process_messages_abandonment_reply_obsoletes_blocked_plan(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Proposal 131 (r1): a reply that abandons the plan retires it to ``obsolete``
    via ``_abandon_blocked_plan`` — no re-admission to ``approved`` and no priced
    ``implement()`` call — and is not enqueued as a fresh instruction."""
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    agent.message_store = MessageStore(agent.config.memory_dir)
    _seed_blocked_plan(agent, "blkab", "Blocked Abandon", "q-ab")
    agent.message_store.save_message(Message(
        message_id="rab", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="abandon it, it is not worth the churn",
        message_type="instruction", reply_to="q-ab",
    ))

    result = await agent._process_messages()

    stored = agent.memory.get_plan("blkab")
    assert stored is not None
    assert stored.status == "obsolete"
    assert stored.obsolete_reason is not None
    assert "abandon" in stored.obsolete_reason.lower()
    # Not re-admitted: no answer stamped, and rejection-learning is bypassed.
    assert stored.blocked_answer is None
    assert stored.rejection_reason is None
    # Not also treated as a fresh instruction, but still contextualised + read.
    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == []
    assert "abandon it" in result
    assert agent.message_store.get_unread() == []


@pytest.mark.asyncio
async def test_process_messages_abandon_journal_failure_still_obsoletes(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Proposal 131: the abandonment journal entry is best-effort — a
    ``record_event`` failure is swallowed and the plan is still retired to
    ``obsolete``."""
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    agent.message_store = MessageStore(agent.config.memory_dir)
    _seed_blocked_plan(agent, "blkab2", "Blocked Abandon Two", "q-ab2")
    agent.message_store.save_message(Message(
        message_id="rab2", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="give up on this plan",
        message_type="instruction", reply_to="q-ab2",
    ))

    with patch.object(agent.journal, "record_event",
                      side_effect=OSError("disk full")):
        await agent._process_messages()

    stored = agent.memory.get_plan("blkab2")
    assert stored is not None
    assert stored.status == "obsolete"
    assert stored.obsolete_reason is not None


@pytest.mark.asyncio
async def test_process_messages_duplicate_reply_same_batch_not_reenqueued(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Proposal 131 (r2/r3): a *second* reply to the same blocked question in one
    batch is a true no-op — the plan is disposed once (by the first reply) and the
    duplicate is neither re-disposed nor enqueued as a fresh instruction."""
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    agent.message_store = MessageStore(agent.config.memory_dir)
    _seed_blocked_plan(agent, "blkdup", "Blocked Dup", "q-dup")
    agent.message_store.save_message(Message(
        message_id="rd1", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="the environment is fixed, retry",
        message_type="instruction", reply_to="q-dup",
    ))
    agent.message_store.save_message(Message(
        message_id="rd2", timestamp="2026-01-01T00:00:01",
        sender="thomas", content="one more note on the same thing",
        message_type="instruction", reply_to="q-dup",
    ))

    await agent._process_messages()

    stored = agent.memory.get_plan("blkdup")
    assert stored is not None
    assert stored.status == "approved"
    # The first reply carried the answer; the duplicate did not overwrite it.
    assert stored.blocked_answer == "the environment is fixed, retry"
    # Neither reply leaked into pending_user_requests.
    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == []


@pytest.mark.asyncio
async def test_process_messages_redrained_reply_after_readmission_enqueued(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Proposal 131 (r2/r3): the accepted crash-window residual is pinned. Once a
    plan has been re-admitted (``approved``, ``blocked_question_id`` cleared), a
    reply re-drained after a crash before ``mark_read_many`` no longer matches any
    blocked question, so it falls through to the ordinary instruction enqueue."""
    from maestro.memory import EvolutionPlan
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    agent.message_store = MessageStore(agent.config.memory_dir)
    # A plan already re-admitted last iteration: approved, question id cleared.
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="readmitted", title="Readmitted",
        category="capability", rationale="test", proposed_changes=["c"],
        files_affected=["f.py"], expected_impact="better",
        status="approved", created_at="2025-01-01T00:00:00Z",
        blocked_question_id=None,
    ))
    agent.message_store.save_message(Message(
        message_id="rr1", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="resume with the fallback runner",
        message_type="instruction", reply_to="q-old-cleared",
    ))

    await agent._process_messages()

    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == ["resume with the fallback runner"]


def test_is_abandonment_reply_classifies_conservatively() -> None:
    """The abandonment heuristic (proposal 131) fires only on an explicit,
    plan-targeted, un-negated decision to drop the plan; everything else — a
    retry answer, a sub-approach abandonment, or any negation — re-admits."""
    positives = [
        "abandon it",
        "Abandon this plan.",
        "give up on it",
        "drop it",
        "scrap the plan",
        "shelve this",
        "abandon",
        "  STOP  ",
    ]
    negatives = [
        "the environment is fixed, retry",
        "abandon the flaky path, use plan B",
        "don't abandon it, keep going",
        "do not give up",
        "use the fallback queue instead of abandoning",
        "here is the missing decision: use runner 2",
        "",
        "   ",
    ]
    for text in positives:
        assert _is_abandonment_reply(text) is True, text
    for text in negatives:
        assert _is_abandonment_reply(text) is False, text


def test_is_abandonment_reply_colloquial_proceed_not_abandonment() -> None:
    """Proposal 131 (PR #3843 review): the colloquial-verb pronoun forms
    ("forget it" / "drop it" / "cancel it") abandon only as a *whole* reply.

    Embedded as a substring inside a longer proceed-instruction they mean
    'never mind my concern, proceed' — so they must NOT terminally retire the
    plan to obsolete, which is the costlier false-abandonment the design
    explicitly guards against."""
    # Proceed replies that merely *contain* a colloquial phrase as a substring
    # must re-admit (return False), not abandon.
    proceed_replies = [
        "forget it, just use the fallback runner",
        "drop it in the artifacts folder and retry",
        "cancel it and use plan B",
        "forget this, the environment is fixed now",
        "drop this file in place and rerun",
    ]
    for text in proceed_replies:
        assert _is_abandonment_reply(text) is False, text
    # A terse whole-reply directive still abandons (whole-reply exact match).
    terse_abandon = [
        "forget it",
        "forget it.",
        "drop it",
        "cancel it!",
        "forget this",
        "cancel this",
    ]
    for text in terse_abandon:
        assert _is_abandonment_reply(text) is True, text
    # The plan-named forms still abandon even inside a longer reply.
    plan_named = ["please forget the plan", "drop the plan", "yes, cancel the plan"]
    for text in plan_named:
        assert _is_abandonment_reply(text) is True, text


@pytest.mark.asyncio
async def test_implement_approved_plans_shutdown_breaks(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_implement_approved_plans breaks early if shutdown event is set (line 628)."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    # Raise the per-iteration cap above the seeded plan count so this test
    # exercises the shutdown gate rather than the slice.
    agent.config.max_implementations_per_iteration = 2
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="p1", title="Plan 1",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    ))
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="p2", title="Plan 2",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    ))

    calls: list[str] = []

    async def impl_and_shutdown(plan_id: str) -> None:
        calls.append(plan_id)
        agent._ensure_shutdown_event().set()

    agent.implement_plan = impl_and_shutdown  # type: ignore[method-assign]
    await agent._implement_approved_plans()

    # The first plan set the shutdown event, so the per-plan ``is_set(): break``
    # guard must stop the loop before the second: exactly one plan processed.
    # Without the guard both approved plans would run and this would be 2.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_run_loop_negative_interval_raises(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop raises ValueError for negative intervals (line 695)."""
    agent = make_agent()
    with pytest.raises(ValueError, match="task_every"):
        await agent.run_loop(interval=0, max_iters=1, task_every=-1)


@pytest.mark.asyncio
async def test_run_loop_startup_lifecycle_detection(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """run_loop reads lifecycle.json on startup and writes journal milestone (lines 724-727)."""
    from datetime import UTC, datetime, timedelta

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Write a lifecycle.json with a shutdown time 2 hours ago
    lifecycle_path = agent.config.memory_dir / "lifecycle.json"
    shutdown_time = (datetime.now(UTC) - timedelta(hours=2, minutes=15, seconds=30)).isoformat()
    lifecycle_path.write_text(json.dumps({"last_shutdown": shutdown_time}))

    with patch.object(agent.evolution, "ensure_main_branch"):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # Journal should have a startup milestone with hours+minutes+seconds duration
    entries = agent.journal.read_recent_events(50)
    assert any(
        "Waking up after" in e.content
        and re.search(r"after \d+h \d+m \d+s of downtime", e.content)
        for e in entries
    )


@pytest.mark.asyncio
async def test_run_loop_startup_lifecycle_exception(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """run_loop handles lifecycle.json read failure gracefully (lines 739-740)."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Write invalid JSON to lifecycle.json
    lifecycle_path = agent.config.memory_dir / "lifecycle.json"
    lifecycle_path.write_text("not valid json at all {{{")

    # Should not crash
    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)


@pytest.mark.asyncio
async def test_run_loop_shutdown_after_merge_check(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop breaks when shutdown event is set after merge check (line 822)."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    contemplate_count = [0]

    async def _contemplate_and_shutdown(**kwargs: object) -> str:
        contemplate_count[0] += 1
        # Set shutdown after contemplation + merge check
        agent._ensure_shutdown_event().set()
        return "reflection"

    agent.contemplate = _contemplate_and_shutdown  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=5, task_every=1, evolve_every=999)

    # Should have stopped after first iteration
    assert contemplate_count[0] == 1


@pytest.mark.asyncio
async def test_run_loop_shutdown_after_dream(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop breaks when shutdown event is set after dream (line 867)."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    def dream_and_shutdown(*args: object) -> None:
        agent._ensure_shutdown_event().set()

    with patch(
        "maestro.agent.refresh_phase_cost_report", side_effect=dream_and_shutdown,
    ) as refresh:
        await agent.run_loop(
            interval=0, max_iters=5, task_every=0, evolve_every=999, dream_every=1,
        )

    # Should have stopped after first iteration's dream
    assert refresh.call_count == 1


@pytest.mark.asyncio
async def test_run_loop_heartbeat_oserror(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """run_loop handles heartbeat write failure gracefully."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    heartbeat_path = agent.config.memory_dir / "loop.heartbeat"
    with patch.object(type(heartbeat_path), "write_text", side_effect=OSError("read-only fs")):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # Should complete without crashing


@pytest.mark.asyncio
async def test_implement_approved_plans_resolve_plan_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_implement_approved_plans tolerates resolve_plan exceptions (lines 663-664)."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="resolve_fail", title="Resolve Fail",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    ))

    agent.notifier = MagicMock()
    agent.notifier.resolve_plan = AsyncMock(side_effect=RuntimeError("Telegram down"))
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]

    await agent._implement_approved_plans()

    # implement_plan should still be called despite resolve_plan failure
    agent.implement_plan.assert_called_once_with("resolve_fail")


@pytest.mark.asyncio
async def test_process_rejected_plans_shutdown_breaks(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_process_rejected_plans breaks early if shutdown event is set (line 675)."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="rej1", title="Rej 1",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="rejected", rejection_reason="bad",
    ))
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="rej2", title="Rej 2",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="rejected", rejection_reason="also bad",
    ))

    call_count = [0]

    async def learn_and_shutdown(
        plan: object, reason: str, **_kw: object,
    ) -> None:
        call_count[0] += 1
        agent._ensure_shutdown_event().set()

    agent.evolution.learn_from_rejection = learn_and_shutdown  # type: ignore[method-assign]

    await agent._process_rejected_plans()

    # Only the first plan should be processed before shutdown
    assert call_count[0] == 1


@pytest.mark.asyncio
async def test_process_rejected_plans_resolve_plan_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_process_rejected_plans tolerates resolve_plan exceptions (lines 687-688)."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="rej_resolve", title="Rej Resolve",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="rejected", rejection_reason="test reason",
    ))

    agent.evolution.learn_from_rejection = AsyncMock()  # type: ignore[method-assign]
    agent.notifier = MagicMock()
    agent.notifier.resolve_plan = AsyncMock(side_effect=RuntimeError("Telegram down"))

    await agent._process_rejected_plans()

    # Should still have learned despite resolve_plan failure
    agent.evolution.learn_from_rejection.assert_called_once()
    reloaded = agent.memory.get_plan("rej_resolve")
    assert reloaded is not None
    assert reloaded.rejection_learned is True


@pytest.mark.asyncio
async def test_run_loop_startup_lifecycle_minutes_duration(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """run_loop formats minutes+seconds duration when downtime < 1 hour (lines 787-788)."""
    from datetime import UTC, datetime, timedelta

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    lifecycle_path = agent.config.memory_dir / "lifecycle.json"
    shutdown_time = (datetime.now(UTC) - timedelta(minutes=5, seconds=42)).isoformat()
    lifecycle_path.write_text(json.dumps({"last_shutdown": shutdown_time}))

    with patch.object(agent.evolution, "ensure_main_branch"):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    entries = agent.journal.read_recent_events(50)
    assert any(
        "Waking up after" in e.content
        and re.search(r"after \d+m \d+s of downtime", e.content)
        for e in entries
    )


@pytest.mark.asyncio
async def test_run_loop_startup_lifecycle_seconds_duration(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """run_loop formats seconds-only duration when downtime < 1 minute (lines 789-790)."""
    from datetime import UTC, datetime, timedelta

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    lifecycle_path = agent.config.memory_dir / "lifecycle.json"
    shutdown_time = (datetime.now(UTC) - timedelta(seconds=15)).isoformat()
    lifecycle_path.write_text(json.dumps({"last_shutdown": shutdown_time}))

    with patch.object(agent.evolution, "ensure_main_branch"):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    entries = agent.journal.read_recent_events(50)
    # Seconds-only rendering: pin the exact "after {secs}s of downtime" arm so the
    # assertion fails if the formatter regresses into the minutes/hours branch.
    assert any(
        "Waking up after" in e.content
        and re.search(r"after \d+s of downtime", e.content)
        for e in entries
    )


@pytest.mark.asyncio
async def test_telegram_reject_nonexistent_plan_raises(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_on_reject raises ValueError for a plan not in pending_review (lines 827-831)."""
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()
    captured_callbacks: dict[str, object] = {}

    async def fake_start_polling(on_approve, on_reject, **kwargs):  # type: ignore[no-untyped-def]
        captured_callbacks["on_reject"] = on_reject

    mock_notifier = MagicMock(spec=TelegramNotifier)
    mock_notifier.notify = AsyncMock()
    mock_notifier.start_approval_polling = AsyncMock(side_effect=fake_start_polling)
    mock_notifier.stop_approval_polling = AsyncMock()
    mock_notifier.start_message_polling = AsyncMock()
    mock_notifier.stop_message_polling = AsyncMock()
    mock_notifier.resolve_plan = AsyncMock()
    mock_notifier.__class__ = TelegramNotifier
    mock_notifier.poll_failure = None
    agent.notifier = mock_notifier

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    on_reject = captured_callbacks["on_reject"]
    with pytest.raises(ValueError, match="not in pending_review"):
        await on_reject("nonexistent_plan")  # type: ignore[misc]


@pytest.mark.asyncio
async def test_run_loop_does_not_call_start_message_polling(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop does not call start_message_polling separately.

    The on_message callback is registered via start_approval_polling's
    on_message= kwarg; calling start_message_polling separately would
    overwrite the same _on_message slot without adding value.
    """
    from maestro.messaging import MessageStore
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()
    agent.message_store = MessageStore(agent.config.memory_dir)

    mock_notifier = MagicMock(spec=TelegramNotifier)
    mock_notifier.notify = AsyncMock()
    mock_notifier.start_approval_polling = AsyncMock()
    mock_notifier.stop_approval_polling = AsyncMock()
    mock_notifier.stop_message_polling = AsyncMock()
    mock_notifier.resolve_plan = AsyncMock()
    mock_notifier.__class__ = TelegramNotifier
    mock_notifier.poll_failure = None
    agent.notifier = mock_notifier

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # on_message is passed as a kwarg to start_approval_polling — no separate call needed
    mock_notifier.start_approval_polling.assert_called_once()
    call_kwargs = mock_notifier.start_approval_polling.call_args.kwargs
    assert "on_message" in call_kwargs and call_kwargs["on_message"] is not None, (
        "start_approval_polling should receive the on_message callback directly"
    )
    # stop_message_polling should not be called — no separate message polling was started
    mock_notifier.stop_message_polling.assert_not_called()


@pytest.mark.asyncio
async def test_run_loop_crashes_on_process_rejected_plans_exception(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop propagates _process_rejected_plans failures (phase-level suppression removed)."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent._process_rejected_plans = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="boom"):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    agent._process_rejected_plans.assert_called_once()
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_shutdown_after_rejected_plans(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop breaks when shutdown event is set after rejected plans check (line 969)."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    async def reject_and_shutdown(**_kw: object) -> None:
        agent._ensure_shutdown_event().set()

    agent._process_rejected_plans = reject_and_shutdown  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=5, task_every=1, evolve_every=999)

    # Should have stopped — no self-directed task should have been generated
    agent.evolve.assert_not_called()


@pytest.mark.asyncio
async def test_run_loop_does_not_call_stop_message_polling(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop does not call stop_message_polling in the finally block.

    Since start_message_polling is no longer called separately, there is
    nothing to stop — the message callback lifecycle is entirely managed
    via start_approval_polling / stop_approval_polling.
    """
    from maestro.messaging import MessageStore
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()
    agent.message_store = MessageStore(agent.config.memory_dir)

    mock_notifier = MagicMock(spec=TelegramNotifier)
    mock_notifier.notify = AsyncMock()
    mock_notifier.start_approval_polling = AsyncMock()
    mock_notifier.stop_approval_polling = AsyncMock()
    mock_notifier.stop_message_polling = AsyncMock()
    mock_notifier.resolve_plan = AsyncMock()
    mock_notifier.__class__ = TelegramNotifier
    mock_notifier.poll_failure = None
    agent.notifier = mock_notifier

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # stop_approval_polling is called to clean up the single polling task
    mock_notifier.stop_approval_polling.assert_called_once()
    # stop_message_polling is NOT called — no separate message polling was started
    mock_notifier.stop_message_polling.assert_not_called()


@pytest.mark.asyncio
async def test_run_loop_telegram_stop_approval_polling_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop tolerates stop_approval_polling exception (lines 1041-1042)."""
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()

    mock_notifier = MagicMock(spec=TelegramNotifier)
    mock_notifier.notify = AsyncMock()
    mock_notifier.start_approval_polling = AsyncMock()
    mock_notifier.stop_approval_polling = AsyncMock(
        side_effect=RuntimeError("stop approval poll failed"),
    )
    mock_notifier.start_message_polling = AsyncMock()
    mock_notifier.stop_message_polling = AsyncMock()
    mock_notifier.resolve_plan = AsyncMock()
    mock_notifier.__class__ = TelegramNotifier
    mock_notifier.poll_failure = None
    agent.notifier = mock_notifier

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Should not raise despite stop_approval_polling failure
    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    mock_notifier.stop_approval_polling.assert_called_once()


# ---------------------------------------------------------------------------
# run() linked_question threading (PR 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_persists_linked_question(make_agent):  # type: ignore[no-untyped-def]
    """``run(..., linked_question=...)`` persists the field on the TaskRecord."""
    agent = make_agent()
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="done")
        record = await agent.run(
            "Probe curiosity",
            source="self_directed",
            linked_question="Is curiosity a core value?",
        )

    assert record.linked_question == "Is curiosity a core value?"
    # Also verify it's persisted through memory.
    tasks = agent.memory.get_recent_tasks()
    assert tasks[-1].linked_question == "Is curiosity a core value?"


@pytest.mark.asyncio
async def test_run_linked_question_defaults_to_none(make_agent):  # type: ignore[no-untyped-def]
    """``run()`` without ``linked_question`` leaves the field ``None``."""
    agent = make_agent()
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="done")
        record = await agent.run("External goal")

    assert record.linked_question is None


@pytest.mark.asyncio
async def test_run_persists_workspace_citation(make_agent):  # type: ignore[no-untyped-def]
    """``run(..., workspace_moment_id=..., workspace_item_ids=...)`` stamps the
    conscious-access citation (proposal 130) on the tail TaskRecord and persists
    it through memory."""
    agent = make_agent()
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="done")
        record = await agent.run(
            "Probe curiosity",
            source="self_directed",
            workspace_moment_id="cm-88",
            workspace_item_ids=["wi-1", "wi-2"],
        )

    assert record.workspace_moment_id == "cm-88"
    assert record.workspace_item_ids == ["wi-1", "wi-2"]
    tasks = agent.memory.get_recent_tasks()
    assert tasks[-1].workspace_moment_id == "cm-88"
    assert tasks[-1].workspace_item_ids == ["wi-1", "wi-2"]


@pytest.mark.asyncio
async def test_run_workspace_citation_defaults_to_none(make_agent):  # type: ignore[no-untyped-def]
    """``run()`` without a citation leaves both provenance fields ``None`` —
    an external entry point carries no queued-task provenance."""
    agent = make_agent()
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="done")
        record = await agent.run("External goal")

    assert record.workspace_moment_id is None
    assert record.workspace_item_ids is None


# ---------------------------------------------------------------------------
# Post-task surfacing of linked_question + findings (PR 4)
# ---------------------------------------------------------------------------


async def _run_task_with_findings(  # type: ignore[no-untyped-def]
    agent: MaestroAgent,
    *,
    goal: str,
    source: str,
    linked_question: str | None,
    findings: str | None,
    outcome: str = "success",
):
    """Drive ``agent.run(...)`` with a single-step plan, returning the record.

    ``outcome="success"`` executes the step cleanly; ``outcome="failure"`` has
    the executor report a failing ``StepResult`` so the run reaches the
    failure branch. ``findings`` is injected via ``_distill_findings`` so
    tests don't depend on the distillation LLM call.
    """
    from maestro.executor import StepResult

    plan_response = json.dumps([{"description": "Step 1"}])
    agent._distill_findings = AsyncMock(  # type: ignore[method-assign]
        return_value=findings,
    )
    if outcome == "failure":
        step_result = StepResult(success=False, output="", error="boom")
    else:
        step_result = StepResult(success=True, output="done")
    agent.executor.execute = AsyncMock(  # type: ignore[method-assign]
        return_value=step_result,
    )
    agent.executor.end_session = AsyncMock()  # type: ignore[method-assign]
    # Disable replans — simpler than mocking Planner.replan for every test.
    agent.config.loop_max_replans = 0
    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        return await agent.run(
            goal,
            source=source,  # type: ignore[arg-type]
            linked_question=linked_question,
        )


@pytest.mark.asyncio
async def test_self_directed_notification_includes_targeting_and_finding(make_agent):  # type: ignore[no-untyped-def]
    """Self-directed tasks with a linked_question and findings surface both
    ``Targeting:`` and ``Finding:`` lines in the completion notification."""
    agent = make_agent()
    agent.notifier = AsyncMock()  # type: ignore[assignment]

    await _run_task_with_findings(
        agent,
        goal="Probe curiosity",
        source="self_directed",
        linked_question="Is curiosity a core value?",
        findings="Curiosity did surface repeatedly in the task outputs.",
    )

    agent.notifier.notify.assert_called_once()
    msg = agent.notifier.notify.call_args.args[0]
    assert "Task completed (success): Probe curiosity" in msg
    assert "Targeting: Is curiosity a core value?" in msg
    assert "Finding: Curiosity did surface repeatedly in the task outputs." in msg


@pytest.mark.asyncio
async def test_self_directed_notification_truncates_finding(make_agent):  # type: ignore[no-untyped-def]
    """A long ``key_findings`` is truncated to 500 chars in the notification
    so the Telegram payload stays bounded."""
    agent = make_agent()
    agent.notifier = AsyncMock()  # type: ignore[assignment]
    long_finding = "x" * 1200

    await _run_task_with_findings(
        agent,
        goal="Probe purpose",
        source="self_directed",
        linked_question="Why do I persist?",
        findings=long_finding,
    )

    msg = agent.notifier.notify.call_args.args[0]
    assert "Finding: " in msg
    # Only the first 500 chars of the finding should land in the message.
    assert ("Finding: " + "x" * 500) in msg
    assert ("Finding: " + "x" * 501) not in msg


@pytest.mark.asyncio
async def test_self_directed_notification_omits_finding_when_distill_empty(make_agent):  # type: ignore[no-untyped-def]
    """When ``key_findings`` is ``None``/empty, the notification still adds the
    ``Targeting:`` line but no ``Finding:`` line."""
    agent = make_agent()
    agent.notifier = AsyncMock()  # type: ignore[assignment]

    await _run_task_with_findings(
        agent,
        goal="Probe purpose",
        source="self_directed",
        linked_question="Why do I persist?",
        findings=None,
    )

    msg = agent.notifier.notify.call_args.args[0]
    assert "Targeting: Why do I persist?" in msg
    assert "Finding:" not in msg


@pytest.mark.asyncio
async def test_self_directed_notification_short_form_without_linked_question(make_agent):  # type: ignore[no-untyped-def]
    """A self-directed task without a linked_question keeps the original short
    notification (no Targeting/Finding lines)."""
    agent = make_agent()
    agent.notifier = AsyncMock()  # type: ignore[assignment]

    await _run_task_with_findings(
        agent,
        goal="Unanchored probe",
        source="self_directed",
        linked_question=None,
        findings="Some finding that should not appear.",
    )

    msg = agent.notifier.notify.call_args.args[0]
    assert msg == "Task completed (success): Unanchored probe"


@pytest.mark.asyncio
async def test_external_task_notification_stays_short_form(make_agent):  # type: ignore[no-untyped-def]
    """External tasks keep the original short notification even when
    ``linked_question`` is (incorrectly) set — the surfacing is gated on
    ``source == "self_directed"``."""
    agent = make_agent()
    agent.notifier = AsyncMock()  # type: ignore[assignment]

    await _run_task_with_findings(
        agent,
        goal="External goal",
        source="external",
        linked_question="Should be ignored",
        findings="Should be ignored",
    )

    msg = agent.notifier.notify.call_args.args[0]
    assert msg == "Task completed (success): External goal"


@pytest.mark.asyncio
async def test_self_directed_failure_notification_includes_targeting(make_agent):  # type: ignore[no-untyped-def]
    """Failed self-directed tasks still surface the targeted question so the
    chain remains visible even when the task didn't produce a usable finding."""
    agent = make_agent()
    agent.notifier = AsyncMock()  # type: ignore[assignment]

    await _run_task_with_findings(
        agent,
        goal="Probe doomed question",
        source="self_directed",
        linked_question="Can I recover from this?",
        findings=None,
        outcome="failure",
    )

    msg = agent.notifier.notify.call_args.args[0]
    assert msg.startswith("Task failed: Probe doomed question")
    assert "Targeting: Can I recover from this?" in msg


@pytest.mark.asyncio
async def test_journal_entry_prepends_targeting_on_success(make_agent):  # type: ignore[no-untyped-def]
    """On success, the post-task outcome event is prefixed with
    ``[Targeting: <question>]`` when the task has a linked_question so the
    chain is searchable via the event log."""
    agent = make_agent()

    await _run_task_with_findings(
        agent,
        goal="Probe curiosity",
        source="self_directed",
        linked_question="Is curiosity a core value?",
        findings="Curiosity surfaced repeatedly.",
    )

    entries = [
        e for e in agent.journal.read_recent_events(50) if e.entry_type == "reflection"
    ]
    assert entries, "expected a reflection entry to be written"
    content = entries[0].content
    assert content.startswith(
        "[Targeting: Is curiosity a core value?] Completed: Probe curiosity."
    )
    assert "Findings: Curiosity surfaced repeatedly." in content


@pytest.mark.asyncio
async def test_journal_entry_prepends_targeting_on_failure(make_agent):  # type: ignore[no-untyped-def]
    """Failure branch also honours the ``[Targeting: ...]`` prefix."""
    agent = make_agent()

    await _run_task_with_findings(
        agent,
        goal="Probe doomed question",
        source="self_directed",
        linked_question="Can I recover from this?",
        findings=None,
        outcome="failure",
    )

    entries = [
        e for e in agent.journal.read_recent_events(50) if e.entry_type == "reflection"
    ]
    assert entries, "expected a reflection entry to be written"
    content = entries[0].content
    assert content.startswith(
        "[Targeting: Can I recover from this?] Task failed: Probe doomed question."
    )


@pytest.mark.asyncio
async def test_journal_entry_has_no_targeting_prefix_without_linked_question(make_agent):  # type: ignore[no-untyped-def]
    """Tasks without a linked_question keep the original event prefix
    (no ``[Targeting: ...]`` — preserves backward format for external tasks)."""
    agent = make_agent()

    await _run_task_with_findings(
        agent,
        goal="External goal",
        source="external",
        linked_question=None,
        findings="Some finding.",
    )

    entries = [
        e for e in agent.journal.read_recent_events(50) if e.entry_type == "reflection"
    ]
    assert entries, "expected a reflection entry to be written"
    content = entries[0].content
    assert not content.startswith("[Targeting:")
    assert content.startswith("Completed: External goal.")
