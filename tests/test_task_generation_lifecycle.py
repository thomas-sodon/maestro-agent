"""generate_task cadence, directive bridges, prior-task context and acceptance-
criteria extraction; split out of ``tests/test_agent_lifecycle.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from maestro.consciousness import PendingPRRequest
from maestro.memory import EvolutionPlan, TaskRecord
from tests.conftest import MockQueryResult

# ──────────────────────────────────────────────────────────────────────
# Experiment cadence tests (generate_task prompt injection)
# ──────────────────────────────────────────────────────────────────────


def _seed_tasks(agent, n: int) -> None:  # type: ignore[no-untyped-def]
    """Record *n* minimal task records so ``task_count()`` returns *n*."""
    for i in range(n):
        agent.memory.record_task(TaskRecord(
            task_id=f"t{i}", goal=f"goal-{i}", plan=[],
            outcome="success", result_summary="ok",
            errors=[], duration_secs=1.0,
            timestamp="2026-01-01T00:00:00Z",
        ))


@pytest.mark.asyncio
async def test_experiment_cadence_triggers_on_positive_multiple(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When task_count is a positive multiple of experiment_cadence,
    the prompt sent to the SDK must contain the experiment-mode block."""
    agent = make_agent()
    agent.config.experiment_cadence = 5
    _seed_tasks(agent, 5)  # 5 % 5 == 0 → triggers

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "an experiment"}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "an experiment"
    assert len(captured_prompt) == 1
    assert "EXPERIMENT MODE" in captured_prompt[0]


@pytest.mark.asyncio
async def test_experiment_cadence_does_not_trigger_on_non_multiple(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When task_count is NOT a multiple of experiment_cadence,
    the prompt must NOT contain the experiment-mode block."""
    agent = make_agent()
    agent.config.experiment_cadence = 5
    _seed_tasks(agent, 7)  # 7 % 5 == 2 → does not trigger

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "normal task"}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "normal task"
    assert len(captured_prompt) == 1
    assert "EXPERIMENT MODE" not in captured_prompt[0]


@pytest.mark.asyncio
async def test_generate_task_rejects_pure_analysis(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A pure-analysis enqueue entry is dropped by the concreteness gate,
    and next_task_directive is steered toward concrete-target tasks."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    async def _query(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{
                    "goal": (
                        "Analyze recent tasks and write synthesis to "
                        "artifacts/synthesis.md"
                    ),
                }],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_query)):
        await agent.generate_task()

    # The pure-analysis entry never reached the queue.
    assert agent.task_queue.load() == []
    # The directive steers the next pass toward concrete-target tasks.
    updated = agent.consciousness.load_self_model()
    assert updated.next_task_directive is not None
    assert "specific files" in updated.next_task_directive
    assert "binary done condition" in updated.next_task_directive


@pytest.mark.asyncio
async def test_generate_task_admits_concrete_task(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A concrete-target task passes the concreteness gate and enqueues."""
    agent = make_agent()

    async def _query(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{
                    "goal": "Add task_id field to IterationUsage in maestro/sdk.py",
                }],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_query)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "Add task_id field to IterationUsage in maestro/sdk.py"


@pytest.mark.asyncio
async def test_generate_task_investigation_observable_gate_steers(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """An investigation→new-artifact enqueue entry steers the next directive.

    The gate does not drop the entry (it is enqueued) — it fills the empty
    next_task_directive slot to reframe the *next* generation pass toward
    reading a named observable instead of creating a fresh artifact file.
    """
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    flee = (
        "Investigate how maestro/escalation_bridge.py selects a handoff and "
        "write a markdown document under artifacts/ describing the mechanism."
    )

    async def _query(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": flee}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_query)):
        await agent.generate_task()

    # The flee task is enqueued — the gate steers, it does not drop the entry.
    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == flee
    # The next-pass directive is reframed toward reading a named observable.
    updated = agent.consciousness.load_self_model()
    assert updated.next_task_directive is not None
    assert "named observable" in updated.next_task_directive
    assert "not a new artifact file" in updated.next_task_directive


@pytest.mark.asyncio
async def test_experiment_cadence_does_not_trigger_on_zero_tasks(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When task_count is 0, experiment mode must not trigger — even though
    0 % N == 0 in Python, the guard requires task_count > 0."""
    agent = make_agent()
    agent.config.experiment_cadence = 5
    # No tasks seeded — task_count() == 0

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "first task"}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "first task"
    assert len(captured_prompt) == 1
    assert "EXPERIMENT MODE" not in captured_prompt[0]


@pytest.mark.asyncio
async def test_experiment_cadence_prompt_contains_required_elements(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """The injected experiment-mode prompt must contain all three required
    elements: (a) falsifiable hypothesis, (b) named metrics and measurement
    procedure, (c) success defined as data collected and analyzed."""
    agent = make_agent()
    agent.config.experiment_cadence = 3
    _seed_tasks(agent, 6)  # 6 % 3 == 0 → triggers

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "experiment"}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    prompt = captured_prompt[0]
    # (a) falsifiable hypothesis
    assert "falsifiable hypothesis" in prompt
    # (b) named metrics and measurement procedure
    assert "named metrics" in prompt
    assert "measurement procedure" in prompt
    # (c) success defined as data collected and analyzed
    assert "data was collected and analyzed" in prompt
    assert "confirmation or disconfirmation are equally valid" in prompt


@pytest.mark.asyncio
async def test_experiment_cadence_resets_after_tiered_memory_archive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Cadence is measured against the hot task_history.jsonl count, so a
    tiered-memory archive that shrinks the hot file resets the effective
    cadence baseline (#1920). Seed a non-multiple hot count, archive the
    older records, and assert the trigger fires off the post-archive hot
    count — pinning the documented reset-after-archive behaviour."""
    agent = make_agent()
    agent.config.experiment_cadence = 5

    now = datetime.now(UTC)
    old_ts = (now - timedelta(days=365)).isoformat()
    recent_ts = now.isoformat()

    # 2 old records (will archive to cold) + 5 recent records (stay hot).
    for i in range(2):
        agent.memory.record_task(TaskRecord(
            task_id=f"old{i}", goal=f"old-{i}", plan=[],
            outcome="success", result_summary="ok",
            errors=[], duration_secs=1.0, timestamp=old_ts,
        ))
    for i in range(5):
        agent.memory.record_task(TaskRecord(
            task_id=f"new{i}", goal=f"new-{i}", plan=[],
            outcome="success", result_summary="ok",
            errors=[], duration_secs=1.0, timestamp=recent_ts,
        ))

    # Pre-archive: 7 hot records, 7 % 5 == 2 → cadence would NOT trigger.
    assert agent.memory.task_count() == 7

    # Archive moves the 2 old records to cold storage; hot count drops to 5.
    agent.memory.archive(threshold_days=30)
    assert agent.memory.task_count() == 5  # 5 % 5 == 0 → now triggers

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "an experiment"}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    # Trigger fired off the post-archive hot count (5), not the pre-archive
    # lifetime count (7) — the cadence baseline reset after the archive.
    assert "EXPERIMENT MODE" in captured_prompt[0]


@pytest.mark.asyncio
async def test_experiment_cadence_audit_stall_takes_precedence(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Audit-stall short-circuit returns (None, None) even when experiment
    cadence would trigger — pre-SDK gates fire first."""
    from maestro.audit_checks import AuditFinding
    from maestro.memory import AuditResult

    agent = make_agent()
    agent.config.experiment_cadence = 5
    agent.config.audit_stall_block_enabled = True
    agent.config.audit_stall_threshold = 1
    _seed_tasks(agent, 10)  # 10 % 5 == 0 → would trigger experiment mode

    # Plant an [audit] open question whose normalized text matches
    # the finding's ``[audit] {kind}: {statement[:160]}`` form.
    stall_statement = "stale question"
    model = agent.consciousness.load_self_model()
    model.open_questions = [
        f"[audit] stalled_question: {stall_statement}",
    ]
    agent.consciousness.save_self_model(model)

    # Simulate a single audit cycle that fired on this question
    finding = AuditFinding(
        kind="stalled_question",
        statement=stall_statement,
        cites=[],
        rule="check_stalled_question",
    )
    agent.memory.append_audit_result(AuditResult(
        iteration=1,
        timestamp="2026-01-01T00:00:00Z",
        findings=[finding],
        rules_fired=["check_stalled_question"],
    ))

    with patch("maestro.loop.task_generation.query", AsyncMock()) as mock_q:
        await agent.generate_task()

    assert agent.task_queue.load() == []
    mock_q.assert_not_awaited()


@pytest.mark.asyncio
async def test_experiment_cadence_cost_risk_gate_takes_precedence(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Cost-risk short-circuit fires before experiment-mode language is
    injected — pinning the precedence the experiment-cadence comment
    promises. A regression that moved the cost-risk gate below the
    experiment-cadence block would build the experiment-mode prompt and
    await the SDK query, failing ``assert_not_awaited()`` below."""
    agent = make_agent()
    agent.config.experiment_cadence = 5
    _seed_tasks(agent, 10)  # 10 % 5 == 0 → would trigger experiment mode

    with patch(
        "maestro.task_cost_gate.check_task_cost_risk",
        return_value="consecutive expensive failures",
    ), patch("maestro.loop.task_generation.query", AsyncMock()) as mock_q:
        await agent.generate_task()

    assert agent.task_queue.load() == []
    mock_q.assert_not_awaited()


# ──────────────────────────────────────────────────────────────────────
# Promotable-backlog pressure tests (generate_task prompt injection)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_promotable_backlog_injected_above_threshold(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When promotable count exceeds threshold, the prompt contains the
    PROMOTABLE BACKLOG section with candidate task IDs."""
    agent = make_agent()
    agent.config.promotable_backlog_threshold = 1  # low threshold

    # Seed 3 successful tasks with branches — none cited by plans.
    for i in range(3):
        agent.memory.record_task(TaskRecord(
            task_id=f"t{i}", goal=f"goal-{i}", plan=[],
            outcome="success", result_summary="ok",
            errors=[], duration_secs=1.0,
            timestamp="2026-01-01T00:00:00Z",
            branch=f"maestro/task/t{i}",
        ))

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "promote t2"}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)), \
            patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    prompt = captured_prompt[0]
    assert "PROMOTABLE BACKLOG: 3" in prompt
    assert "source_task_id" in prompt
    # Most recent candidate listed
    assert "t2:" in prompt


@pytest.mark.asyncio
async def test_promotable_backlog_omitted_at_or_below_threshold(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When promotable count is at or below threshold, the prompt must NOT
    contain the PROMOTABLE BACKLOG section."""
    agent = make_agent()
    agent.config.promotable_backlog_threshold = 5  # high threshold

    # Seed only 2 successful tasks with branches — below threshold.
    for i in range(2):
        agent.memory.record_task(TaskRecord(
            task_id=f"t{i}", goal=f"goal-{i}", plan=[],
            outcome="success", result_summary="ok",
            errors=[], duration_secs=1.0,
            timestamp="2026-01-01T00:00:00Z",
            branch=f"maestro/task/t{i}",
        ))

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "normal task"}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)), \
            patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "PROMOTABLE BACKLOG" not in captured_prompt[0]


