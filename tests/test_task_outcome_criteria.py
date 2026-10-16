"""Criteria-anchored task outcome, API-error outcome guard and task-phase outcome
recording (``maestro/loop/task_outcome.py``); split out of
``tests/test_agent_lifecycle.py``.
"""

from __future__ import annotations

import json
from typing import get_args
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.consciousness import PendingPRRequest
from maestro.memory import TaskRecord
from maestro.types import TaskOutcome
from tests.conftest import MockQueryResult

_IMPL = "maestro.loop.implementation"


# ──────────────────────────────────────────────────────────────────────
# Criteria-anchored outcome classification tests
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outcome_all_criteria_met_yields_success(make_agent):  # type: ignore[no-untyped-def]
    """When all 5 acceptance criteria are MET, outcome is overridden to
    success even if the step-based outcome would be partial."""
    agent = make_agent()
    agent.config.loop_max_replans = 0
    # Lock the dispatch path: with session reuse disabled, run_session is
    # None and _assess_criteria_outcome routes through bare ``query`` which
    # accepts kwargs. Pinning this explicitly avoids invisible regressions
    # if the fixture default ever flips.
    agent.config.enable_session_reuse = False

    criteria = [
        "Feature X is implemented",
        "Tests cover all branches",
        "Docs updated",
        "No regressions",
        "Performance within budget",
    ]

    # Two steps: first succeeds, second fails → step-based outcome = partial.
    plan_response = json.dumps([
        {"description": "Step OK"},
        {"description": "Step fail"},
    ])

    call_count = 0

    async def alternating_exec(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return MockQueryResult(response="Done")
        raise RuntimeError("Step 2 failed")

    # LLM criteria assessment: all 5 MET
    criteria_response = (
        "1. MET — Feature X implemented in module.py\n"
        "2. MET — Tests added for all branches\n"
        "3. MET — Docs updated in README\n"
        "4. MET — No regressions found\n"
        "5. MET — Performance within budget\n"
    )

    agent_query_calls: list[str] = []

    async def mock_agent_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        # ``query`` is called with kwargs; ``query_on_session`` positionally
        # as ``query_on_session(session, prompt, ...)``. Accept both so the
        # mock cannot silently TypeError into the broad-except fallback.
        prompt = kwargs.get("prompt") or (args[1] if len(args) >= 2 else "")
        agent_query_calls.append(prompt)
        if "acceptance criterion" in prompt.lower() or "acceptance criteria" in prompt.lower():
            return MockQueryResult(response=criteria_response)
        return MockQueryResult(response="Key findings here")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", side_effect=alternating_exec), \
         patch("maestro.agent.query", side_effect=mock_agent_query), \
         patch(f"{_IMPL}.query", side_effect=mock_agent_query), \
         patch("maestro.agent.query_on_session", side_effect=mock_agent_query):
        mock_plan.return_value = MockQueryResult(response=plan_response)

        record = await agent.run(
            "Test goal", acceptance_criteria=criteria,
        )

    # Step-based would be partial, but all criteria MET → success
    assert record.outcome == "success"
    # Sanity: the criteria-assessment call was actually issued (guards
    # against silent fallback to the step-based outcome).
    assert any(
        "acceptance criteri" in p.lower() for p in agent_query_calls
    )
    # The per-criterion verdicts are persisted on the constructed record so
    # downstream consumers can trace the classification basis.
    assert record.criteria_verdicts is not None
    assert [v["verdict"] for v in record.criteria_verdicts] == ["MET"] * 5
    assert [v["criterion"] for v in record.criteria_verdicts] == criteria


@pytest.mark.asyncio
async def test_outcome_partial_criteria_yields_partial(make_agent):  # type: ignore[no-untyped-def]
    """When 3/5 acceptance criteria are MET, outcome stays partial."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture

    criteria = [
        "Feature X is implemented",
        "Tests cover all branches",
        "Docs updated",
        "No regressions",
        "Performance within budget",
    ]

    plan_response = json.dumps([{"description": "Step 1"}])

    # LLM criteria assessment: 3 MET, 2 UNMET
    criteria_response = (
        "1. MET — Feature X implemented\n"
        "2. UNMET — Tests missing for error branch\n"
        "3. MET — Docs updated\n"
        "4. MET — No regressions found\n"
        "5. UNMET — Performance not measured\n"
    )

    async def mock_agent_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        prompt = kwargs.get("prompt") or (args[1] if len(args) >= 2 else "")
        if "acceptance criteria" in prompt.lower():
            return MockQueryResult(response=criteria_response)
        return MockQueryResult(response="Key findings here")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_agent_query), \
         patch(f"{_IMPL}.query", side_effect=mock_agent_query), \
         patch("maestro.agent.query_on_session", side_effect=mock_agent_query):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")

        record = await agent.run(
            "Test goal", acceptance_criteria=criteria,
        )

    assert record.outcome == "partial"


@pytest.mark.asyncio
async def test_outcome_no_criteria_uses_existing_path(make_agent):  # type: ignore[no-untyped-def]
    """Without acceptance_criteria, the classification prompt does NOT
    contain per-criterion language and the outcome is step-based."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture

    plan_response = json.dumps([{"description": "Step 1"}, {"description": "Step 2"}])

    captured_prompts: list[str] = []

    async def tracking_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        # Support both ``query(prompt=..., ...)`` and the positional
        # ``query_on_session(session, prompt, ...)`` shapes so the test
        # exercises both dispatch paths reliably regardless of session-reuse
        # config defaults.
        prompt = kwargs.get("prompt") or (args[1] if len(args) >= 2 else "")
        captured_prompts.append(prompt)
        return MockQueryResult(response="Key findings here")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=tracking_query), \
         patch(f"{_IMPL}.query", side_effect=tracking_query), \
         patch("maestro.agent.query_on_session", side_effect=tracking_query):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")

        record = await agent.run("Test goal")

    # No criteria → step-based outcome (all steps passed)
    assert record.outcome == "success"
    # The criteria-assessment branch is skipped, so no verdicts are persisted.
    assert record.criteria_verdicts is None
    # Verify NO criteria-assessment prompt was issued. Anchor on the
    # distinctive sentinel from ``CRITERIA_ASSESSMENT_PROMPT`` rather than
    # the bare substring ``MET``/``UNMET`` — those would also match
    # incidental occurrences (PARAMETER, METHODOLOGY, etc.) in unrelated
    # prompts and silently break this test for orthogonal reasons.
    criteria_sentinel = "Assess each acceptance criterion for this completed task."
    for prompt in captured_prompts:
        assert "acceptance criteria" not in prompt.lower()
        assert criteria_sentinel not in prompt


