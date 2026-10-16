"""Tests for MaestroAgent.generate_task() queue maintenance and the Edit-path
content gates — split out of tests/test_agent.py (#3875). Fully mocked I/O."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.agent import (
    MaestroAgent,
)
from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine, QualityGateResult
from maestro.executor import Executor
from maestro.journal import Journal
from maestro.loop import assembly as assembly_module
from maestro.memory import Memory
from maestro.notifications.base import NullNotifier
from maestro.planner import Planner
from maestro.remediation_tracker import (
    TrackerState,
)
from tests.conftest import MockQueryResult

FAKE_DIR = Path("/fake/agent_test")

_OK_REVIEW = QualityGateResult(
    passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True,
    raw_output="Ruff: clean\n\nMypy: clean\n\nTests: clean",
)


def _build_agent(
    mem_fs: dict[str, str], config: Config | None = None,
) -> MaestroAgent:
    if config is None:
        config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    config.ensure_dirs()
    if isinstance(assembly_module.StateStore, MagicMock):
        # ``@patch("maestro.loop.assembly.StateStore")`` hands every SQLite adapter a
        # MagicMock. All but one are lazy, but the remediation tracker loads its
        # state eagerly at construction and unpacks it (proposal 106), so the
        # mocked store must hand back a real, empty ``TrackerState`` rather than
        # a MagicMock that iterates empty.
        mocked = assembly_module.StateStore.return_value
        mocked.load_remediation_tracker.return_value = TrackerState(
            {}, {}, {}, {}, 0,
        )
    memory = Memory(FAKE_DIR)
    consciousness = Consciousness(FAKE_DIR)
    journal = Journal(FAKE_DIR)
    notifier = NullNotifier()
    executor = Executor()
    planner = Planner()
    evolution = EvolutionEngine(memory, consciousness, journal, notifier,
                                config=config)
    vcs = MagicMock()
    vcs.has_changes.return_value = False

    return MaestroAgent(
        config=config, consciousness=consciousness, journal=journal,
        memory=memory, executor=executor, planner=planner,
        evolution=evolution, notifier=notifier, vcs=vcs,
    )


def _activation_tuples(
    mem_fs: dict[str, str], agent: MaestroAgent,
) -> list[tuple[str, str, str]]:
    """Read (gate, action, detail) tuples from gate_activations.jsonl."""
    raw = mem_fs.get(
        str(agent.config.memory_dir / "gate_activations.jsonl"), "",
    )
    return [
        (rec["gate"], rec["action"], rec["detail"])
        for rec in (json.loads(line) for line in raw.splitlines() if line.strip())
    ]

# --- generate_task() tests (queue-maintenance contract) ---


def _enqueue_payload(
    goal: str,
    linked_question: str | None = None,
    rationale: str | None = None,
) -> str:
    """Build a JSON response for the prune/edit/enqueue schema."""
    entry: dict[str, str | None] = {"goal": goal}
    if linked_question is not None:
        entry["linked_question"] = linked_question
    if rationale is not None:
        entry["rationale"] = rationale
    return json.dumps({"prune": [], "edit": [], "enqueue": [entry]})


@pytest.mark.asyncio
async def test_generate_task_enqueues_new_entry(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A valid enqueue entry appends to the on-disk queue."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I capable of?"]
    agent.consciousness.save_self_model(model)
    task_text = "Write a brief summary of my open questions."
    mock_result = MockQueryResult(response=_enqueue_payload(
        task_text, linked_question="What am I capable of?",
    ))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == task_text
    assert queue[0].linked_question == "What am I capable of?"


@pytest.mark.asyncio
async def test_generate_task_passes_json_complete(make_agent) -> None:  # type: ignore[no-untyped-def]
    """generate_task wires json_complete through both paths."""
    from maestro.sdk import json_complete

    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    mock_result = MockQueryResult(response=_enqueue_payload("a task"))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)) as mock_q:
        await agent.generate_task()
    assert mock_q.call_args.kwargs.get("complete_on") is json_complete

    with patch(
        "maestro.loop.task_generation.query_on_session", AsyncMock(return_value=mock_result),
    ) as mock_qos:
        await agent.generate_task(session=MagicMock())
    assert mock_qos.call_args.kwargs.get("complete_on") is json_complete


@pytest.mark.asyncio
async def test_generate_task_runs_cluster_sweep_when_enabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The pending-plan cluster sweep runs (before the SDK pop) when enabled."""
    agent = make_agent()
    agent.config.loop_pending_cluster_sweep_enabled = True
    order: list[str] = []

    def _record_sweep(**kwargs: Any) -> list[str]:
        order.append("sweep")
        return []

    sweep = MagicMock(side_effect=_record_sweep)
    agent.memory.sweep_clustered_pending_plans = sweep  # type: ignore[method-assign]
    mock_result = MockQueryResult(response=_enqueue_payload("a task"))

    async def _record_query(*args: Any, **kwargs: Any) -> MockQueryResult:
        order.append("query")
        return mock_result

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_record_query)):
        await agent.generate_task()

    sweep.assert_called_once()
    # Reuses the cluster thresholds — no new knobs.
    assert sweep.call_args.kwargs["threshold"] == agent.config.loop_pending_cluster_threshold
    assert sweep.call_args.kwargs["min_cluster_size"] == agent.config.loop_pending_cluster_max
    assert sweep.call_args.kwargs["lever_attempts"] is agent.lever_attempts
    # The sweep fires before the SDK pop.
    assert order[0] == "sweep"
    assert "query" in order


