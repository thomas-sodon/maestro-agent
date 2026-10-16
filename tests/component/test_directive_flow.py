"""Component tests for the contemplate → directive → generate_task flow.

Verifies that:
1. ``next_task_directive`` set during contemplation persists to disk.
2. ``generate_task`` injects the directive into the task-generation prompt.
3. ``generate_task`` clears the directive after a successful SDK response.
4. The audit→directive bridge writes a directive when findings exist and
   the directive is empty.
5. The audit→directive bridge does NOT overwrite an existing directive.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from maestro.audit_checks import AuditFinding
from maestro.sdk import QueryResult
from tests.component.conftest import write_self_model
from tests.component.sdk_mock import SDKMock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enqueue_payload(
    goal: str,
    linked_question: str | None = None,
) -> str:
    entry: dict[str, str | None] = {"goal": goal}
    if linked_question is not None:
        entry["linked_question"] = linked_question
    return json.dumps({"prune": [], "edit": [], "enqueue": [entry]})


# ---------------------------------------------------------------------------
# Full flow: contemplate → directive persists → generate_task sees it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_directive_flows_from_contemplate_into_generate_task_prompt(
    make_agent, memory_dir,
):
    """End-to-end: contemplation sets directive → generate_task prompt contains it."""
    directive_text = "Focus on error-handling coverage gaps next"
    write_self_model(
        memory_dir,
        open_questions=["What should I work on?"],
        next_task_directive=None,
    )
    agent, _code_host, _vcs = make_agent()

    # --- Phase 1: contemplate ---
    # Mock introspect to return a fixed reflection.
    introspect_mock = AsyncMock(return_value="I notice gaps in error handling.")

    # Mock update_self_model to return a model with the directive set,
    # and actually persist it on disk (mimicking the real save).
    async def _fake_update_self_model(
        reflection, journal=None, session=None,
        recent_self_directed=None, recent_audit_findings=None,
        restatement_concept_key_enabled=False,
        self_model_external_anchor_enabled=False,
        evidence_bound_claims_enabled=False,
        claim_evidence_sources=None, open_question_cap_evicted=None,
    ):
        model = agent.consciousness.load_self_model()
        model.next_task_directive = directive_text
        model.identity_statements = ["I am Maestro"]
        agent.consciousness.save_self_model(model)
        return model

    with patch.object(agent.consciousness, "introspect", introspect_mock), \
         patch.object(
             agent.consciousness, "update_self_model",
             side_effect=_fake_update_self_model,
         ), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[]), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate()

    # Verify the directive was persisted.
    model_after_contemplate = agent.consciousness.load_self_model()
    assert model_after_contemplate.next_task_directive == directive_text

    # --- Phase 2: generate_task ---
    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload(
            "Fix error-handling patterns in maestro/agent.py",
            linked_question="What should I work on?",
        ))

    with patch("maestro.loop.task_generation.query", side_effect=_capture_query):
        await agent.generate_task()

    # The directive MUST appear in the task-generation prompt.
    assert len(captured_kwargs) == 1
    prompt = captured_kwargs[0]["prompt"]
    assert directive_text in prompt

    # After a successful generate_task, the directive is cleared.
    model_after_generate = agent.consciousness.load_self_model()
    assert model_after_generate.next_task_directive is None


@pytest.mark.asyncio
async def test_directive_survives_sdk_failure_in_generate_task(
    make_agent, memory_dir,
):
    """Directive persists when generate_task's SDK call raises."""
    directive_text = "Investigate flaky CI tests"
    write_self_model(
        memory_dir,
        open_questions=["What should I work on?"],
        next_task_directive=directive_text,
    )
    agent, _code_host, _vcs = make_agent()

    failing_mock = AsyncMock(side_effect=RuntimeError("SDK unavailable"))
    with patch("maestro.loop.task_generation.query", failing_mock), \
         pytest.raises(RuntimeError, match="SDK unavailable"):
        await agent.generate_task()

    # Directive must survive the failure.
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive == directive_text


@pytest.mark.asyncio
async def test_directive_survives_malformed_json_in_generate_task(
    make_agent, memory_dir,
):
    """Directive persists when generate_task gets a non-JSON response."""
    directive_text = "Focus on test coverage"
    write_self_model(
        memory_dir,
        open_questions=["What should I work on?"],
        next_task_directive=directive_text,
    )
    agent, _code_host, _vcs = make_agent()

    sdk = SDKMock()
    sdk.then("not valid json at all")
    with sdk.patch("maestro.loop.task_generation"):
        await agent.generate_task()

    # Directive preserved (parse error returns before the clear).
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive == directive_text


# ---------------------------------------------------------------------------
# Audit→directive bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_bridge_sets_directive_when_empty(
    make_agent, memory_dir,
):
    """_audit_self_model writes a directive when findings exist and directive is None."""
    write_self_model(
        memory_dir,
        open_questions=["What am I?"],
        next_task_directive=None,
    )
    agent, _code_host, _vcs = make_agent()

    # Inject a synthetic audit finding so the bridge fires.
    fake_finding = AuditFinding(
        kind="contradicted_claim",
        rule="test_rule",
        statement="Capability 'X' is claimed but never exercised",
    )
    with patch(
        "maestro.audit_checks.run_all_checks", return_value=[fake_finding],
    ), patch.object(agent.memory, "get_recent_tasks", return_value=[]), \
         patch.object(agent.memory, "get_success_rate", return_value=1.0), \
         patch.object(
             agent.memory, "get_recent_audit_results", return_value=[],
         ), \
         patch.object(
             agent.memory, "get_recent_question_resolutions", return_value=[],
         ), \
         patch.object(
             agent.memory, "get_cited_source_task_ids", return_value=set(),
         ), \
         patch.object(agent.journal, "write_thought"), \
         patch.object(agent.memory, "append_audit_result"):
        await agent._audit_self_model(iteration=1)

    model = agent.consciousness.load_self_model()
    assert model.next_task_directive is not None
    assert len(model.next_task_directive) > 0