@pytest.mark.asyncio
async def test_no_criteria_assertion_robust_to_orthogonal_prompt_edits(  # type: ignore[no-untyped-def]
    make_agent,
):
    """Regression: the no-criteria assertion must not break when a
    behaviour-neutral prompt edit happens to introduce a common uppercase
    word that contains the substring ``MET`` (e.g. PARAMETER, METHODOLOGY).
    Anchoring on the distinctive CRITERIA_ASSESSMENT_PROMPT sentinel keeps
    the assertion focused on the criteria-gate behaviour under test."""
    import maestro.prompts.defaults as defaults

    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture
    plan_response = json.dumps([{"description": "Step 1"}])

    captured_prompts: list[str] = []

    async def tracking_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        prompt = kwargs.get("prompt") or (args[1] if len(args) >= 2 else "")
        captured_prompts.append(prompt)
        return MockQueryResult(response="Key findings here")

    patched_prompt = (
        defaults.FINDINGS_DISTILLATION_PROMPT
        + "\nNote any PARAMETER changes."
    )

    with patch("maestro.agent.FINDINGS_DISTILLATION_PROMPT", patched_prompt), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=tracking_query), \
         patch("maestro.agent.query_on_session", side_effect=tracking_query):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")

        record = await agent.run("Test goal")

    # Criteria-gate behaviour unchanged.
    assert record.outcome == "success"

    # Sentinel-based check is immune to incidental ``MET`` substrings in
    # narrative prompt text (PARAMETER, METHODOLOGY, METRICS, ...).
    criteria_sentinel = "Assess each acceptance criterion for this completed task."
    for prompt in captured_prompts:
        assert criteria_sentinel not in prompt


@pytest.mark.asyncio
async def test_outcome_empty_criteria_list_uses_existing_path(make_agent):  # type: ignore[no-untyped-def]
    """An explicit empty list ``[]`` skips criteria assessment entirely,
    exercising the early-return guard in _assess_criteria_outcome."""
    agent = make_agent()

    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_aq, \
         patch(f"{_IMPL}.query", new_callable=AsyncMock), \
         patch("maestro.agent.query_on_session", new_callable=AsyncMock) as mock_qs:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        mock_aq.return_value = MockQueryResult(response="Key findings here")
        mock_qs.return_value = MockQueryResult(response="Key findings here")

        record = await agent.run(
            "Test goal", acceptance_criteria=[],
        )

    # Empty list → step-based outcome (all steps passed)
    assert record.outcome == "success"