@pytest.mark.asyncio
async def test_promotable_backlog_candidate_list_formatting(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """The PROMOTABLE BACKLOG section lists candidates most-recent-first
    with goal text, and includes source_task_id guidance."""
    agent = make_agent()
    agent.config.promotable_backlog_threshold = 0  # inject for any count > 0

    # Seed tasks: t0 (oldest) → t2 (newest), with t1 already cited.
    for i in range(3):
        agent.memory.record_task(TaskRecord(
            task_id=f"t{i}", goal=f"goal-{i}", plan=[],
            outcome="success", result_summary="ok",
            errors=[], duration_secs=1.0,
            timestamp="2026-01-01T00:00:00Z",
            branch=f"maestro/task/t{i}",
        ))

    # t1 is cited by a plan → excluded from candidates
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="p1",
        title="promote t1",
        category="capability",  # type: ignore[arg-type]
        rationale="r",
        proposed_changes=[],
        files_affected=[],
        expected_impact="none",
        source_task_id="t1",
    ))

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "promote t2"}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)), \
            patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False):
        await agent.generate_task()

    prompt = captured_prompt[0]
    # 2 promotable tasks (t0, t2) — t1 is cited
    assert "PROMOTABLE BACKLOG: 2" in prompt
    assert "t2:" in prompt
    assert "t0:" in prompt
    # t1 should NOT be listed as a candidate
    assert "t1:" not in prompt
    assert "Set source_task_id" in prompt


# ──────────────────────────────────────────────────────────────────────
# Instrument-consumption directive bridge tests
# ──────────────────────────────────────────────────────────────────────


def _seed_instrument_tasks(
    agent,  # type: ignore[no-untyped-def]
    count: int,
    *,
    consumed: int = 0,
) -> None:
    """Record *count* instrument-shipping tasks, with *consumed* of them
    referenced by a later task."""
    for i in range(count):
        agent.memory.record_task(TaskRecord(
            task_id=f"inst-{i}", goal=f"Build measurement of metric-{i}",
            plan=[], outcome="success", result_summary="shipped",
            errors=[], duration_secs=1.0,
            timestamp=f"2026-01-0{i + 1}T00:00:00Z",
        ))
    for i in range(consumed):
        agent.memory.record_task(TaskRecord(
            task_id=f"consumer-{i}",
            goal=f"Review output of task inst-{i} and extend",
            plan=[], outcome="success", result_summary="reviewed",
            errors=[], duration_secs=1.0,
            timestamp=f"2026-01-1{i + 1}T00:00:00Z",
        ))


