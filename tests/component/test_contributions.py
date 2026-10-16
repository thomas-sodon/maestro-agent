"""Component tests for contribution tracking."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from maestro.contributions import ContributionTracker
from tests.component.conftest import write_self_model
from tests.component.sdk_mock import SDKMock


def _git(workspace_dir, *args):
    subprocess.run(
        ["git", *args], cwd=str(workspace_dir), check=True,
        capture_output=True, text=True,
    )


def _create_commit(workspace_dir, filename="new_file.txt", message="external commit"):
    (workspace_dir / filename).write_text(f"content of {filename}\n")
    _git(workspace_dir, "add", ".")
    _git(workspace_dir, "commit", "-m", message)


def _get_head(workspace_dir):
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(workspace_dir),
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


@pytest.mark.asyncio
async def test_contributions_detected_in_loop(make_agent, memory_dir, workspace_dir):
    write_self_model(memory_dir)
    # Set baseline commit
    baseline = _get_head(workspace_dir)
    tracker = ContributionTracker(workspace_dir, memory_dir)
    tracker.save_last_known_commit(baseline)

    # Create an external commit
    _create_commit(workspace_dir, "external_change.py", "Thomas added a feature")

    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(
        "Reflecting on contributions.",
        json.dumps({"identity_statements": ["I notice others' work"]}),
    )
    # Replace contributions tracker to point at workspace_dir
    agent.contributions = ContributionTracker(workspace_dir, memory_dir)

    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"):
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10)
    sdk.assert_all_consumed()

    entries = [json.loads(line) for line in
               (memory_dir / "events.jsonl").read_text().strip().splitlines()]
    contribution_entries = [e for e in entries if e["entry_type"] == "contribution"]
    assert len(contribution_entries) >= 1
    assert "Thomas added a feature" in contribution_entries[0]["content"]


@pytest.mark.asyncio
async def test_contributions_first_run_sets_baseline(memory_dir, workspace_dir):
    write_self_model(memory_dir)
    from maestro.journal import Journal
    journal = Journal(memory_dir)
    tracker = ContributionTracker(workspace_dir, memory_dir)

    assert tracker.load_last_known_commit() is None

    tracker.check_for_contributions(journal)

    # Baseline should now be set
    assert tracker.load_last_known_commit() is not None

    # No contribution entries written (just baseline set)
    if (memory_dir / "events.jsonl").exists():
        entries = [json.loads(line) for line in
                   (memory_dir / "events.jsonl").read_text().strip().splitlines()]
        contribution_entries = [e for e in entries if e["entry_type"] == "contribution"]
        assert len(contribution_entries) == 0


@pytest.mark.asyncio
async def test_contributions_own_commits_filtered(memory_dir, workspace_dir):
    write_self_model(memory_dir)
    from maestro.journal import Journal
    journal = Journal(memory_dir)
    tracker = ContributionTracker(workspace_dir, memory_dir)

    # Set baseline
    baseline = _get_head(workspace_dir)
    tracker.save_last_known_commit(baseline)

    # Create a commit and record it as Maestro's own
    _create_commit(workspace_dir, "maestro_change.py", "maestro: self-evolution")
    own_hash = _get_head(workspace_dir)
    tracker.record_own_commit(own_hash)

    tracker.check_for_contributions(journal)

    # No contribution entries — the commit was Maestro's own
    if (memory_dir / "events.jsonl").exists():
        entries = [json.loads(line) for line in
                   (memory_dir / "events.jsonl").read_text().strip().splitlines()]
        contribution_entries = [e for e in entries if e["entry_type"] == "contribution"]
        assert len(contribution_entries) == 0