@pytest.mark.asyncio
async def test_outcome_no_criteria_met_yields_failure(make_agent):  # type: ignore[no-untyped-def]
    """When zero acceptance criteria are MET, outcome is overridden to failure."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture

    criteria = ["Feature A", "Feature B"]
    plan_response = json.dumps([{"description": "Step 1"}])

    criteria_response = (
        "1. UNMET — Feature A not found\n"
        "2. UNMET — Feature B missing\n"
    )

    async def mock_agent_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        prompt = kwargs.get("prompt") or (args[1] if len(args) >= 2 else "")
        if "acceptance criteria" in prompt.lower():
            return MockQueryResult(response=criteria_response)
        return MockQueryResult(response="Key findings here")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_agent_query), \
         patch(f"{_IMPL}.query", side_effect=mock_agent_query), \
         patch("maestro.agent.query_on_session", side_effect=mock_agent_query):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")

        record = await agent.run(
            "Test goal", acceptance_criteria=criteria,
        )

    # All steps passed (step-based = success) but 0 criteria MET → failure
    assert record.outcome == "failure"


@pytest.mark.asyncio
async def test_outcome_criteria_assessment_error_falls_back(make_agent):  # type: ignore[no-untyped-def]
    """When the criteria assessment LLM call fails (non-SDK), the step-based
    outcome is preserved as fallback."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture

    criteria = ["Feature A"]
    plan_response = json.dumps([{"description": "Step 1"}])

    call_count = 0

    async def mock_agent_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        prompt = kwargs.get("prompt") or (args[1] if len(args) >= 2 else "")
        if "acceptance criteria" in prompt.lower():
            raise ValueError("LLM parsing error")
        return MockQueryResult(response="Key findings here")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_agent_query), \
         patch(f"{_IMPL}.query", side_effect=mock_agent_query), \
         patch("maestro.agent.query_on_session", side_effect=mock_agent_query):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")

        record = await agent.run(
            "Test goal", acceptance_criteria=criteria,
        )

    # Error in assessment → falls back to step-based (success)
    assert record.outcome == "success"


@pytest.mark.asyncio
async def test_outcome_criteria_sdk_error_propagates(make_agent):  # type: ignore[no-untyped-def]
    """When the criteria assessment raises SDKError, it propagates rather
    than being swallowed."""
    from maestro.sdk import ErrorCategory, SDKError

    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture

    criteria = ["Feature A"]
    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_agent_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        prompt = kwargs.get("prompt") or (args[1] if len(args) >= 2 else "")
        if "acceptance criteria" in prompt.lower():
            raise SDKError("stream stall", category=ErrorCategory.STREAM_STALL)
        return MockQueryResult(response="Key findings here")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_agent_query), \
         patch(f"{_IMPL}.query", side_effect=mock_agent_query), \
         patch("maestro.agent.query_on_session", side_effect=mock_agent_query):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")

        with pytest.raises(SDKError):
            await agent.run(
                "Test goal", acceptance_criteria=criteria,
            )


@pytest.mark.asyncio
async def test_assess_criteria_empty_list_returns_step_outcome(make_agent):  # type: ignore[no-untyped-def]
    """Calling _assess_criteria_outcome with an empty list returns the
    step-based outcome unchanged — exercises the internal early return."""
    from maestro.memory import PlanStep

    agent = make_agent()
    outcome, verdicts = await agent._assess_criteria_outcome(
        "partial", [], "goal", [PlanStep(step_id="s1", description="d")],
    )
    assert outcome == "partial"
    # Empty-criteria early return persists no verdict list.
    assert verdicts is None


@pytest.mark.asyncio
async def test_assess_criteria_uses_session_when_provided(make_agent):  # type: ignore[no-untyped-def]
    """When a session is passed, the assessment routes through
    query_on_session rather than bare query."""
    from maestro.memory import PlanStep

    agent = make_agent()
    criteria = ["Feature A"]
    steps = [PlanStep(step_id="s1", description="d", status="done", result="ok")]

    criteria_response = "1. MET — done\n"
    fake_session = MagicMock()

    with patch(f"{_IMPL}.query_on_session", new_callable=AsyncMock) as mock_qs, \
         patch(f"{_IMPL}.query", new_callable=AsyncMock) as mock_q:
        mock_qs.return_value = MockQueryResult(response=criteria_response)

        outcome, verdicts = await agent._assess_criteria_outcome(
            "partial", criteria, "goal", steps, session=fake_session,
        )

    assert outcome == "success"
    # Success path persists one structured verdict per criterion.
    assert verdicts == [
        {"criterion": "Feature A", "verdict": "MET", "evidence": "done"},
    ]
    mock_qs.assert_called_once()
    mock_q.assert_not_called()