@pytest.mark.asyncio
async def test_generate_task_skips_cluster_sweep_when_disabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The sweep is not invoked when the flag is false."""
    agent = make_agent()
    agent.config.loop_pending_cluster_sweep_enabled = False
    sweep = MagicMock(return_value=[])
    agent.memory.sweep_clustered_pending_plans = sweep  # type: ignore[method-assign]
    mock_result = MockQueryResult(response=_enqueue_payload("a task"))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    sweep.assert_not_called()


@pytest.mark.asyncio
async def test_generate_task_runs_restatement_sweep_when_enabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The pairwise restatement sweep runs when the flag is enabled."""
    agent = make_agent()
    agent.config.loop_restatement_sweep_enabled = True
    sweep = MagicMock(return_value=[])
    agent.memory.sweep_restatement_pending_plans = sweep  # type: ignore[method-assign]
    mock_result = MockQueryResult(response=_enqueue_payload("a task"))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    sweep.assert_called_once()
    # Reuses the duplicate-guard Jaccard cutoff — no new knob.
    assert (
        sweep.call_args.kwargs["threshold"]
        == agent.config.loop_duplicate_pending_plan_threshold
    )


@pytest.mark.asyncio
async def test_generate_task_skips_restatement_sweep_when_disabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The restatement sweep is not invoked when the flag is false."""
    agent = make_agent()
    agent.config.loop_restatement_sweep_enabled = False
    sweep = MagicMock(return_value=[])
    agent.memory.sweep_restatement_pending_plans = sweep  # type: ignore[method-assign]
    mock_result = MockQueryResult(response=_enqueue_payload("a task"))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    sweep.assert_not_called()


@pytest.mark.asyncio
async def test_generate_task_restatement_sweep_journals_obsoletions(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A non-empty restatement sweep result is journaled with the retired ids."""
    agent = make_agent()
    agent.config.loop_restatement_sweep_enabled = True
    agent.memory.sweep_restatement_pending_plans = MagicMock(  # type: ignore[method-assign]
        return_value=["dead1"],
    )
    journal_spy = MagicMock(wraps=agent.journal.record_event)
    agent.journal.record_event = journal_spy  # type: ignore[method-assign]
    mock_result = MockQueryResult(response=_enqueue_payload("a task"))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    journaled = "".join(str(c.args) + str(c.kwargs) for c in journal_spy.call_args_list)
    assert "1 restated plan(s)" in journaled
    assert "dead1" in journaled


@pytest.mark.asyncio
async def test_generate_task_runs_expiry_sweep_when_enabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The age-based expiry sweep runs when the horizon is positive."""
    agent = make_agent()
    agent.config.loop_plan_review_expiry_days = 21
    sweep = MagicMock(return_value=[])
    agent.memory.sweep_expired_pending_plans = sweep  # type: ignore[method-assign]
    mock_result = MockQueryResult(response=_enqueue_payload("a task"))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    sweep.assert_called_once()
    # Passes the configured horizon and the shared lever ledger.
    assert sweep.call_args.kwargs["max_age_days"] == agent.config.loop_plan_review_expiry_days
    assert sweep.call_args.kwargs["lever_attempts"] is agent.lever_attempts


@pytest.mark.asyncio
async def test_generate_task_skips_expiry_sweep_when_disabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The expiry sweep is not invoked when the horizon is <= 0."""
    agent = make_agent()
    agent.config.loop_plan_review_expiry_days = 0
    sweep = MagicMock(return_value=[])
    agent.memory.sweep_expired_pending_plans = sweep  # type: ignore[method-assign]
    mock_result = MockQueryResult(response=_enqueue_payload("a task"))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    sweep.assert_not_called()