@pytest.mark.asyncio
async def test_audit_bridge_does_not_overwrite_existing_directive(
    make_agent, memory_dir,
):
    """_audit_self_model preserves an existing directive even when findings exist."""
    existing_directive = "Already set by contemplation"
    write_self_model(
        memory_dir,
        open_questions=["What am I?"],
        next_task_directive=existing_directive,
    )
    agent, _code_host, _vcs = make_agent()

    fake_finding = AuditFinding(
        kind="contradicted_claim",
        rule="test_rule",
        statement="Capability 'X' is claimed but never exercised",
    )
    with patch(
        "maestro.audit_checks.run_all_checks", return_value=[fake_finding],
    ), patch.object(agent.memory, "get_recent_tasks", return_value=[]), \
         patch.object(agent.memory, "get_success_rate", return_value=1.0), \
         patch.object(
             agent.memory, "get_recent_audit_results", return_value=[],
         ), \
         patch.object(
             agent.memory, "get_recent_question_resolutions", return_value=[],
         ), \
         patch.object(
             agent.memory, "get_cited_source_task_ids", return_value=set(),
         ), \
         patch.object(agent.journal, "write_thought"), \
         patch.object(agent.memory, "append_audit_result"):
        await agent._audit_self_model(iteration=1)

    model = agent.consciousness.load_self_model()
    assert model.next_task_directive == existing_directive


@pytest.mark.asyncio
async def test_audit_bridge_no_directive_when_no_findings(
    make_agent, memory_dir,
):
    """_audit_self_model does not set a directive when there are no findings."""
    write_self_model(
        memory_dir,
        open_questions=["What am I?"],
        next_task_directive=None,
    )
    agent, _code_host, _vcs = make_agent()

    with patch(
        "maestro.audit_checks.run_all_checks", return_value=[],
    ), patch.object(agent.memory, "get_recent_tasks", return_value=[]), \
         patch.object(agent.memory, "get_success_rate", return_value=1.0), \
         patch.object(
             agent.memory, "get_recent_audit_results", return_value=[],
         ), \
         patch.object(
             agent.memory, "get_recent_question_resolutions", return_value=[],
         ), \
         patch.object(
             agent.memory, "get_cited_source_task_ids", return_value=set(),
         ), \
         patch.object(agent.journal, "write_thought"), \
         patch.object(agent.memory, "append_audit_result"):
        await agent._audit_self_model(iteration=1)

    model = agent.consciousness.load_self_model()
    assert model.next_task_directive is None


# ---------------------------------------------------------------------------
# Directive prompt placement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_task_prompt_shows_none_when_no_directive(
    make_agent, memory_dir,
):
    """When directive is None, the prompt slot reads '(none)'."""
    write_self_model(
        memory_dir,
        open_questions=["What am I?"],
        next_task_directive=None,
    )
    agent, _code_host, _vcs = make_agent()
    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload("do work"))

    with patch("maestro.loop.task_generation.query", side_effect=_capture_query):
        await agent.generate_task()

    assert len(captured_kwargs) == 1
    prompt = captured_kwargs[0]["prompt"]
    # The prompt template formats None as "(none)".
    assert "Contemplation directive (one-shot, from your most recent reflection):" in prompt
    # The line after the label should contain "(none)".
    lines = prompt.split("\n")
    directive_line_idx = next(
        i for i, line in enumerate(lines)
        if "Contemplation directive" in line
    )
    assert "(none)" in lines[directive_line_idx + 1]


@pytest.mark.asyncio
async def test_generate_task_consumes_directive_set_by_gate_chain(
    make_agent, memory_dir,
):
    """#4302: a gate-set directive reaches the prompt and is then cleared.

    The gate chain rebinds ``ctx.model`` to the persisted copy; generate_task
    must read that copy, not its pre-chain snapshot.
    """
    from maestro.task_gates import GateOutcome, TaskGate

    gate_directive = "Gate-set directive for issue 4302"
    write_self_model(memory_dir, next_task_directive=None)
    agent, _code_host, _vcs = make_agent()
    gate = TaskGate(
        name="test_bridge", kind="directive", enabled=lambda _c: True,
        run=lambda _ctx: GateOutcome(
            detail="d", log_message="m", directive=gate_directive,
        ),
    )
    captured_kwargs: list[dict] = []

    async def _capture_query(**kwargs):
        captured_kwargs.append(kwargs)
        return QueryResult(response=_enqueue_payload("Fix maestro/agent.py"))

    with patch("maestro.loop.task_generation.query", side_effect=_capture_query), \
         patch("maestro.loop.task_generation.GATE_CHAIN", [gate]), \
         patch.object(agent.memory, "get_recent_tasks", return_value=[]):
        await agent.generate_task()

    assert len(captured_kwargs) == 1
    assert gate_directive in captured_kwargs[0]["prompt"]
    assert agent.consciousness.load_self_model().next_task_directive is None