@pytest.mark.asyncio
async def test_assess_criteria_mock_supports_positional_session_dispatch(make_agent):  # type: ignore[no-untyped-def]
    """Regression: the kwargs-only ``mock_agent_query(**kwargs)`` shape
    used elsewhere in this module silently TypeError'd when
    ``query_on_session`` was patched (it is invoked positionally as
    ``query_on_session(session, prompt, ...)``), and the broad
    ``except Exception`` in ``_assess_criteria_outcome`` swallowed the
    failure so the assertion compared against the step-based fallback
    rather than the criteria verdict. The fix accepts ``*args, **kwargs``
    so the mock works on either dispatch path."""
    from maestro.memory import PlanStep

    agent = make_agent()
    # Force the session-attached path even though the fixture default has
    # session reuse off — passing ``session=`` directly is enough.
    criteria = [
        "Feature X is implemented",
        "Tests cover all branches",
        "Docs updated",
        "No regressions",
        "Performance within budget",
    ]
    criteria_response = (
        "1. MET ok\n2. MET ok\n3. MET ok\n4. MET ok\n5. MET ok\n"
    )

    async def mock_agent_query(*args, **kwargs):  # type: ignore[no-untyped-def]
        prompt = kwargs.get("prompt") or (args[1] if len(args) >= 2 else "")
        if "acceptance criteri" in prompt.lower():
            return MockQueryResult(response=criteria_response)
        return MockQueryResult(response="other")

    fake_session = MagicMock()
    with patch(f"{_IMPL}.query_on_session", side_effect=mock_agent_query), \
         patch(f"{_IMPL}.query", side_effect=mock_agent_query):
        outcome, verdicts = await agent._assess_criteria_outcome(
            "partial", criteria, "goal",
            steps=[PlanStep(step_id="s1", description="d")],
            session=fake_session,
        )

    # Must be ``success`` (all 5 MET). Would silently degrade to
    # ``partial`` (the step_based_outcome) if the mock raised TypeError
    # under positional dispatch and the broad except swallowed it.
    assert outcome == "success"
    # Verdicts pair positionally with the input criteria and capture the
    # trailing evidence text.
    assert verdicts is not None
    assert [v["verdict"] for v in verdicts] == ["MET"] * 5
    assert [v["criterion"] for v in verdicts] == criteria
    assert all(v["evidence"] == "ok" for v in verdicts)


@pytest.mark.asyncio
async def test_assess_criteria_scales_chars_complete_with_count(make_agent):  # type: ignore[no-untyped-def]
    """The chars_complete budget must scale with criterion count so a
    multi-criterion response with concrete per-line evidence isn't
    truncated before the final verdict — otherwise an all-MET reply
    would be miscounted and downgraded to ``partial``."""
    from maestro.memory import PlanStep
    from maestro.sdk import chars_complete

    agent = make_agent()
    # Five criteria — a realistic worst case from the assessment prompt.
    criteria = [f"Criterion {i}" for i in range(1, 6)]
    steps = [PlanStep(step_id="s1", description="d", status="done", result="ok")]

    # Realistic response with concrete evidence per line (well over 300 chars
    # if rendered as a single buffer).  All five must parse as MET so we
    # detect the success → partial regression when the predicate truncates.
    criteria_response = (
        "1. MET — covered by tests/test_thing.py::test_a verifying the new path\n"
        "2. MET — implemented in maestro/widget.py::do_thing with guard branch\n"
        "3. MET — quality gates pass: ruff, mypy, pytest all clean on CI\n"
        "4. MET — coverage 100% on the changed module per pytest-cov report\n"
        "5. MET — performance within budget; benchmark in tests/bench.py\n"
    )
    fake_session = MagicMock()

    with patch(f"{_IMPL}.query_on_session", new_callable=AsyncMock) as mock_qs:
        mock_qs.return_value = MockQueryResult(response=criteria_response)
        outcome, verdicts = await agent._assess_criteria_outcome(
            "partial", criteria, "goal", steps, session=fake_session,
        )

    assert outcome == "success"
    assert verdicts is not None
    assert [v["verdict"] for v in verdicts] == ["MET"] * 5
    assert [v["criterion"] for v in verdicts] == criteria
    # The completion predicate must comfortably exceed N * 120 chars so a
    # full multi-criterion response is never truncated mid-verdict.
    # ``chars_complete`` is functools-cached, so identity equality is
    # the canonical way to assert the threshold (see sdk.py docstring).
    passed_predicate = mock_qs.call_args.kwargs["complete_on"]
    expected_budget = max(600, 200 * len(criteria))
    assert passed_predicate is chars_complete(expected_budget)
    assert expected_budget >= 600
    assert expected_budget > len(criteria) * 120