@pytest.mark.asyncio
async def test_generate_task_expiry_sweep_journals_obsoletions(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A non-empty expiry sweep result is journaled with the retired ids."""
    agent = make_agent()
    agent.config.loop_plan_review_expiry_days = 21
    agent.memory.sweep_expired_pending_plans = MagicMock(  # type: ignore[method-assign]
        return_value=["stale1"],
    )
    journal_spy = MagicMock(wraps=agent.journal.record_event)
    agent.journal.record_event = journal_spy  # type: ignore[method-assign]
    mock_result = MockQueryResult(response=_enqueue_payload("a task"))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    journaled = "".join(str(c.args) + str(c.kwargs) for c in journal_spy.call_args_list)
    assert "1 stale plan(s)" in journaled
    assert "stale1" in journaled


@pytest.mark.asyncio
async def test_generate_task_cluster_sweep_journals_obsoletions(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A non-empty sweep result is journaled with the obsoleted count."""
    agent = make_agent()
    agent.config.loop_pending_cluster_sweep_enabled = True
    sweep = MagicMock(return_value=["dead1", "dead2"])
    agent.memory.sweep_clustered_pending_plans = sweep  # type: ignore[method-assign]
    journal_spy = MagicMock(wraps=agent.journal.record_event)
    agent.journal.record_event = journal_spy  # type: ignore[method-assign]
    mock_result = MockQueryResult(response=_enqueue_payload("a task"))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    journaled = "".join(str(c.args) + str(c.kwargs) for c in journal_spy.call_args_list)
    assert "2 redundant plan(s)" in journaled
    assert "dead1" in journaled


@pytest.mark.asyncio
async def test_generate_task_empty_enqueue_leaves_queue_empty(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A no-op response leaves the queue unchanged."""
    agent = make_agent()
    mock_result = MockQueryResult(response=json.dumps(
        {"prune": [], "edit": [], "enqueue": []},
    ))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    assert agent.task_queue.load() == []


@pytest.mark.asyncio
async def test_generate_task_skips_enqueue_with_null_goal(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An enqueue entry whose goal is null is silently dropped."""
    agent = make_agent()
    mock_result = MockQueryResult(response=json.dumps(
        {"prune": [], "edit": [], "enqueue": [{"goal": None}]},
    ))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    assert agent.task_queue.load() == []


@pytest.mark.asyncio
async def test_generate_task_leaves_queue_unchanged_on_malformed_json(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A non-JSON response leaves the queue untouched (no exceptions)."""
    agent = make_agent()
    mock_result = MockQueryResult(response="not json at all -- a stray sentence")

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    assert agent.task_queue.load() == []


@pytest.mark.asyncio
async def test_generate_task_leaves_queue_unchanged_on_empty_response(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    mock_result = MockQueryResult(response="   ")

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    assert agent.task_queue.load() == []


@pytest.mark.asyncio
async def test_generate_task_propagates_sdk_exception(make_agent) -> None:  # type: ignore[no-untyped-def]
    """generate_task() propagates SDK errors (phase-level suppression removed)."""
    agent = make_agent()

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=RuntimeError("SDK error"))), \
         pytest.raises(RuntimeError, match="SDK error"):
        await agent.generate_task()


@pytest.mark.asyncio
async def test_generate_task_mismatched_question_drops_link(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A linked_question not in open_questions falls back to None on the enqueued entry."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I capable of?"]
    agent.consciousness.save_self_model(model)
    mock_result = MockQueryResult(response=_enqueue_payload(
        "do the work", linked_question="completely fabricated",
    ))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "do the work"
    assert queue[0].linked_question is None


@pytest.mark.asyncio
async def test_generate_task_fuzzy_matches_linked_question(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Case + whitespace differences still resolve to the live verbatim question."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = ["What am I?"]
    agent.consciousness.save_self_model(model)
    mock_result = MockQueryResult(response=_enqueue_payload(
        "Fix identity handling in maestro/consciousness.py",
        linked_question="  WHAT  AM   I?  ",
    ))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].linked_question == "What am I?"


@pytest.mark.asyncio
async def test_generate_task_empty_open_questions_accepts_null_linked(make_agent) -> None:  # type: ignore[no-untyped-def]
    """With no open questions, null linked_question is accepted and the entry still enqueues."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.open_questions = []
    agent.consciousness.save_self_model(model)
    mock_result = MockQueryResult(response=_enqueue_payload("probe purpose"))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "probe purpose"
    assert queue[0].linked_question is None


@pytest.mark.asyncio
async def test_generate_task_uses_self_model_context(make_agent) -> None:  # type: ignore[no-untyped-def]
    """generate_task() prompt includes the self-model's open questions and no purpose line."""
    agent = make_agent()

    model = agent.consciousness.load_self_model()
    model.open_questions = ["Can I be surprised?", "What does action mean for me?"]
    agent.consciousness.save_self_model(model)

    captured_prompt: list[str] = []

    async def capturing_query(**kwargs):  # type: ignore[no-untyped-def]
        captured_prompt.append(kwargs.get("prompt", ""))
        return MockQueryResult(response=_enqueue_payload(
            "Investigate whether I can be surprised by external data.",
            linked_question="Can I be surprised?",
        ))

    with patch("maestro.loop.task_generation.query", side_effect=capturing_query):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "Can I be surprised?" in captured_prompt[0]
    assert "purpose hypothesis" not in captured_prompt[0]


@pytest.mark.asyncio
async def test_generate_task_injects_directive_into_prompt(make_agent) -> None:  # type: ignore[no-untyped-def]
    """generate_task() injects next_task_directive from self-model into the prompt."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "Focus on a creative visualization task"
    model.open_questions = ["What am I?"]
    agent.consciousness.save_self_model(model)

    captured_prompt: list[str] = []

    async def capturing_query(**kwargs):  # type: ignore[no-untyped-def]
        captured_prompt.append(kwargs.get("prompt", ""))
        return MockQueryResult(response=_enqueue_payload(
            "draw something", linked_question="What am I?",
        ))

    with patch("maestro.loop.task_generation.query", side_effect=capturing_query):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "Focus on a creative visualization task" in captured_prompt[0]


@pytest.mark.asyncio
async def test_generate_task_clears_directive_after_parseable_response(make_agent) -> None:  # type: ignore[no-untyped-def]
    """next_task_directive clears once the SDK returns a parseable response."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "Try infrastructure work"
    model.open_questions = []
    agent.consciousness.save_self_model(model)

    mock_result = MockQueryResult(response=_enqueue_payload("refactor config"))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive is None


@pytest.mark.asyncio
async def test_generate_task_no_directive_shows_none_placeholder(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When next_task_directive is None, '(none)' appears in the prompt."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    model.open_questions = ["What am I?"]
    agent.consciousness.save_self_model(model)

    captured_prompt: list[str] = []

    async def capturing_query(**kwargs):  # type: ignore[no-untyped-def]
        captured_prompt.append(kwargs.get("prompt", ""))
        return MockQueryResult(response=_enqueue_payload(
            "Add a probe to maestro/purpose.py", linked_question="What am I?",
        ))

    with patch("maestro.loop.task_generation.query", side_effect=capturing_query):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "(none)" in captured_prompt[0]
    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive is None


@pytest.mark.asyncio
async def test_generate_task_preserves_directive_on_sdk_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Directive survives SDK failure so resume-in-place retry can use it."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "This should survive"
    model.open_questions = []
    agent.consciousness.save_self_model(model)

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=RuntimeError("boom"))), \
         pytest.raises(RuntimeError):
        await agent.generate_task()

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive == "This should survive"


@pytest.mark.asyncio
async def test_generate_task_clears_directive_even_when_gate_drops_new_entry(make_agent) -> None:  # type: ignore[no-untyped-def]
    """next_task_directive clears even when a post-call gate drops every new entry.

    Queue-curation contract: the directive steers one *curation pass*, not
    one *accepted entry*. If the LLM returns a parseable enqueue payload and
    every proposed entry is then dropped by a post-call gate (here: the
    PR-mutation goal filter), the directive is still consumed — the next
    iteration fires fresh against current state rather than replaying stale
    steering. The happy-path test
    ``test_generate_task_clears_directive_after_parseable_response``
    only exercises the *accepted* case, so this guards against a regression
    that accidentally gates the clear on ``accepted_new``. (Directive
    survival on SDK failure is covered separately by
    ``test_generate_task_preserves_directive_on_sdk_failure``.)
    """
    agent = make_agent()

    model = agent.consciousness.load_self_model()
    model.next_task_directive = "Focus on reliability improvements"
    model.open_questions = []
    agent.consciousness.save_self_model(model)

    # The only proposed entry is a PR-opening goal, which the post-call
    # PR-mutation filter drops before it enters the queue.
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [], "edit": [],
        "enqueue": [{"goal": "Open a pull request to merge the reliability fix"}],
    }))

    with (
        patch("maestro.task_gates.task_cost_gate.check_task_cost_risk",
              return_value=None),
        patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)),
    ):
        await agent.generate_task()

    # The PR-mutation filter dropped the only new entry; queue stays empty.
    assert agent.task_queue.load() == []

    # The SDK call returned a parseable response, so the directive is cleared
    # as the one-shot contract requires — independent of whether the post-call
    # gate accepted the entry.
    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive is None


