"""Self-directed task pairs and their contemplate wiring
(``maestro/loop/contemplation.py``); split out of ``tests/test_agent.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.test_agent import (
    _make_task_record,
)


def test_build_self_directed_pairs_filters_and_dedupes(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Filters non-self-directed and missing fields; dedupes by question with
    most-recent winning. Failed outcomes ARE surfaced (with a marker)."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I?", "Which revenue stream pays first?"]
    agent.consciousness.save_self_model(model)

    recent = [
        _make_task_record(task_id="external", source="external",
                          linked_question="What am I?", key_findings="ext"),
        _make_task_record(task_id="no_q", linked_question=None,
                          key_findings="x"),
        _make_task_record(task_id="no_findings", linked_question="What am I?",
                          key_findings=None),
        _make_task_record(task_id="old", linked_question="What am I?",
                          key_findings="older finding",
                          timestamp="2026-01-01T00:00:01+00:00"),
        _make_task_record(task_id="new", linked_question="What am I?",
                          key_findings="newer finding",
                          timestamp="2026-01-01T00:00:02+00:00"),
        _make_task_record(task_id="other", linked_question="Which revenue stream pays first?",
                          key_findings="purpose finding"),
    ]
    pairs = agent._build_self_directed_pairs(recent)
    questions = [p.question for p in pairs]
    findings = {p.question: p.finding for p in pairs}
    assert "What am I?" in questions
    assert "Which revenue stream pays first?" in questions
    # Most recent wins for the duplicate question.
    assert findings["What am I?"] == "newer finding"


def test_build_self_directed_pairs_marks_unknown_questions(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A linked_question no longer in open_questions is preserved with a
    [no longer tracked] marker so the LLM can re-add or discard."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["Which revenue stream pays first?"]
    agent.consciousness.save_self_model(model)

    recent = [
        _make_task_record(task_id="t", linked_question="Old retired question?",
                          key_findings="some answer", outcome="partial"),
    ]
    pairs = agent._build_self_directed_pairs(recent)
    assert len(pairs) == 1
    pair = pairs[0]
    assert "Old retired question?" in pair.question
    assert "[no longer tracked]" in pair.question
    assert pair.finding == "some answer"


def test_build_self_directed_pairs_surfaces_failure_with_marker(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Failed self-directed tasks reach the prompt with a marker finding."""
    from maestro.agent import _FAILED_TASK_FINDING_MARKER

    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I capable of?"]
    agent.consciousness.save_self_model(model)

    recent = [
        _make_task_record(task_id="t-fail", outcome="failure",
                          linked_question="What am I capable of?",
                          key_findings=None),
    ]
    pairs = agent._build_self_directed_pairs(recent)
    assert len(pairs) == 1
    assert pairs[0].outcome == "failure"
    assert pairs[0].finding == _FAILED_TASK_FINDING_MARKER
    assert pairs[0].task_id == "t-fail"


def test_build_self_directed_pairs_failure_dedupes_with_success(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Mixed cohort: most-recent wins regardless of outcome."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I capable of?"]
    agent.consciousness.save_self_model(model)

    recent = [
        _make_task_record(task_id="old-success",
                          linked_question="What am I capable of?",
                          key_findings="answer", outcome="success",
                          timestamp="2026-01-01T00:00:00+00:00"),
        _make_task_record(task_id="recent-fail", outcome="failure",
                          linked_question="What am I capable of?",
                          key_findings=None,
                          timestamp="2026-01-01T00:00:02+00:00"),
    ]
    pairs = agent._build_self_directed_pairs(recent)
    assert len(pairs) == 1
    assert pairs[0].outcome == "failure"
    assert pairs[0].task_id == "recent-fail"


def test_build_self_directed_pairs_skips_unknown_outcome(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A corrupted record with an outcome outside TaskOutcome is silently skipped."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I?"]
    agent.consciousness.save_self_model(model)
    rec = _make_task_record(
        task_id="t-weird", linked_question="What am I?",
        key_findings="x",
    )
    rec.outcome = "weird"  # type: ignore[assignment]
    pairs = agent._build_self_directed_pairs([rec])
    assert pairs == []