def test_criterion_verdict_regex_tolerates_formatting_variants():
    """The verdict regex must match the canonical ``1. MET`` form as well
    as common formatting variants the LLM may emit even when the prompt
    forbids them — bold/italic emphasis, paren-style numbering, list-bullet
    prefixes, and checkbox glyphs.  Silent undercounts here deterministically
    downgrade ``success`` → ``partial``/``failure``, which is exactly the
    false-negative the criteria-anchored outcome gate is meant to eliminate.
    """
    from maestro.agent import MaestroAgent

    pat = MaestroAgent._CRITERION_VERDICT_RE

    # Canonical form (prompt-prescribed) — must match.
    assert pat.findall(
        "1. MET — ev\n2. MET — ev\n3. MET — ev",
    ) == ["MET", "MET", "MET"]

    # Variants the prompt does NOT forbid — must now match.
    assert pat.findall("**1. MET** — ev") == ["MET"]           # bold
    assert pat.findall("1) MET — ev") == ["MET"]                # paren
    assert pat.findall("- 1. MET — ev") == ["MET"]              # list bullet
    assert pat.findall("[x] 1. MET — ev") == ["MET"]            # checkbox

    # Casing variants — the prompt prescribes upper case but the parser
    # must tolerate the more frequent casing variance the LLM may emit.
    assert pat.findall("1. Met — ev") == ["Met"]                # title case
    assert pat.findall("1. met — ev") == ["met"]                # lower case
    assert pat.findall("1. unmet — ev") == ["unmet"]            # lower UNMET

    # Mixed-case run of 3 criteria — must recover all three.
    assert pat.findall(
        "1. Met — ev\n2. met — ev\n3. MET — ev",
    ) == ["Met", "met", "MET"]

    # Mixed run of 3 criteria where the LLM bolds the verdict lines.
    # Expected: all 3 MET; previously observed 0 → outcome wrongly
    # downgraded to ``failure``.
    resp = "**1. MET** — ev\n**2. MET** — ev\n**3. MET** — ev"
    verdicts = pat.findall(resp)
    met = sum(1 for v in verdicts if v == "MET")
    total = 3
    assert met == total

    # UNMET verdicts must continue to be matched alongside MET.
    assert pat.findall("1. MET — a\n2. UNMET — b") == ["MET", "UNMET"]


@pytest.mark.asyncio
async def test_assess_criteria_tolerates_bolded_verdicts(make_agent):  # type: ignore[no-untyped-def]
    """When the LLM emits Markdown-bolded verdict lines, the assessment
    still recognises every MET line and returns ``success`` instead of
    silently downgrading to ``failure``.
    """
    from maestro.memory import PlanStep

    agent = make_agent()
    criteria = ["Feature A", "Feature B", "Feature C"]
    steps = [PlanStep(step_id="s1", description="d", status="done", result="ok")]

    criteria_response = (
        "**1. MET** — covered by tests/test_thing.py::test_a\n"
        "**2. MET** — implemented in maestro/widget.py\n"
        "**3. MET** — quality gates pass on CI\n"
    )
    fake_session = MagicMock()

    with patch(f"{_IMPL}.query_on_session", new_callable=AsyncMock) as mock_qs:
        mock_qs.return_value = MockQueryResult(response=criteria_response)
        outcome, verdicts = await agent._assess_criteria_outcome(
            "partial", criteria, "goal", steps, session=fake_session,
        )

    assert outcome == "success"
    # Bold markup is stripped from both the verdict and the evidence text.
    assert verdicts is not None
    assert [v["verdict"] for v in verdicts] == ["MET", "MET", "MET"]
    assert verdicts[0]["evidence"] == "covered by tests/test_thing.py::test_a"


@pytest.mark.asyncio
async def test_assess_criteria_falls_back_on_parse_undercount(make_agent):  # type: ignore[no-untyped-def]
    """If the verdict parser fails to recover one line per criterion the
    LLM's formatting has drifted far enough that the count is untrustworthy.
    The assessment must conservatively fall back to the step-based outcome
    rather than silently downgrade ``success`` to ``partial``/``failure``.
    """
    from maestro.memory import PlanStep

    agent = make_agent()
    criteria = ["Feature A", "Feature B", "Feature C"]
    steps = [PlanStep(step_id="s1", description="d", status="done", result="ok")]

    # Only two verdict lines for three criteria — exotic formatting on the
    # third line (e.g. ``Criterion 3: MET``) is not matched by the regex.
    criteria_response = (
        "1. MET — ev\n"
        "2. MET — ev\n"
        "Criterion 3: MET — ev\n"
    )
    fake_session = MagicMock()

    with patch(f"{_IMPL}.query_on_session", new_callable=AsyncMock) as mock_qs:
        mock_qs.return_value = MockQueryResult(response=criteria_response)
        outcome, verdicts = await agent._assess_criteria_outcome(
            "partial", criteria, "goal", steps, session=fake_session,
        )

    # Parse incomplete → fall back to the step-based outcome unchanged.
    assert outcome == "partial"
    # A parse-undercount fallback persists no verdict list.
    assert verdicts is None