@pytest.mark.asyncio
async def test_generate_task_does_not_drain_pending_user_requests(make_agent) -> None:  # type: ignore[no-untyped-def]
    """generate_task() must NOT touch pending_user_requests — that's _run_task_phase's job."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["Add a test for feature X"]
    model.open_questions = ["What am I?"]
    agent.consciousness.save_self_model(model)

    mock_result = MockQueryResult(response=_enqueue_payload(
        "self-directed work", linked_question="What am I?",
    ))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.pending_user_requests == ["Add a test for feature X"]


@pytest.mark.asyncio
async def test_generate_task_tolerates_non_list_response_fields(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Non-list prune/edit/enqueue fields are coerced to empty lists."""
    agent = make_agent()
    mock_result = MockQueryResult(response=json.dumps(
        {"prune": "not a list", "edit": 42, "enqueue": {"not": "a list"}},
    ))
    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()
    assert agent.task_queue.load() == []


@pytest.mark.asyncio
async def test_generate_task_skips_malformed_edit_entries(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Edit entries that are not dicts or lack a task_id are silently skipped."""
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    # Reach the edit-curation loop: the quiet-skip gate would otherwise
    # short-circuit generate_task on a non-empty queue with no external work.
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([QueuedTask(task_id="a", goal="original")])
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [
            "not a dict",
            {"task_id": "", "goal": "blank id"},
            {"goal": "no task_id at all"},
        ],
        "enqueue": [],
    }))
    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()
    queue = agent.task_queue.load()
    assert [q.goal for q in queue] == ["original"]


@pytest.mark.asyncio
async def test_generate_task_edit_clears_rationale_to_none(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An edit with rationale=null clears the stored rationale."""
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([
        QueuedTask(task_id="a", goal="g", rationale="prior reason"),
    ])
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [{"task_id": "a", "rationale": None}],
        "enqueue": [],
    }))
    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()
    queue = agent.task_queue.load()
    assert queue[0].rationale is None


