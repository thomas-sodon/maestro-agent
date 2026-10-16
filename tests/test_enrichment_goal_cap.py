"""Post-run enrichment prompts bound the persisted task goal (#4388).

``TaskRecord.goal`` is persisted uncapped, so ``_distill_findings`` and
``_assess_criteria_outcome`` must render it through ``cap_utf8`` at the
``MAX_TASK_GOAL_BYTES`` ceiling (``maestro/prompt_caps.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.memory import PlanStep
from maestro.prompt_caps import MAX_TASK_GOAL_BYTES, TRUNCATED_SENTINEL, cap_utf8
from tests.conftest import MockQueryResult

_LONG_GOAL = "é" * 3000
_STEPS = [PlanStep(step_id="s1", description="Do it", status="done", result="ok")]


async def _distill(agent: Any, goal: str, session: Any) -> None:
    await agent._distill_findings(goal, _STEPS, "success", session=session)


async def _assess(agent: Any, goal: str, session: Any) -> None:
    await agent._assess_criteria_outcome(
        "success", ["It works"], goal, _STEPS, session=session,
    )


def _module_of(call: Any) -> str:
    """``_distill_findings`` stays on the agent; ``_assess_criteria_outcome`` moved (proposal 147)."""
    return "maestro.agent" if call is _distill else "maestro.loop.implementation"


def _captured_prompt(mock: AsyncMock, session: Any) -> str:
    if session is None:
        return str(mock.call_args.kwargs["prompt"])
    return str(mock.call_args.args[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [_distill, _assess])
@pytest.mark.parametrize("use_session", [False, True])
async def test_enrichment_prompt_caps_goal_bytes(make_agent, call, use_session) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    session = MagicMock() if use_session else None
    name = "query_on_session" if use_session else "query"
    target = f"{_module_of(call)}.{name}"
    with patch(target, new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="1. MET — done")
        await call(agent, _LONG_GOAL, session)

    prompt = _captured_prompt(mock_q, session)
    capped = cap_utf8(_LONG_GOAL, MAX_TASK_GOAL_BYTES)
    assert capped.endswith(TRUNCATED_SENTINEL)
    assert capped in prompt
    assert _LONG_GOAL not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [_distill, _assess])
async def test_short_goal_passes_through_unchanged(make_agent, call) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    with patch(f"{_module_of(call)}.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="1. MET — done")
        await call(agent, "Fix parser bugs", None)

    prompt = _captured_prompt(mock_q, None)
    assert "Fix parser bugs" in prompt
    assert TRUNCATED_SENTINEL not in prompt


def test_enrichment_templates_render_capped_goal() -> None:
    """Every ``.format`` of the two templates passes ``goal=cap_utf8(...)``."""
    root = Path(__file__).resolve().parents[1] / "maestro"
    trees = [
        ast.parse((root / rel).read_text(encoding="utf-8"))
        for rel in ("agent.py", "loop/implementation.py")
    ]
    templates = {"FINDINGS_DISTILLATION_PROMPT", "CRITERIA_ASSESSMENT_PROMPT"}
    seen: list[str] = []
    for node in (n for tree in trees for n in ast.walk(tree)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in templates
        ):
            continue
        seen.append(node.func.value.id)
        goal_kw = next(kw for kw in node.keywords if kw.arg == "goal")
        assert isinstance(goal_kw.value, ast.Call)
        assert isinstance(goal_kw.value.func, ast.Name)
        assert goal_kw.value.func.id == "cap_utf8"
    assert sorted(seen) == sorted(templates)