@pytest.mark.asyncio
async def test_assess_criteria_tolerates_lowercase_verdicts(make_agent):  # type: ignore[no-untyped-def]
    """When the LLM emits lowercase/mixed-case verdict lines, the assessment
    still recognises every MET line and returns ``success`` instead of
    silently downgrading to the step-based outcome via a parse undercount.
    """
    from maestro.memory import PlanStep

    agent = make_agent()
    criteria = ["Feature A", "Feature B", "Feature C"]
    steps = [PlanStep(step_id="s1", description="d", status="done", result="ok")]

    criteria_response = (
        "1. Met — covered by tests/test_thing.py::test_a\n"
        "2. met — implemented in maestro/widget.py\n"
        "3. MET — quality gates pass on CI\n"
    )
    fake_session = MagicMock()

    with patch(f"{_IMPL}.query_on_session", new_callable=AsyncMock) as mock_qs:
        mock_qs.return_value = MockQueryResult(response=criteria_response)
        outcome, verdicts = await agent._assess_criteria_outcome(
            "partial", criteria, "goal", steps, session=fake_session,
        )

    assert outcome == "success"
    # Persisted verdict tokens are canonicalised to upper case regardless of
    # the casing the LLM emitted.
    assert verdicts is not None
    assert [v["verdict"] for v in verdicts] == ["MET", "MET", "MET"]
    assert verdicts[0]["evidence"] == "covered by tests/test_thing.py::test_a"


@pytest.mark.asyncio
async def test_assess_criteria_lowercase_mixed_met_unmet(make_agent):  # type: ignore[no-untyped-def]
    """A lowercase reply mixing met and unmet verdicts must tally correctly:
    some-but-not-all MET → ``partial``, never silently downgraded by a case
    mismatch in the verdict token.
    """
    from maestro.memory import PlanStep

    agent = make_agent()
    criteria = ["Feature A", "Feature B", "Feature C"]
    steps = [PlanStep(step_id="s1", description="d", status="done", result="ok")]

    criteria_response = (
        "1. met — covered by tests/test_thing.py::test_a\n"
        "2. unmet — not yet implemented\n"
        "3. Met — quality gates pass on CI\n"
    )
    fake_session = MagicMock()

    with patch(f"{_IMPL}.query_on_session", new_callable=AsyncMock) as mock_qs:
        mock_qs.return_value = MockQueryResult(response=criteria_response)
        outcome, verdicts = await agent._assess_criteria_outcome(
            "success", criteria, "goal", steps, session=fake_session,
        )

    assert outcome == "partial"
    # A complete parse persists the verdict list even when the outcome is
    # ``partial`` (some UNMET) — distinct from a fallback, which persists None.
    assert verdicts is not None
    assert [v["verdict"] for v in verdicts] == ["MET", "UNMET", "MET"]
    assert verdicts[1]["evidence"] == "not yet implemented"


# ──────────────────────────────────────────────────────────────────────
# API-error outcome guard tests
# ──────────────────────────────────────────────────────────────────────


class TestApiErrorOutcomeGuard:
    """Tests for _is_api_error_finding() and its wiring in the outcome path."""

    def test_api_error_500_forces_failure(self) -> None:
        from maestro.loop.task_outcome import _is_api_error_finding

        finding = "API Error: 500 Internal server error. This is a server-side issue..."
        assert _is_api_error_finding(finding) is True

    def test_error_prefix_forces_failure(self) -> None:
        from maestro.loop.task_outcome import _is_api_error_finding

        finding = "Error: connection reset by peer"
        assert _is_api_error_finding(finding) is True

    def test_successful_output_not_flagged(self) -> None:
        from maestro.loop.task_outcome import _is_api_error_finding

        finding = "Successfully refactored module X with 3 files changed"
        assert _is_api_error_finding(finding) is False

    def test_midtext_mention_not_flagged(self) -> None:
        from maestro.loop.task_outcome import _is_api_error_finding

        finding = "Analysis found that API Error: 500 occurs in production logs when..."
        assert _is_api_error_finding(finding) is False

    def test_http_error_prefix(self) -> None:
        from maestro.loop.task_outcome import _is_api_error_finding

        finding = "HTTPError: 503 Service Unavailable"
        assert _is_api_error_finding(finding) is True

    def test_connection_error_prefix(self) -> None:
        from maestro.loop.task_outcome import _is_api_error_finding

        finding = "ConnectionError: failed to establish connection to host"
        assert _is_api_error_finding(finding) is True

    def test_timeout_error_prefix(self) -> None:
        from maestro.loop.task_outcome import _is_api_error_finding

        finding = "TimeoutError: read timed out after 30s"
        assert _is_api_error_finding(finding) is True

    def test_whitespace_stripped_before_check(self) -> None:
        from maestro.loop.task_outcome import _is_api_error_finding

        finding = "  \n  API Error: 429 Rate limited"
        assert _is_api_error_finding(finding) is True

    def test_empty_string_not_flagged(self) -> None:
        from maestro.loop.task_outcome import _is_api_error_finding

        assert _is_api_error_finding("") is False

    def test_multiline_error_prefix_not_flagged(self) -> None:
        """Multi-line step output starting with 'Error:' is legitimate
        narrative analysis — the bare prefix must not force a downgrade."""
        from maestro.loop.task_outcome import _is_api_error_finding

        finding = (
            "Error: file not found while parsing config — adding default "
            "and continuing.\n"
            "Then proceeded to validate the remaining inputs and "
            "successfully completed the step."
        )
        assert _is_api_error_finding(finding) is False

    def test_long_singleline_error_prefix_not_flagged(self) -> None:
        """A long single-line output that begins with 'Error:' but is
        clearly narrative (above the length cap) is not an error
        silhouette and must not force a downgrade."""
        from maestro.loop.task_outcome import _is_api_error_finding

        finding = "Error: " + "x " * 200
        assert _is_api_error_finding(finding) is False

    def test_short_singleline_error_prefix_still_flagged(self) -> None:
        """A short, single-line output starting with 'Error:' remains
        the canonical error silhouette and is still flagged."""
        from maestro.loop.task_outcome import _is_api_error_finding

        finding = "Error: stale lock detected"
        assert _is_api_error_finding(finding) is True

    def test_specific_prefixes_still_flagged_when_multiline(self) -> None:
        """The narrow transport-shaped prefixes (API Error:, HTTPError: …)
        remain unconditional — multi-line content does not relax them."""
        from maestro.loop.task_outcome import _is_api_error_finding

        finding = "API Error: 500 Internal server error\nRetry suggested."
        assert _is_api_error_finding(finding) is True


