"""Unit test for ``loop.task_outcome.classify_task_outcome``.

The stage is the verbatim lift of ``run``'s ``# 4.`` + ``# 4a.`` + ``# 4b.``
outcome-classification blocks (proposal 106): before any PR-remediation stamping
it answers one question — what is this task's outcome verdict? ``# 4.`` derives
the base verdict from the step pass/fail/skip counts (empty plan or
all-failed/skipped → ``failure``; none failed or skipped → ``success``;
otherwise ``partial``). ``# 4a.`` is the zero-SDK API-error guard, which forces
``failure`` when a "successful" step output is actually a raw transport error.
``# 4b.`` is the criteria-anchored override, which gates the final outcome on
per-criterion MET/UNMET assessment — deliberately skipped when the guard already
tripped, so an error-message step output cannot overturn it.

These tests call the stage directly, agent-first-positional, on a bare ``make_agent``
agent (no ``agent.run()``, no SDK patching), driving
``agent._assess_criteria_outcome`` with an ``AsyncMock`` so every guard arm is
pinned for 100 % coverage on its own. The full-``run`` regression harness
exercises the same blocks end-to-end and must pass unchanged — in particular the
base step-count mapping (``tests/test_agent.py``: ``test_run_success``,
``test_run_step_failure``, ``test_run_partial_failure``,
``test_run_empty_plan_returns_failure``), the end-to-end API-error guard and
criteria override (``tests/test_task_outcome_criteria.py``), and the criteria
forwarding through ``run()`` (``tests/test_run_task_phase.py``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.loop.task_outcome import classify_task_outcome
from maestro.memory import PlanStep
from maestro.types import StepStatus

_GOAL = "Improve the dashboard status layer"


def _step(status: StepStatus) -> PlanStep:
    """Build a bare ``PlanStep`` — only ``.status`` is read by the classifier."""
    return PlanStep(step_id="s", description="d", status=status)


@pytest.mark.asyncio
async def test_empty_steps_yields_failure_without_override(make_agent):  # type: ignore[no-untyped-def]
    """(a) An empty plan is ``failure`` and never consults the criteria
    override (no criteria, and the base verdict is already ``failure``)."""
    agent = make_agent()
    assess = AsyncMock()
    agent._assess_criteria_outcome = assess  # type: ignore[method-assign]

    outcome, verdicts, failed = await classify_task_outcome(
        agent,
        steps=[], all_output=[], acceptance_criteria=None,
        goal=_GOAL, session=None,
    )

    assert outcome == "failure"
    assert verdicts is None
    assert failed == 0
    assess.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_failed_or_skipped_yields_failure(make_agent):  # type: ignore[no-untyped-def]
    """(b) When every step either failed or was skipped, nothing succeeded —
    the verdict is ``failure`` and the API-error guard loop is skipped because
    the outcome is already ``failure``."""
    agent = make_agent()
    agent._assess_criteria_outcome = AsyncMock()  # type: ignore[method-assign]
    steps = [_step("failed"), _step("pending")]

    outcome, verdicts, failed = await classify_task_outcome(
        agent,
        steps=steps, all_output=["some output"], acceptance_criteria=None,
        goal=_GOAL, session=None,
    )

    assert outcome == "failure"
    assert verdicts is None
    assert failed == 1


@pytest.mark.asyncio
async def test_mixed_pass_and_fail_yields_partial(make_agent):  # type: ignore[no-untyped-def]
    """(c) A mix of passing and failing steps is ``partial``; the API-error
    guard loop runs over clean output without tripping, and with no criteria the
    override is skipped."""
    agent = make_agent()
    assess = AsyncMock()
    agent._assess_criteria_outcome = assess  # type: ignore[method-assign]
    steps = [_step("done"), _step("failed")]

    outcome, verdicts, failed = await classify_task_outcome(
        agent,
        steps=steps, all_output=["all good, no errors"], acceptance_criteria=None,
        goal=_GOAL, session=None,
    )

    assert outcome == "partial"
    assert verdicts is None
    assert failed == 1
    assess.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_passing_no_criteria_yields_success(make_agent):  # type: ignore[no-untyped-def]
    """(d) Every step passing with no acceptance criteria is ``success``; the
    override is never awaited."""
    agent = make_agent()
    assess = AsyncMock()
    agent._assess_criteria_outcome = assess  # type: ignore[method-assign]
    steps = [_step("done"), _step("done")]

    outcome, verdicts, failed = await classify_task_outcome(
        agent,
        steps=steps, all_output=[], acceptance_criteria=None,
        goal=_GOAL, session=None,
    )

    assert outcome == "success"
    assert verdicts is None
    assert failed == 0
    assess.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_error_forces_failure_and_short_circuits_override(make_agent):  # type: ignore[no-untyped-def]
    """(e) An API-error-shaped step output forces ``failure`` even when every
    step passed, and — because the guard tripped — the criteria override is
    short-circuited (never awaited) even though criteria are present."""
    agent = make_agent()
    assess = AsyncMock()
    agent._assess_criteria_outcome = assess  # type: ignore[method-assign]
    steps = [_step("done")]

    outcome, verdicts, failed = await classify_task_outcome(
        agent,
        steps=steps,
        all_output=["API Error: 500 internal server error"],
        acceptance_criteria=["Feature A works"],
        goal=_GOAL,
        session=None,
    )

    assert outcome == "failure"
    assert verdicts is None
    assert failed == 0
    assess.assert_not_awaited()


@pytest.mark.asyncio
async def test_criteria_override_awaited_and_returned(make_agent):  # type: ignore[no-untyped-def]
    """(f)+(g) With criteria present and the guard un-tripped, the override is
    awaited once with the base outcome and the run session, and its
    ``(outcome, verdicts)`` becomes the method's result; ``failed`` reports the
    failing-step count."""
    agent = make_agent()
    verdict_rows = [{"criterion": "Feature A", "verdict": "MET", "evidence": "ok"}]
    assess = AsyncMock(return_value=("success", verdict_rows))
    agent._assess_criteria_outcome = assess  # type: ignore[method-assign]
    steps = [_step("done"), _step("failed")]  # base verdict: partial
    session = MagicMock()

    outcome, verdicts, failed = await classify_task_outcome(
        agent,
        steps=steps,
        all_output=["clean run, nothing wrong"],
        acceptance_criteria=["Feature A"],
        goal=_GOAL,
        session=session,
    )

    assert outcome == "success"
    assert verdicts == verdict_rows
    assert failed == 1
    assess.assert_awaited_once_with(
        "partial", ["Feature A"], _GOAL, steps, session=session,
    )
