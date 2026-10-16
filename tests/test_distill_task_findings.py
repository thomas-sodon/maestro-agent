"""Tests for the extracted ``maestro.loop.task_record.distill_task_findings``.

This is ``run``'s ``# 5b.`` findings-distillation step lifted into one named,
independently-testable stage (proposal 106). After every step group has
executed and the branch is committed, ``run`` distills a short LLM summary of
the task's results onto the ``TaskRecord``. That enrichment is deliberately
best-effort: an ``SDKError`` from the inner ``_distill_findings`` seam must
degrade to ``None`` rather than abort record-construction, because aborting
would permanently discard the completed task's outcome, errors, duration, and
artifacts along with its already-popped queue slot.

The extraction is behaviour-preserving, so these tests pin both arms of the
method directly — the pass-through that returns the distilled value and
forwards the ``session=`` kwarg unchanged, and the ``except SDKError`` degrade
to ``None`` — the way ``tests/test_apply_remediation_narration_framing.py``
pins its sibling. No ``agent.run`` and no module-level SDK patching: the inner
``_distill_findings`` seam is stubbed with an ``AsyncMock`` and the method is
called directly on a bare ``make_agent()`` agent, which is what the extraction
bought. The degrade arm is parametrized over a transient and a non-transient
``ErrorCategory`` to pin the deliberately category-blind contract.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from maestro.loop.task_record import distill_task_findings
from maestro.memory import PlanStep
from maestro.sdk import ErrorCategory, SDKError


def _steps() -> list[PlanStep]:
    """Two executed steps — the shape ``_distill_findings`` reads."""
    return [
        PlanStep(step_id="1", description="First", status="completed", result="ok"),
        PlanStep(step_id="2", description="Second", status="completed", result="go"),
    ]


@pytest.mark.asyncio
async def test_distill_task_findings_returns_value_and_forwards_session(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Pass-through: the distilled value is returned and ``session=`` forwarded.

    The ``session=run_session`` forwarding is the load-bearing invariant the
    session-lifecycle regression harness asserts end-to-end; pinning it here
    guards it directly at the seam.
    """
    agent = make_agent()
    agent._distill_findings = AsyncMock(  # type: ignore[method-assign]
        return_value="findings",
    )
    steps = _steps()
    sentinel_session = object()

    result = await distill_task_findings(
        agent,
        goal="goal", steps=steps, outcome="success",
        run_session=sentinel_session,  # type: ignore[arg-type]
    )

    assert result == "findings"
    agent._distill_findings.assert_awaited_once_with(
        "goal", steps, "success", session=sentinel_session,
    )


@pytest.mark.parametrize(
    "category", [ErrorCategory.RATE_LIMIT, ErrorCategory.UNKNOWN],
)
@pytest.mark.asyncio
async def test_distill_task_findings_degrades_to_none_on_sdk_error(
    make_agent,  # type: ignore[no-untyped-def]
    category: ErrorCategory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Degrade: an ``SDKError`` returns ``None`` and warns, category-blind.

    Parametrized over a transient (RATE_LIMIT) and a non-transient (UNKNOWN)
    category because the catch is deliberately category-blind — the record must
    survive regardless of why the cosmetic enrichment failed. The fault is not
    swallowed for good: ``_distill_findings`` keeps its own fail-loud
    ``except SDKError: raise`` for its direct callers, so a persistent fault
    re-surfaces at the next SDK call.
    """
    agent = make_agent()
    agent._distill_findings = AsyncMock(  # type: ignore[method-assign]
        side_effect=SDKError("distill stalled", category=category),
    )

    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = await distill_task_findings(
            agent,
            goal="goal", steps=_steps(), outcome="partial", run_session=None,
        )

    assert result is None
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "Findings distillation failed" in r.getMessage()
    ]
    assert len(warnings) == 1