@pytest.mark.asyncio
async def test_api_error_in_step_output_forces_failure(make_agent):  # type: ignore[no-untyped-def]
    """Integration: when a 'successful' step's output starts with an
    API/transport error prefix, the run-level outcome is forced to
    'failure' before any criteria-based classification runs.

    Exercises the wiring at agent.py:_API_ERROR_PREFIXES guard path
    (the warning-log + outcome assignment + break block)."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture

    plan_response = json.dumps([{"description": "Step 1"}])
    error_output = "API Error: 500 Internal server error"

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_q, \
         patch(
             "maestro.agent.query_on_session", new_callable=AsyncMock,
         ) as mock_qs:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=error_output)
        mock_q.return_value = MockQueryResult(response="Key findings here")
        mock_qs.return_value = MockQueryResult(response="Key findings here")

        record = await agent.run("Test goal")

    # Even though step succeeded (step-based outcome = success), the API
    # error prefix in the output forces the outcome to failure.
    assert record.outcome == "failure"


@pytest.mark.asyncio
async def test_api_error_with_criteria_still_forces_failure(make_agent):  # type: ignore[no-untyped-def]
    """Sibling to test_api_error_in_step_output_forces_failure: when
    ``acceptance_criteria`` is populated, the criteria-anchored
    assessment must not overturn the API-error guard's ``failure``
    outcome.  Exercises the criteria-present branch at
    ``agent.py:_assess_criteria_outcome`` invocation so the guard's
    decision is preserved end-to-end."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture

    plan_response = json.dumps([{"description": "Step 1"}])
    error_output = "API Error: 500 Internal server error"
    # LLM marks single criterion MET — would overturn failure to success
    # if criteria assessment were allowed to run after the guard fires.
    criteria_assessment_response = "1. MET — looks good"

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_q, \
         patch(
             "maestro.agent.query_on_session", new_callable=AsyncMock,
         ) as mock_qs:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=error_output)
        mock_q.return_value = MockQueryResult(
            response=criteria_assessment_response,
        )
        mock_qs.return_value = MockQueryResult(
            response=criteria_assessment_response,
        )

        record = await agent.run(
            "Test goal",
            acceptance_criteria=["The thing must work"],
        )

    # API error must stay as failure even with criteria assessment.
    assert record.outcome == "failure"


# ──────────────────────────────────────────────────────────────────────
# Remediation repetition gate — outcome recording in _run_task_phase
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_task_phase_records_noop_outcome(
    make_agent, mem_fs,
):  # type: ignore[no-untyped-def]
    """When a PR remediation task completes with a no-op summary, the
    tracker records it so future re-routes are gated."""
    agent = make_agent()

    # Plant a PR remediation request
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #42: Fix lint",
            pr_branch="feat/branch",
        ),
    ]
    agent.consciousness.save_self_model(model)

    # Mock run() to return a TaskRecord with a no-op summary
    noop_record = TaskRecord(
        task_id="abc123",
        goal="Remediate PR #42: Fix lint",
        plan=[],
        outcome="success",
        result_summary="Quality gates already pass on this branch",
        errors="",
        duration_secs=10.0,
        timestamp="2025-01-01T00:00:00Z",
        source="external",
    )
    # The escalation that fires once the gate trips makes its own VCS/notify
    # calls — neutralise it so this test stays focused on tracker recording.
    agent._escalate_stuck_pr = AsyncMock()  # type: ignore[method-assign]
    with patch.object(agent, "run", AsyncMock(return_value=noop_record)):
        await agent._run_task_phase()

    # Verify the tracker recorded a no-op
    assert agent.remediation_tracker.should_reroute(42) is True  # 1 noop, threshold 2

    # Record a second no-op by running again
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #42: Fix lint again",
            pr_branch="feat/branch",
        ),
    ]
    agent.consciousness.save_self_model(model)
    with patch.object(agent, "run", AsyncMock(return_value=noop_record)):
        await agent._run_task_phase()

    # Now the gate should block, and the stuck PR is escalated. No items
    # context this lap → ``items_remaining=None`` (git merge-block CTA) (#2744).
    assert agent.remediation_tracker.should_reroute(42) is False
    agent._escalate_stuck_pr.assert_awaited_once_with(
        42, "feat/branch", items_remaining=None,
    )


