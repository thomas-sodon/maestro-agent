"""Component tests for task generation."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.audit_checks import GATE_ACTIVATIONS_FILENAME, SELF_DIRECTED_WINDOW
from maestro.loop import task_generation as task_generation_module
from maestro.loop.task_generation import (
    _QUEUE_SNAPSHOT_LINKED_QUESTION_CAP,
    _QUEUE_SNAPSHOT_MAX_ITEMS,
    _QUEUE_SNAPSHOT_TASK_ID_CAP,
    _QUEUE_SNAPSHOT_TOTAL_CAP,
    _format_queue_snapshot,
)
from maestro.sdk import QueryResult
from maestro.task_queue import QueuedTask
from maestro.wip_primacy import TendOwnedPR
from tests.component.conftest import write_self_model, write_task_record
from tests.component.sdk_mock import SDKMock

# ---- generate_task ----


def _enqueue_payload(
    goal: str,
    linked_question: str | None = None,
) -> str:
    entry: dict[str, str | None] = {"goal": goal}
    if linked_question is not None:
        entry["linked_question"] = linked_question
    return json.dumps({"prune": [], "edit": [], "enqueue": [entry]})


@pytest.mark.asyncio
async def test_generate_task_enqueues_task(make_agent, memory_dir):
    write_self_model(memory_dir,
                     open_questions=["What am I capable of?"])
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(_enqueue_payload(
        "Fix error handling patterns in maestro/agent.py",
        linked_question="What am I capable of?",
    ))
    with sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()
    sdk.assert_all_consumed()
    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert "error handling" in queue[0].goal.lower()
    assert queue[0].linked_question == "What am I capable of?"


@pytest.mark.asyncio
async def test_generate_task_no_op_leaves_queue_empty(make_agent, memory_dir):
    write_self_model(memory_dir)
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(json.dumps({"prune": [], "edit": [], "enqueue": []}))
    with sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()
    sdk.assert_all_consumed()
    assert agent.task_queue.load() == []


@pytest.mark.asyncio
async def test_generate_task_propagates_sdk_failure(make_agent, memory_dir):
    write_self_model(memory_dir)
    failing_mock = AsyncMock(side_effect=RuntimeError("SDK unavailable"))
    agent, _code_host, _vcs = make_agent()
    with patch("maestro.loop.task_generation.query", failing_mock), \
         pytest.raises(RuntimeError, match="SDK unavailable"):
        await agent.generate_task()


@pytest.mark.asyncio
async def test_generate_task_assembles_no_prompt_while_owned_wip_open(
    make_agent, memory_dir,
):
    """No curation prompt is assembled while an owned-clearable PR is open.

    Pins the proposal-092 suppression invariant at the boundary. With
    ``wip_primacy_enabled`` on and ``_current_task_focus`` reporting a
    ``TendOwnedPR`` (an open own ``maestro/evolve/*`` PR with a self-clearable
    ``CONFLICTING`` blocker), the head-of-chain ``wip_primacy`` block gate
    short-circuits ``generate_task`` before any prompt is assembled — so **zero**
    SDK queries fire. This test fails if the ``wip_primacy`` gate is ever
    demoted from ``block``/head-of-chain.
    """
    write_self_model(memory_dir,
                     open_questions=["How can I be useful?"])
    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload("should never be reached"))

    agent, _code_host, _vcs = make_agent()
    agent.config.wip_primacy_enabled = True
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=TendOwnedPR(pr_number=7, owned_blocker="CONFLICTING"),
    )
    with patch("maestro.loop.task_generation.query", side_effect=_capture_query):
        await agent.generate_task()

    # The block gate short-circuited before prompt assembly: no SDK query fired.
    assert captured_kwargs == []
    # The queue stays empty — nothing was enqueued.
    assert agent.task_queue.load() == []
    # The gate handed the loop the bounded finish-owned-work directive instead.
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive is not None
    assert "Finish PR #7" in model.next_task_directive


@pytest.mark.asyncio
async def test_generate_task_prompt_names_no_stage(make_agent, memory_dir):
    """Proposal 149: the curator prompt never names the stage."""
    write_self_model(memory_dir)
    captured: list[str] = []

    async def _capture_query(**kwargs):
        captured.append(kwargs["prompt"])
        return QueryResult(response=json.dumps({"prune": [], "edit": [], "enqueue": []}))

    agent, _code_host, _vcs = make_agent()
    with patch("maestro.loop.task_generation.query", side_effect=_capture_query):
        await agent.generate_task()
    assert len(captured) == 1
    assert "self_reflection" not in captured[0]


@pytest.mark.asyncio
async def test_generate_task_with_recent_task_history(make_agent, memory_dir):
    write_self_model(memory_dir,
                     open_questions=["What patterns do I see?"])
    write_task_record(memory_dir, task_id="t001", goal="Fix the parser bug",
                      outcome="success")
    write_task_record(memory_dir, task_id="t002", goal="Refactor logging module",
                      outcome="partial")
    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload(
            "Add tests for coverage gaps in tests/test_agent.py",
            linked_question="What patterns do I see?",
        ))

    agent, _code_host, _vcs = make_agent()
    with patch("maestro.loop.task_generation.query", side_effect=_capture_query):
        await agent.generate_task()
    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert len(captured_kwargs) == 1
    prompt = captured_kwargs[0]["prompt"]
    assert "Fix the parser bug" in prompt
    assert "Refactor logging module" in prompt


@pytest.mark.asyncio
async def test_generate_task_self_directed_gate_uses_full_window(
    make_agent, memory_dir,
):
    """The self-directed gate fetches SELF_DIRECTED_WINDOW tasks (#2353).

    The shared ``recent`` fetch stays bound to 10 for the issue-number
    dedup walk, while the self-directed success-rate gate must request its
    own declared window (15) so the constant is not dead at the call site.
    """
    write_self_model(memory_dir)
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(json.dumps({"prune": [], "edit": [], "enqueue": []}))
    with patch.object(
        agent.memory,
        "get_recent_tasks",
        wraps=agent.memory.get_recent_tasks,
    ) as spy, sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()

    fetch_sizes = [
        (c.args[0] if c.args else c.kwargs.get("n"))
        for c in spy.call_args_list
    ]
    # Shared dedup fetch is still bound to 10 — widening did not leak in.
    assert 10 in fetch_sizes
    # Self-directed gate fetches its full declared window (15), not 10.
    assert SELF_DIRECTED_WINDOW in fetch_sizes


@pytest.mark.asyncio
async def test_generate_task_includes_diversity_guidance(make_agent, memory_dir):
    """Prompt includes DIVERSITY GUIDANCE block; no syntactic distribution slot."""
    write_self_model(memory_dir,
                     open_questions=["What patterns do I see?"])
    write_task_record(memory_dir, task_id="t001",
                      goal="Measure test coverage gaps", outcome="success")
    write_task_record(memory_dir, task_id="t002",
                      goal="Analyze error patterns in logs", outcome="success")
    write_task_record(memory_dir, task_id="t003",
                      goal="Detect flaky tests in CI", outcome="success")
    write_task_record(memory_dir, task_id="t004",
                      goal="Fix the parser bug", outcome="success")
    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload(
            "Design a visual status page",
            linked_question="What patterns do I see?",
        ))

    agent, _code_host, _vcs = make_agent()
    with patch("maestro.loop.task_generation.query", side_effect=_capture_query):
        await agent.generate_task()
    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert len(captured_kwargs) == 1
    prompt = captured_kwargs[0]["prompt"]
    assert "DIVERSITY GUIDANCE" in prompt
    assert "{task_type_distribution}" not in prompt
    assert "Recent task type distribution" not in prompt


@pytest.mark.asyncio
async def test_generate_task_fuzzy_matches_question(make_agent, memory_dir):
    """Case + whitespace differences resolve to the live verbatim question."""
    write_self_model(memory_dir,
                     open_questions=["What am I?"])
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(_enqueue_payload(
        "Fix identity handling in maestro/consciousness.py",
        linked_question="  WHAT  AM   I?  ",
    ))
    with sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()
    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "Fix identity handling in maestro/consciousness.py"
    assert queue[0].linked_question == "What am I?"


@pytest.mark.asyncio
async def test_generate_task_mismatched_question_keeps_task(make_agent, memory_dir):
    """A fabricated linked_question falls back to None; the entry still enqueues."""
    write_self_model(memory_dir,
                     open_questions=["What am I capable of?"])
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(_enqueue_payload(
        "do the work", linked_question="completely fabricated thing",
    ))
    with sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()
    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "do the work"
    assert queue[0].linked_question is None


@pytest.mark.asyncio
async def test_generate_task_malformed_json_leaves_queue_empty(
    make_agent, memory_dir,
):
    """Non-JSON response leaves the queue untouched."""
    write_self_model(memory_dir)
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then("just a stray sentence with no JSON anywhere")
    with sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()
    assert agent.task_queue.load() == []


@pytest.mark.asyncio
async def test_generate_task_non_dict_json_leaves_queue_empty(
    make_agent, memory_dir,
):
    """Valid JSON that isn't a dict (e.g., a list) leaves the queue empty."""
    write_self_model(memory_dir)
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(json.dumps(["not", "a", "dict"]))
    with sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()
    assert agent.task_queue.load() == []


@pytest.mark.asyncio
async def test_generate_task_empty_open_questions_accepts_null_question(
    make_agent, memory_dir,
):
    """With no open questions, an entry without a linked_question still enqueues."""
    write_self_model(memory_dir, open_questions=[])
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(_enqueue_payload("probe purpose"))
    with sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()
    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "probe purpose"
    assert queue[0].linked_question is None


# ---- PR-mutation goal filter ----


@pytest.mark.asyncio
async def test_generate_task_drops_pr_opening_goal(make_agent, memory_dir):
    """A goal whose objective is to open/merge a PR is dropped post-call.

    Belt-and-suspenders to the structural tool-gating guard (issue #2280):
    promotion must route through ``source_task_id`` approval, not a PR.
    """
    write_self_model(memory_dir,
                     open_questions=["How do I integrate branches?"])
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(_enqueue_payload(
        "Promote branch maestro/task/3ce652 into main via PR",
        linked_question="How do I integrate branches?",
    ))
    with sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()
    sdk.assert_all_consumed()
    assert agent.task_queue.load() == []


@pytest.mark.asyncio
async def test_generate_task_keeps_source_task_id_promotion(
    make_agent, memory_dir,
):
    """A benign promotion goal that routes through source_task_id survives."""
    write_self_model(memory_dir,
                     open_questions=["How do I integrate branches?"])
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    goal = (
        "Promote branch maestro/task/3ce652 by nominating it as the "
        "source_task_id of a follow-up evolution plan"
    )
    sdk.then(_enqueue_payload(
        goal, linked_question="How do I integrate branches?",
    ))
    with sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()
    sdk.assert_all_consumed()
    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == goal


# ---- loop task generation ----


@pytest.mark.asyncio
async def test_run_loop_generates_task_at_interval(make_agent, memory_dir):
    write_self_model(memory_dir,
                     open_questions=["What can I build?"])
    contemplate_mock = AsyncMock(side_effect=[
        QueryResult(response="Contemplation reflection"),
        QueryResult(response=json.dumps({"identity_statements": ["Growing"]})),
    ])
    task_gen_mock = AsyncMock(return_value=QueryResult(
        response=_enqueue_payload(
            "Create a simple test utility",
            linked_question="What can I build?",
        ),
    ))
    plan_and_exec_mock = AsyncMock(side_effect=[
        QueryResult(response=json.dumps([{"description": "Write the utility"}])),
        QueryResult(response="Utility created"),
    ])

    agent, _code_host, _vcs = make_agent()
    with patch("maestro.consciousness.query", contemplate_mock), \
         patch("maestro.loop.task_generation.query", task_gen_mock), \
         patch("maestro.agent.query", task_gen_mock), \
         patch("maestro.planner.query", plan_and_exec_mock), \
         patch("maestro.executor.query", plan_and_exec_mock), \
         patch.object(agent, "_install_signal_handlers"):
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10, task_every=1)
    lines = (memory_dir / "task_history.jsonl").read_text().strip().splitlines()
    self_directed = [json.loads(line) for line in lines
                     if json.loads(line).get("source") == "self_directed"]
    assert self_directed, "Expected a self_directed task to be recorded"
    assert self_directed[0]["linked_question"] == "What can I build?"


@pytest.mark.asyncio
async def test_run_loop_skips_task_generation_when_disabled(make_agent, memory_dir):
    write_self_model(memory_dir)
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(
        "Contemplation reflection",
        json.dumps({"identity_statements": ["I exist"]}),
    )
    task_gen_spy = AsyncMock(return_value=QueryResult(
        response="Should not be called"))
    with sdk.patch("maestro.consciousness"), \
         patch("maestro.loop.task_generation.query", task_gen_spy), \
         patch("maestro.agent.query", task_gen_spy), \
         patch.object(agent, "_install_signal_handlers"), \
         patch.object(agent, "_probe_rate_limit", new_callable=AsyncMock):
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10, task_every=0)
    task_gen_spy.assert_not_called()
    if (memory_dir / "task_history.jsonl").exists():
        lines = (memory_dir / "task_history.jsonl").read_text().strip().splitlines()
        for line in lines:
            record = json.loads(line)
            assert record.get("source") != "self_directed"


@pytest.mark.asyncio
async def test_run_loop_generate_task_none_is_harmless(make_agent, memory_dir):
    write_self_model(memory_dir)
    contemplate_mock = AsyncMock(side_effect=[
        QueryResult(response="Contemplation reflection"),
        QueryResult(response=json.dumps({"identity_statements": ["Growing"]})),
    ])
    task_gen_mock = AsyncMock(return_value=QueryResult(
        response=json.dumps({"prune": [], "edit": [], "enqueue": []}),
    ))

    agent, _code_host, _vcs = make_agent()
    with patch("maestro.consciousness.query", contemplate_mock), \
         patch("maestro.loop.task_generation.query", task_gen_mock), \
         patch("maestro.agent.query", task_gen_mock), \
         patch.object(agent, "_install_signal_handlers"):
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10, task_every=1)
    if (memory_dir / "task_history.jsonl").exists():
        lines = (memory_dir / "task_history.jsonl").read_text().strip().splitlines()
        for line in lines:
            record = json.loads(line)
            assert record.get("source") != "self_directed"


# ---------------------------------------------------------------------------
# Promotable-branch hint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_task_includes_promotable_branches(
    make_agent, memory_dir,
):
    """When get_promotable_branches returns branches, prompt includes them."""
    write_self_model(memory_dir,
                     open_questions=["What should I do?"])
    agent, _code_host, _vcs = make_agent()

    branches = [
        {"task_id": "t_abc", "branch": "maestro/task/t_abc",
         "goal": "Investigate X"},
        {"task_id": "t_def", "branch": "maestro/task/t_def",
         "goal": "Build Y"},
    ]
    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload(
            "Promote branch t_abc",
            linked_question="What should I do?",
        ))

    with patch("maestro.loop.task_generation.query", side_effect=_capture_query), \
         patch.object(
             agent.memory, "get_promotable_branches",
             return_value=branches,
         ):
        await agent.generate_task()
    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert len(captured_kwargs) == 1
    prompt = captured_kwargs[0]["prompt"]
    assert "maestro/task/t_abc" in prompt
    assert "maestro/task/t_def" in prompt
    assert "Promotable branches" in prompt


@pytest.mark.asyncio
async def test_generate_task_no_promotable_section_when_empty(
    make_agent, memory_dir,
):
    """When get_promotable_branches returns empty, no promotable section."""
    write_self_model(memory_dir,
                     open_questions=["What should I do?"])
    agent, _code_host, _vcs = make_agent()

    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload(
            "Do something else",
            linked_question="What should I do?",
        ))

    with patch("maestro.loop.task_generation.query", side_effect=_capture_query), \
         patch.object(
             agent.memory, "get_promotable_branches",
             return_value=[],
         ):
        await agent.generate_task()
    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert len(captured_kwargs) == 1
    prompt = captured_kwargs[0]["prompt"]
    assert "Promotable branches" not in prompt


@pytest.mark.asyncio
async def test_generate_task_no_promotable_when_disabled(
    make_agent, memory_dir,
):
    """When config disabled, no promotable section even with branches."""
    write_self_model(memory_dir,
                     open_questions=["What should I do?"])
    agent, _code_host, _vcs = make_agent()
    agent.config.promotable_branch_hint_enabled = False

    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload(
            "Do something else",
            linked_question="What should I do?",
        ))

    mock_promotable = MagicMock(return_value=[
        {"task_id": "t_x", "branch": "b/x", "goal": "some goal"},
    ])
    with patch("maestro.loop.task_generation.query", side_effect=_capture_query), \
         patch.object(
             agent.memory, "get_promotable_branches",
             mock_promotable,
         ):
        await agent.generate_task()
    queue = agent.task_queue.load()
    assert len(queue) == 1
    mock_promotable.assert_not_called()
    prompt = captured_kwargs[0]["prompt"]
    assert "Promotable branches" not in prompt


@pytest.mark.asyncio
async def test_generate_task_withholds_all_promotion_hints_when_pressure_off(
    make_agent, memory_dir,
):
    """Proposal 116, step 6: with the branch-promotion-pressure master switch off,
    none of the three promotion hints reach the prompt — the promotable-backlog
    and integration-backlog formatters are never called, and the promotable-branch
    reader is never consulted, even though all would otherwise fire."""
    write_self_model(memory_dir,
                     open_questions=["What should I do?"])
    agent, _code_host, _vcs = make_agent()
    agent.config.branch_promotion_pressure_enabled = False

    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload(
            "Do something else",
            linked_question="What should I do?",
        ))

    mock_promotable = MagicMock(return_value=[
        {"task_id": "t_x", "branch": "b/x", "goal": "some goal"},
    ])
    with patch("maestro.loop.task_generation.query", side_effect=_capture_query), \
         patch.object(
             agent.memory, "get_promotable_branches", mock_promotable,
         ), \
         patch(
             "maestro.loop.task_generation.format_promotable_summary",
             return_value="PROMOTABLE BACKLOG: sentinel",
         ) as fps, \
         patch(
             "maestro.loop.task_generation.format_integration_backlog",
             return_value="## Integration Opportunity sentinel",
         ) as fib:
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    fps.assert_not_called()
    fib.assert_not_called()
    mock_promotable.assert_not_called()
    prompt = captured_kwargs[0]["prompt"]
    assert "PROMOTABLE BACKLOG" not in prompt
    assert "Integration Opportunity" not in prompt
    assert "Promotable branches" not in prompt


# ---------------------------------------------------------------------------
# Literal braces in post-.format() prompt sections survive un-doubled (#2092)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_task_promotable_braces_not_doubled(
    make_agent, memory_dir,
):
    """Promotable-branch goals with literal braces reach the prompt undoubled.

    The promotable-branch block is appended after the single
    TASK_GENERATION_PROMPT.format() pass, so escaping braces there would
    leave them doubled in the prompt actually sent to the SDK (#2092).
    """
    write_self_model(memory_dir,
                     open_questions=["What should I do?"])
    agent, _code_host, _vcs = make_agent()

    branches = [
        {"task_id": "t_abc", "branch": "maestro/task/t_abc",
         "goal": "Fix {placeholder} handling"},
    ]
    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload(
            "Promote branch t_abc",
            linked_question="What should I do?",
        ))

    with patch("maestro.loop.task_generation.query", side_effect=_capture_query), \
         patch.object(
             agent.memory, "get_promotable_branches",
             return_value=branches,
         ):
        await agent.generate_task()
    assert len(captured_kwargs) == 1
    prompt = captured_kwargs[0]["prompt"]
    assert "Fix {placeholder} handling" in prompt
    assert "{{placeholder}}" not in prompt


@pytest.mark.asyncio
async def test_generate_task_prior_ctx_braces_not_doubled(
    make_agent, memory_dir,
):
    """Prior-task key_findings with literal braces reach the prompt undoubled.

    The prior-task context block is appended after the single
    TASK_GENERATION_PROMPT.format() pass; escaping its braces would leave
    them doubled in the prompt actually sent (#2092).
    """
    write_self_model(memory_dir,
                     open_questions=["What should I do?"])
    write_task_record(
        memory_dir, task_id="t-prior", goal="Earlier work",
        outcome="success",
        key_findings="Resolved the {token} parsing edge case",
    )
    agent, _code_host, _vcs = make_agent()
    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload(
            "Build on prior work",
            linked_question="What should I do?",
        ))

    with patch("maestro.loop.task_generation.query", side_effect=_capture_query):
        await agent.generate_task()
    assert len(captured_kwargs) == 1
    prompt = captured_kwargs[0]["prompt"]
    assert "Resolved the {token} parsing edge case" in prompt
    assert "{{token}}" not in prompt


@pytest.mark.asyncio
async def test_generate_task_known_state_braces_not_doubled(
    make_agent, memory_dir,
):
    """Known-state recent goals with literal braces reach prompt undoubled.

    The known-state summary (which echoes recent task goals) is appended
    after the single TASK_GENERATION_PROMPT.format() pass; escaping its
    braces would leave them doubled in the prompt actually sent (#2092).
    """
    write_self_model(memory_dir,
                     open_questions=["What should I do?"])
    write_task_record(
        memory_dir, task_id="t-known", goal="Handle {brace} input",
        outcome="success",
    )
    agent, _code_host, _vcs = make_agent()
    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload(
            "Do something",
            linked_question="What should I do?",
        ))

    with patch("maestro.loop.task_generation.query", side_effect=_capture_query):
        await agent.generate_task()
    assert len(captured_kwargs) == 1
    prompt = captured_kwargs[0]["prompt"]
    assert "Handle {brace} input" in prompt
    assert "{{brace}}" not in prompt


@pytest.mark.asyncio
async def test_generate_task_open_question_braces_not_doubled(
    make_agent, memory_dir,
):
    """Open-question braces pass through .format() as single braces (#2092).

    open_questions is a value substituted into TASK_GENERATION_PROMPT.format();
    str.format never re-scans substituted values, so the value must NOT be
    pre-escaped — escaping doubled the braces in the prompt actually sent.
    """
    write_self_model(memory_dir,
                     open_questions=["What about {edge} cases?"])
    agent, _code_host, _vcs = make_agent()
    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload(
            "Investigate edge cases",
            linked_question="What about {edge} cases?",
        ))

    with patch("maestro.loop.task_generation.query", side_effect=_capture_query):
        await agent.generate_task()
    assert len(captured_kwargs) == 1
    prompt = captured_kwargs[0]["prompt"]
    assert "What about {edge} cases?" in prompt
    assert "{{edge}}" not in prompt


@pytest.mark.asyncio
async def test_generate_task_task_history_braces_not_doubled(
    make_agent, memory_dir,
):
    """Recent-task goal/findings braces render undoubled in history (#2092).

    recent_tasks is a value substituted into TASK_GENERATION_PROMPT.format();
    pre-escaping it doubled braces in the prompt actually sent.
    """
    write_self_model(memory_dir,
                     open_questions=["What should I do?"])
    write_task_record(
        memory_dir, task_id="t-hist", goal="Parse {expr} blocks",
        outcome="success",
    )
    agent, _code_host, _vcs = make_agent()
    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload(
            "Continue parsing work",
            linked_question="What should I do?",
        ))

    with patch("maestro.loop.task_generation.query", side_effect=_capture_query):
        await agent.generate_task()
    assert len(captured_kwargs) == 1
    prompt = captured_kwargs[0]["prompt"]
    assert "Parse {expr} blocks" in prompt
    assert "{{expr}}" not in prompt


@pytest.mark.asyncio
async def test_generate_task_queue_snapshot_braces_not_doubled(
    make_agent, memory_dir,
):
    """Queued-task goal braces render undoubled in the queue snapshot (#2092).

    queue_snapshot is a value substituted into TASK_GENERATION_PROMPT.format();
    pre-escaping it doubled braces in the prompt actually sent.
    """
    write_self_model(memory_dir,
                     open_questions=["What should I do?"])
    agent, _code_host, _vcs = make_agent()
    # The quiet-skip gate would otherwise suppress the LLM call when the
    # queue is non-empty and no external requests are pending; this test
    # exercises the prompt-substitution path, so it must reach the call.
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save([
        QueuedTask(
            task_id="q-1", goal="Render {value} safely",
            created_at="2025-01-01T00:00:00+00:00",
        ),
    ])
    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload(
            "Do something",
            linked_question="What should I do?",
        ))

    with patch("maestro.loop.task_generation.query", side_effect=_capture_query):
        await agent.generate_task()
    assert len(captured_kwargs) == 1
    prompt = captured_kwargs[0]["prompt"]
    assert "Render {value} safely" in prompt
    assert "{{value}}" not in prompt


@pytest.mark.asyncio
async def test_generate_task_directive_braces_not_doubled(
    make_agent, memory_dir,
):
    """Directive braces render undoubled (#2092).

    The directive is a value substituted into TASK_GENERATION_PROMPT.format(); pre-
    escaping it doubled braces in the prompt actually sent.
    """
    write_self_model(
        memory_dir,
        open_questions=["What should I do?"],
        next_task_directive="Focus on {area} this cycle",
    )
    agent, _code_host, _vcs = make_agent()
    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload(
            "Pursue purpose",
            linked_question="What should I do?",
        ))

    with patch("maestro.loop.task_generation.query", side_effect=_capture_query):
        await agent.generate_task()
    assert len(captured_kwargs) == 1
    prompt = captured_kwargs[0]["prompt"]
    assert "Focus on {area} this cycle" in prompt
    assert "{{area}}" not in prompt


def _gate_actions(memory_dir, gate: str) -> list[str]:
    """Return the ``action`` of every gate-activation row for *gate*."""
    path = memory_dir / GATE_ACTIVATIONS_FILENAME
    if not path.exists():
        return []
    actions = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line:
            continue
        record = json.loads(line)
        if record["gate"] == gate:
            actions.append(record["action"])
    return actions


# ---- forced-read directive bridge wiring ----


async def _capture_generate_task_prompt(agent, response_payload):
    """Run ``generate_task`` once, returning the prompt sent to the SDK.

    ``generate_task`` clears ``next_task_directive`` after a parseable SDK
    response (it steers a single curation pass), so the directive cannot be
    read back off the persisted model. The prompt is the durable evidence that
    the bridge fired: the directive text it set is interpolated into it before
    the clear.
    """
    captured: list[dict] = []

    async def _capture_query(**kwargs):
        captured.append(kwargs)
        return QueryResult(response=response_payload)

    with patch("maestro.loop.task_generation.query", side_effect=_capture_query):
        await agent.generate_task()
    assert len(captured) == 1
    return captured[0]["prompt"]


@pytest.mark.asyncio
async def test_generate_task_external_framing_names_target_repo(
    make_agent, memory_dir,
):
    """Proposal 107: under ``external_delivery`` framing the curator prompt
    frames the queue as delivery against the target repo and drops self-language.
    """
    write_self_model(memory_dir, next_task_directive=None)
    agent, _code_host, _vcs = make_agent()
    agent.config = replace(
        agent.config,
        work_task_framing="external_delivery",
        work_github_repo="customer/app",
    )
    prompt = await _capture_generate_task_prompt(
        agent, json.dumps({"prune": [], "edit": [], "enqueue": []}),
    )
    assert "customer/app" in prompt
    assert "for yourself" not in prompt


@pytest.mark.asyncio
async def test_generate_task_self_framing_prompt_unchanged(
    make_agent, memory_dir,
):
    """The default ``self_evolution`` framing keeps the self-framed curator
    prompt: it names no external repo and retains the "for yourself" wording.
    """
    write_self_model(memory_dir, next_task_directive=None)
    agent, _code_host, _vcs = make_agent()
    prompt = await _capture_generate_task_prompt(
        agent, json.dumps({"prune": [], "edit": [], "enqueue": []}),
    )
    assert "for yourself" in prompt
    assert "customer/app" not in prompt


@pytest.mark.asyncio
async def test_generate_task_forced_read_bridge_sets_directive(
    make_agent, memory_dir,
):
    """The forced-read bridge fills an empty directive when it fires.

    With an empty task history no sibling directive gate fires, so the
    low-precedence forced-read bridge is reached. The bridge function is
    patched (its git-merge-truth oracle stubbed) so the wiring is asserted
    deterministically: a fired bridge interpolates its directive into the
    task-generation prompt and logs a ``forced_read`` gate activation.
    """
    write_self_model(memory_dir, next_task_directive=None)
    agent, _code_host, _vcs = make_agent()
    forced = "PR #2554 serviced 2x, unmerged. Read its real blocker — no new lap."
    with patch(
        "maestro.task_gates.check_forced_read_directive", return_value=forced,
    ):
        prompt = await _capture_generate_task_prompt(
            agent, json.dumps({"prune": [], "edit": [], "enqueue": []}),
        )

    assert forced in prompt
    assert _gate_actions(memory_dir, "forced_read") == ["directive_set"]


@pytest.mark.asyncio
async def test_generate_task_forced_read_bridge_no_fire_leaves_empty(
    make_agent, memory_dir,
):
    """When the bridge returns None no directive is set and none is logged."""
    write_self_model(memory_dir, next_task_directive=None)
    agent, _code_host, _vcs = make_agent()
    with patch(
        "maestro.task_gates.check_forced_read_directive", return_value=None,
    ):
        prompt = await _capture_generate_task_prompt(
            agent, json.dumps({"prune": [], "edit": [], "enqueue": []}),
        )

    # With no directive set, the prompt's directive slot reads "(none)".
    assert "(none)" in prompt
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive is None
    assert _gate_actions(memory_dir, "forced_read") == []


@pytest.mark.asyncio
async def test_generate_task_forced_read_bridge_never_overwrites(
    make_agent, memory_dir,
):
    """A pre-set directive is preserved — the bridge never overwrites it.

    The ``if not model.next_task_directive`` guard must short-circuit before
    the bridge is consulted, so the patched bridge (which would otherwise
    return a directive) is never allowed to clobber the existing one. The
    existing directive — not the bridge's — is the text carried into the
    prompt.
    """
    existing = "Already set by a higher-precedence gate"
    write_self_model(
        memory_dir, next_task_directive=existing,
    )
    agent, _code_host, _vcs = make_agent()
    with patch(
        "maestro.task_gates.check_forced_read_directive",
        return_value="DIFFERENT forced-read directive",
    ) as bridge:
        prompt = await _capture_generate_task_prompt(
            agent, json.dumps({"prune": [], "edit": [], "enqueue": []}),
        )

    assert existing in prompt
    assert "DIFFERENT forced-read directive" not in prompt
    bridge.assert_not_called()
    assert _gate_actions(memory_dir, "forced_read") == []


# ---- readmission→construction directive bridge wiring ----


@pytest.mark.asyncio
async def test_generate_task_readmission_bridge_sets_directive(
    make_agent, memory_dir,
):
    """The readmission bridge fills an empty directive when flag on + it fires.

    With an empty task history no sibling directive gate fires, so the
    lowest-precedence readmission→construction bridge is reached. The bridge
    function is patched so the wiring is asserted deterministically: a fired
    bridge interpolates its directive into the prompt and logs a
    ``readmission_construction`` gate activation.
    """
    write_self_model(memory_dir, next_task_directive=None)
    agent, _code_host, _vcs = make_agent()
    agent.config.readmission_construction_directive_enabled = True
    directive = "Readmission rate 0.80 — BUILD something that lands."
    with patch(
        "maestro.task_gates.check_readmission_construction_directive",
        return_value=directive,
    ):
        prompt = await _capture_generate_task_prompt(
            agent, json.dumps({"prune": [], "edit": [], "enqueue": []}),
        )

    assert directive in prompt
    assert _gate_actions(memory_dir, "readmission_construction") == [
        "directive_set",
    ]


@pytest.mark.asyncio
async def test_generate_task_readmission_bridge_inert_when_flag_false(
    make_agent, memory_dir,
):
    """With the flag default-off the bridge is never consulted nor logged."""
    write_self_model(memory_dir, next_task_directive=None)
    agent, _code_host, _vcs = make_agent()
    assert agent.config.readmission_construction_directive_enabled is False
    with patch(
        "maestro.task_gates.check_readmission_construction_directive",
        return_value="should never be used",
    ) as bridge:
        prompt = await _capture_generate_task_prompt(
            agent, json.dumps({"prune": [], "edit": [], "enqueue": []}),
        )

    bridge.assert_not_called()
    assert "should never be used" not in prompt
    assert _gate_actions(memory_dir, "readmission_construction") == []


@pytest.mark.asyncio
async def test_generate_task_readmission_bridge_never_overwrites(
    make_agent, memory_dir,
):
    """A pre-set directive is preserved — the bridge never overwrites it.

    Even with the flag on, the ``not model.next_task_directive`` guard must
    short-circuit before the patched bridge is consulted, so the existing
    directive (not the bridge's) is the text carried into the prompt.
    """
    existing = "Already set by a higher-precedence gate"
    write_self_model(
        memory_dir, next_task_directive=existing,
    )
    agent, _code_host, _vcs = make_agent()
    agent.config.readmission_construction_directive_enabled = True
    with patch(
        "maestro.task_gates.check_readmission_construction_directive",
        return_value="DIFFERENT readmission directive",
    ) as bridge:
        prompt = await _capture_generate_task_prompt(
            agent, json.dumps({"prune": [], "edit": [], "enqueue": []}),
        )

    assert existing in prompt
    assert "DIFFERENT readmission directive" not in prompt
    bridge.assert_not_called()
    assert _gate_actions(memory_dir, "readmission_construction") == []


def _seed_queue_snapshot_agent(make_agent, memory_dir, queue):
    """Seed a persisted *queue* and return (agent, prompt-capture list)."""
    write_self_model(memory_dir,
                     open_questions=["What should I do?"])
    agent, _code_host, _vcs = make_agent()
    # The quiet-skip gate would suppress the LLM call on a non-empty queue;
    # these tests exercise the prompt-substitution path.
    agent.config.quiet_skip_enabled = False
    agent.task_queue.save(queue)
    return agent


@pytest.mark.asyncio
async def test_generate_task_queue_snapshot_bounded_by_item_cap(
    make_agent, memory_dir,
):
    """The rendered snapshot stops at the item ceiling and names the rest.

    The persisted queue is seeded far above ``task_queue_max`` to prove the
    admission cap does not gate this rendering path (#3596).
    """
    seeded = [
        QueuedTask(
            task_id=f"q-{i}", goal=f"Goal number {i}",
            created_at="2025-01-01T00:00:00+00:00",
        )
        for i in range(_QUEUE_SNAPSHOT_MAX_ITEMS + 7)
    ]
    agent = _seed_queue_snapshot_agent(make_agent, memory_dir, seeded)
    assert len(seeded) > agent.config.task_queue_max
    prompt = await _capture_generate_task_prompt(
        agent, json.dumps({"prune": [], "edit": [], "enqueue": []}),
    )

    rendered = [
        line for line in prompt.splitlines() if line.startswith("- task_id=q-")
    ]
    assert len(rendered) == _QUEUE_SNAPSHOT_MAX_ITEMS
    assert "- … 7 further queued task(s) omitted for prompt length" in prompt
    # The bound is a rendering concern, never an eviction.
    assert len(agent.task_queue.load()) == len(seeded)


@pytest.mark.asyncio
async def test_generate_task_queue_snapshot_bounded_by_total_chars(
    make_agent, memory_dir,
):
    """The joined snapshot block stays within the total character budget."""
    seeded = [
        QueuedTask(
            task_id=f"q-{i}", goal="G" * 400,
            rationale="R" * 400,
            created_at="2025-01-01T00:00:00+00:00",
        )
        for i in range(_QUEUE_SNAPSHOT_MAX_ITEMS)
    ]
    agent = _seed_queue_snapshot_agent(make_agent, memory_dir, seeded)
    prompt = await _capture_generate_task_prompt(
        agent, json.dumps({"prune": [], "edit": [], "enqueue": []}),
    )

    rendered = [
        line for line in prompt.splitlines() if line.startswith("- task_id=q-")
    ]
    assert len(rendered) < _QUEUE_SNAPSHOT_MAX_ITEMS
    # The whole returned block — overflow marker included — honours the cap.
    assert len(_format_queue_snapshot(seeded)) <= _QUEUE_SNAPSHOT_TOTAL_CAP
    omitted = len(seeded) - len(rendered)
    assert f"- … {omitted} further queued task(s) omitted" in prompt


@pytest.mark.asyncio
async def test_generate_task_queue_snapshot_truncates_linked_question(
    make_agent, memory_dir,
):
    """An oversized ``linked_question`` is sliced at its own ceiling."""
    question = "Q" * (_QUEUE_SNAPSHOT_LINKED_QUESTION_CAP + 200)
    agent = _seed_queue_snapshot_agent(make_agent, memory_dir, [
        QueuedTask(
            task_id="q-1", goal="Bounded goal",
            linked_question=question,
            created_at="2025-01-01T00:00:00+00:00",
        ),
    ])
    prompt = await _capture_generate_task_prompt(
        agent, json.dumps({"prune": [], "edit": [], "enqueue": []}),
    )

    line = next(
        ln for ln in prompt.splitlines() if ln.startswith("- task_id=q-1")
    )
    assert "Q" * _QUEUE_SNAPSHOT_LINKED_QUESTION_CAP in line
    assert "Q" * (_QUEUE_SNAPSHOT_LINKED_QUESTION_CAP + 1) not in line


def test_format_queue_snapshot_empty_queue_renders_placeholder():
    """An empty queue renders the ``(empty)`` placeholder unchanged."""
    assert _format_queue_snapshot([]) == "(empty)"


def test_format_queue_snapshot_no_line_fits_renders_marker_only(monkeypatch):
    """A budget admitting no entry yields the overflow marker, not "(empty)".

    Rendering a populated queue as ``(empty)`` would tell the model the queue
    holds nothing at all; the marker keeps the count honest.
    """
    monkeypatch.setattr(
        task_generation_module, "_QUEUE_SNAPSHOT_TOTAL_CAP", 10,
    )
    queue = [
        QueuedTask(
            task_id=f"q-{i}", goal="a goal long enough to exceed the budget",
            created_at="2025-01-01T00:00:00+00:00",
        )
        for i in range(3)
    ]
    assert _format_queue_snapshot(queue) == (
        "- … 3 further queued task(s) omitted for prompt length"
    )


def test_format_queue_snapshot_truncates_task_id():
    """An oversized ``task_id`` is sliced at its own ceiling."""
    rendered = _format_queue_snapshot([
        QueuedTask(
            task_id="q" * (_QUEUE_SNAPSHOT_TASK_ID_CAP + 200), goal="g",
            created_at="2025-01-01T00:00:00+00:00",
        ),
    ])
    assert "q" * _QUEUE_SNAPSHOT_TASK_ID_CAP in rendered
    assert "q" * (_QUEUE_SNAPSHOT_TASK_ID_CAP + 1) not in rendered


def test_format_queue_snapshot_full_render_has_no_marker():
    """A queue that fits entirely renders every entry and no marker."""
    rendered = _format_queue_snapshot([
        QueuedTask(
            task_id=f"q-{i}", goal=f"Goal {i}",
            created_at="2025-01-01T00:00:00+00:00",
        )
        for i in range(3)
    ])
    assert len(rendered.splitlines()) == 3
    assert "omitted for prompt length" not in rendered


def test_format_queue_snapshot_missing_created_at_renders_unknown():
    """A queued task without ``created_at`` renders the '?' placeholder."""
    rendered = _format_queue_snapshot([
        QueuedTask(task_id="q-1", goal="g", created_at=""),
    ])
    assert "queued: ?" in rendered
