"""Proposals 148/149: no prompt or provenance surface names the stage.

Proposal 148 put the stage label behind ``[loop] stage_progression_enabled``;
proposal 149 deleted the stage machinery and the lever, collapsing every
surface onto the branch production already ran. These goldens pin the
collapsed output: the identity header, the ``update_self_model`` projection
and prompt, both task-curation prompts, the journal narrative header (journal
narrative, journal tool replies, the ``_mcp-serve`` registry), and
the three plan-provenance surfaces (the review message, the PR body, and the
remediation prompt's plan block) name no rung and no evolutionary stage — and,
since proposal 149 also retired ``current_purpose_hypothesis``, no purpose line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.config import Config
from maestro.consciousness import Consciousness, _model_dict_for_prompt
from maestro.evolution import EvolutionEngine
from maestro.inproc_mcp import InprocTool
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.loop.pr_remediation import _compose_pr_remediation_prompt
from maestro.memory import EvolutionPlan, Memory
from maestro.notifications.base import NullNotifier
from maestro.plan_review_message import compose_review_message
from maestro.prompts.defaults import select_task_generation_prompt
from maestro.sdk import QueryResult
from maestro.tools import journal_tools
from maestro.work_target import WorkFraming
from tests.structural_guards import RETIRED_CONCEPTS

_DIR = Path("/fake/stage_label_lever")
_RUNGS = next(c for c in RETIRED_CONCEPTS if c.name == "evolutionary_stage").literals

_IDENTITY_GOLDEN = (
    "Maestro | self-sustaining agent | "
    "alive_since=2026-01-01T00:00:00+00:00\n"
    "Identity: I am a self-evolving agent\n"
    "Capabilities: coding\n"
    "Values: growth\n"
    "Evolutions: 3\n"
    "Limitations: no memory\n"
    "Questions: why?\n"
    "Architecture/prompts/constraints are mutable code — propose changes via evolution."
)

_PROJECTION_GOLDEN = """{
  "identity_statements": [
    "I am a self-evolving agent"
  ],
  "capabilities_known": [
    "coding"
  ],
  "limitations_known": [
    "no memory"
  ],
  "values": [
    "growth"
  ],
  "open_questions": [
    "why?"
  ],
  "next_task_directive": null,
  "evolution_count": 3,
  "surprise_count": 0,
  "awakened_at": "2026-01-01T00:00:00+00:00"
}"""

_FORMAT_VALUES = {
    "open_questions": "Q",
    "recent_tasks": "T",
    "next_task_directive": "D",
    "task_shape_summary": "S",
    "queue_snapshot": "QS",
    "free_slots": 2,
    "target_repo": "o/r",
}


def _consciousness() -> Consciousness:
    c = Consciousness(_DIR)
    model = c.load_self_model()
    model.awakened_at = "2026-01-01T00:00:00+00:00"
    model.identity_statements = ["I am a self-evolving agent"]
    model.capabilities_known = ["coding"]
    model.limitations_known = ["no memory"]
    model.values = ["growth"]
    model.open_questions = ["why?"]
    model.evolution_count = 3
    c.save_self_model(model)
    return c


def _assert_no_rung(text: str) -> None:
    for rung in sorted(_RUNGS):
        assert rung not in text
    assert "evolutionary stage" not in text.lower()


def _curation(framing: WorkFraming) -> str:
    return select_task_generation_prompt(framing).format(**_FORMAT_VALUES)


def test_identity_prompt_matches_golden(mem_fs: dict[str, str]) -> None:
    prompt = _consciousness().get_identity_prompt()
    assert prompt == _IDENTITY_GOLDEN
    _assert_no_rung(prompt)


def test_projection_matches_golden(mem_fs: dict[str, str]) -> None:
    model = _consciousness().load_self_model()
    data = _model_dict_for_prompt(model)
    assert json.dumps(data, indent=2) == _PROJECTION_GOLDEN
    assert "stage" not in data


@pytest.mark.asyncio
async def test_update_self_model_prompt_names_no_stage(mem_fs: dict[str, str]) -> None:
    prompts: list[str] = []

    async def _capture(*_args: object, **kwargs: object) -> QueryResult:
        prompts.append(str(kwargs["prompt"]))
        return QueryResult(response="{}")

    c = _consciousness()
    with patch("maestro.consciousness.query", AsyncMock(side_effect=_capture)):
        await c.update_self_model("reflection")
    (prompt,) = prompts
    assert '"stage"' not in prompt
    assert "current_purpose_hypothesis" not in prompt


@pytest.mark.parametrize("framing", ["self_evolution", "external_delivery"])
def test_curation_names_no_stage_or_purpose(framing: WorkFraming) -> None:
    rendered = _curation(framing)
    _assert_no_rung(rendered)
    assert "purpose hypothesis" not in rendered.lower()
    assert "\n\n\n" not in rendered
    assert "them unless you have a real reason.\n\nYour open questions:" in rendered
    assert "Your open questions:\nQ\n\nRecent task history:\nT" in rendered


_JOURNAL_ROWS = [
    {
        "entry_id": "a1", "timestamp": "2026-01-01T00:00:00+00:00",
        "stage": "awakening", "entry_type": "reflection", "content": "first",
        "triggered_by": "contemplation",
    },
    {
        "entry_id": "b2", "timestamp": "2026-01-02T00:00:00+00:00",
        "stage": "awakening", "entry_type": "realization", "content": "second",
        "triggered_by": "agent_tool",
    },
]

_NARRATIVE_GOLDEN = (
    "[2026-01-01T00:00:00+00:00 | reflection]\nfirst"
    "\n\n---\n\n"
    "[2026-01-02T00:00:00+00:00 | realization]\nsecond"
)


def _journal(mem_fs: dict[str, str]) -> Journal:
    mem_fs[str(_DIR / "journal.jsonl")] = "".join(
        json.dumps(row) + "\n" for row in _JOURNAL_ROWS
    )
    return Journal(_DIR)


async def _journal_tool_text(
    tools: list[InprocTool], name: str, args: dict[str, Any],
) -> str:
    handler = next(t for t in tools if t.name == name).handler
    return json.dumps(await handler(args))


def test_narrative_matches_golden(mem_fs: dict[str, str]) -> None:
    journal = _journal(mem_fs)
    for narrative in (journal.get_recent_narrative(), journal.get_full_narrative()):
        _assert_no_rung(narrative)
        assert narrative == _NARRATIVE_GOLDEN


@pytest.mark.asyncio
async def test_journal_tool_replies_name_no_stage(mem_fs: dict[str, str]) -> None:
    tools = journal_tools(_journal(mem_fs))
    for name, args in (
        ("read_journal", {}),
        ("search_journal", {"query": "first second"}),
    ):
        reply = await _journal_tool_text(tools, name, args)
        _assert_no_rung(reply)
        assert "[2026-01-01T00:00:00+00:00 | reflection]" in reply


@pytest.mark.asyncio
async def test_mcp_serve_registry_journal_names_no_stage(mem_fs: dict[str, str]) -> None:
    from maestro.cli import _build_mcp_serve_registry

    _journal(mem_fs)
    config = Config(memory_dir=_DIR, workspace=_DIR, publication_enabled=False)
    registry = _build_mcp_serve_registry(config)
    tools = [t for server in registry.values() for t in server]
    reply = await _journal_tool_text(tools, "read_journal", {})
    _assert_no_rung(reply)
    assert "[2026-01-01T00:00:00+00:00 | reflection]" in reply


# --- Plan-provenance surfaces: review message, PR body, remediation prompt ---


def _assert_no_stage(text: str) -> None:
    _assert_no_rung(text)
    assert "Stage" not in text


def _plan() -> EvolutionPlan:
    """A fresh plan per use: ``save_evolution_plan`` stamps
    ``plan_content_hash`` in place, which would leak into the review golden."""
    return EvolutionPlan(
        plan_id="abc123",
        title="Test evolution",
        category="capability",
        rationale="For testing",
        proposed_changes=["Change X"],
        files_affected=["maestro/agent.py"],
        expected_impact="Improvement",
        priority="capability",
        status="approved",
        created_at="2026-01-01T00:00:00Z",
    )


_REVIEW_GOLDEN = (
    "New Evolution Plan [plan abc123@]: Test evolution\n"
    "Category: capability | Priority: capability\n"
    "Rationale: For testing\n"
    "Impact: Improvement\n\n"
    "Changes:\n  - Change X\n\n"
    "Files:\n  - maestro/agent.py\n\n"
    "Review on dashboard or use the buttons below."
)

_PR_BODY_GOLDEN = (
    "## Evolution Plan: Test evolution\n\n"
    "**Category:** capability\n"
    "**Priority:** capability\n\n"
    "### Rationale\nFor testing\n\n"
    "### Expected Impact\nImprovement\n\n"
    "### Changes\n- Change X"
)


def _engine(mem_fs: dict[str, str]) -> tuple[EvolutionEngine, MagicMock]:
    memory = Memory(_DIR)
    vcs = MagicMock()
    vcs.porcelain_status.return_value = ""
    vcs.get_current_branch.return_value = "main"
    host = MagicMock()
    host.create_pr.return_value = "https://example/pr/1"
    host.get_default_branch.return_value = "main"
    engine = EvolutionEngine(
        memory, Consciousness(_DIR), Journal(_DIR),
        NullNotifier(),
        issues=IssueTracker(github_repo=None), github_repo="owner/repo",
        config=Config(self_review_max_rounds=0),
        vcs=vcs, code_host=host,
    )
    memory.save_evolution_plan(_plan())
    return engine, host


def test_review_message_matches_golden(mem_fs: dict[str, str]) -> None:
    assert compose_review_message(_plan()) == _REVIEW_GOLDEN
    engine, _host = _engine(mem_fs)
    msg = engine._compose_review_message(_plan())
    _assert_no_stage(msg)
    assert "\nCategory: capability | Priority: capability\n" in msg


def test_remediation_plan_block_names_no_stage() -> None:
    prompt = _compose_pr_remediation_prompt(
        pr_number=7, pr_title="T", plan=_plan(), plan_id="abc123",
        pr_body="body", pr_url="u", problems=["prob"],
    )
    _assert_no_stage(prompt)
    assert "- Category: capability / Priority: capability\n" in prompt


@pytest.mark.asyncio
async def test_pr_body_matches_golden(mem_fs: dict[str, str]) -> None:
    engine, host = _engine(mem_fs)
    await engine.create_pr(_plan())
    _title, body = host.create_pr.call_args.args
    assert body == _PR_BODY_GOLDEN