@pytest.mark.asyncio
async def test_run_task_phase_records_productive_outcome(
    make_agent, mem_fs,
):  # type: ignore[no-untyped-def]
    """A productive remediation does not block future re-routes."""
    agent = make_agent()

    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #99: Fix mypy",
            pr_branch="feat/branch",
        ),
    ]
    agent.consciousness.save_self_model(model)

    productive_record = TaskRecord(
        task_id="def456",
        goal="Remediate PR #99: Fix mypy",
        plan=[],
        outcome="success",
        result_summary="Fixed 3 mypy errors in agent.py",
        errors="",
        duration_secs=15.0,
        timestamp="2025-01-01T00:00:00Z",
        source="external",
    )
    # The pre-execution merge-block and stand-down gates are exercised
    # separately; neutralise them so their head-resolution does not consume the
    # ``rev_parse`` side_effect sized for the post-run HEAD-advance check.
    agent._gate_pr_remediation_merge_block = (  # type: ignore[method-assign]
        AsyncMock(return_value=False)
    )
    agent._gate_pr_remediation_stood_down = (  # type: ignore[method-assign]
        AsyncMock(return_value=False)
    )
    # Three ``rev_parse`` consumers in order: the owned-relap divert's head
    # resolution (#2864; discarded here — an empty history diverts nothing), then
    # the post-run HEAD-advance check's before/after pair. The branch HEAD
    # advances between the latter two — a real commit was made — so the outcome
    # is a non-no-op even before the free-text classifier sees the summary.
    agent._vcs.rev_parse = MagicMock(
        side_effect=["origin-head", "before", "after"],
    )
    with patch.object(agent, "run", AsyncMock(return_value=productive_record)):
        await agent._run_task_phase()

    # Productive outcome should not block
    assert agent.remediation_tracker.should_reroute(99) is True
    assert agent.remediation_tracker._data["99"] == [False]


@pytest.mark.asyncio
async def test_run_task_phase_non_pr_task_no_tracking(
    make_agent, mem_fs,
):  # type: ignore[no-untyped-def]
    """Non-PR tasks (from queue) do not record remediation outcomes."""
    agent = make_agent()

    # Plant a normal queued task (not a PR remediation)
    from maestro.task_queue import QueuedTask
    agent.task_queue.save([QueuedTask(goal="Refactor planner module")])

    normal_record = TaskRecord(
        task_id="ghi789",
        goal="Refactor planner module",
        plan=[],
        outcome="success",
        result_summary="Refactored successfully",
        errors="",
        duration_secs=20.0,
        timestamp="2025-01-01T00:00:00Z",
        source="self_directed",
    )
    with patch.object(agent, "run", AsyncMock(return_value=normal_record)):
        await agent._run_task_phase()

    # No remediation tracking should happen — tracker stays empty
    assert agent.remediation_tracker._data == {}


# ──────────────────────────────────────────────────────────────────────
# Outcome prediction tracker — _run_task_phase wiring
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_task_phase_records_prediction(
    make_agent, mem_fs,
):  # type: ignore[no-untyped-def]
    """_run_task_phase records a prediction for queued tasks."""
    agent = make_agent()

    from maestro.task_queue import QueuedTask
    agent.task_queue.save([QueuedTask(goal="Measure audit rate")])

    record = TaskRecord(
        task_id="pred_001",
        goal="Measure audit rate",
        plan=[],
        outcome="success",
        result_summary="Measured successfully",
        errors="",
        duration_secs=10.0,
        timestamp="2025-01-01T00:00:00Z",
        source="self_directed",
    )
    with patch.object(agent, "run", AsyncMock(return_value=record)):
        await agent._run_task_phase()

    # Verify predictions.jsonl contains the record
    pred_path = str(agent.config.memory_dir / "predictions.jsonl")
    assert pred_path in mem_fs
    lines = mem_fs[pred_path].strip().splitlines()
    assert len(lines) == 1
    pred = json.loads(lines[0])
    assert pred["task_id"] == "pred_001"
    assert pred["predicted_outcome"] in get_args(TaskOutcome)