@pytest.mark.asyncio
async def test_instrument_bridge_sets_directive_when_threshold_met(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When ≥3 unconsumed instrument tasks exist and directive is empty,
    the bridge auto-fills the directive."""
    agent = make_agent()
    _seed_instrument_tasks(agent, 3)

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    # Directive was set before the SDK call and cleared after the
    # parseable response; check the journal for evidence the bridge fired.
    entries = agent.journal.read_recent_events(20)
    redirects = [
        e for e in entries
        if getattr(e, "triggered_by", None) == "instrument_consumption_gate"
    ]
    assert len(redirects) >= 1


@pytest.mark.asyncio
async def test_instrument_bridge_silent_below_threshold(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When <3 unconsumed instrument tasks, the bridge does NOT fire."""
    agent = make_agent()
    _seed_instrument_tasks(agent, 2)

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    entries = agent.journal.read_recent_events(20)
    redirects = [
        e for e in entries
        if getattr(e, "triggered_by", None) == "instrument_consumption_gate"
    ]
    assert len(redirects) == 0


@pytest.mark.asyncio
async def test_instrument_bridge_defers_to_existing_directive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When next_task_directive is already set, the bridge does NOT fire."""
    agent = make_agent()
    _seed_instrument_tasks(agent, 4)

    model = agent.consciousness.load_self_model()
    model.next_task_directive = "Existing directive takes priority"
    agent.consciousness.save_self_model(model)

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    entries = agent.journal.read_recent_events(20)
    redirects = [
        e for e in entries
        if getattr(e, "triggered_by", None) == "instrument_consumption_gate"
    ]
    assert len(redirects) == 0


@pytest.mark.asyncio
async def test_instrument_bridge_truncates_long_directive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When the instrument goal is very long, the directive is capped at 200 chars."""
    agent = make_agent()
    long_goal = "Build measurement of " + "x" * 200
    long_id_base = "a" * 120
    for i in range(3):
        agent.memory.record_task(TaskRecord(
            task_id=f"{long_id_base}-{i}", goal=long_goal,
            plan=[], outcome="success", result_summary="shipped",
            errors=[], duration_secs=1.0,
            timestamp=f"2026-01-0{i + 1}T00:00:00Z",
        ))

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    captured_directive: list[str | None] = []

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        # Capture the directive from the self-model at call time.
        m = agent.consciousness.load_self_model()
        captured_directive.append(m.next_task_directive)
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    assert len(captured_directive) == 1
    directive = captured_directive[0]
    assert directive is not None
    assert len(directive) <= 200
    assert directive.endswith("…")


# ──────────────────────────────────────────────────────────────────────
# Investigation-streak directive bridge tests
# ──────────────────────────────────────────────────────────────────────


def _seed_investigation_tasks(
    agent,  # type: ignore[no-untyped-def]
    count: int,
) -> None:
    """Record *count* investigation-shaped tasks (measurement/other goals)."""
    for i in range(count):
        agent.memory.record_task(TaskRecord(
            task_id=f"inv-{i}",
            goal=f"Measure task completion rate {i}",
            plan=[], outcome="success", result_summary="measured",
            errors=[], duration_secs=1.0,
            timestamp=f"2026-01-0{i + 1}T00:00:00Z",
        ))


@pytest.mark.asyncio
async def test_investigation_streak_sets_directive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When last K tasks are investigation-shaped and directive is empty,
    the investigation-streak bridge sets the directive."""
    agent = make_agent()
    agent.config.investigation_streak_threshold = 3
    agent.config.investigation_streak_enabled = True
    # Seed an integration task first so integration_cadence gate is satisfied.
    agent.memory.record_task(TaskRecord(
        task_id="triage-0",
        goal="Triage unmerged branches and promote one",
        plan=[], outcome="success", result_summary="triaged",
        errors=[], duration_secs=1.0,
        timestamp="2025-12-31T00:00:00Z",
    ))
    _seed_investigation_tasks(agent, 3)

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    captured_directive: list[str | None] = []

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        m = agent.consciousness.load_self_model()
        captured_directive.append(m.next_task_directive)
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    assert len(captured_directive) == 1
    directive = captured_directive[0]
    assert directive is not None
    assert "construction" in directive.lower()


@pytest.mark.asyncio
async def test_investigation_streak_silent_when_disabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When investigation_streak_enabled is False, the bridge does NOT fire."""
    agent = make_agent()
    agent.config.investigation_streak_threshold = 3
    agent.config.investigation_streak_enabled = False
    _seed_investigation_tasks(agent, 5)

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    captured_directive: list[str | None] = []

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        m = agent.consciousness.load_self_model()
        captured_directive.append(m.next_task_directive)
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    assert len(captured_directive) == 1
    # The investigation-streak directive contains "investigation-shaped";
    # verify that exact marker is absent so the assertion fails if the
    # gate fires despite being disabled.
    directive = captured_directive[0]
    if directive is not None:
        assert "investigation-shaped" not in directive.lower()
    # Also verify no gate activation was logged for investigation_streak.
    gate_path = agent.config.memory_dir / "gate_activations.jsonl"
    if gate_path.exists():
        lines = gate_path.read_text(encoding="utf-8").strip().split("\n")
        records = [json.loads(line) for line in lines if line.strip()]
        assert not any(
            r.get("gate") == "investigation_streak" for r in records
        )


@pytest.mark.asyncio
async def test_investigation_streak_defers_to_existing_directive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When next_task_directive is already set, the bridge does NOT fire."""
    agent = make_agent()
    agent.config.investigation_streak_threshold = 3
    agent.config.investigation_streak_enabled = True
    _seed_investigation_tasks(agent, 5)

    model = agent.consciousness.load_self_model()
    model.next_task_directive = "Existing directive takes priority"
    agent.consciousness.save_self_model(model)

    captured_directive: list[str | None] = []

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        m = agent.consciousness.load_self_model()
        captured_directive.append(m.next_task_directive)
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    assert len(captured_directive) == 1
    assert captured_directive[0] == "Existing directive takes priority"


@pytest.mark.asyncio
async def test_investigation_streak_logs_gate_activation(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Gate activation is logged to gate_activations.jsonl when streak fires."""
    agent = make_agent()
    agent.config.investigation_streak_threshold = 3
    agent.config.investigation_streak_enabled = True
    # Seed an integration task first so integration_cadence gate is satisfied.
    agent.memory.record_task(TaskRecord(
        task_id="triage-0",
        goal="Triage unmerged branches and promote one",
        plan=[], outcome="success", result_summary="triaged",
        errors=[], duration_secs=1.0,
        timestamp="2025-12-31T00:00:00Z",
    ))
    _seed_investigation_tasks(agent, 3)

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    gate_path = agent.config.memory_dir / "gate_activations.jsonl"
    assert gate_path.exists()
    lines = gate_path.read_text(encoding="utf-8").strip().split("\n")
    records = [json.loads(line) for line in lines if line.strip()]
    investigation_records = [
        r for r in records if r.get("gate") == "investigation_streak"
    ]
    assert len(investigation_records) >= 1
    assert investigation_records[0]["action"] == "directive_set"


# ──────────────────────────────────────────────────────────────────────
# Branchless-streak directive tests (structural code-change forcing)
# ──────────────────────────────────────────────────────────────────────


def _seed_branchless_construction_tasks(
    agent,  # type: ignore[no-untyped-def]
    count: int,
) -> None:
    """Seed a branchless integration task plus *count* branchless tasks.

    The leading task carries an integration goal, satisfying the
    integration-cadence gate by goal text alone (no branch — so the
    promotable-branch check never shells git). All seeded tasks have
    ``branch=None``, so the trailing branchless streak is ``count + 1``.
    """
    agent.memory.record_task(TaskRecord(
        task_id="integ-0",
        goal="Triage unmerged branches and promote one",
        plan=[], outcome="success", result_summary="triaged",
        errors=[], duration_secs=1.0, timestamp="2026-01-01T00:00:00Z",
        branch=None,
    ))
    for i in range(count):
        agent.memory.record_task(TaskRecord(
            task_id=f"branchless-{i}",
            goal=f"Analyze remediation lap patterns {i}",
            plan=[], outcome="success", result_summary="analyzed",
            errors=[], duration_secs=1.0,
            timestamp=f"2026-01-0{i + 2}T00:00:00Z",
            branch=None,
        ))


@pytest.mark.asyncio
async def test_branchless_streak_sets_directive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """At/above the threshold of consecutive branchless tasks, the bridge
    fills the empty directive with the code-change requirement."""
    agent = make_agent()
    agent.config.branchless_streak_threshold = 3
    # Isolate from the shape-based investigation gates that precede it.
    agent.config.investigation_streak_enabled = False
    agent.config.investigation_rate_enabled = False
    _seed_branchless_construction_tasks(agent, 3)

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    captured_directive: list[str | None] = []

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        m = agent.consciousness.load_self_model()
        captured_directive.append(m.next_task_directive)
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    assert len(captured_directive) == 1
    directive = captured_directive[0]
    assert directive is not None
    assert "target specific code files" in directive
    assert "No analysis-only tasks" in directive


@pytest.mark.asyncio
async def test_branchless_streak_silent_below_threshold(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A streak shorter than the threshold leaves the gate quiet — no
    branchless_streak activation is logged."""
    agent = make_agent()
    # Seeding yields a streak of 3 (1 integration + 2 analysis); a
    # threshold of 5 keeps it below the firing line.
    agent.config.branchless_streak_threshold = 5
    agent.config.investigation_streak_enabled = False
    agent.config.investigation_rate_enabled = False
    _seed_branchless_construction_tasks(agent, 2)  # streak = 3 < 5

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    gate_path = agent.config.memory_dir / "gate_activations.jsonl"
    if gate_path.exists():
        lines = gate_path.read_text(encoding="utf-8").strip().split("\n")
        records = [json.loads(line) for line in lines if line.strip()]
        assert not any(
            r.get("gate") == "branchless_streak" for r in records
        )


@pytest.mark.asyncio
async def test_branchless_streak_defers_to_existing_directive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When another gate already set the directive, branchless-streak does
    not overwrite it."""
    agent = make_agent()
    agent.config.branchless_streak_threshold = 3
    agent.config.investigation_streak_enabled = False
    agent.config.investigation_rate_enabled = False
    _seed_branchless_construction_tasks(agent, 5)

    model = agent.consciousness.load_self_model()
    model.next_task_directive = "Existing directive takes priority"
    agent.consciousness.save_self_model(model)

    captured_directive: list[str | None] = []

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        m = agent.consciousness.load_self_model()
        captured_directive.append(m.next_task_directive)
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    assert len(captured_directive) == 1
    assert captured_directive[0] == "Existing directive takes priority"


# ──────────────────────────────────────────────────────────────────────
# Investigation-rate directive tests (windowed complement to streak)
# ──────────────────────────────────────────────────────────────────────


def _record(agent, task_id: str, goal: str, ts: str) -> None:  # type: ignore[no-untyped-def]
    agent.memory.record_task(TaskRecord(
        task_id=task_id, goal=goal, plan=[], outcome="success",
        result_summary="done", errors=[], duration_secs=1.0, timestamp=ts,
    ))


@pytest.mark.asyncio
async def test_investigation_rate_sets_directive_on_distributed_pattern(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A distributed investigation pattern (streak broken by a tail
    construction task) still fills the directive via the rate gate."""
    agent = make_agent()
    agent.config.investigation_streak_threshold = 3
    agent.config.investigation_streak_enabled = True
    agent.config.investigation_rate_window = 10
    agent.config.investigation_rate_threshold = 0.4
    agent.config.investigation_rate_enabled = True
    # Integration task keeps the integration-cadence gate satisfied.
    _record(agent, "triage-0", "Triage unmerged branches and promote one",
            "2026-01-01T00:00:00Z")
    _record(agent, "inv-1", "Measure task completion rate",
            "2026-01-02T00:00:00Z")
    _record(agent, "inv-2", "Count no-op tasks in recent history",
            "2026-01-03T00:00:00Z")
    _record(agent, "inv-3", "Audit self-model predictions",
            "2026-01-04T00:00:00Z")
    # Tail construction task breaks the consecutive streak (last 3 not all
    # investigation) while the windowed rate stays at 3/5 = 0.6 > 0.4.
    _record(agent, "fix-4", "Fix #321 dashboard crash",
            "2026-01-05T00:00:00Z")

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    captured_directive: list[str | None] = []

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        m = agent.consciousness.load_self_model()
        captured_directive.append(m.next_task_directive)
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    assert len(captured_directive) == 1
    directive = captured_directive[0]
    assert directive is not None
    assert "construction" in directive.lower()
    # The streak gate cannot have fired (tail is construction); confirm the
    # rate gate is the one that logged the activation.
    gate_path = agent.config.memory_dir / "gate_activations.jsonl"
    records = [
        json.loads(line)
        for line in gate_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rate_records = [r for r in records if r.get("gate") == "investigation_rate"]
    assert len(rate_records) >= 1
    assert rate_records[0]["action"] == "directive_set"


@pytest.mark.asyncio
async def test_investigation_rate_silent_when_below_threshold(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When the investigation fraction is at or below threshold, the rate
    gate does not fire."""
    agent = make_agent()
    agent.config.investigation_streak_threshold = 3
    agent.config.investigation_streak_enabled = True
    agent.config.investigation_rate_window = 10
    agent.config.investigation_rate_threshold = 0.4
    agent.config.investigation_rate_enabled = True
    # "Triage ..." matches no construction pattern, so classify_task_shape
    # returns "other" — i.e. it counts as investigation-shaped. The three
    # tasks below (github_issue, branch_promotion, module_extension) are the
    # only construction-shaped records.
    _record(agent, "triage-0", "Triage unmerged branches and promote one",
            "2026-01-01T00:00:00Z")
    _record(agent, "fix-1", "Fix #1 bug", "2026-01-02T00:00:00Z")
    _record(agent, "promo-2", "Promote task branch via source_task_id",
            "2026-01-03T00:00:00Z")
    _record(agent, "ext-3", "Add new method to module executor",
            "2026-01-04T00:00:00Z")
    # triage-0 (other) + inv-4 (measurement) = 2/5 = 0.4, exactly the
    # threshold. The gate uses a strict ``>`` comparison, so 0.4 > 0.4 is
    # False -> the rate gate must not fire (boundary case).
    _record(agent, "inv-4", "Measure something", "2026-01-05T00:00:00Z")

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    gate_path = agent.config.memory_dir / "gate_activations.jsonl"
    if gate_path.exists():
        records = [
            json.loads(line)
            for line in gate_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert not any(
            r.get("gate") == "investigation_rate" for r in records
        )


@pytest.mark.asyncio
async def test_investigation_rate_defers_to_existing_directive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When a directive is already set, the rate gate does not overwrite it."""
    agent = make_agent()
    agent.config.investigation_streak_enabled = False  # isolate the rate gate
    agent.config.investigation_rate_window = 10
    agent.config.investigation_rate_threshold = 0.4
    agent.config.investigation_rate_enabled = True
    _record(agent, "triage-0", "Triage unmerged branches and promote one",
            "2026-01-01T00:00:00Z")
    _record(agent, "inv-1", "Measure task completion rate",
            "2026-01-02T00:00:00Z")
    _record(agent, "inv-2", "Count no-op tasks", "2026-01-03T00:00:00Z")
    _record(agent, "inv-3", "Audit predictions", "2026-01-04T00:00:00Z")

    model = agent.consciousness.load_self_model()
    model.next_task_directive = "Existing directive takes priority"
    agent.consciousness.save_self_model(model)

    captured_directive: list[str | None] = []

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        m = agent.consciousness.load_self_model()
        captured_directive.append(m.next_task_directive)
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    assert len(captured_directive) == 1
    assert captured_directive[0] == "Existing directive takes priority"


@pytest.mark.asyncio
async def test_investigation_rate_silent_when_disabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When investigation_rate_enabled is False, the rate gate does not fire."""
    agent = make_agent()
    agent.config.investigation_streak_enabled = False  # isolate this test
    agent.config.investigation_rate_window = 10
    agent.config.investigation_rate_threshold = 0.4
    agent.config.investigation_rate_enabled = False
    # Seed a high investigation rate (all investigation tasks).
    _record(agent, "triage-0", "Triage unmerged branches and promote one",
            "2026-01-01T00:00:00Z")
    _record(agent, "inv-1", "Measure task completion rate",
            "2026-01-02T00:00:00Z")
    _record(agent, "inv-2", "Count no-op tasks", "2026-01-03T00:00:00Z")
    _record(agent, "inv-3", "Audit predictions", "2026-01-04T00:00:00Z")

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    gate_path = agent.config.memory_dir / "gate_activations.jsonl"
    if gate_path.exists():
        records = [
            json.loads(line)
            for line in gate_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert not any(
            r.get("gate") == "investigation_rate" for r in records
        )


# ──────────────────────────────────────────────────────────────────────
# Promotable-backlog directive bridge tests
# ──────────────────────────────────────────────────────────────────────


def _seed_promotable_backlog(
    agent,  # type: ignore[no-untyped-def]
    count: int,
) -> None:
    """Seed an integration task (satisfying integration_cadence) plus
    *count* successful tasks with uncited branches forming the backlog."""
    agent.memory.record_task(TaskRecord(
        task_id="triage-0",
        goal="Triage unmerged branches and promote one",
        plan=[], outcome="success", result_summary="triaged",
        errors=[], duration_secs=1.0,
        timestamp="2025-12-31T00:00:00Z",
    ))
    for i in range(count):
        agent.memory.record_task(TaskRecord(
            task_id=f"build-{i}",
            goal=f"Build executor feature {i}",
            plan=[], outcome="success", result_summary="built",
            errors=[], duration_secs=1.0,
            branch=f"maestro/task/build-{i}",
            timestamp=f"2026-01-0{i + 1}T00:00:00Z",
        ))


@pytest.mark.asyncio
async def test_promotable_backlog_directive_sets_directive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When uncited branches reach the threshold and the directive is
    empty, the promotable-backlog bridge names the oldest branch."""
    agent = make_agent()
    agent.config.promotable_backlog_threshold = 3
    agent.config.promotable_backlog_directive_enabled = True
    # Isolate the bridge: the seeded build goals classify as "other"
    # (investigation-shaped), so disable the earlier streak and rate gates
    # which would otherwise fill the directive first.
    agent.config.investigation_streak_enabled = False
    agent.config.investigation_rate_enabled = False
    _seed_promotable_backlog(agent, 3)

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    captured_directive: list[str | None] = []

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        m = agent.consciousness.load_self_model()
        captured_directive.append(m.next_task_directive)
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch(
        "maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)
    ), patch(
        "maestro.audit_checks.is_branch_merged_into_main",
        return_value=False,
    ):
        await agent.generate_task()

    assert len(captured_directive) == 1
    directive = captured_directive[0]
    assert directive is not None
    # Oldest uncited branch named (lowest task_id, ascending sort).
    assert "maestro/task/build-0" in directive
    assert len(directive) <= 200
    # Gate activation recorded for the bridge.
    gate_path = agent.config.memory_dir / "gate_activations.jsonl"
    assert gate_path.exists()
    lines = gate_path.read_text(encoding="utf-8").strip().split("\n")
    records = [json.loads(line) for line in lines if line.strip()]
    assert any(
        r.get("gate") == "promotable_backlog_directive" for r in records
    )


@pytest.mark.asyncio
async def test_promotable_backlog_directive_silent_when_disabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When promotable_backlog_directive_enabled is False, the bridge does
    NOT fire even though the backlog is above threshold."""
    agent = make_agent()
    agent.config.promotable_backlog_threshold = 3
    agent.config.promotable_backlog_directive_enabled = False
    # Isolate the bridge: disable the earlier investigation-streak and
    # investigation-rate gates so the directive stays empty when the
    # promotable-backlog bridge is off.
    agent.config.investigation_streak_enabled = False
    agent.config.investigation_rate_enabled = False
    _seed_promotable_backlog(agent, 3)

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    captured_directive: list[str | None] = []

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        m = agent.consciousness.load_self_model()
        captured_directive.append(m.next_task_directive)
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch(
        "maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)
    ), patch(
        "maestro.audit_checks.is_branch_merged_into_main",
        return_value=False,
    ):
        await agent.generate_task()

    assert len(captured_directive) == 1
    # No other directive gate fires for this seeding, so it stays empty.
    assert captured_directive[0] is None
    gate_path = agent.config.memory_dir / "gate_activations.jsonl"
    if gate_path.exists():
        lines = gate_path.read_text(encoding="utf-8").strip().split("\n")
        records = [json.loads(line) for line in lines if line.strip()]
        assert not any(
            r.get("gate") == "promotable_backlog_directive" for r in records
        )


# ──────────────────────────────────────────────────────────────────────
# Self-directed success-rate directive tests
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_task_self_directed_success_gate_fires(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When check_self_directed_success_rate returns a directive string,
    generate_task sets the directive and logs a gate activation."""
    agent = make_agent()
    # Isolate this gate: disable the upstream investigation-rate gate so it
    # cannot pre-empt the directive.
    agent.config.investigation_rate_enabled = False

    # Seed enough tasks so earlier directive gates don't fire.
    # Use an integration task to satisfy integration_cadence.
    agent.memory.record_task(TaskRecord(
        task_id="triage-0",
        goal="Triage unmerged branches and promote one",
        plan=[], outcome="success", result_summary="triaged",
        errors=[], duration_secs=1.0,
        timestamp="2025-12-31T00:00:00Z",
    ))
    # Add a non-investigation task so investigation_streak doesn't fire.
    agent.memory.record_task(TaskRecord(
        task_id="build-0",
        goal="Build a new executor module feature",
        plan=[], outcome="success", result_summary="built",
        errors=[], duration_secs=1.0,
        timestamp="2026-01-01T00:00:00Z",
    ))

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    fake_directive = (
        "Self-directed success rate is 50% (3/6). "
        "Next task must be bounded construction."
    )

    captured_directive: list[str | None] = []

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        # Capture the directive at query time — it is cleared after.
        m = agent.consciousness.load_self_model()
        captured_directive.append(m.next_task_directive)
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with (
        patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)),
        patch(
            "maestro.task_gates.check_self_directed_success_rate",
            return_value=fake_directive,
        ),
    ):
        await agent.generate_task()

    # Directive was set at query time (before post-call cleanup clears it).
    assert len(captured_directive) == 1
    assert captured_directive[0] == fake_directive

    # Gate activation should be logged.
    gate_path = agent.config.memory_dir / "gate_activations.jsonl"
    assert gate_path.exists()
    lines = gate_path.read_text(encoding="utf-8").strip().split("\n")
    records = [json.loads(line) for line in lines if line.strip()]
    sd_records = [
        r for r in records if r.get("gate") == "self_directed_success"
    ]
    assert len(sd_records) >= 1
    assert sd_records[0]["action"] == "directive_set"


# ──────────────────────────────────────────────────────────────────────
# Prior-task context injection tests
# ──────────────────────────────────────────────────────────────────────


def _seed_task_with_findings(
    agent,  # type: ignore[no-untyped-def]
    *,
    key_findings: str | None = None,
    result_summary: str = "ok",
    outcome: str = "success",
) -> None:
    """Record a single task with configurable findings and outcome."""
    agent.memory.record_task(TaskRecord(
        task_id="t-findings", goal="goal-findings", plan=[],
        outcome=outcome, result_summary=result_summary,
        errors=[], duration_secs=1.0,
        timestamp="2026-01-01T00:00:00Z",
        key_findings=key_findings,
    ))


@pytest.mark.asyncio
async def test_generate_task_injects_prior_context(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When a recent successful task has key_findings, the prompt must
    contain '## Prior Task Context' with the findings text."""
    agent = make_agent()
    agent.config.prior_task_context_enabled = True
    _seed_task_with_findings(agent, key_findings="discovered X leads to Y")

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({"question": None, "task": "follow-up"}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "Prior Task Context" in captured_prompt[0]
    assert "discovered X leads to Y" in captured_prompt[0]


@pytest.mark.asyncio
async def test_generate_task_prior_context_fallback_to_summary(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When key_findings is empty but result_summary is non-empty,
    the prompt should contain the result_summary text."""
    agent = make_agent()
    agent.config.prior_task_context_enabled = True
    _seed_task_with_findings(
        agent, key_findings="", result_summary="summary of work done",
    )

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({"question": None, "task": "next task"}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "Prior Task Context" in captured_prompt[0]
    assert "summary of work done" in captured_prompt[0]


@pytest.mark.asyncio
async def test_generate_task_no_prior_context_when_disabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When prior_task_context_enabled is False, the prompt must NOT
    contain 'Prior Task Context' even if findings exist."""
    agent = make_agent()
    agent.config.prior_task_context_enabled = False
    _seed_task_with_findings(agent, key_findings="should not appear")

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({"question": None, "task": "unrelated"}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "Prior Task Context" not in captured_prompt[0]


@pytest.mark.asyncio
async def test_generate_task_no_prior_context_empty_history(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When task history is empty, the prompt must NOT contain
    'Prior Task Context'."""
    agent = make_agent()
    agent.config.prior_task_context_enabled = True
    # No tasks seeded

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({"question": None, "task": "first ever"}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "Prior Task Context" not in captured_prompt[0]


@pytest.mark.asyncio
async def test_generate_task_no_prior_context_empty_findings(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When the most recent successful task has empty key_findings AND empty
    result_summary, the prompt must NOT contain 'Prior Task Context'."""
    agent = make_agent()
    agent.config.prior_task_context_enabled = True
    _seed_task_with_findings(
        agent, key_findings="", result_summary="",
    )

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({"question": None, "task": "no context"}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "Prior Task Context" not in captured_prompt[0]


@pytest.mark.asyncio
async def test_generate_task_prior_context_truncation(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When key_findings exceeds 500 chars, the injected text must be
    truncated to exactly 500 chars plus an ellipsis character."""
    agent = make_agent()
    agent.config.prior_task_context_enabled = True
    long_findings = "A" * 600
    _seed_task_with_findings(agent, key_findings=long_findings)

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({"question": None, "task": "truncated task"}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    prompt = captured_prompt[0]
    assert "Prior Task Context" in prompt
    # The full 600 chars must NOT appear
    assert long_findings not in prompt
    # The truncated form (500 A's + ellipsis) must appear
    assert "A" * 500 + "…" in prompt


@pytest.mark.asyncio
async def test_prior_task_context_injects_partial_outcome(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A partial-outcome task should inject findings with the 'partial'
    label so the task generator knows to continue unfinished work."""
    agent = make_agent()
    agent.config.prior_task_context_enabled = True
    _seed_task_with_findings(
        agent, key_findings="step 2 of 4 done", outcome="partial",
    )

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({"question": None, "task": "continue"}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "Prior Task Context" in captured_prompt[0]
    assert "LAST TASK (partial" in captured_prompt[0]
    assert "step 2 of 4 done" in captured_prompt[0]


@pytest.mark.asyncio
async def test_prior_task_context_injects_failed_outcome(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A failure-outcome task should inject findings with the 'failed'
    label so the task generator avoids the same approach."""
    agent = make_agent()
    agent.config.prior_task_context_enabled = True
    _seed_task_with_findings(
        agent, key_findings="import error in module X", outcome="failure",
    )

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({"question": None, "task": "retry"}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "Prior Task Context" in captured_prompt[0]
    assert "LAST TASK (failed" in captured_prompt[0]
    assert "import error in module X" in captured_prompt[0]


@pytest.mark.asyncio
async def test_prior_task_context_success_label_unchanged(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A successful task should inject findings with the 'completed'
    label, preserving the original behavior."""
    agent = make_agent()
    agent.config.prior_task_context_enabled = True
    _seed_task_with_findings(
        agent, key_findings="all tests green", outcome="success",
    )

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({"question": None, "task": "build on it"}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "Prior Task Context" in captured_prompt[0]
    assert "LAST TASK (completed)" in captured_prompt[0]
    assert "all tests green" in captured_prompt[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["partial", "failure"])
async def test_prior_task_context_respects_cap_for_all_outcomes(
    make_agent, mem_fs, outcome: str,  # type: ignore[no-untyped-def]
) -> None:
    """The 500-char cap must apply to partial and failure outcomes,
    not just success."""
    agent = make_agent()
    agent.config.prior_task_context_enabled = True
    long_findings = "B" * 600
    _seed_task_with_findings(
        agent, key_findings=long_findings, outcome=outcome,
    )

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({"question": None, "task": "capped"}),
        )

    with patch(
        "maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query),
    ):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    prompt = captured_prompt[0]
    assert "Prior Task Context" in prompt
    # Full 600 chars must NOT appear
    assert long_findings not in prompt
    # Truncated form must appear
    assert "B" * 500 + "…" in prompt


def test_format_prior_task_context_skips_unknown_outcome() -> None:
    """Tasks with an outcome not in the eligible set are skipped;
    the function falls through to the next task or returns None."""
    from maestro.agent import _format_prior_task_context

    good = TaskRecord(
        task_id="t1", goal="g1", plan=[], outcome="success",
        result_summary="ok", errors=[], duration_secs=1.0,
        timestamp="2026-01-01T00:00:00Z", key_findings="found it",
    )
    # Simulate a corrupted record with an unrecognised outcome
    bad = TaskRecord(
        task_id="t2", goal="g2", plan=[],
        outcome="unknown",  # type: ignore[arg-type]
        result_summary="irrelevant", errors=[], duration_secs=1.0,
        timestamp="2026-01-02T00:00:00Z", key_findings="skip me",
    )
    # bad is last (most recent) — should be skipped, falling back to good
    result = _format_prior_task_context([good, bad], enabled=True)
    assert result is not None
    assert "found it" in result
    assert "skip me" not in result

    # When only bad exists, function returns None
    result2 = _format_prior_task_context([bad], enabled=True)
    assert result2 is None


# ──────────────────────────────────────────────────────────────────────
# Integration-backlog prompt hint (generate_task prompt injection)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_integration_backlog_injected_when_above_threshold(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When ≥3 successful branches lack a source_task_id citation,
    the prompt sent to the SDK must contain the Integration Opportunity
    section."""
    agent = make_agent()
    # Raise promotable threshold so the promotable-backlog section does
    # not fire and suppress the integration-backlog hint we're testing.
    agent.config.promotable_backlog_threshold = 100
    # Seed 4 successful tasks with branches and no cited plans.
    for i in range(4):
        agent.memory.record_task(TaskRecord(
            task_id=f"t{i}", goal=f"goal-{i}", plan=[],
            outcome="success", result_summary="ok",
            errors=[], duration_secs=1.0,
            timestamp="2026-01-01T00:00:00Z",
            branch=f"maestro/task/t{i}",
        ))

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "promote a branch"}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)), \
            patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "Integration Opportunity" in captured_prompt[0]


@pytest.mark.asyncio
async def test_integration_backlog_excluded_when_below_threshold(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When fewer than 3 successful branches lack a source_task_id
    citation, the prompt must NOT contain the Integration Opportunity
    section."""
    agent = make_agent()
    # Seed 2 successful tasks — below the default threshold of 3.
    for i in range(2):
        agent.memory.record_task(TaskRecord(
            task_id=f"t{i}", goal=f"goal-{i}", plan=[],
            outcome="success", result_summary="ok",
            errors=[], duration_secs=1.0,
            timestamp="2026-01-01T00:00:00Z",
            branch=f"maestro/task/t{i}",
        ))

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "normal task"}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)), \
            patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "Integration Opportunity" not in captured_prompt[0]


@pytest.mark.asyncio
async def test_integration_backlog_excludes_cited_branches(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Branches already cited as source_task_id in evolution plans are
    excluded from the backlog count, so the hint disappears when all
    branches have follow-up plans."""
    agent = make_agent()
    # Seed 3 successful tasks with branches.
    for i in range(3):
        agent.memory.record_task(TaskRecord(
            task_id=f"t{i}", goal=f"goal-{i}", plan=[],
            outcome="success", result_summary="ok",
            errors=[], duration_secs=1.0,
            timestamp="2026-01-01T00:00:00Z",
            branch=f"maestro/task/t{i}",
        ))
    # Record evolution plans citing all three.
    for i in range(3):
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=f"p{i}",
            title=f"plan-{i}",
            rationale="r",
            category="feature",
            proposed_changes=["c"],
            files_affected=["f.py"],
            expected_impact="none",
            source_task_id=f"t{i}",
        ))

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "fresh work"}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "Integration Opportunity" not in captured_prompt[0]


# ──────────────────────────────────────────────────────────────────────
# Acceptance-criteria population in generate_task
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_task_extracts_acceptance_criteria(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When the LLM response includes acceptance_criteria in an enqueue entry,
    generate_task populates QueuedTask.acceptance_criteria with validated items."""
    agent = make_agent()

    async def _respond(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{
                    "goal": "Add retry logic to the HTTP client module",
                    "linked_question": None,
                    "rationale": "Improves resilience",
                    "acceptance_criteria": [
                        "maestro/http_client.py contains a retry decorator that retries on 429 and 503 status codes",
                        "tests/test_http_client.py exercises the retry path with a mock returning 503 then 200",
                        "ruff check and mypy pass with zero errors after the change",
                    ],
                }],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_respond)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "Add retry logic to the HTTP client module"
    # All three criteria are concrete and should survive validation
    assert queue[0].acceptance_criteria is not None
    assert len(queue[0].acceptance_criteria) == 3
    assert "retry" in queue[0].acceptance_criteria[0]


@pytest.mark.asyncio
async def test_generate_task_absent_criteria_yields_none(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When the LLM response omits acceptance_criteria entirely, the
    QueuedTask has criteria=None and no error is raised."""
    agent = make_agent()

    async def _respond(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{
                    "goal": "Fix BM25 scoring anomaly in maestro/search.py",
                    "linked_question": None,
                    "rationale": "Understanding scoring gaps",
                }],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_respond)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].acceptance_criteria is None


@pytest.mark.asyncio
async def test_generate_task_empty_criteria_list_yields_none(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """An explicit empty list for acceptance_criteria produces None (no criteria)."""
    agent = make_agent()

    async def _respond(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{
                    "goal": "Refactor logging module for clarity",
                    "linked_question": None,
                    "rationale": "Cleanup",
                    "acceptance_criteria": [],
                }],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_respond)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].acceptance_criteria is None


@pytest.mark.asyncio
async def test_generate_task_filters_vacuous_criteria(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Vacuous criteria (too short, filler-dominated, generic-noun only) are
    stripped by validate_success_criteria, leaving only the concrete ones."""
    agent = make_agent()

    async def _respond(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{
                    "goal": "Add caching layer for expensive queries",
                    "linked_question": None,
                    "rationale": "Performance",
                    "acceptance_criteria": [
                        # Too short (< 25 chars)
                        "it works well",
                        # Filler-dominated
                        "the system should be properly handling the code improvement",
                        # Concrete — should survive
                        "maestro/cache.py exports a LRUCache class with get/set methods and a max_size parameter",
                        # Generic-noun only (non-filler tokens are all generic)
                        "the code implementation handles the system functionality",
                    ],
                }],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_respond)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    # Only the concrete criterion survives
    assert queue[0].acceptance_criteria is not None
    assert len(queue[0].acceptance_criteria) == 1
    assert "LRUCache" in queue[0].acceptance_criteria[0]


@pytest.mark.asyncio
async def test_generate_task_criteria_cap_tracks_evolution_constant(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """generate_task caps acceptance_criteria at the same shared constant
    used by EvolutionEngine.propose_plan(), so plan-level and task-level
    surfaces stay in lockstep if the constant ever changes."""
    from maestro.evolution import ACCEPTANCE_CRITERIA_MAX_ITEMS

    agent = make_agent()

    # Build N+3 concrete (non-vacuous) criteria so we observe the cap rather
    # than vacuousness filtering. Each entry is long, specific, and free of
    # filler-heavy or generic-noun-only phrasing.
    criteria = [
        f"maestro/module_{i}.py exports function f_{i} returning an int "
        f"and tests/test_module_{i}.py asserts f_{i}() == {i}"
        for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS + 3)
    ]

    async def _respond(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{
                    "goal": "Add many concrete criteria to exercise the cap",
                    "linked_question": None,
                    "rationale": "Cap regression",
                    "acceptance_criteria": criteria,
                }],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_respond)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].acceptance_criteria is not None
    assert len(queue[0].acceptance_criteria) == ACCEPTANCE_CRITERIA_MAX_ITEMS


@pytest.mark.asyncio
async def test_generate_task_criteria_filter_runs_before_item_cap(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Regression for #1955: an LLM enqueue entry with vacuous criteria at the
    head and specific criteria at the tail (total > cap) must keep the
    specific tail, not silently truncate to the vacuous head. The vacuous
    filter must run *before* the ACCEPTANCE_CRITERIA_MAX_ITEMS slice."""
    from maestro.evolution import ACCEPTANCE_CRITERIA_MAX_ITEMS

    agent = make_agent()

    vacuous = [
        "improve performance",
        "make it better",
        "the system works",
    ]
    specific = [
        f"maestro/module_{i}.py exports function f_{i} returning an int "
        f"and tests/test_module_{i}.py asserts f_{i}() == {i}"
        for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS)
    ]

    async def _respond(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{
                    "goal": "Exercise filter-then-cap ordering on criteria",
                    "linked_question": None,
                    "rationale": "Filter-vs-cap ordering regression",
                    "acceptance_criteria": vacuous + specific,
                }],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_respond)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].acceptance_criteria is not None
    assert len(queue[0].acceptance_criteria) == len(specific)
    for entry in specific:
        assert entry in queue[0].acceptance_criteria


@pytest.mark.asyncio
async def test_generate_task_criteria_per_item_char_cap(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """generate_task truncates each acceptance_criterion to the same
    per-item char cap used by EvolutionEngine.propose_plan(), so a
    malformed LLM response with multi-thousand-character criteria cannot
    bloat downstream prompts (self_review, PR body, _assess_criteria_outcome).
    """
    from maestro.evolution import ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS

    agent = make_agent()

    # Build one concrete (non-vacuous) prefix that survives validation, then
    # pad with junk to far exceed the per-item char cap. The prefix is
    # specific enough to pass length/filler/generic-noun heuristics.
    concrete_prefix = (
        "maestro/http_client.py contains a retry decorator that retries on "
        "429 and 503 status codes, and tests/test_http_client.py exercises "
        "that retry path with a mock returning 503 then 200"
    )
    oversized = concrete_prefix + (" extra padding text" * 500)
    assert len(oversized) > ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS * 5

    async def _respond(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{
                    "goal": "Exercise the per-item char cap for criteria",
                    "linked_question": None,
                    "rationale": "Char cap regression",
                    "acceptance_criteria": [oversized],
                }],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_respond)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].acceptance_criteria is not None
    assert len(queue[0].acceptance_criteria) == 1
    assert (
        len(queue[0].acceptance_criteria[0])
        <= ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS
    )


@pytest.mark.asyncio
async def test_generate_task_criteria_prompt_language_present(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """The task-generation prompt must contain the acceptance_criteria
    instruction language so the LLM knows to produce them."""
    agent = make_agent()

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "a task"}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "acceptance_criteria" in captured_prompt[0]
    # The prompt includes guidance distinguishing good from bad criteria
    assert "GOOD:" in captured_prompt[0]
    assert "BAD:" in captured_prompt[0]


# ──────────────────────────────────────────────────────────────────────
# Promotable-backlog / integration-backlog consolidation
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_integration_backlog_suppressed_when_promotable_fires(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When the promotable-backlog section fires (count > threshold), the
    integration-backlog section must NOT also appear — they carry the same
    signal and duplicating them confuses the LLM."""
    agent = make_agent()
    # Set promotable threshold low so it fires with few tasks.
    agent.config.promotable_backlog_threshold = 1

    # Seed 4 successful tasks — exceeds both thresholds:
    # promotable_backlog_threshold=1 and _INTEGRATION_BACKLOG_THRESHOLD=3.
    for i in range(4):
        agent.memory.record_task(TaskRecord(
            task_id=f"t{i}", goal=f"goal-{i}", plan=[],
            outcome="success", result_summary="ok",
            errors=[], duration_secs=1.0,
            timestamp="2026-01-01T00:00:00Z",
            branch=f"maestro/task/t{i}",
        ))

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "promote"}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)), \
            patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    prompt = captured_prompt[0]
    # Promotable-backlog fired
    assert "PROMOTABLE BACKLOG: 4" in prompt
    # Integration-backlog suppressed (same signal, less detail)
    assert "Integration Opportunity" not in prompt


# ──────────────────────────────────────────────────────────────────────
# Task-mode balance hint tests (generate_task prompt injection)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_mode_summary_injected_with_enough_tasks(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When >=3 recent tasks exist, the prompt contains the TASK MODE BALANCE
    section with maintenance/generative counts."""
    agent = make_agent()
    # Seed 4 tasks: 2 maintenance + 2 generative
    for i, goal in enumerate([
        "Promote branch t1",
        "Rebase PR on main",
        "Add cost widget",
        "Implement search ranking",
    ]):
        agent.memory.record_task(TaskRecord(
            task_id=f"t{i}", goal=goal, plan=[],
            outcome="success", result_summary="ok",
            errors=[], duration_secs=1.0,
            timestamp="2026-01-01T00:00:00Z",
        ))

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "next task"}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    prompt = captured_prompt[0]
    assert "TASK MODE BALANCE:" in prompt
    assert "2 maintenance" in prompt
    assert "2 generative" in prompt


@pytest.mark.asyncio
async def test_task_mode_summary_absent_with_few_tasks(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When <3 recent tasks exist, the prompt must NOT contain the
    TASK MODE BALANCE section."""
    agent = make_agent()
    # Seed only 2 tasks — below the 3-task minimum
    for i in range(2):
        agent.memory.record_task(TaskRecord(
            task_id=f"t{i}", goal=f"Add feature {i}", plan=[],
            outcome="success", result_summary="ok",
            errors=[], duration_secs=1.0,
            timestamp="2026-01-01T00:00:00Z",
        ))

    captured_prompt: list[str] = []

    async def _capture_query(**kwargs: object) -> MockQueryResult:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "something"}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_capture_query)):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    assert "TASK MODE BALANCE:" not in captured_prompt[0]


# ──────────────────────────────────────────────────────────────────────
# Gate-activation trace — log_gate_activation wiring in generate_task
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_task_audit_stall_logs_gate_activation(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When the audit-stall gate fires, a record appears in gate_activations.jsonl."""
    import json as json_mod

    from maestro.audit_checks import GATE_ACTIVATIONS_FILENAME, AuditFinding
    from maestro.memory import AuditResult

    agent = make_agent()
    agent.config.audit_stall_block_enabled = True
    agent.config.audit_stall_threshold = 1

    stall_statement = "stale question"
    model = agent.consciousness.load_self_model()
    model.open_questions = [
        f"[audit] stalled_question: {stall_statement}",
    ]
    agent.consciousness.save_self_model(model)

    finding = AuditFinding(
        kind="stalled_question",
        statement=stall_statement,
        cites=[],
        rule="check_stalled_question",
    )
    agent.memory.append_audit_result(AuditResult(
        iteration=1,
        timestamp="2026-01-01T00:00:00Z",
        findings=[finding],
        rules_fired=["check_stalled_question"],
    ))

    with patch("maestro.loop.task_generation.query", AsyncMock()):
        await agent.generate_task()

    path = str(agent.config.memory_dir / GATE_ACTIVATIONS_FILENAME)
    assert path in mem_fs
    records = [json_mod.loads(line) for line in mem_fs[path].strip().splitlines()]
    assert any(r["gate"] == "audit_stall" for r in records)


@pytest.mark.asyncio
async def test_generate_task_cost_gate_logs_gate_activation(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When the cost-risk gate fires, a record appears in gate_activations.jsonl."""
    import json as json_mod

    from maestro.audit_checks import GATE_ACTIVATIONS_FILENAME

    agent = make_agent()

    with patch(
        "maestro.task_cost_gate.check_task_cost_risk",
        return_value="consecutive expensive failures",
    ), patch("maestro.loop.task_generation.query", AsyncMock()):
        await agent.generate_task()

    path = str(agent.config.memory_dir / GATE_ACTIVATIONS_FILENAME)
    assert path in mem_fs
    records = [json_mod.loads(line) for line in mem_fs[path].strip().splitlines()]
    assert any(r["gate"] == "cost_risk" for r in records)


@pytest.mark.asyncio
async def test_partial_construction_bridge_sets_directive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When last task is partial with findings and directive is empty,
    the partial-construction bridge populates next_task_directive."""
    agent = make_agent()
    # Isolate this gate: disable the upstream investigation-rate gate so it
    # cannot pre-empt the directive.
    agent.config.investigation_rate_enabled = False
    # Seed an integration task to satisfy the integration_cadence gate.
    agent.memory.record_task(TaskRecord(
        task_id="triage-0",
        goal="Triage unmerged branches and promote one",
        plan=[], outcome="success", result_summary="triaged",
        errors=[], duration_secs=1.0,
        timestamp="2025-12-31T00:00:00Z",
    ))
    # Record a partial task with key findings.
    agent.memory.record_task(TaskRecord(
        task_id="partial-abc",
        goal="Investigate config drift",
        plan=[], outcome="partial",
        result_summary="",
        key_findings="Found config drift in toml_config.py line 42",
        errors=[], duration_secs=5.0,
        timestamp="2025-12-31T01:00:00Z",
    ))

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    captured_directive: list[str | None] = []

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        m = agent.consciousness.load_self_model()
        captured_directive.append(m.next_task_directive)
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    assert len(captured_directive) == 1
    directive = captured_directive[0]
    assert directive is not None
    assert "partial-abc" in directive
    assert "config drift" in directive


@pytest.mark.asyncio
async def test_partial_construction_bridge_defers_to_existing_directive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When next_task_directive is already set, partial-construction bridge
    does not overwrite it."""
    agent = make_agent()
    agent.memory.record_task(TaskRecord(
        task_id="partial-xyz",
        goal="Investigate something",
        plan=[], outcome="partial",
        result_summary="Found something",
        errors=[], duration_secs=1.0,
        timestamp="2025-12-31T00:00:00Z",
    ))

    model = agent.consciousness.load_self_model()
    model.next_task_directive = "Existing directive takes priority"
    agent.consciousness.save_self_model(model)

    captured_directive: list[str | None] = []

    async def _mock_query(**kwargs: object) -> MockQueryResult:
        m = agent.consciousness.load_self_model()
        captured_directive.append(m.next_task_directive)
        return MockQueryResult(
            response=json.dumps({"enqueue": [], "prune": [], "edit": []}),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_mock_query)):
        await agent.generate_task()

    assert len(captured_directive) == 1
    assert captured_directive[0] == "Existing directive takes priority"


def test_agent_module_introduces_no_data_sufficiency_directive_or_gate() -> None:
    """The data-sufficiency change is informational context only.

    PR #2346's acceptance criteria state verbatim: 'No new directive, gate,
    or blocking mechanism is introduced — the change is purely informational
    context injection.' This test enforces that contract by checking the
    agent module source does not contain a data-sufficiency directive writer
    or gate-activation entry.
    """
    from pathlib import Path as _Path

    import maestro.agent as _agent_mod

    source = _Path(_agent_mod.__file__).read_text(encoding="utf-8")
    forbidden_patterns = [
        'gate="data_sufficiency"',
        'check_data_sufficiency(',
    ]
    violations = [pat for pat in forbidden_patterns if pat in source]
    assert not violations, (
        "agent.py introduces directive/gate mechanism forbidden by PR "
        f"acceptance criteria. Violations: {violations}"
    )


# ``test_uncalled_symbol_gate_config_validation`` /
# ``test_criteria_alignment_config_validation`` /
# ``test_compound_scope_gate_config_validation`` were retired in proposal 106.
# Their ``..._enabled`` non-bool rejects tested the ``Config.validate`` ladder's
# ``isinstance(x, bool)`` guards, which have no declarative equivalent (pydantic
# lax-coerces ``"yes"`` -> ``True``) and were removed with the ladder. The one
# surviving numeric reject — ``uncalled_symbol_max_allowed >= 0`` — moved onto the
# ``_LoopSettings`` ``Field(ge=0)`` guard, pinned by
# ``tests/test_config_settings_parity.py::TestConstraintRejects``.


# ──────────────────────────────────────────────────────────────────────
# Compound-scope post-call gate
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compound_scope_gate_rejects_compound_task(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A goal combining build+wire verbs is dropped and gate activation logged."""
    import json as json_mod

    from maestro.audit_checks import GATE_ACTIVATIONS_FILENAME

    agent = make_agent()
    agent.config.compound_scope_gate_enabled = True

    async def _fake_query(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [
                    {"goal": "Build a data-sufficiency gate and wire it into generate_task"},
                ],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_fake_query)):
        await agent.generate_task()

    # Task should NOT have been added to the queue
    queue = agent.task_queue.load()
    assert not any(
        "data-sufficiency" in q.goal for q in queue
    ), "Compound-scope goal should have been rejected"

    # Gate activation should be logged
    path = str(agent.config.memory_dir / GATE_ACTIVATIONS_FILENAME)
    assert path in mem_fs
    records = [json_mod.loads(line) for line in mem_fs[path].strip().splitlines()]
    assert any(r["gate"] == "compound_scope" for r in records)


@pytest.mark.asyncio
async def test_compound_scope_gate_disabled_allows_compound_task(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When gate is disabled, compound-scope goals pass through to the queue."""
    agent = make_agent()
    agent.config.compound_scope_gate_enabled = False

    compound_goal = "Build a data-sufficiency gate and wire it into generate_task"

    async def _fake_query(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": compound_goal}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_fake_query)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert any(
        "data-sufficiency" in q.goal for q in queue
    ), "Compound-scope goal should have been accepted when gate is disabled"


@pytest.mark.asyncio
async def test_compound_scope_gate_enabled_allows_noncompound_task(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An enabled gate must not reject ordinary single-verb (non-compound) goals."""
    agent = make_agent()
    agent.config.compound_scope_gate_enabled = True

    noncompound_goal = "Add a data-sufficiency gate to generate_task"

    async def _fake_query(**kwargs: object) -> MockQueryResult:
        return MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": noncompound_goal}],
            }),
        )

    with patch("maestro.loop.task_generation.query", AsyncMock(side_effect=_fake_query)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert any(
        "data-sufficiency" in q.goal for q in queue
    ), "Non-compound goal should pass through even when the gate is enabled"


# ──────────────────────────────────────────────────────────────────────
# Quiet-skip gate tests
# ──────────────────────────────────────────────────────────────────────


def _quiet_skip_records(mem_fs: dict[str, str], agent) -> list[dict]:  # type: ignore[no-untyped-def]
    """Return the ``quiet_skip`` gate-activation records written to mem_fs.

    Returns an empty list when no activation file exists (other gates may
    write the file, so the absence of a *quiet_skip* record — not the file —
    is what proves the gate stayed silent)."""
    from maestro.audit_checks import GATE_ACTIVATIONS_FILENAME

    path = str(agent.config.memory_dir / GATE_ACTIVATIONS_FILENAME)
    if path not in mem_fs:
        return []
    records = [json.loads(line) for line in mem_fs[path].strip().splitlines()]
    return [r for r in records if r["gate"] == "quiet_skip"]


@pytest.mark.asyncio
async def test_quiet_skip_fires_when_queue_nonempty_no_external(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When the queue has items and no external requests are pending,
    the quiet-skip gate fires: no SDK call, gate_activations.jsonl entry."""
    from maestro.task_queue import QueuedTask
    from tests.component.sdk_mock import SDKMock

    agent = make_agent()
    agent.config.quiet_skip_enabled = True

    # Seed the queue with one item
    agent.task_queue.save([
        QueuedTask(task_id="q1", goal="existing queued task"),
    ])

    # Ensure no external requests
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = []
    model.pending_pr_requests = []
    agent.consciousness.save_self_model(model)

    sdk = SDKMock()
    with sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()

    # SDK query must NOT have been called — the gate short-circuited.
    sdk.assert_not_called()

    # Exactly one quiet_skip activation, recording the blocking action and
    # the queue size that triggered it.
    quiet = _quiet_skip_records(mem_fs, agent)
    assert len(quiet) == 1
    assert quiet[0]["action"] == "blocked"
    assert quiet[0]["detail"] == "queue_size=1"


@pytest.mark.asyncio
async def test_quiet_skip_does_not_fire_when_queue_empty(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When the queue is empty, the quiet-skip gate does not fire and
    the SDK query proceeds."""
    from tests.component.sdk_mock import SDKMock

    agent = make_agent()
    agent.config.quiet_skip_enabled = True

    # Queue is empty (default state)
    agent.task_queue.save([])

    # Ensure no external requests
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = []
    model.pending_pr_requests = []
    agent.consciousness.save_self_model(model)

    sdk = SDKMock().on_any({"prune": [], "edit": [], "enqueue": []})
    with sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()

    # The gate stayed silent and the SDK query proceeded.
    sdk.assert_called()
    assert _quiet_skip_records(mem_fs, agent) == []


@pytest.mark.asyncio
async def test_quiet_skip_does_not_fire_when_external_pending(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When the queue has items but pending_user_requests is non-empty,
    the quiet-skip gate does not fire."""
    from maestro.task_queue import QueuedTask
    from tests.component.sdk_mock import SDKMock

    agent = make_agent()
    agent.config.quiet_skip_enabled = True

    # Seed the queue with one item
    agent.task_queue.save([
        QueuedTask(task_id="q1", goal="existing queued task"),
    ])

    # Add a pending user request
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["please do something"]
    model.pending_pr_requests = []
    agent.consciousness.save_self_model(model)

    sdk = SDKMock().on_any({"prune": [], "edit": [], "enqueue": []})
    with sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()

    # SDK query was called — the gate did not fire.
    sdk.assert_called()
    assert _quiet_skip_records(mem_fs, agent) == []


@pytest.mark.asyncio
async def test_quiet_skip_does_not_fire_when_pr_request_pending(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When the queue has items but pending_pr_requests is non-empty,
    the quiet-skip gate does not fire (covers the pr-request disjunct)."""
    from maestro.task_queue import QueuedTask
    from tests.component.sdk_mock import SDKMock

    agent = make_agent()
    agent.config.quiet_skip_enabled = True

    # Seed the queue with one item
    agent.task_queue.save([
        QueuedTask(task_id="q1", goal="existing queued task"),
    ])

    # No user requests, but a pending PR remediation request.
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = []
    model.pending_pr_requests = [
        PendingPRRequest(directive="remediate PR #42"),
    ]
    agent.consciousness.save_self_model(model)

    sdk = SDKMock().on_any({"prune": [], "edit": [], "enqueue": []})
    with sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()

    # SDK query was called — the gate did not fire.
    sdk.assert_called()
    assert _quiet_skip_records(mem_fs, agent) == []


@pytest.mark.asyncio
async def test_quiet_skip_does_not_fire_when_disabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When quiet_skip_enabled is False, the gate does not fire even if
    the queue has items and no external requests are pending."""
    from maestro.task_queue import QueuedTask
    from tests.component.sdk_mock import SDKMock

    agent = make_agent()
    agent.config.quiet_skip_enabled = False

    # Seed the queue with one item
    agent.task_queue.save([
        QueuedTask(task_id="q1", goal="existing queued task"),
    ])

    # Ensure no external requests
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = []
    model.pending_pr_requests = []
    agent.consciousness.save_self_model(model)

    sdk = SDKMock().on_any({"prune": [], "edit": [], "enqueue": []})
    with sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()

    # SDK query was called — the gate did not fire.
    sdk.assert_called()
    assert _quiet_skip_records(mem_fs, agent) == []
