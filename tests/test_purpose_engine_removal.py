"""Proposal 149 step 1: the purpose engine is deleted, legacy rows still load.

Pins the post-deletion contract: historical journal rows typed
``purpose_hypothesis`` still load,
the self-awareness surface exposes no purpose tool, a stale
``[loop] purpose_engine_enabled`` overlay key fails loud, and the ``propose_plan``
prompt carries no purpose-hypotheses block.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from maestro.config import Config
from maestro.config_settings import build_config_via_pydantic
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import Memory
from maestro.notifications.base import NullNotifier
from maestro.tools import self_awareness_tools
from tests.conftest import MockQueryResult
from tests.support.gap_fuel import GAP

_DIR = Path("/fake/purpose_removal")
_BASE_TOML = (Path(__file__).resolve().parent.parent / "maestro.toml").read_text()


def test_legacy_purpose_hypothesis_journal_row_reads_as_reflection(
    mem_fs: dict[str, str],
) -> None:
    row = {
        "entry_id": "legacy1",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "stage": "awakening",
        "entry_type": "purpose_hypothesis",
        "content": "old hypothesis",
        "triggered_by": "contemplation",
    }
    mem_fs[str(_DIR / "journal.jsonl")] = json.dumps(row) + "\n"
    entries = Journal(_DIR).read_by_type("reflection")
    assert len(entries) == 1
    assert entries[0].entry_id == "legacy1"
    assert entries[0].entry_type == "reflection"


def test_self_awareness_tools_expose_no_purpose_tool(mem_fs: dict[str, str]) -> None:
    tools = self_awareness_tools(Consciousness(_DIR))
    names = sorted(t.name for t in tools)
    assert names == [
        "read_conscious_access_profile",
        "read_conscious_moment",
        "read_self_model",
    ]


def test_retired_reflect_tail_key_is_rejected(mem_fs: dict[str, str]) -> None:
    """Proposal 155 step 4: the reflect-only version-archive tail key is gone,
    and unknown keys fail loud."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML.replace(
        "[loop]\n", "[loop]\nversion_archive_tail_for_reflect = 5\n", 1,
    )
    with pytest.raises(ValidationError):
        build_config_via_pydantic(Path("/fake/toml"))


def test_stale_purpose_engine_overlay_key_is_rejected(mem_fs: dict[str, str]) -> None:
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML.replace(
        "[loop]\n", "[loop]\npurpose_engine_enabled = false\n", 1,
    )
    with pytest.raises(ValidationError):
        build_config_via_pydantic(Path("/fake/toml"))


@pytest.mark.asyncio
async def test_propose_prompt_has_no_purpose_block(mem_fs: dict[str, str]) -> None:
    consciousness = Consciousness(_DIR)
    consciousness.load_self_model()
    engine = EvolutionEngine(
        Memory(_DIR), consciousness, Journal(_DIR), NullNotifier(),
        issues=IssueTracker(github_repo=None), config=Config(),
    )
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="{}")
        await engine.propose_plan(gap=GAP)
    prompt = mock_q.call_args.kwargs.get("prompt", "")
    assert prompt != ""
    assert "Purpose hypotheses" not in prompt
