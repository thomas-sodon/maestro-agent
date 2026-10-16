"""Component tests for evolution."""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from maestro.evolution import QualityGateResult
from maestro.sdk import ErrorCategory, QueryResult, SDKError
from tests.component.conftest import (
    make_quality_gates_mock,
    write_evolution_plan,
    write_self_model,
)
from tests.component.sdk_mock import SDKMock
from tests.support.gap_fuel import fuel_patch as _fuel

_OK_REVIEW = QualityGateResult(
    passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True,
    raw_output="Ruff: clean\n\nMypy: clean\n\nTests: clean",
)

# ---- PR creation ----


@pytest.mark.asyncio
async def test_implement_creates_pr(make_agent, memory_dir, workspace_dir):
    """implement_plan creates a non-draft PR without bumping evolution_count.

    evolution_count tracks integrations, not PRs opened — it is incremented
    on merge (the merged-plans loop in run_loop), not here at PR creation.
    """
    write_self_model(memory_dir, evolution_count=1, identity_statements=["I am Maestro"])

    write_evolution_plan(memory_dir, plan_id="cipass01", title="New feature",
                         status="approved", priority="resilience",
                         proposed_changes=["Add feature"], files_affected=["maestro/agent.py"])

    async def _mock_query(**kwargs):
        (workspace_dir / "ci_feature.py").write_text("# ci feature\n")
        return QueryResult(response="Implemented feature")

    agent, code_host, _vcs = make_agent()
    code_host.next_pr_url = "https://github.com/test-owner/test-repo/pull/10"

    sdk = SDKMock()
    # Under gate convergence (proposal 110, default-on) self_review must reach
    # LGTM or implement_plan demotes before create_pr — approve it here so this
    # test still exercises the end-to-end create_pr path.
    sdk.on("reviewing an evolution implementation", "LGTM")
    sdk.on("", _mock_query)
    with sdk.patch("maestro.evolution", "maestro.evolution_quality"), \
         patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW):
        await agent.implement_plan("cipass01")

    plans = [json.loads(line) for line in
             (memory_dir / "evolution_plans.jsonl").read_text().strip().splitlines()]
    impl_plan = next((p for p in plans if p["plan_id"] == "cipass01"), None)
    assert impl_plan is not None
    assert impl_plan["status"] == "implemented"

    assert len(code_host.created_prs) == 1
    assert code_host.created_prs[-1]["draft"] is False

    events_text = (memory_dir / "events.jsonl").read_text()
    assert "Evolution implemented" in events_text

    # PR creation must not bump evolution_count — it stays at the seeded value
    # and is incremented only when the PR actually merges.
    model_data = json.loads((memory_dir / "self_model.json").read_text())
    assert model_data["evolution_count"] == 1


@pytest.mark.asyncio
async def test_own_commit_records_evolution_hash(make_agent, memory_dir, workspace_dir):
    """record_own_commit should record the evolution branch commit, not original branch HEAD."""
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=str(workspace_dir), check=True, capture_output=True,
    )
    write_self_model(memory_dir, evolution_count=1, identity_statements=["I am Maestro"])

    write_evolution_plan(memory_dir, plan_id="ownhash1", title="Own hash test",
                         status="approved", priority="resilience",
                         proposed_changes=["Test hash"], files_affected=["maestro/agent.py"])

    main_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(workspace_dir),
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    async def _mock_query(**kwargs):
        (workspace_dir / "hash_feature.py").write_text("# hash test\n")
        return QueryResult(response="Implemented")

    agent, code_host, _vcs = make_agent()
    code_host.next_pr_url = "https://github.com/test-owner/test-repo/pull/50"

    sdk = SDKMock()
    # Under gate convergence (proposal 110, default-on) self_review must reach
    # LGTM or implement_plan demotes before create_pr — approve it here so this
    # test still exercises the end-to-end create_pr path.
    sdk.on("reviewing an evolution implementation", "LGTM")
    sdk.on("", _mock_query)
    with sdk.patch("maestro.evolution", "maestro.evolution_quality"), \
         patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW):
        await agent.implement_plan("ownhash1")

    evo_head = subprocess.run(
        ["git", "rev-parse", "maestro/evolve/ownhash1"],
        cwd=str(workspace_dir), capture_output=True, text=True, check=True,
    ).stdout.strip()

    assert evo_head != main_head, "Evolution branch should have a new commit"

    own_commits_file = memory_dir / "own_commits.txt"
    assert own_commits_file.exists(), "own_commits.txt should exist"
    recorded_hashes = {
        line.strip() for line in own_commits_file.read_text().splitlines() if line.strip()
    }

    assert evo_head in recorded_hashes, (
        f"Evolution hash {evo_head[:8]} should be recorded but found: "
        f"{[h[:8] for h in recorded_hashes]}"
    )
    assert main_head not in recorded_hashes, (
        f"Main branch hash {main_head[:8]} should NOT be recorded"
    )


