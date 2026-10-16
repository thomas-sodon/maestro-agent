"""Regression tests for #4585: criteria-verdict evidence alignment.

The evidence regex's separator once admitted ``\\n``, so an evidence-less
verdict line swallowed the next line and shifted every later evidence
string by one criterion.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.agent import MaestroAgent
from maestro.memory import PlanStep
from tests.conftest import MockQueryResult

_DRIFT_CASES = [
    ("1. MET\n2. UNMET — missing test\n3. MET — ok", ["", "missing test", "ok"]),
    ("1. MET —\n2. UNMET — x\n3. MET — y", ["", "x", "y"]),
    ("1. MET — a\n2. UNMET\n\n3. MET — c", ["a", "", "c"]),
    ("1. MET — a\n2. UNMET — b\n3. MET — c", ["a", "b", "c"]),
    ("1. MET — a\r\n2. UNMET — b\r\n3. MET — c\r\n", ["a", "b", "c"]),
    ("1. MET\u00a0— a\n2. UNMET\u202f— b\n3. MET — c", ["a", "b", "c"]),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "expected"), _DRIFT_CASES)
async def test_assess_criteria_evidence_aligned_when_verdict_line_has_no_evidence(  # type: ignore[no-untyped-def]
    make_agent, response, expected,
):
    agent = make_agent()
    steps = [PlanStep(step_id="s1", description="d", status="done", result="ok")]
    with patch("maestro.loop.implementation.query_on_session", new_callable=AsyncMock) as mock_qs:
        mock_qs.return_value = MockQueryResult(response=response)
        outcome, verdicts = await agent._assess_criteria_outcome(
            "success", ["A", "B", "C"], "goal", steps, session=MagicMock(),
        )
    assert outcome == "partial"
    assert verdicts is not None
    assert [v["verdict"] for v in verdicts] == ["MET", "UNMET", "MET"]
    assert [v["evidence"] for v in verdicts] == expected


_VARIANTS = [
    "1. MET — ev\n2. MET — ev\n3. MET — ev",
    "**1. MET** — ev",
    "1) MET — ev",
    "- 1. MET — ev",
    "[x] 1. MET — ev",
    "1. Met — ev",
    "1. met — ev",
    "1. unmet — ev",
    "1. Met — a\n2. UNMET — b\n3. met — c",
    "1. UNMET — ev",
] + [case for case, _ in _DRIFT_CASES]


@pytest.mark.parametrize("text", _VARIANTS)
def test_criterion_evidence_regex_match_count_equals_verdict_regex(text: str) -> None:
    evidence = MaestroAgent._CRITERION_VERDICT_EVIDENCE_RE.findall(text)
    verdicts = MaestroAgent._CRITERION_VERDICT_RE.findall(text)
    assert len(evidence) == len(verdicts)
    assert [m[0] for m in evidence] == verdicts
