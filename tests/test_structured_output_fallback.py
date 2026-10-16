"""#4535: schema callers route ``STRUCTURED_OUTPUT_INVALID`` into their
existing unparseable-reply branch via ``structured_output_fallback``; every
other ``SDKError`` category still propagates."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from maestro.memory import PlanStep
from maestro.planner import Planner
from maestro.sdk import ErrorCategory, SDKError, structured_output_fallback


def _exhausted(caller: str) -> SDKError:
    return SDKError(
        "schema exhausted",
        category=ErrorCategory.STRUCTURED_OUTPUT_INVALID,
        caller=caller,
    )


def test_fallback_returns_empty_result_for_exhaustion() -> None:
    result = structured_output_fallback(_exhausted("planner"))
    assert result.response == ""


def test_fallback_reraises_other_category() -> None:
    err = SDKError("boom", category=ErrorCategory.UNKNOWN, caller="planner")
    with pytest.raises(SDKError) as ei:
        structured_output_fallback(err)
    assert ei.value is err


@pytest.mark.asyncio
async def test_decompose_falls_back_to_single_step() -> None:
    with patch(
        "maestro.planner.query", new_callable=AsyncMock,
        side_effect=_exhausted("planner"),
    ):
        steps = await Planner().decompose("Simple goal", "ctx")
    assert len(steps) == 1
    assert steps[0].description == "Simple goal"


@pytest.mark.asyncio
async def test_replan_returns_no_steps() -> None:
    failed = PlanStep(step_id="s1", description="Do it", status="failed")
    with patch(
        "maestro.planner.query", new_callable=AsyncMock,
        side_effect=_exhausted("replan"),
    ):
        steps = await Planner().replan("Goal", "ctx", [], failed)
    assert steps == []


@pytest.mark.asyncio
async def test_decompose_other_category_propagates() -> None:
    err = SDKError("boom", category=ErrorCategory.UNKNOWN, caller="planner")
    with patch(
        "maestro.planner.query", new_callable=AsyncMock, side_effect=err,
    ), pytest.raises(SDKError) as ei:
        await Planner().decompose("Goal", "ctx")
    assert ei.value is err