# ---- edge cases ----


@pytest.mark.asyncio
async def test_submit_deduplication_skips_identical_title(make_agent, memory_dir):
    write_self_model(memory_dir, evolution_count=2,
                     identity_statements=["I am Maestro"],
                     capabilities_known=["code"], limitations_known=["speed"])
    write_evolution_plan(memory_dir, plan_id="existing001",
                         title="Improve error handling",
                         status="pending_review")
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(
        json.dumps({"title": "Improve error handling", "category": "capability",
                     "priority": "resilience", "rationale": "Errors are bad",
                     "proposed_changes": ["Add retry"],
                     "files_affected": ["maestro/executor.py"],
                     "expected_impact": "Better errors"}),
    )
    with _fuel(), sdk.patch("maestro.consciousness", "maestro.evolution", "maestro.evolution_quality"), \
         patch("maestro.evolution.subprocess.run",
               side_effect=make_quality_gates_mock()):
        await agent.evolve()
    sdk.assert_all_consumed()
    plans = [json.loads(line) for line in
             (memory_dir / "evolution_plans.jsonl").read_text().strip().splitlines()]
    matching = [p for p in plans if p["title"] == "Improve error handling"]
    statuses = sorted(p["status"] for p in matching)
    assert statuses == ["obsolete", "pending_review"], "Duplicate plan should not be submitted"
    refused = next(p for p in matching if p["status"] == "obsolete")
    # A gap plan's exact-title refusal is an attempt on its issue (A7).
    assert refused["obsolete_reason"] == "Near-duplicate of existing001 (exact title)"