@pytest.mark.asyncio
async def test_generate_task_edit_absent_rationale_keeps_stored_value(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A refused goal-only edit leaves rationale and touch stamp alone (#4394)."""
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([
        QueuedTask(
            task_id="a", goal="g", rationale="prior reason",
            last_touched_at="2026-01-01T00:00:00+00:00",
        ),
    ])
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [{"task_id": "a", "goal": "merge PR #123 into main"}],
        "enqueue": [],
    }))
    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()
    queue = agent.task_queue.load()
    assert queue[0].goal == "g"
    assert queue[0].rationale == "prior reason"
    assert queue[0].last_touched_at == "2026-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_generate_task_edit_accepted_goal_without_rationale_key_keeps_rationale(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """An accepted goal rewrite with no rationale key keeps the rationale."""
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([
        QueuedTask(task_id="a", goal="g", rationale="prior reason"),
    ])
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [{
            "task_id": "a",
            "goal": "Add a unit test for maestro/fileutil.py atomic_write",
        }],
        "enqueue": [],
    }))
    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()
    queue = agent.task_queue.load()
    assert queue[0].goal == "Add a unit test for maestro/fileutil.py atomic_write"
    assert queue[0].rationale == "prior reason"


@pytest.mark.asyncio
async def test_generate_task_edit_updates_rationale(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An edit with a new rationale string overwrites the stored value."""
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([
        QueuedTask(task_id="a", goal="g", rationale="old"),
    ])
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [{"task_id": "a", "rationale": "new sharper reason"}],
        "enqueue": [],
    }))
    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()
    queue = agent.task_queue.load()
    assert queue[0].rationale == "new sharper reason"


@pytest.mark.asyncio
async def test_generate_task_does_not_resurrect_a_task_popped_mid_call(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A head popped during the SDK call stays popped (#3561).

    Curation loads its snapshot before the priced call and persists after it.
    The consumer pops through the same backend meanwhile, so the persistence
    step must rebase onto the re-read live queue rather than replace it with
    the stale snapshot.
    """
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([
        QueuedTask(task_id="a", goal="already dispatched"),
        QueuedTask(task_id="b", goal="still queued"),
    ])
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [{"task_id": "b", "goal": "still queued, sharpened"}],
        "enqueue": [],
    }))

    async def _pop_during_the_call(*args: object, **kwargs: object) -> object:
        agent.task_queue.pop_head()
        return mock_result

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_pop_during_the_call)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert [q.task_id for q in queue] == ["b"]
    assert queue[0].goal == "still queued, sharpened"


@pytest.mark.asyncio
async def test_generate_task_preserves_a_task_enqueued_mid_call(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A task another writer appended during the SDK call survives curation."""
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([QueuedTask(task_id="a", goal="snapshot entry")])
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [], "edit": [],
        "enqueue": [{"goal": "the curated one"}],
    }))

    async def _enqueue_during_the_call(*args: object, **kwargs: object) -> object:
        live = agent.task_queue.load()
        agent.task_queue.save([
            *live, QueuedTask(task_id="z", goal="arrived concurrently"),
        ])
        return mock_result

    with patch(
        "maestro.loop.task_generation.query", AsyncMock(side_effect=_enqueue_during_the_call),
    ):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert [q.goal for q in queue] == [
        "snapshot entry", "arrived concurrently", "the curated one",
    ]


@pytest.mark.asyncio
async def test_generate_task_skips_non_dict_enqueue_entries(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Enqueue entries that are not dicts are silently skipped."""
    agent = make_agent()
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [], "edit": [],
        "enqueue": ["not a dict", 42, {"goal": "the real one"}],
    }))
    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()
    queue = agent.task_queue.load()
    assert [q.goal for q in queue] == ["the real one"]


@pytest.mark.asyncio
async def test_generate_task_enqueue_records_rationale(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A non-empty rationale on an enqueue entry is persisted on the queued task."""
    agent = make_agent()
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [], "edit": [],
        "enqueue": [{"goal": "the goal", "rationale": "the reason"}],
    }))
    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()
    queue = agent.task_queue.load()
    assert queue[0].rationale == "the reason"


@pytest.mark.asyncio
async def test_generate_task_prune_removes_named_entries(make_agent) -> None:  # type: ignore[no-untyped-def]
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([
        QueuedTask(task_id="a", goal="keep"),
        QueuedTask(task_id="b", goal="drop"),
        QueuedTask(task_id="c", goal="keep too"),
    ])
    mock_result = MockQueryResult(response=json.dumps(
        {"prune": ["b"], "edit": [], "enqueue": []},
    ))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    remaining = agent.task_queue.load()
    assert [q.task_id for q in remaining] == ["a", "c"]