def test_build_self_directed_pairs_caps_to_limit(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The pair list is capped at _SELF_DIRECTED_PAIR_LIMIT."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    questions = [f"Q{i}?" for i in range(20)]
    model.open_questions = questions[: agent.consciousness.MAX_LIST_ITEMS]
    agent.consciousness.save_self_model(model)
    saved_questions = agent.consciousness.load_self_model().open_questions

    recent = [
        _make_task_record(task_id=f"t{i}", linked_question=q, key_findings=f"f{i}",
                          timestamp=f"2026-01-01T00:00:{i:02d}+00:00")
        for i, q in enumerate(saved_questions)
    ]
    pairs = agent._build_self_directed_pairs(recent)
    assert len(pairs) == agent._SELF_DIRECTED_PAIR_LIMIT


def test_build_self_directed_pairs_uses_fuzzy_match(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Case- and whitespace-mangled linked_question still resolves to the
    live verbatim entry."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I capable of?"]
    agent.consciousness.save_self_model(model)

    recent = [
        _make_task_record(task_id="t", linked_question="  WHAT  am  I  capable  OF?  ",
                          key_findings="answer"),
    ]
    pairs = agent._build_self_directed_pairs(recent)
    assert len(pairs) == 1
    assert pairs[0].question == "What am I capable of?"
    assert pairs[0].finding == "answer"


def test_build_self_directed_pairs_annotates_refinement_count(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A live question with two predecessor wordings carries refinement_count=2.

    The pair builder reads the recent-resolution log via lineage_keys and
    surfaces the chain depth so ``Consciousness.update_self_model`` can
    show the LLM ``(refined Nx before)``.
    """
    from maestro.memory import QuestionResolution

    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What truly am I capable of?"]
    agent.consciousness.save_self_model(model)

    # Q1 → Q1' → Q1'' chain in the resolution log.
    agent.memory.append_question_resolution(QuestionResolution(
        resolution_id="r1", iteration=1,
        timestamp="2026-04-26T00:00:00+00:00",
        question="What am I capable of?",
        task_id="t-pred-1", outcome="success",
        disposition="refined", finding="some",
        refined_to="What am I capable of really?",
    ))
    agent.memory.append_question_resolution(QuestionResolution(
        resolution_id="r2", iteration=2,
        timestamp="2026-04-26T00:00:01+00:00",
        question="What am I capable of really?",
        task_id="t-pred-2", outcome="success",
        disposition="refined", finding="some",
        refined_to="What truly am I capable of?",
    ))

    recent = [
        _make_task_record(
            task_id="t-live",
            linked_question="What truly am I capable of?",
            key_findings="answer",
        ),
    ]
    pairs = agent._build_self_directed_pairs(recent)
    assert len(pairs) == 1
    assert pairs[0].refinement_count == 2


def test_build_self_directed_pairs_zero_refinement_count_no_lineage(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A pair with no prior REFINE rows in the log carries ``refinement_count == 0``."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I capable of?"]
    agent.consciousness.save_self_model(model)

    recent = [
        _make_task_record(
            task_id="t",
            linked_question="What am I capable of?",
            key_findings="answer",
        ),
    ]
    pairs = agent._build_self_directed_pairs(recent)
    assert len(pairs) == 1
    assert pairs[0].refinement_count == 0


def test_build_self_directed_pairs_resolution_load_failure_falls_back(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """Resolution-log read failure is logged; pairs continue with refinement_count=0."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I capable of?"]
    agent.consciousness.save_self_model(model)

    recent = [
        _make_task_record(
            task_id="t",
            linked_question="What am I capable of?",
            key_findings="answer",
        ),
    ]
    with patch.object(
        agent.memory, "get_recent_question_resolutions",
        side_effect=OSError("disk gone"),
    ):
        pairs = agent._build_self_directed_pairs(recent)
    assert len(pairs) == 1
    assert pairs[0].refinement_count == 0


def test_build_self_directed_pairs_skips_already_verdicted_task(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A task whose task_id is already in the resolution log is not
    re-surveyed — the LLM has already committed a verdict on its
    (question, finding) pair, so re-surfacing would only emit a zombie
    resolution row."""
    from maestro.memory import QuestionResolution

    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I capable of?"]
    agent.consciousness.save_self_model(model)

    agent.memory.append_question_resolution(QuestionResolution(
        resolution_id="r-prior", iteration=1,
        timestamp="2026-04-26T00:00:00+00:00",
        question="What am I capable of?",
        task_id="t-prior", outcome="success",
        disposition="removed", finding="answer",
    ))

    recent = [
        _make_task_record(
            task_id="t-prior",
            linked_question="What am I capable of?",
            key_findings="answer",
        ),
    ]
    pairs = agent._build_self_directed_pairs(recent)
    assert pairs == []


def test_build_self_directed_pairs_surfaces_task_without_prior_resolution(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A task whose task_id is absent from the resolution log surfaces
    normally, even when other tasks in the log are present."""
    from maestro.memory import QuestionResolution

    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I capable of?"]
    agent.consciousness.save_self_model(model)

    agent.memory.append_question_resolution(QuestionResolution(
        resolution_id="r-other", iteration=1,
        timestamp="2026-04-26T00:00:00+00:00",
        question="Some other question?",
        task_id="t-other", outcome="success",
        disposition="removed", finding="answer",
    ))

    recent = [
        _make_task_record(
            task_id="t-fresh",
            linked_question="What am I capable of?",
            key_findings="answer",
        ),
    ]
    pairs = agent._build_self_directed_pairs(recent)
    assert len(pairs) == 1
    assert pairs[0].task_id == "t-fresh"


def test_build_self_directed_pairs_filters_only_verdicted_in_mixed_window(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """In a window mixing verdicted and fresh tasks, only the fresh task
    surfaces. Verdicted tasks are filtered regardless of their
    disposition (removed / refined / kept)."""
    from maestro.memory import QuestionResolution

    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["Q-old1?", "Q-old2?", "Q-new?"]
    agent.consciousness.save_self_model(model)

    agent.memory.append_question_resolution(QuestionResolution(
        resolution_id="r-old1", iteration=1,
        timestamp="2026-04-26T00:00:00+00:00",
        question="Q-old1?",
        task_id="t-old1", outcome="success",
        disposition="removed", finding="answer",
    ))
    agent.memory.append_question_resolution(QuestionResolution(
        resolution_id="r-old2", iteration=2,
        timestamp="2026-04-26T00:00:01+00:00",
        question="Q-old2?",
        task_id="t-old2", outcome="success",
        disposition="kept", finding="answer",
    ))

    recent = [
        _make_task_record(task_id="t-old1", linked_question="Q-old1?",
                          key_findings="f1",
                          timestamp="2026-04-27T00:00:00+00:00"),
        _make_task_record(task_id="t-old2", linked_question="Q-old2?",
                          key_findings="f2",
                          timestamp="2026-04-27T00:00:01+00:00"),
        _make_task_record(task_id="t-new", linked_question="Q-new?",
                          key_findings="f3",
                          timestamp="2026-04-27T00:00:02+00:00"),
    ]
    pairs = agent._build_self_directed_pairs(recent)
    assert [p.task_id for p in pairs] == ["t-new"]


@pytest.mark.asyncio
async def test_contemplate_does_not_re_surface_verdicted_pairs(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """End-to-end: contemplate twice over the same TaskRecord. The first
    call writes one QuestionResolution row; the second call writes none
    because the task_id is already in the recent resolution log.

    This is the production pattern this fix addresses: a successful
    self-directed task surfaces, the LLM closes its question, and the
    TaskRecord stays inside the recent window for several more
    iterations — without this filter, every subsequent contemplation
    would emit a zombie ``unresolved`` row for the same task.
    """
    from maestro.consciousness import SelfModel

    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I capable of?"]
    agent.consciousness.save_self_model(model)
    agent.memory.record_task(_make_task_record(
        task_id="t-once", linked_question="What am I capable of?",
        key_findings="I can refactor multi-file changes.",
    ))

    # First contemplate: LLM closes the question. One row is written.
    first_returned = SelfModel(open_questions=[])
    first_update = AsyncMock(return_value=first_returned)
    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch.object(agent.consciousness, "update_self_model", first_update):
        await agent.contemplate(iteration=1)

    rows_after_first = agent.memory.get_recent_question_resolutions(50)
    assert len(rows_after_first) == 1
    assert rows_after_first[0].task_id == "t-once"
    assert rows_after_first[0].disposition == "removed"

    # Second contemplate over the same recent window: the task is still
    # there, but its task_id is now in the resolution log, so no new
    # pair is built and no new row is written. update_self_model still
    # runs (the prompt just has no recent_self_directed entry).
    second_returned = SelfModel(open_questions=[])
    second_update = AsyncMock(return_value=second_returned)
    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection"), \
         patch.object(agent.consciousness, "update_self_model", second_update):
        await agent.contemplate(iteration=2)

    rows_after_second = agent.memory.get_recent_question_resolutions(50)
    assert len(rows_after_second) == 1
    second_pairs = second_update.await_args.kwargs["recent_self_directed"]
    assert second_pairs == []


@pytest.mark.asyncio
async def test_contemplate_renders_refinement_count_in_prompt(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """End-to-end: a refined-twice question carries ``(refined 2x before)``
    into the prompt, while a fresh question does not."""
    from maestro.consciousness import SelfModel
    from maestro.memory import QuestionResolution

    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = [
        "What truly am I capable of?",
        "Which revenue stream pays first?",
    ]
    agent.consciousness.save_self_model(model)

    # Lineage rows for the first question only.
    agent.memory.append_question_resolution(QuestionResolution(
        resolution_id="r1", iteration=1,
        timestamp="2026-04-26T00:00:00+00:00",
        question="What am I capable of?",
        task_id="t-pred-1", outcome="success",
        disposition="refined", finding="some",
        refined_to="What am I capable of really?",
    ))
    agent.memory.append_question_resolution(QuestionResolution(
        resolution_id="r2", iteration=2,
        timestamp="2026-04-26T00:00:01+00:00",
        question="What am I capable of really?",
        task_id="t-pred-2", outcome="success",
        disposition="refined", finding="some",
        refined_to="What truly am I capable of?",
    ))
    agent.memory.record_task(_make_task_record(
        task_id="t-live-a",
        linked_question="What truly am I capable of?",
        key_findings="answer for capability",
    ))
    agent.memory.record_task(_make_task_record(
        task_id="t-live-b",
        linked_question="Which revenue stream pays first?",
        key_findings="answer for purpose",
    ))

    # Capture the prompt via a real Consciousness call against a mocked SDK.
    captured: dict[str, list[tuple[str, str, int]]] = {}

    async def fake_update(*_args, **kwargs):  # type: ignore[no-untyped-def]
        captured["pairs"] = kwargs["recent_self_directed"]
        return SelfModel(open_questions=list(model.open_questions))

    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection text"), \
         patch.object(agent.consciousness, "update_self_model",
                      side_effect=fake_update):
        await agent.contemplate()

    by_q = {q: (f, n) for q, f, n in captured["pairs"]}
    assert by_q["What truly am I capable of?"][1] == 2
    assert by_q["Which revenue stream pays first?"][1] == 0


@pytest.mark.asyncio
async def test_contemplate_passes_pairs_to_update_self_model(make_agent) -> None:  # type: ignore[no-untyped-def]
    """contemplate() builds the pair list from recent tasks and threads it
    into update_self_model as recent_self_directed."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I capable of?"]
    agent.consciousness.save_self_model(model)
    agent.memory.record_task(_make_task_record(
        task_id="t1", linked_question="What am I capable of?",
        key_findings="I can refactor multi-file changes.",
    ))

    returned_model = SelfModel(open_questions=["What am I capable of?"])
    update_mock = AsyncMock(return_value=returned_model)
    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="reflection text"), \
         patch.object(agent.consciousness, "update_self_model", update_mock):
        await agent.contemplate()

    update_mock.assert_awaited_once()
    pairs = update_mock.await_args.kwargs["recent_self_directed"]
    # update_self_model receives ``(question, finding, refinement_count)``
    # triples derived from the QuestionPair list. With no prior REFINE
    # rows in the resolution log, ``refinement_count`` is zero.
    assert pairs == [
        ("What am I capable of?", "I can refactor multi-file changes.", 0),
    ]