@pytest.mark.asyncio
async def test_evolve_runs_no_quality_gates(make_agent, memory_dir):
    """Proposal 155 step 4: evolve runs no gate subprocess, so a gate that
    would time out never delays the plan."""
    write_self_model(memory_dir, evolution_count=2,
                     identity_statements=["I am Maestro"],
                     capabilities_known=["x"], limitations_known=["y"])

    _real_run = subprocess.run

    def _timeout_ruff_mock(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if not isinstance(cmd, list):
            return _real_run(cmd, **kwargs)  # type: ignore[arg-type]
        prog = cmd[0] if cmd else ""
        if prog == "ruff":
            raise subprocess.TimeoutExpired(cmd, 300)
        if prog == "mypy":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if prog == "pytest":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return _real_run(cmd, **kwargs)

    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(
        json.dumps({"title": "Fix ruff timeout", "category": "infrastructure",
                     "priority": "resilience", "rationale": "Ruff keeps timing out",
                     "proposed_changes": ["Increase timeout"],
                     "files_affected": ["maestro/config.py"],
                     "expected_impact": "More reliable CI"}),
    )
    with _fuel(), sdk.patch("maestro.consciousness", "maestro.evolution", "maestro.evolution_quality"), \
         patch("maestro.evolution.subprocess.run",
               side_effect=_timeout_ruff_mock) as gates:
        await agent.evolve()
    gate_cmds = [
        c.args[0] for c in gates.call_args_list
        if any(tool in " ".join(map(str, c.args[0])) for tool in ("ruff", "mypy", "pytest"))
    ]
    assert gate_cmds == []
    sdk.assert_all_consumed()
    plans = [json.loads(line) for line in
             (memory_dir / "evolution_plans.jsonl").read_text().strip().splitlines()]
    assert any(p["title"] == "Fix ruff timeout" for p in plans)


@pytest.mark.asyncio
async def test_evolve_with_open_github_issues(make_agent, memory_dir):
    write_self_model(memory_dir, evolution_count=2,
                     identity_statements=["I am Maestro"],
                     capabilities_known=["code"], limitations_known=["bugs"])
    agent, code_host, _vcs = make_agent()
    code_host.issues = [
        {"number": 5, "title": "Dashboard crash", "labels": [{"name": "bug"}],
         "body": "Crashes on load", "createdAt": "2025-06-01T00:00:00Z"},
    ]
    sdk = SDKMock()
    sdk.then(
        json.dumps({"title": "Fix dashboard crash", "category": "infrastructure",
                     "priority": "resilience", "rationale": "Dashboard crashes",
                     "proposed_changes": ["Fix the crash"],
                     "files_affected": ["maestro/dashboard/app.py"],
                     "expected_impact": "Stable dashboard",
                     "issue_number": 9}),
    )
    with _fuel(5), sdk.patch("maestro.consciousness", "maestro.evolution", "maestro.evolution_quality"), \
         patch("maestro.evolution.subprocess.run",
               side_effect=make_quality_gates_mock()):
        await agent.evolve()
    sdk.assert_all_consumed()
    plans = [json.loads(line) for line in
             (memory_dir / "evolution_plans.jsonl").read_text().strip().splitlines()]
    # The issue is pinned from the gap, not the reply's issue_number.
    assert any(p["title"] == "Fix dashboard crash" and p["issue_number"] == 5
               for p in plans)


@pytest.mark.asyncio
async def test_evolve_handles_unparseable_proposal(make_agent, memory_dir):
    """If propose_plan raises ProposalParseError, evolve() journals and returns
    cleanly — no junk fallback plan is submitted for review (H5)."""
    write_self_model(memory_dir, evolution_count=2,
                     identity_statements=["I am Maestro"],
                     capabilities_known=["code"], limitations_known=["bugs"])
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then("this is not valid JSON at all")  # propose_plan response
    with _fuel(), sdk.patch("maestro.consciousness", "maestro.evolution", "maestro.evolution_quality"), \
         patch("maestro.evolution.subprocess.run",
               side_effect=make_quality_gates_mock()):
        await agent.evolve()
    sdk.assert_all_consumed()
    plans_path = memory_dir / "evolution_plans.jsonl"
    plans = (
        [json.loads(line) for line in plans_path.read_text().strip().splitlines()]
        if plans_path.exists() and plans_path.read_text().strip()
        else []
    )
    # No plan was saved — the parse failure was recorded as an event instead.
    assert plans == []
    events_text = (memory_dir / "events.jsonl").read_text()
    assert "unparseable response" in events_text


@pytest.mark.asyncio
async def test_evolve_survives_structured_output_exhaustion(make_agent, memory_dir):
    """#4535: a ``propose_plan`` turn the CLI's schema validator exhausts
    surfaces as the unparseable-reply skip, not a loop-killing SDKError."""
    write_self_model(memory_dir, evolution_count=2,
                     identity_statements=["I am Maestro"],
                     capabilities_known=["code"], limitations_known=["bugs"])
    agent, _code_host, _vcs = make_agent()
    replies: list[str] = []

    async def handler(**kwargs: Any) -> QueryResult:
        if kwargs["options"].caller == "propose_plan":
            raise SDKError(
                "SDK returned terminal error result "
                "(subtype=error_max_structured_output_retries)",
                category=ErrorCategory.STRUCTURED_OUTPUT_INVALID,
                caller="propose_plan",
            )
        return QueryResult(response=replies.pop(0))

    sdk = SDKMock()
    sdk.on_any(handler)
    with _fuel(), sdk.patch("maestro.consciousness", "maestro.evolution", "maestro.evolution_quality"), \
         patch("maestro.evolution.subprocess.run",
               side_effect=make_quality_gates_mock()):
        await agent.evolve()
    assert replies == []
    plans_path = memory_dir / "evolution_plans.jsonl"
    plans_text = plans_path.read_text() if plans_path.exists() else ""
    assert plans_text.strip() == ""
    events_text = (memory_dir / "events.jsonl").read_text()
    assert "unparseable response" in events_text


@pytest.mark.asyncio
async def test_evolve_propagates_sdk_error_in_propose_plan(make_agent, memory_dir):
    """SDK errors raised inside propose_plan() propagate out of evolve() so
    the loop crashes and crash detection can handle restart."""
    from maestro.sdk import SDKError

    write_self_model(memory_dir, evolution_count=2,
                     identity_statements=["I am Maestro"],
                     capabilities_known=["code"], limitations_known=["bugs"])
    agent, _code_host, _vcs = make_agent()

    sdk = SDKMock()
    with _fuel(), sdk.patch("maestro.consciousness", "maestro.evolution", "maestro.evolution_quality"), \
         patch("maestro.evolution.subprocess.run",
               side_effect=make_quality_gates_mock()), \
         patch.object(agent.evolution, "propose_plan", new_callable=AsyncMock) as mock_propose, \
         pytest.raises(SDKError, match="rate limit exhausted"):
        mock_propose.side_effect = SDKError("rate limit exhausted")
        await agent.evolve()


# ---- implementation ----


@pytest.mark.asyncio
async def test_implement_approved_plan(make_agent, memory_dir, workspace_dir):
    write_self_model(memory_dir, evolution_count=1, identity_statements=["I am Maestro"])
    write_evolution_plan(memory_dir, plan_id="impl001", title="Add retry logic",
                         status="approved", priority="resilience",
                         proposed_changes=["Add retry"], files_affected=["maestro/executor.py"])

    async def _mock_query(**kwargs):
        (workspace_dir / "new_feature.py").write_text("# retry logic\n")
        return QueryResult(response="Implemented retry logic")

    agent, code_host, vcs = make_agent()
    sdk = SDKMock()
    # Under gate convergence (proposal 110, default-on) self_review must reach
    # LGTM or implement_plan demotes before create_pr — approve it here so this
    # test still exercises the end-to-end create_pr path.
    sdk.on("reviewing an evolution implementation", "LGTM")
    sdk.on("", _mock_query)
    with sdk.patch("maestro.evolution", "maestro.evolution_quality"), \
         patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW):
        await agent.implement_plan("impl001")
    plans = [json.loads(line) for line in (memory_dir / "evolution_plans.jsonl").read_text().strip().splitlines()]
    impl_plan = next((p for p in plans if p["plan_id"] == "impl001"), None)
    assert impl_plan is not None
    assert impl_plan["status"] == "implemented"
    assert impl_plan.get("pr_url") is not None
    # PR creation must not bump evolution_count — the increment fires on merge.
    model = json.loads((memory_dir / "self_model.json").read_text())
    assert model["evolution_count"] == 1
    assert len(vcs.pushes) >= 1
    assert len(code_host.created_prs) >= 1