@pytest.mark.asyncio
async def test_generate_task_edit_updates_goal(make_agent) -> None:  # type: ignore[no-untyped-def]
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([QueuedTask(task_id="a", goal="old goal")])
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [{"task_id": "a", "goal": "new sharper goal"}],
        "enqueue": [],
    }))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    remaining = agent.task_queue.load()
    assert remaining[0].goal == "new sharper goal"



# --- Edit-path goal/criteria coupling (issue #3963) ---
# A goal rewrite on a task carrying binding criteria must bring valid
# replacement criteria, or the old contract would grade the new work.

_CSV_GOAL = "Add a CSV export in maestro/stats.py"
_CSV_CRITERIA = ["CSV export contains the header task_id,outcome"]
_JSON_GOAL = "Add a JSON export in maestro/stats.py"
_JSON_CRITERIA = ["tests/test_stats.py asserts the JSON export has a task_id key"]


async def _run_edit(agent, edit: dict) -> None:  # type: ignore[no-untyped-def]
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [], "edit": [edit], "enqueue": [],
    }))
    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()


def _seed_csv_task(agent, **kwargs) -> None:  # type: ignore[no-untyped-def]
    from maestro.task_queue import QueuedTask
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([QueuedTask(
        task_id="a", goal=_CSV_GOAL,
        acceptance_criteria=list(_CSV_CRITERIA), **kwargs,
    )])


@pytest.mark.asyncio
async def test_generate_task_edit_goal_replaces_acceptance_criteria(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    _seed_csv_task(agent)
    await _run_edit(agent, {
        "task_id": "a", "goal": _JSON_GOAL, "acceptance_criteria": _JSON_CRITERIA,
    })
    head = agent.task_queue.load()[0]
    assert head.goal == _JSON_GOAL
    assert head.acceptance_criteria == _JSON_CRITERIA


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {},
    {"acceptance_criteria": []},
    {"acceptance_criteria": ["", "   ", 7]},
    {"acceptance_criteria": "not a list"},
])
async def test_generate_task_edit_goal_without_replacement_criteria_is_refused(  # type: ignore[no-untyped-def]
    make_agent, extra,
) -> None:
    agent = make_agent()
    _seed_csv_task(agent)
    await _run_edit(agent, {"task_id": "a", "goal": _JSON_GOAL, **extra})
    head = agent.task_queue.load()[0]
    assert head.goal == _CSV_GOAL
    assert head.acceptance_criteria == _CSV_CRITERIA


