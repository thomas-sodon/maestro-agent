"""Proposal 149: ``propose_task_docs`` and the doc-proposals pipeline are retired.

Pins the deletion: no phase, module, caller, SDK phase name, config section or
``Config`` field survives, while ``maestro docs`` still reads historical
``DocumentRecord`` rows.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

from maestro.agent import _LOOP_PHASES
from maestro.call_policy import CALL_POLICIES
from maestro.cli import cmd_docs
from maestro.config import SDK_PHASE_NAMES, Config
from maestro.config_settings import _SECTION_MODELS
from maestro.cost_tracking import CALLER_GROUPS, RETIRED_CALLERS
from maestro.loop.lifecycle import _BUDGET_GATED_PHASES
from maestro.loop.phases import REGISTERED_PHASE_NAMES
from maestro.memory import DocumentRecord, Memory
from tests.conftest import capture_stdout

_PKG = Path(__file__).resolve().parent.parent / "maestro"
_PUBLIC_JS = _PKG / "dashboard" / "static" / "public.js"


def test_phase_absent_everywhere() -> None:
    assert "propose_task_docs" not in _LOOP_PHASES
    assert "propose_task_docs" not in REGISTERED_PHASE_NAMES
    assert "propose_task_docs" not in _BUDGET_GATED_PHASES
    assert "propose_task_docs" not in _PUBLIC_JS.read_text(encoding="utf-8")


def test_modules_deleted() -> None:
    assert not (_PKG / "doc_proposals.py").exists()
    assert not (_PKG / "prompts" / "doc_proposals.py").exists()


def test_callers_and_sdk_phase_gone() -> None:
    assert [k for k in CALL_POLICIES if k.startswith("doc_proposals")] == []
    assert "doc_proposals" not in SDK_PHASE_NAMES


def test_retired_callers_keep_cost_group() -> None:
    """Historical doc-proposal usage rows still roll up to their phase."""
    for caller in ("doc_proposals", "doc_proposals_session"):
        assert caller in RETIRED_CALLERS
        assert CALLER_GROUPS[caller] == "Artifacts & Docs"


def test_config_section_and_fields_gone() -> None:
    assert "doc_proposals" not in _SECTION_MODELS
    names = [f.name for f in dataclasses.fields(Config)]
    assert [n for n in names if n.startswith("doc_proposals_")] == []


def test_maestro_docs_lists_historical_row(mem_fs: dict[str, str]) -> None:
    config = Config(memory_dir=Path("/fake/docs"), workspace=Path("/fake/docs"))
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    record = DocumentRecord(
        doc_id="d-hist",
        doc_type="core",
        title="Historical doc",
        linked_task_ids=["t-1"],
        target_path=None,
        doc_branch=None,
        pr_url=None,
        bundle_file_names=[],
        reasoning="",
        status="skipped",
        error=None,
        created_at="2026-04-25T00:00:00+00:00",
    )
    mem_fs[str(memory._documents_path)] = (
        json.dumps(dataclasses.asdict(record)) + "\n"
    )
    args = argparse.Namespace(
        count=None, doc_type=None, status=None, task=None, json=False,
    )
    output = capture_stdout(cmd_docs, args, config)
    assert "Historical doc" in output