@pytest.mark.asyncio
async def test_implement_recovers_stale_branch(make_agent, memory_dir, workspace_dir):
    """If the workspace is already on the target evolution branch, implement recovers."""
    write_self_model(memory_dir, evolution_count=0, identity_statements=["I am Maestro"])
    write_evolution_plan(memory_dir, plan_id="impl001", title="Add retry logic",
                         status="approved", priority="resilience",
                         proposed_changes=["Add retry"], files_affected=["maestro/executor.py"])

    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=str(workspace_dir), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", "maestro/evolve/impl001"],
        cwd=str(workspace_dir), check=True, capture_output=True,
    )

    async def _mock_query(**kwargs):
        (workspace_dir / "new_feature.py").write_text("# retry logic\n")
        return QueryResult(response="Implemented retry logic")

    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    # Under gate convergence (proposal 110, default-on) self_review must reach
    # LGTM or implement_plan demotes before create_pr — approve it here so this
    # test still exercises the end-to-end create_pr path.
    sdk.on("reviewing an evolution implementation", "LGTM")
    sdk.on("", _mock_query)
    with sdk.patch("maestro.evolution", "maestro.evolution_quality"), \
         patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW):
        await agent.implement_plan("impl001")

    plans = [json.loads(line) for line in
             (memory_dir / "evolution_plans.jsonl").read_text().strip().splitlines()]
    impl_plan = next((p for p in plans if p["plan_id"] == "impl001"), None)
    assert impl_plan is not None
    assert impl_plan["status"] == "implemented"
    assert impl_plan.get("pr_url") is not None