@pytest.mark.asyncio
async def test_generate_task_edit_goal_with_vacuous_criteria_is_refused(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Replacement criteria the vacuousness filter drops count as none."""
    agent = make_agent()
    _seed_csv_task(agent)
    agent.evolution.validate_success_criteria = lambda criteria: []
    await _run_edit(agent, {
        "task_id": "a", "goal": _JSON_GOAL, "acceptance_criteria": _JSON_CRITERIA,
    })
    head = agent.task_queue.load()[0]
    assert head.goal == _CSV_GOAL
    assert head.acceptance_criteria == _CSV_CRITERIA


@pytest.mark.asyncio
async def test_generate_task_edit_goal_refusal_still_applies_rationale(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    _seed_csv_task(agent, rationale="old reason")
    await _run_edit(agent, {
        "task_id": "a", "goal": _JSON_GOAL, "rationale": "new reason",
    })
    head = agent.task_queue.load()[0]
    assert head.goal == _CSV_GOAL
    assert head.acceptance_criteria == _CSV_CRITERIA
    assert head.rationale == "new reason"


@pytest.mark.asyncio
async def test_generate_task_edit_rejected_goal_keeps_criteria(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    _seed_csv_task(agent)
    await _run_edit(agent, {
        "task_id": "a", "goal": "merge PR #123 into main",
        "acceptance_criteria": _JSON_CRITERIA,
    })
    head = agent.task_queue.load()[0]
    assert head.goal == _CSV_GOAL
    assert head.acceptance_criteria == _CSV_CRITERIA


@pytest.mark.asyncio
async def test_generate_task_edit_rationale_only_preserves_criteria(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    _seed_csv_task(agent, rationale="old reason")
    await _run_edit(agent, {
        "task_id": "a", "goal": _CSV_GOAL, "rationale": "new reason",
        "acceptance_criteria": _JSON_CRITERIA,
    })
    head = agent.task_queue.load()[0]
    assert head.goal == _CSV_GOAL
    assert head.rationale == "new reason"
    assert head.acceptance_criteria == _CSV_CRITERIA


@pytest.mark.asyncio
async def test_generate_task_edit_goal_on_criteria_less_task(make_agent) -> None:  # type: ignore[no-untyped-def]
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([QueuedTask(task_id="a", goal=_CSV_GOAL)])
    await _run_edit(agent, {"task_id": "a", "goal": _JSON_GOAL})
    head = agent.task_queue.load()[0]
    assert head.goal == _JSON_GOAL
    assert head.acceptance_criteria is None


@pytest.mark.asyncio
async def test_generate_task_edit_goal_on_criteria_less_task_adopts_criteria(make_agent) -> None:  # type: ignore[no-untyped-def]
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([QueuedTask(task_id="a", goal=_CSV_GOAL)])
    await _run_edit(agent, {
        "task_id": "a", "goal": _JSON_GOAL, "acceptance_criteria": _JSON_CRITERIA,
    })
    head = agent.task_queue.load()[0]
    assert head.goal == _JSON_GOAL
    assert head.acceptance_criteria == _JSON_CRITERIA


def test_queue_snapshot_line_marks_criteria_count() -> None:
    from maestro.loop.task_generation import _format_queue_snapshot_line
    from maestro.task_queue import QueuedTask
    marked = _format_queue_snapshot_line(QueuedTask(
        task_id="a", goal="g", acceptance_criteria=["x", "y"],
    ))
    bare = _format_queue_snapshot_line(QueuedTask(task_id="a", goal="g"))
    assert marked.endswith(" | criteria: 2")
    assert "criteria" not in bare

# --- Edit-path content gating (issue #2939) ---
# A rewrite replaces an already-vetted queue entry and nothing re-vets a
# queue head at pop time, so the edit path runs the same content gates the
# enqueue path does. A rejected rewrite leaves the goal untouched and leaves
# a ``rejected`` gate-activation row behind as the trace.

#: The directive the enqueue pass re-arms on a concreteness drop; the edit
#: pass must never write it (mirrors the literal in ``task_generation.py``).
_CONCRETENESS_STEERING_DIRECTIVE = (
    "Next task must target specific files with a binary done condition"
)


@pytest.mark.asyncio
async def test_generate_task_edit_rejects_pr_mutation_goal(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """An edit rewriting a goal into a PR merge is refused, goal unchanged."""
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([QueuedTask(task_id="a", goal="old goal")])
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [{"task_id": "a", "goal": "merge PR #123 into main"}],
        "enqueue": [],
    }))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    remaining = agent.task_queue.load()
    assert remaining[0].goal == "old goal"
    assert ("pr_mutation_goal", "rejected", "merge PR #123 into main") in (
        _activation_tuples(mem_fs, agent)
    )


@pytest.mark.asyncio
async def test_generate_task_edit_rejects_nonconcrete_goal(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """An edit rewriting a goal into pure analysis is refused, goal unchanged."""
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([QueuedTask(task_id="a", goal="old goal")])
    analysis_goal = "analyze the recent loop behaviour patterns"
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [{"task_id": "a", "goal": analysis_goal}],
        "enqueue": [],
    }))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    remaining = agent.task_queue.load()
    assert remaining[0].goal == "old goal"
    assert ("concreteness", "rejected", analysis_goal) in (
        _activation_tuples(mem_fs, agent)
    )


@pytest.mark.asyncio
async def test_generate_task_edit_rejection_still_applies_rationale(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A refused goal rewrite does not block the entry's rationale edit."""
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([
        QueuedTask(task_id="a", goal="old goal", rationale="old reason"),
    ])
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [{
            "task_id": "a",
            "goal": "merge PR #123 into main",
            "rationale": "new reason",
        }],
        "enqueue": [],
    }))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    remaining = agent.task_queue.load()
    assert remaining[0].goal == "old goal"
    assert remaining[0].rationale == "new reason"


