"""Component tests for evolution learn_from_rejection."""

from __future__ import annotations

import json

import pytest

from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import Memory
from maestro.notifications.base import NullNotifier
from tests.component.conftest import write_evolution_plan, write_self_model
from tests.component.mock_vcs import MockCodeHost
from tests.component.sdk_mock import SDKMock


@pytest.mark.asyncio
async def test_learn_from_rejection_journals_and_updates_model(memory_dir, config):
    write_self_model(memory_dir,
                     identity_statements=["I am Maestro"],
                     capabilities_known=["code generation"],
                     limitations_known=["sometimes overambitious"])
    write_evolution_plan(memory_dir, plan_id="rej001", title="Add teleportation",
                         status="rejected", rejection_reason="Too ambitious")

    memory = Memory(memory_dir)
    consciousness = Consciousness(memory_dir)
    journal = Journal(memory_dir)
    notifier = NullNotifier()
    code_host = MockCodeHost()
    issues = IssueTracker(github_repo="test-owner/test-repo", code_host=code_host)
    evolution = EvolutionEngine(memory, consciousness, journal, notifier,
                                issues=issues, github_repo="test-owner/test-repo",
                                config=config, code_host=code_host)

    plan = memory.get_plan("rej001")
    assert plan is not None

    sdk = SDKMock()
    # Order matches the call sequence inside learn_from_rejection():
    #   1. maestro.evolution.query — rejection classifier (low effort).
    #   2. maestro.consciousness.query — update_self_model.
    sdk.then(
        "scope_too_large",
        json.dumps({
            "identity_statements": ["I am Maestro"],
            "capabilities_known": ["code generation"],
            "limitations_known": [
                "sometimes overambitious", "teleportation is impossible",
            ],
        }),
    )
    with sdk.patch("maestro.consciousness", "maestro.evolution", "maestro.evolution_quality"):
        await evolution.learn_from_rejection(plan, "Too ambitious")
    sdk.assert_all_consumed()

    entries = [json.loads(line) for line in
               (memory_dir / "events.jsonl").read_text().strip().splitlines()]
    realization_entries = [e for e in entries if e["entry_type"] == "realization"]
    assert len(realization_entries) >= 1
    assert "rejected" in realization_entries[0]["content"].lower()
    assert "Add teleportation" in realization_entries[0]["content"]

    rejections = [
        json.loads(line) for line in
        (memory_dir / "rejection_history.jsonl").read_text().strip().splitlines()
    ]
    assert len(rejections) == 1
    assert rejections[0]["plan_id"] == "rej001"
    assert rejections[0]["rejection_category"] == "scope_too_large"