@pytest.mark.asyncio
async def test_implement_unapproved_plan_noop(make_agent, memory_dir):
    write_self_model(memory_dir)
    write_evolution_plan(memory_dir, plan_id="pending001", status="pending_review")
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then("should not be called")
    with sdk.patch("maestro.evolution", "maestro.evolution_quality"):
        await agent.implement_plan("pending001")
    sdk.assert_not_called()


# ---- implement failure ----


@pytest.mark.asyncio
async def test_implement_plan_failure_resets_status(make_agent, memory_dir, workspace_dir):
    write_self_model(memory_dir, evolution_count=1, identity_statements=["I am Maestro"])
    write_evolution_plan(memory_dir, plan_id="fail001", title="Failing plan",
                         status="approved", priority="resilience",
                         proposed_changes=["Change X"], files_affected=["test.py"])
    failing_query = AsyncMock(side_effect=RuntimeError("SDK exploded"))

    agent, _code_host, _vcs = make_agent()
    with patch("maestro.evolution.query", failing_query), \
         pytest.raises(RuntimeError, match="SDK exploded"):
        await agent.implement_plan("fail001")
    plans = [json.loads(line) for line in
             (memory_dir / "evolution_plans.jsonl").read_text().strip().splitlines()]
    plan = next(p for p in plans if p["plan_id"] == "fail001")
    assert plan["status"] == "failed"
    assert plan["failure_count"] == 1
    assert plan.get("last_failure_reason") is not None
    assert "Implementation failed" in plan["last_failure_reason"]


@pytest.mark.asyncio
async def test_implement_plan_nonexistent_is_noop(make_agent, memory_dir):
    write_self_model(memory_dir)
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then("should not be called")
    with sdk.patch("maestro.evolution", "maestro.evolution_quality"):
        await agent.implement_plan("nonexistent_id")
    sdk.assert_not_called()


@pytest.mark.asyncio
async def test_implement_plan_records_own_commit(make_agent, memory_dir, workspace_dir):
    write_self_model(memory_dir, evolution_count=0, identity_statements=["I am Maestro"])
    write_evolution_plan(memory_dir, plan_id="rec001", title="Record commit test",
                         status="approved", priority="capability",
                         proposed_changes=["Add a file"], files_affected=["test.py"])

    async def _mock_query(**kwargs):
        (workspace_dir / "new_file.py").write_text("# new file\n")
        return QueryResult(response="Implemented changes")

    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    # Under gate convergence (proposal 110, default-on) self_review must reach
    # LGTM or implement_plan demotes before create_pr — approve it here so this
    # test still exercises the end-to-end create_pr path.
    sdk.on("reviewing an evolution implementation", "LGTM")
    sdk.on("", _mock_query)
    with sdk.patch("maestro.evolution", "maestro.evolution_quality"), \
         patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW):
        await agent.implement_plan("rec001")
    own_commits_path = memory_dir / "own_commits.txt"
    assert own_commits_path.exists()
    lines = own_commits_path.read_text().strip().splitlines()
    assert len(lines) >= 1
    assert len(lines[0].strip()) == 40