@pytest.mark.asyncio
async def test_generate_task_edit_rejection_does_not_steer_directive(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Concreteness steering stays an enqueue-pass contract, not an edit one."""
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([QueuedTask(task_id="a", goal="old goal")])
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [
            {"task_id": "a", "goal": "analyze the recent loop behaviour patterns"},
        ],
        "enqueue": [],
    }))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    # The enqueue pass re-arms the steering directive on a concreteness drop;
    # the edit pass must not. Assert on the steering text rather than on an
    # empty slot, so an unrelated directive bridge filling it later cannot
    # fail this test for the wrong reason.
    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive != _CONCRETENESS_STEERING_DIRECTIVE


@pytest.mark.asyncio
async def test_generate_task_edit_accepts_vetted_goal(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """A gate-clean rewrite still lands — the gating does not over-reject."""
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([QueuedTask(task_id="a", goal="old goal")])
    vetted_goal = "fix the queue cap check in maestro/loop/task_generation.py"
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [{"task_id": "a", "goal": vetted_goal}],
        "enqueue": [],
    }))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    remaining = agent.task_queue.load()
    assert remaining[0].goal == vetted_goal
    rejected = [
        tup for tup in _activation_tuples(mem_fs, agent) if tup[1] == "rejected"
    ]
    assert rejected == []


@pytest.mark.asyncio
async def test_generate_task_edit_echoed_goal_is_not_revetted(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """An edit echoing the queued goal verbatim writes no rejected row.

    The curation schema lists ``goal`` beside ``rationale``, so a
    rationale-only edit typically repeats the existing goal. Vetting that
    no-op would stamp a fresh ``rejected`` row every pass for a queued goal
    whose terrain has since drifted, diluting the tail-windowed sensors.
    """
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    # A queued goal that would fail the concreteness gate today — the
    # already-admitted entry whose content has drifted out of the gates.
    drifted_goal = "analyze the recent loop behaviour patterns"
    agent.task_queue.save([
        QueuedTask(task_id="a", goal=drifted_goal, rationale="old reason"),
    ])
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [
            {"task_id": "a", "goal": drifted_goal, "rationale": "new reason"},
        ],
        "enqueue": [],
    }))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    remaining = agent.task_queue.load()
    assert remaining[0].goal == drifted_goal
    assert remaining[0].rationale == "new reason"
    rejected = [
        tup for tup in _activation_tuples(mem_fs, agent) if tup[1] == "rejected"
    ]
    assert rejected == []


@pytest.mark.asyncio
async def test_generate_task_enqueue_pr_mutation_logs_activation(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """The enqueue PR-mutation drop now leaves a gate-activation trace too."""
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [],
        "enqueue": [{"goal": "open a PR for the task branch"}],
    }))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    assert agent.task_queue.load() == []
    assert ("pr_mutation_goal", "rejected", "open a PR for the task branch") in (
        _activation_tuples(mem_fs, agent)
    )


@pytest.mark.asyncio
async def test_generate_task_enqueue_respects_queue_cap(make_agent) -> None:  # type: ignore[no-untyped-def]
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.config.task_queue_max = 3
    agent.task_queue.save([
        QueuedTask(task_id="a", goal="g1"),
        QueuedTask(task_id="b", goal="g2"),
    ])
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [],
        "enqueue": [
            {"goal": "new one"},
            {"goal": "new two"},
            {"goal": "new three — dropped at cap"},
        ],
    }))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    after = agent.task_queue.load()
    assert [q.goal for q in after] == ["g1", "g2", "new one"]


@pytest.mark.asyncio
async def test_generate_task_enqueue_skips_duplicates_of_existing(make_agent) -> None:  # type: ignore[no-untyped-def]
    from maestro.task_queue import QueuedTask
    agent = make_agent()
    agent.task_queue.save([QueuedTask(task_id="a", goal="already there")])
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [],
        "edit": [],
        "enqueue": [{"goal": "Already There"}],
    }))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    after = agent.task_queue.load()
    assert len(after) == 1
    assert after[0].task_id == "a"




# --- Directive consumption applies to the live self-model (#4040) ---


def _concurrent_self_model_edit(agent, directive: str | None) -> None:  # type: ignore[no-untyped-def]
    def _edit(live) -> None:  # type: ignore[no-untyped-def]
        live.pending_user_requests.append("operator-request")
        if directive is not None:
            live.next_task_directive = directive

    agent.consciousness.mutate_self_model(_edit)


async def _generate_with_concurrent_edit(  # type: ignore[no-untyped-def]
    agent, response: dict[str, object], directive: str | None,
) -> None:
    mock_result = MockQueryResult(response=json.dumps(response))

    async def _edit_during_the_call(*args: object, **kwargs: object) -> object:
        _concurrent_self_model_edit(agent, directive)
        return mock_result

    with patch(
        "maestro.loop.task_generation.query", AsyncMock(side_effect=_edit_during_the_call),
    ):
        await agent.generate_task()


@pytest.mark.asyncio
async def test_generate_task_keeps_a_directive_replaced_mid_call(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A directive another writer set during the call is not the consumed one."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "the consumed directive"
    agent.consciousness.save_self_model(model)

    await _generate_with_concurrent_edit(
        agent, {"prune": [], "edit": [], "enqueue": []}, "a fresh directive",
    )

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive == "a fresh directive"
    assert reloaded.pending_user_requests == ["operator-request"]


@pytest.mark.asyncio
async def test_generate_task_clears_consumed_directive_keeping_live_fields(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The consumed directive clears; an unrelated concurrent field survives."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "the consumed directive"
    agent.consciousness.save_self_model(model)

    await _generate_with_concurrent_edit(
        agent, {"prune": [], "edit": [], "enqueue": []}, None,
    )

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive is None
    assert reloaded.pending_user_requests == ["operator-request"]


@pytest.mark.asyncio
async def test_generate_task_concreteness_rearm_lands_on_live_model(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A concreteness re-arm is written to the live model, not the snapshot."""
    agent = make_agent()
    agent.config.quiet_skip_enabled = False

    await _generate_with_concurrent_edit(
        agent,
        {
            "prune": [], "edit": [],
            "enqueue": [{"goal": "analyze the recent loop behaviour patterns"}],
        },
        None,
    )

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive == _CONCRETENESS_STEERING_DIRECTIVE
    assert reloaded.pending_user_requests == ["operator-request"]
