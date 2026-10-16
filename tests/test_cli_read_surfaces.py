"""Read-only CLI surfaces over persisted state - history, ledgers, owed acts,
proposals, reviews, publications, email, docs; split out of
``tests/test_cli.py``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import ANY, MagicMock, patch

import pytest

from maestro.cli import (
    _build_agent,
    _build_email_client,
    _compose_pre_deploy_content,
    _extract_pr_numbers,
    _resolve_pre_deploy_prs,
    cmd_deed_ledger,
    cmd_docs,
    cmd_email,
    cmd_history,
    cmd_list_proposals,
    cmd_notify_pre_deploy,
    cmd_owned_acts,
    cmd_pr_owed_act,
    cmd_publications,
    cmd_reality_inventory,
    cmd_reviews,
)
from maestro.config import Config
from maestro.memory import (
    ArtifactReview,
    DocumentRecord,
    EvolutionPlan,
    EvolutionProposal,
    Memory,
    PublicationProposal,
    TaskRecord,
)
from maestro.remediation_tracker import JsonlStandDownVerdictLedger
from tests.conftest import capture_stdout
from tests.test_cli import (
    _make_config,
)

# ── cmd_history ────────────────────────────────────────────────────


def _make_task(
    task_id: str = "t1",
    goal: str = "Implement feature X",
    outcome: str = "success",
    source: str = "external",
    duration_secs: float = 120.0,
    timestamp: str = "2026-04-22T10:00:00",
    linked_question: str | None = None,
    key_findings: str | None = None,
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        goal=goal,
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary="Done",
        errors=[],
        duration_secs=duration_secs,
        timestamp=timestamp,
        source=source,  # type: ignore[arg-type]
        linked_question=linked_question,
        key_findings=key_findings,
    )


def test_cmd_history_empty(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(count=10, outcome=None, source=None, json=False)
    output = capture_stdout(cmd_history, args, config)
    assert "No task history" in output


def test_cmd_history_default_output(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_task())

    args = argparse.Namespace(count=10, outcome=None, source=None, json=False)
    output = capture_stdout(cmd_history, args, config)
    assert "2026-04-22" in output
    assert "success" in output
    assert "2.0m" in output
    assert "external" in output
    assert "Implement feature X" in output


def test_cmd_history_count_flag(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    for i in range(5):
        memory.record_task(_make_task(task_id=f"t{i}", goal=f"Task {i}"))

    args = argparse.Namespace(count=2, outcome=None, source=None, json=False)
    output = capture_stdout(cmd_history, args, config)
    # get_recent_tasks returns last N, so we should see Task 3 and Task 4
    assert "Task 3" in output
    assert "Task 4" in output
    assert "Task 0" not in output


def test_cmd_history_outcome_filter(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_task(task_id="t1", goal="Good task", outcome="success"))
    memory.record_task(_make_task(task_id="t2", goal="Bad task", outcome="failure"))

    args = argparse.Namespace(count=10, outcome="failure", source=None, json=False)
    output = capture_stdout(cmd_history, args, config)
    assert "Bad task" in output
    assert "Good task" not in output


def test_cmd_history_source_filter(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_task(task_id="t1", goal="External", source="external"))
    memory.record_task(_make_task(task_id="t2", goal="Self", source="self_directed"))

    args = argparse.Namespace(count=10, outcome=None, source="self_directed", json=False)
    output = capture_stdout(cmd_history, args, config)
    assert "Self" in output
    assert "External" not in output


def test_cmd_history_combined_filters(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_task(task_id="t1", outcome="success", source="external"))
    memory.record_task(_make_task(task_id="t2", outcome="failure", source="self_directed"))
    memory.record_task(_make_task(task_id="t3", outcome="success", source="self_directed", goal="Match"))

    args = argparse.Namespace(count=10, outcome="success", source="self_directed", json=False)
    output = capture_stdout(cmd_history, args, config)
    assert "Match" in output
    # Only one task should match both filters
    assert output.count("[") == 1


def test_cmd_history_json_output(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_task(task_id="t1", goal="JSON task"))

    args = argparse.Namespace(count=10, outcome=None, source=None, json=True)
    output = capture_stdout(cmd_history, args, config)
    data = json.loads(output)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["task_id"] == "t1"
    assert data[0]["goal"] == "JSON task"


def test_cmd_history_json_empty(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(count=10, outcome=None, source=None, json=True)
    output = capture_stdout(cmd_history, args, config)
    data = json.loads(output)
    assert data == []


def test_cmd_history_linked_question_shown(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_task(
        task_id="t1",
        linked_question="What is the meaning of purpose?",
    ))

    args = argparse.Namespace(count=10, outcome=None, source=None, json=False)
    output = capture_stdout(cmd_history, args, config)
    assert "? What is the meaning of purpose?" in output


def test_cmd_history_goal_truncated(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    long_goal = "A" * 100
    memory.record_task(_make_task(task_id="t1", goal=long_goal))

    args = argparse.Namespace(count=10, outcome=None, source=None, json=False)
    output = capture_stdout(cmd_history, args, config)
    # 80 chars + ellipsis
    assert "A" * 80 + "…" in output
    assert "A" * 81 not in output


def test_cmd_history_linked_question_truncated(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    long_q = "Q" * 100
    memory.record_task(_make_task(task_id="t1", linked_question=long_q))

    args = argparse.Namespace(count=10, outcome=None, source=None, json=False)
    output = capture_stdout(cmd_history, args, config)
    assert "Q" * 60 + "…" in output
    assert "Q" * 61 not in output


# ── cmd_deed_ledger ─────────────────────────────────────────────────


def test_cmd_deed_ledger_empty(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(count=None, json=False)
    output = capture_stdout(cmd_deed_ledger, args, config)
    assert "No terminal deeds recorded." in output
    assert "Total deeds recorded: 0" in output


def test_cmd_deed_ledger_default_output(mem_fs: dict[str, str]) -> None:
    from maestro import deed_ledger

    config = _make_config()
    config.ensure_dirs()
    deed_ledger.record_deed(
        config.memory_dir, deed_ledger.DeedType.PR_CLOSE, 42, "closed it",
        now="2026-07-31T00:00:00+00:00",
    )
    args = argparse.Namespace(count=None, json=False)
    output = capture_stdout(cmd_deed_ledger, args, config)
    assert "pr_close" in output
    assert "#42" in output
    assert "closed it" in output
    assert "Total deeds recorded: 1" in output


def test_cmd_deed_ledger_json_output(mem_fs: dict[str, str]) -> None:
    from maestro import deed_ledger

    config = _make_config()
    config.ensure_dirs()
    deed_ledger.record_deed(
        config.memory_dir, deed_ledger.DeedType.OWNED_PRUNE, None, "pruned 3",
        now="2026-07-31T00:00:00+00:00",
    )
    args = argparse.Namespace(count=None, json=True)
    output = capture_stdout(cmd_deed_ledger, args, config)
    data = json.loads(output)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["deed_type"] == "owned_prune"
    assert data[0]["pr_number"] is None
    assert data[0]["detail"] == "pruned 3"


def test_cmd_deed_ledger_reads_without_mutating_state(
    mem_fs: dict[str, str],
) -> None:
    """The command is a pure read — it appends no row to the ledger."""
    from maestro import deed_ledger

    config = _make_config()
    config.ensure_dirs()
    deed_ledger.record_deed(
        config.memory_dir, deed_ledger.DeedType.MERGE, 5, "merged",
        now="2026-07-31T00:00:00+00:00",
    )
    ledger_path = str(config.memory_dir / deed_ledger.DEED_LEDGER_FILENAME)
    before = mem_fs[ledger_path]
    args = argparse.Namespace(count=None, json=False)
    capture_stdout(cmd_deed_ledger, args, config)
    # No append, no rewrite — the file is byte-identical after the read.
    assert mem_fs[ledger_path] == before
    assert deed_ledger.count_deeds(config.memory_dir) == 1


@patch("maestro.cli.StateStore")
def test_cmd_deed_ledger_flag_on_reads_sqlite_seeded_from_legacy(
    mock_store_cls: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on, the CLI reads via SQLite, seeded from the legacy JSONL.

    ``StateStore`` is patched to a real in-memory store (``:memory:``) so no real
    ``state.db`` file is opened, while the legacy ``deed_ledger.jsonl`` (in
    ``mem_fs``) carries the history across the seam — matching the plan's manual
    flag-flip check that the SQLite count mirrors the flag-off reading.
    """
    from maestro import deed_ledger
    from maestro.state_store import StateStore as RealStateStore

    config = _make_config(sqlite_state_enabled=True)
    config.ensure_dirs()
    deed_ledger.record_deed(
        config.memory_dir, deed_ledger.DeedType.PR_CLOSE, 77, "legacy close",
        now="2026-07-31T00:00:00+00:00",
    )
    mock_store_cls.return_value = RealStateStore(":memory:")
    args = argparse.Namespace(count=None, json=False)
    output = capture_stdout(cmd_deed_ledger, args, config)
    assert "pr_close" in output
    assert "#77" in output
    assert "legacy close" in output
    assert "Total deeds recorded: 1" in output


# ── cmd_owned_acts ──────────────────────────────────────────────────


def test_cmd_owned_acts_empty(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(count=None, json=False)
    output = capture_stdout(cmd_owned_acts, args, config)
    assert "no owned acts recorded" in output


def test_cmd_owned_acts_populated_text(mem_fs: dict[str, str]) -> None:
    """A chosen stand-down and an obsolete prune render with the chosen flag."""
    from maestro import remediation_tracker

    config = _make_config()
    config.ensure_dirs()
    remediation_tracker.record_stand_down(
        3179, "abc", JsonlStandDownVerdictLedger(config.memory_dir),
        now="2026-07-03T00:00:00+00:00", branch_ahead_of_origin=True,
    )
    mem_fs[str(config.memory_dir / "evolution_plans.jsonl")] = json.dumps({
        "plan_id": "abc123def456", "status": "obsolete",
        "reviewed_at": "2026-07-01T00:00:00+00:00",
    }) + "\n"
    args = argparse.Namespace(count=None, json=False)
    output = capture_stdout(cmd_owned_acts, args, config)
    assert "stand_down" in output
    assert "#3179" in output
    assert "obsolete_prune" in output
    assert "abc123def456" in output
    assert "(chosen)" in output


def test_cmd_owned_acts_json_round_trip(mem_fs: dict[str, str]) -> None:
    from maestro import remediation_tracker

    config = _make_config()
    config.ensure_dirs()
    remediation_tracker.record_stand_down(
        4, "z", JsonlStandDownVerdictLedger(config.memory_dir),
        now="2026-07-04T00:00:00+00:00",
    )
    args = argparse.Namespace(count=None, json=True)
    output = capture_stdout(cmd_owned_acts, args, config)
    data = json.loads(output)
    assert isinstance(data, list)
    assert len(data) == 1
    assert set(data[0]) == {"kind", "timestamp", "lineage", "chosen"}
    assert data[0]["kind"] == "stand_down"
    assert data[0]["lineage"] == "#4"
    assert data[0]["chosen"] is False


# ── cmd_pr_owed_act ─────────────────────────────────────────────────

def _pr_state(
    number: int, *, merge_state_status: str, head_ref: str,
    mergeable: str = "MERGEABLE",
) -> Any:
    """Build a minimal PrMergeState for the owed-act handler tests."""
    from maestro.vcs import PrMergeState

    return PrMergeState(
        number=number,
        head_ref=head_ref,
        title="t",
        body="b",
        state="OPEN",
        mergeable=mergeable,
        merge_state_status=merge_state_status,
        review_decision="",
        created_at="",
        updated_at="",
        url="",
    )


def _stub_staleness(
    commits_behind: int = 0, *, cleanly_mergeable: bool = True,
) -> Any:
    """Patch ``pr_facts.observe_pr_staleness`` to a fixed measured reading.

    The owed-act handler measures each head branch's ``commits_behind`` from
    local git via ``observe_pr_staleness``; unit tests must not shell out, so
    every ``cmd_pr_owed_act`` test patches the observer to a deterministic
    :class:`PrStaleness` (default: even with ``origin/main`` — the prior read).
    """
    from maestro.pr_facts import PrStaleness

    def _observe(branch: str, **_kwargs: object) -> PrStaleness:
        return PrStaleness(
            branch=branch,
            commits_behind=commits_behind,
            cleanly_mergeable=commits_behind == 0 or cleanly_mergeable,
            needs_catchup=commits_behind > 0 and cleanly_mergeable,
            base_head_sha=None,
        )

    return patch("maestro.pr_facts.observe_pr_staleness", side_effect=_observe)


def test_cmd_pr_owed_act_no_open_prs(mem_fs: dict[str, str]) -> None:
    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {}
    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host):
        output = capture_stdout(cmd_pr_owed_act, argparse.Namespace(), config)
    assert "no open own PRs" in output


def test_cmd_pr_owed_act_failed_listing_fails_loud(mem_fs: dict[str, str]) -> None:
    """Regression (#4387): a failed listing exits non-zero, never 'no open own PRs'."""
    from maestro.vcs import VCSError

    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.side_effect = VCSError("gh pr list failed")
    buf = io.StringIO()
    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host), \
            contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc_info:
        cmd_pr_owed_act(argparse.Namespace(), config)
    assert exc_info.value.code == 1
    assert "no open own PRs" not in buf.getvalue()
    assert "could not list open own PRs: gh pr list failed" in buf.getvalue()
    mock_host.get_open_evolution_pr_states.assert_called_once_with(
        "maestro/evolve/", strict=True,
    )


def test_cmd_pr_owed_act_failed_thread_scan_is_not_handoff_ready(
    mem_fs: dict[str, str],
) -> None:
    """Regression (#4387): a failed thread scan counts as a blocker, not zero."""
    from maestro.vcs import VCSError

    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/scan": _pr_state(
            60, merge_state_status="CLEAN", head_ref="maestro/evolve/scan",
        ),
    }
    mock_host.get_pr_check_conclusion.return_value = "success"
    mock_host.get_pr_review_threads_strict.side_effect = VCSError("graphql 403")
    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host), \
            _stub_staleness():
        output = capture_stdout(cmd_pr_owed_act, argparse.Namespace(), config)

    assert "handoff-ready" not in output
    assert "#60 code-owed" in output


def test_cmd_pr_owed_act_one_line_per_pr(mem_fs: dict[str, str]) -> None:
    """One verdict line per open own PR; the four verdicts render distinctly."""
    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/aaa": _pr_state(
            10, merge_state_status="CLEAN", head_ref="maestro/evolve/aaa",
        ),
        "maestro/evolve/bbb": _pr_state(
            11, merge_state_status="CLEAN", head_ref="maestro/evolve/bbb",
        ),
        "maestro/evolve/ccc": _pr_state(
            12, merge_state_status="BLOCKED", head_ref="maestro/evolve/ccc",
        ),
        "maestro/evolve/ddd": _pr_state(
            13, merge_state_status="BEHIND", head_ref="maestro/evolve/ddd",
        ),
    }
    # #10 pending CI → wait-ci; #11 failing CI → code-owed; #12 green+blocked →
    # handoff-ready; #13 green+behind → external-blocked.
    conclusions = {10: "", 11: "failure", 12: "success", 13: "success"}
    mock_host.get_pr_check_conclusion.side_effect = lambda n: conclusions[n]
    mock_host.get_pr_review_threads_strict.return_value = []
    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host), \
            _stub_staleness():
        output = capture_stdout(cmd_pr_owed_act, argparse.Namespace(), config)

    lines = [ln for ln in output.splitlines() if ln.startswith("#")]
    assert len(lines) == 4
    assert "#10 wait-ci" in output
    assert "#11 code-owed" in output
    assert "#12 handoff-ready" in output
    assert "#13 external-blocked" in output


def test_cmd_pr_owed_act_thread_blocked_is_code_owed(mem_fs: dict[str, str]) -> None:
    """A green+BLOCKED PR with an unresolved AI thread reports code-owed, not handoff."""
    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/threaded": _pr_state(
            40, merge_state_status="BLOCKED", head_ref="maestro/evolve/threaded",
        ),
    }
    mock_host.get_pr_check_conclusion.return_value = "success"
    # One unresolved, non-outdated AI bug thread lacking a terminal reply — the
    # loop's fix-routing selector counts it, so the owned-thread promotion fires.
    mock_host.get_pr_review_threads_strict.return_value = [
        {
            "id": "PRRT_x",
            "isResolved": False,
            "isOutdated": False,
            "comments": {
                "nodes": [
                    {
                        "databaseId": 1,
                        "path": "maestro/cli.py",
                        "body": "🐛 **Thread-blocked PR is misclassified**",
                        "author": {"login": "github-actions[bot]"},
                    },
                ],
            },
        },
    ]
    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host), \
            _stub_staleness():
        output = capture_stdout(cmd_pr_owed_act, argparse.Namespace(), config)

    assert "#40 code-owed" in output
    assert "handoff-ready" not in output


def _ai_thread() -> dict[str, Any]:
    """One unresolved, non-outdated AI bug thread lacking a terminal reply."""
    return {
        "id": "PRRT_y",
        "isResolved": False,
        "isOutdated": False,
        "comments": {
            "nodes": [
                {
                    "databaseId": 2,
                    "path": "maestro/cli.py",
                    "body": "🐛 **Pending-CI PR is misclassified**",
                    "author": {"login": "github-actions[bot]"},
                },
            ],
        },
    }


@pytest.mark.parametrize("merge_state_status", ["BLOCKED", "CLEAN"])
@pytest.mark.parametrize("conclusion", ["pending", ""])
def test_cmd_pr_owed_act_pending_ci_with_thread_is_code_owed(
    mem_fs: dict[str, str], merge_state_status: str, conclusion: str,
) -> None:
    """Regression (#4048): CI in flight must not block thread promotion."""
    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p": _pr_state(
            50, merge_state_status=merge_state_status, head_ref="maestro/evolve/p",
        ),
    }
    mock_host.get_pr_check_conclusion.return_value = conclusion
    mock_host.get_pr_review_threads_strict.return_value = [_ai_thread()]
    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host), \
            _stub_staleness():
        output = capture_stdout(cmd_pr_owed_act, argparse.Namespace(), config)

    assert "#50 code-owed" in output


def test_cmd_pr_owed_act_merge_tree_conflict_is_code_owed(
    mem_fs: dict[str, str],
) -> None:
    """Regression (#4048): a measured merge-tree conflict reads code-owed even
    while the host still reports ``UNKNOWN``."""
    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/c": _pr_state(
            51, merge_state_status="UNKNOWN", head_ref="maestro/evolve/c",
        ),
    }
    mock_host.get_pr_check_conclusion.return_value = "success"
    mock_host.get_pr_review_threads_strict.return_value = []
    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host), \
            _stub_staleness(3, cleanly_mergeable=False):
        output = capture_stdout(cmd_pr_owed_act, argparse.Namespace(), config)

    assert "#51 code-owed" in output


def test_cmd_pr_owed_act_host_mergeable_conflicting_is_code_owed(
    mem_fs: dict[str, str],
) -> None:
    """Regression (#4048): host ``mergeable == CONFLICTING`` reads code-owed."""
    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/h": _pr_state(
            52, merge_state_status="CLEAN", head_ref="maestro/evolve/h",
            mergeable="CONFLICTING",
        ),
    }
    mock_host.get_pr_check_conclusion.return_value = "success"
    mock_host.get_pr_review_threads_strict.return_value = []
    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host), \
            _stub_staleness():
        output = capture_stdout(cmd_pr_owed_act, argparse.Namespace(), config)

    assert "#52 code-owed" in output


def test_cmd_pr_owed_act_excludes_settled(mem_fs: dict[str, str]) -> None:
    """A PR in the settled ledger is excluded from the surfaced verdicts."""
    from maestro.audit_checks_settlement import (
        JsonlSettledVerdictsLedger,
        SettledVerdictRecord,
    )

    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    JsonlSettledVerdictsLedger(config.memory_dir).append(
        [
            SettledVerdictRecord(
                pr_number=20,
                settled=True,
                kind="merged",
                iteration=1,
                verdict_task_id=None,
            ),
        ],
    )
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/settled": _pr_state(
            20, merge_state_status="CLEAN", head_ref="maestro/evolve/settled",
        ),
        "maestro/evolve/live": _pr_state(
            21, merge_state_status="CLEAN", head_ref="maestro/evolve/live",
        ),
    }
    mock_host.get_pr_check_conclusion.return_value = "success"
    mock_host.get_pr_review_threads_strict.return_value = []
    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host), \
            _stub_staleness() as observe:
        output = capture_stdout(cmd_pr_owed_act, argparse.Namespace(), config)

    assert "#20" not in output
    assert "#21 handoff-ready" in output
    # A settled PR is skipped before any per-PR read — no thread/CI fetch for it.
    fetched = [c.args[0] for c in mock_host.get_pr_check_conclusion.call_args_list]
    assert 20 not in fetched
    # The settled short-circuit precedes staleness measurement: the observer is
    # invoked only for the live PR's head (#21), never for the settled #20.
    observed_branches = [c.args[0] for c in observe.call_args_list]
    assert observed_branches == ["maestro/evolve/live"]


def test_cmd_pr_owed_act_reads_flag_selected_settled_backend(
    mem_fs: dict[str, str],
) -> None:
    """Under ``sqlite_state_enabled`` the settled set comes from the seam, not JSONL.

    Regression for #4134: the frozen JSONL seed still settles #20 (retracted on
    the SQLite side), while the live backend settles #21. The handler must
    honour the flag-selected backend — #20 surfaces and is fetched, #21 is
    skipped before any per-PR read.
    """
    from maestro.audit_checks_settlement import (
        JsonlSettledVerdictsLedger,
        SettledVerdictRecord,
    )

    config = _make_config(github_repo="owner/repo", sqlite_state_enabled=True)
    config.ensure_dirs()
    JsonlSettledVerdictsLedger(config.memory_dir).append(
        [
            SettledVerdictRecord(
                pr_number=20,
                settled=True,
                kind="merged",
                iteration=1,
                verdict_task_id=None,
            ),
        ],
    )
    backend = MagicMock()
    backend.effective_settled_prs.return_value = {21}
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/reopened": _pr_state(
            20, merge_state_status="CLEAN", head_ref="maestro/evolve/reopened",
        ),
        "maestro/evolve/settled": _pr_state(
            21, merge_state_status="CLEAN", head_ref="maestro/evolve/settled",
        ),
    }
    mock_host.get_pr_check_conclusion.return_value = "success"
    mock_host.get_pr_review_threads_strict.return_value = []
    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host), \
            patch(
                "maestro.cli._build_settled_verdicts_backend", return_value=backend,
            ) as build, \
            _stub_staleness():
        output = capture_stdout(cmd_pr_owed_act, argparse.Namespace(), config)

    build.assert_called_once_with(config)
    assert "#20 handoff-ready" in output
    assert "#21" not in output
    fetched = [c.args[0] for c in mock_host.get_pr_check_conclusion.call_args_list]
    assert fetched == [20]


def test_cmd_pr_owed_act_reaches_no_mutation_seam(mem_fs: dict[str, str]) -> None:
    """No push/merge/ready/open (create) seam is reachable from the handler."""
    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/aaa": _pr_state(
            30, merge_state_status="CLEAN", head_ref="maestro/evolve/aaa",
        ),
    }
    mock_host.get_pr_check_conclusion.return_value = "failure"
    mock_host.get_pr_review_threads_strict.return_value = []
    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host), \
            _stub_staleness():
        capture_stdout(cmd_pr_owed_act, argparse.Namespace(), config)

    # Explicit: none of the mutating seams was invoked.
    mock_host.push.assert_not_called()
    mock_host.merge_pr.assert_not_called()
    mock_host.mark_pr_ready.assert_not_called()
    mock_host.create_pr.assert_not_called()
    mock_host.reopen_pr.assert_not_called()
    mock_host.close_pr.assert_not_called()
    # Exhaustive: every method the handler *did* call is a known read-only seam.
    called = {name for name, _args, _kwargs in mock_host.method_calls}
    assert called <= {
        "get_open_evolution_pr_states",
        "get_pr_check_conclusion",
        "get_pr_review_threads_strict",
    }


def test_cmd_pr_owed_act_behind_but_clean_reports_catchup(
    mem_fs: dict[str, str],
) -> None:
    """A behind-but-clean open own PR reports the catch-up state, not handoff-ready.

    Bug guard: ``cmd_pr_owed_act`` previously passed a hardcoded
    ``commits_behind=0`` into ``classify_merge_block``, so a PR trailing
    ``origin/main`` yet cleanly mergeable — reported ``CLEAN`` by the
    permissive-protection host proxy — was misclassified ``HANDOFF_READY``.
    Threading the measured ``commits_behind`` routes it to the Tend catch-up
    state (``BEHIND_MAIN`` → ``external-blocked``).
    """
    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/behind": _pr_state(
            50, merge_state_status="CLEAN", head_ref="maestro/evolve/behind",
        ),
    }
    mock_host.get_pr_check_conclusion.return_value = "success"
    mock_host.get_pr_review_threads_strict.return_value = []
    # Measured: 3 commits behind origin/main, no merge-tree conflict (clean).
    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host), \
            _stub_staleness(3, cleanly_mergeable=True):
        output = capture_stdout(cmd_pr_owed_act, argparse.Namespace(), config)

    assert "#50 external-blocked" in output
    assert "handoff-ready" not in output


def test_cmd_pr_owed_act_threads_measured_commits_behind(
    mem_fs: dict[str, str],
) -> None:
    """The measured ``commits_behind`` — not a literal 0 — reaches classify_merge_block."""
    from maestro import remediation_tracker

    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/behind": _pr_state(
            51, merge_state_status="CLEAN", head_ref="maestro/evolve/behind",
        ),
    }
    mock_host.get_pr_check_conclusion.return_value = "success"
    mock_host.get_pr_review_threads_strict.return_value = []
    captured: list[int] = []
    real = remediation_tracker.classify_merge_block

    def _spy(commits_behind: int, conclusion: str, state: str) -> str:
        captured.append(commits_behind)
        return real(commits_behind, conclusion, state)

    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host), \
            _stub_staleness(7), \
            patch(
                "maestro.remediation_tracker.classify_merge_block",
                side_effect=_spy,
            ):
        capture_stdout(cmd_pr_owed_act, argparse.Namespace(), config)

    # The single open PR's measured behind-count flowed through unchanged.
    assert captured == [7]


def test_cmd_pr_owed_act_even_with_main_stays_handoff(
    mem_fs: dict[str, str],
) -> None:
    """An even-with-main (behind=0), clean, green-CI PR still reports handoff-ready."""
    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/even": _pr_state(
            60, merge_state_status="CLEAN", head_ref="maestro/evolve/even",
        ),
    }
    mock_host.get_pr_check_conclusion.return_value = "success"
    mock_host.get_pr_review_threads_strict.return_value = []
    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host), \
            _stub_staleness(0):
        output = capture_stdout(cmd_pr_owed_act, argparse.Namespace(), config)

    assert "#60 handoff-ready" in output
    assert "external-blocked" not in output


def test_cmd_pr_owed_act_settled_skips_staleness_probe(
    mem_fs: dict[str, str],
) -> None:
    """A settled PR is skipped before staleness measurement — observer never runs for it."""
    from maestro.audit_checks_settlement import (
        JsonlSettledVerdictsLedger,
        SettledVerdictRecord,
    )

    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    JsonlSettledVerdictsLedger(config.memory_dir).append(
        [
            SettledVerdictRecord(
                pr_number=70,
                settled=True,
                kind="merged",
                iteration=1,
                verdict_task_id=None,
            ),
        ],
    )
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/settled": _pr_state(
            70, merge_state_status="CLEAN", head_ref="maestro/evolve/settled",
        ),
    }
    mock_host.get_pr_check_conclusion.return_value = "success"
    mock_host.get_pr_review_threads_strict.return_value = []
    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host), \
            _stub_staleness() as observe:
        capture_stdout(cmd_pr_owed_act, argparse.Namespace(), config)

    # Criterion 4: the only open PR is settled, so the staleness probe — which
    # would incur a fresh local-git read — is never reached.
    observe.assert_not_called()


def test_cmd_pr_owed_act_staleness_oracles_read_local_git(
    mem_fs: dict[str, str],
) -> None:
    """The staleness oracles measure ``origin/<branch>`` vs ``origin/main`` via VCS.

    Exercises the real ``observe_pr_staleness`` (unpatched) over a mocked
    :class:`SubprocessVCS`, so the injected git oracles actually run: a head
    2 commits behind ``origin/main`` with no merge-tree conflict is measured
    ``commits_behind=2`` and routes to ``external-blocked``. Asserts each oracle
    reads the expected remote refs — the behaviour the loop adapter mirrors.
    """
    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/behind": _pr_state(
            80, merge_state_status="CLEAN", head_ref="maestro/evolve/behind",
        ),
    }
    mock_host.get_pr_check_conclusion.return_value = "success"
    mock_host.get_pr_review_threads_strict.return_value = []
    mock_vcs = MagicMock()
    mock_vcs.commits_ahead_behind.return_value = (0, 2)
    mock_vcs.merge_tree_conflicts.return_value = False
    mock_vcs.rev_parse.return_value = "basesha"
    with patch("maestro.cli.SubprocessCodeHost", return_value=mock_host), \
            patch("maestro.cli.SubprocessVCS", return_value=mock_vcs):
        output = capture_stdout(cmd_pr_owed_act, argparse.Namespace(), config)

    assert "#80 external-blocked" in output
    assert "handoff-ready" not in output
    # The behind-count oracle compares the remote head against origin/main.
    mock_vcs.commits_ahead_behind.assert_called_once_with(
        "origin/maestro/evolve/behind", "origin/main",
    )
    # The clean-merge oracle probes the same pair in merge-tree order.
    mock_vcs.merge_tree_conflicts.assert_called_once_with(
        "origin/main", "origin/maestro/evolve/behind",
    )
    mock_vcs.rev_parse.assert_called_once_with("origin/main")


def test_main_dispatches_pr_owed_act(mem_fs: dict[str, str]) -> None:
    """main() routes ``pr-owed-act`` to the handler."""
    from maestro.cli import main

    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    mock_host = MagicMock()
    mock_host.get_open_evolution_pr_states.return_value = {}
    with patch("sys.argv", ["maestro", "pr-owed-act"]), \
         patch("maestro.cli.Config.from_env", return_value=config), \
         patch("maestro.cli.configure_provider"), \
         patch("maestro.cli.SubprocessCodeHost", return_value=mock_host):
        output = capture_stdout(main)
    assert "no open own PRs" in output


def test_main_dispatches_owned_acts(mem_fs: dict[str, str]) -> None:
    """main() routes ``owned-acts -n 5 --json`` to the formatter with args."""
    from maestro.cli import main

    config = _make_config()
    config.ensure_dirs()
    with patch("sys.argv", ["maestro", "owned-acts", "-n", "5", "--json"]), \
         patch("maestro.cli.Config.from_env", return_value=config), \
         patch("maestro.cli.configure_provider"), \
         patch(
             "maestro.remediation_tracker.format_owned_acts_report",
             return_value="SENTINEL_OUTPUT",
         ) as mock_fmt:
        output = capture_stdout(main)

    mock_fmt.assert_called_once_with(
        config.memory_dir, 5, True, stand_down_backend=ANY, deed_backend=ANY,
    )
    assert "SENTINEL_OUTPUT" in output


def test_owned_acts_includes_forced_handoff_row(mem_fs: dict[str, str]) -> None:
    """owned-acts projects a seeded HANDOFF_DELIVERED deed as a forced row.

    The deed-ledger backend is threaded into ``format_owned_acts_report`` so a
    fired handoff deed surfaces as a ``handoff`` row flagged ``forced`` — and no
    chosen-arm handoff row is producible (the state is unreachable in prod).
    """
    from maestro import deed_ledger
    from maestro.cli import main

    config = _make_config()
    config.ensure_dirs()
    deed_ledger.record_deed(
        config.memory_dir,
        deed_ledger.DeedType.HANDOFF_DELIVERED,
        4242,
        "handed off PR #4242",
    )
    with patch("sys.argv", ["maestro", "owned-acts"]), \
         patch("maestro.cli.Config.from_env", return_value=config), \
         patch("maestro.cli.configure_provider"):
        output = capture_stdout(main)

    assert "handoff #4242 (forced)" in output
    assert "(chosen)" not in output


# ── cmd_list_proposals (proposal 118) ────────────────────────────────


def _seed_proposal_with_slices(config: Config) -> None:
    """Seed one EvolutionProposal plus two linked, ordered slice plans."""
    memory = Memory(config.memory_dir)
    memory.write_evolution_proposal(EvolutionProposal(
        proposal_id="prop-1",
        title="Split the big refactor",
        goal="Land it across ordered slices.",
        status="rolling_out",
        created_at="2026-08-24T00:00:00Z",
    ))
    memory.save_evolution_plan(EvolutionPlan(
        plan_id="slice-0",
        title="First slice",
        category="capability",
        rationale="reason",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        status="merged",
        pr_url="https://example/pr/1",
        parent_plan_id="prop-1",
        phase_index=0,
    ))
    memory.save_evolution_plan(EvolutionPlan(
        plan_id="slice-1",
        title="Second slice",
        category="capability",
        rationale="reason",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        status="sliced",
        parent_plan_id="prop-1",
        phase_index=1,
    ))


def test_cmd_list_proposals_renders_status_and_ordered_slices(
    mem_fs: dict[str, str],
) -> None:
    """The human table shows the proposal's rollout status and each slice's
    ordered status/pr_url."""
    config = _make_config()
    config.ensure_dirs()
    _seed_proposal_with_slices(config)

    args = argparse.Namespace(json=False)
    output = capture_stdout(cmd_list_proposals, args, config)

    assert "prop-1 [rolling_out] Split the big refactor" in output
    assert (
        "phase 0: slice-0 [merged] First slice (https://example/pr/1)"
        in output
    )
    assert "phase 1: slice-1 [sliced] Second slice (-)" in output
    # Slices render in phase_index order.
    assert output.index("slice-0") < output.index("slice-1")


def test_cmd_list_proposals_json_is_parseable(mem_fs: dict[str, str]) -> None:
    """The --json variant emits json.loads-parseable output with ordered slices."""
    config = _make_config()
    config.ensure_dirs()
    _seed_proposal_with_slices(config)

    args = argparse.Namespace(json=True)
    output = capture_stdout(cmd_list_proposals, args, config)

    data = json.loads(output)
    assert len(data) == 1
    assert data[0]["proposal_id"] == "prop-1"
    assert data[0]["status"] == "rolling_out"
    slices = data[0]["slices"]
    assert [s["phase_index"] for s in slices] == [0, 1]
    assert slices[0]["status"] == "merged"
    assert slices[0]["pr_url"] == "https://example/pr/1"
    assert slices[1]["pr_url"] is None


def test_cmd_list_proposals_empty_store_message(mem_fs: dict[str, str]) -> None:
    """With no proposals recorded, the empty-store message prints."""
    config = _make_config()
    config.ensure_dirs()

    args = argparse.Namespace(json=False)
    output = capture_stdout(cmd_list_proposals, args, config)

    assert "No evolution proposals recorded." in output


def test_cmd_list_proposals_proposal_without_slices(
    mem_fs: dict[str, str],
) -> None:
    """A proposal with no linked slice plans renders the '(no slices)' marker."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.write_evolution_proposal(EvolutionProposal(
        proposal_id="prop-empty",
        title="Not yet sliced",
        goal="No slices minted yet.",
        status="slicing",
        created_at="2026-08-24T00:00:00Z",
    ))

    args = argparse.Namespace(json=False)
    output = capture_stdout(cmd_list_proposals, args, config)

    assert "prop-empty [slicing] Not yet sliced" in output
    assert "(no slices)" in output


def test_list_proposals_registered_in_handlers() -> None:
    """The list-proposals subcommand is wired into the parser and handler map."""
    with patch.object(sys, "argv", ["maestro", "list-proposals", "--json"]), \
            patch("maestro.cli.cmd_list_proposals") as mock_handler, \
            patch("maestro.cli.Config.from_env", return_value=_make_config()):
        from maestro.cli import main
        main()
    mock_handler.assert_called_once()


# ── cmd_reviews ────────────────────────────────────────────────────


def _make_review(
    review_id: str = "r1",
    task_id: str = "t1",
    verdict: str = "publish",
    title: str = "Nice artifact",
    reasoning: str = "Self-contained and polished",
    timestamp: str = "2026-04-22T10:00:00",
) -> ArtifactReview:
    return ArtifactReview(
        review_id=review_id,
        task_id=task_id,
        verdict=verdict,  # type: ignore[arg-type]
        title=title,
        reasoning=reasoning,
        bundle_file_names=["README.md"],
        proposed_name="nice-artifact",
        proposed_description="A nice artifact",
        timestamp=timestamp,
        proposed_readme="# Nice\n" + "Body. " * 200,
    )


def test_cmd_reviews_empty(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(count=None, verdict=None, json=False)
    output = capture_stdout(cmd_reviews, args, config)
    assert "No artifact reviews" in output


def test_cmd_reviews_default_output(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.write_artifact_review(_make_review())
    args = argparse.Namespace(count=None, verdict=None, json=False)
    output = capture_stdout(cmd_reviews, args, config)
    assert "2026-04-22" in output
    assert "publish" in output
    assert "t1" in output
    assert "Nice artifact" in output


def test_cmd_reviews_verdict_filter(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.write_artifact_review(_make_review(
        review_id="r1", task_id="t1", verdict="publish", title="Ship it",
    ))
    memory.write_artifact_review(_make_review(
        review_id="r2", task_id="t2", verdict="skip", title="Skip me",
    ))
    args = argparse.Namespace(count=None, verdict="publish", json=False)
    output = capture_stdout(cmd_reviews, args, config)
    assert "Ship it" in output
    assert "Skip me" not in output


def test_cmd_reviews_json_output(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.write_artifact_review(_make_review(review_id="r1", task_id="t1"))
    args = argparse.Namespace(count=None, verdict=None, json=True)
    output = capture_stdout(cmd_reviews, args, config)
    data = json.loads(output)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["review_id"] == "r1"


# ── cmd_publications (proposal 032) ──────────────────────────────


def _make_proposal(
    proposal_id: str = "prop-1",
    status: str = "pending_approval",
    proposed_name: str = "cool-thing",
    created_at: str = "2026-04-24T00:00:00+00:00",
    rejection_reason: str | None = None,
    published_url: str | None = None,
) -> PublicationProposal:
    return PublicationProposal(
        proposal_id=proposal_id,
        task_id="t1",
        review_id="r1",
        proposed_name=proposed_name,
        description="A cool thing",
        proposed_readme="# cool",
        publishable_files=["README.md"],
        status=status,  # type: ignore[arg-type]
        created_at=created_at,
        rejection_reason=rejection_reason,
        published_url=published_url,
    )


def test_cmd_publications_empty(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(count=None, status=None, json=False)
    output = capture_stdout(cmd_publications, args, config)
    assert "No publication proposals" in output


def test_cmd_publications_default_output(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.write_publication_proposal(_make_proposal())
    args = argparse.Namespace(count=None, status=None, json=False)
    output = capture_stdout(cmd_publications, args, config)
    assert "2026-04-24" in output
    assert "pending_approval" in output
    assert "prop-1" in output
    assert "cool-thing" in output


def test_cmd_publications_filters_by_status(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.write_publication_proposal(_make_proposal(
        proposal_id="p-a", proposed_name="alpha",
    ))
    memory.write_publication_proposal(_make_proposal(
        proposal_id="p-b",
        status="rejected",
        proposed_name="beta",
        rejection_reason="not ready",
    ))
    args = argparse.Namespace(
        count=None, status="rejected", json=False,
    )
    output = capture_stdout(cmd_publications, args, config)
    assert "beta" in output
    assert "alpha" not in output
    assert "not ready" in output


def test_cmd_publications_json_output(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.write_publication_proposal(_make_proposal(proposal_id="prop-json"))
    args = argparse.Namespace(count=None, status=None, json=True)
    output = capture_stdout(cmd_publications, args, config)
    rows = json.loads(output)
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["proposal_id"] == "prop-json"


def test_cmd_publications_count_zero_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.write_publication_proposal(_make_proposal())
    args = argparse.Namespace(count=0, status=None, json=False)
    output = capture_stdout(cmd_publications, args, config)
    assert "No publication proposals" in output


def test_cmd_publications_published_url_printed(
    mem_fs: dict[str, str],
) -> None:
    """A ``published`` row with a url surfaces the url in text output."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.write_publication_proposal(_make_proposal(
        proposal_id="p-live",
        status="published",
        published_url="https://github.com/owner/repo",
    ))
    args = argparse.Namespace(count=None, status=None, json=False)
    output = capture_stdout(cmd_publications, args, config)
    assert "https://github.com/owner/repo" in output
    assert "url:" in output


def test_cmd_reviews_json_empty(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(count=None, verdict=None, json=True)
    output = capture_stdout(cmd_reviews, args, config)
    assert json.loads(output) == []


def test_cmd_reviews_count_flag(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    for i in range(5):
        memory.write_artifact_review(_make_review(
            review_id=f"r{i}", task_id=f"t{i}", title=f"Review {i}",
        ))
    args = argparse.Namespace(count=2, verdict=None, json=False)
    output = capture_stdout(cmd_reviews, args, config)
    assert "Review 3" in output
    assert "Review 4" in output
    assert "Review 0" not in output


# ── cmd_email ──────────────────────────────────────────────────────


def test_build_email_client_missing_config() -> None:
    config = _make_config(email_address=None, email_password=None)
    assert _build_email_client(config) is None


def test_build_email_client_success() -> None:
    config = _make_config(
        email_address="agent@test.xyz",
        email_password="secret",
    )
    client = _build_email_client(config)
    assert client._address == "agent@test.xyz"


def test_build_agent_with_email(mem_fs: dict[str, str]) -> None:
    config = _make_config(
        email_address="agent@test.xyz",
        email_password="secret",
    )
    agent = _build_agent(config)
    assert agent.email_client is not None
    assert agent.email_client._address == "agent@test.xyz"


def test_build_agent_without_email(mem_fs: dict[str, str]) -> None:
    config = _make_config(email_address=None, email_password=None)
    agent = _build_agent(config)
    assert agent.email_client is None


def test_cmd_email_send(mem_fs: dict[str, str]) -> None:
    config = _make_config(email_address="a@b.com", email_password="s")
    args = argparse.Namespace(
        email_command="send", to="to@b.com", subject="Hi", body="Hello",
    )
    with patch("maestro.cli.asyncio.run", return_value="<msg@id>"):
        output = capture_stdout(cmd_email, args, config)
    assert "Sent" in output
    assert "<msg@id>" in output


def test_cmd_email_inbox_empty(mem_fs: dict[str, str]) -> None:
    config = _make_config(email_address="a@b.com", email_password="s")
    args = argparse.Namespace(email_command="inbox", count=20, show_all=False)
    with patch("maestro.cli.asyncio.run", return_value=[]):
        output = capture_stdout(cmd_email, args, config)
    assert "No emails found" in output


def test_cmd_email_inbox_with_messages(mem_fs: dict[str, str]) -> None:
    from maestro.email import EmailMessage

    msgs = [
        EmailMessage(
            uid="1", from_addr="sender@test.com", subject="Test Subject",
            body="body", date="2026-04-01", message_id="<x@y>", seen=False,
        ),
    ]
    config = _make_config(email_address="a@b.com", email_password="s")
    args = argparse.Namespace(email_command="inbox", count=20, show_all=False)
    with patch("maestro.cli.asyncio.run", return_value=msgs):
        output = capture_stdout(cmd_email, args, config)
    assert "sender@test.com" in output
    assert "Test Subject" in output
    assert "*" in output  # unseen marker


@pytest.mark.parametrize("bad_count", [0, -1])
def test_cmd_email_inbox_non_positive_count_rejected(
    mem_fs: dict[str, str], bad_count: int,
) -> None:
    """`maestro email inbox -n 0` must exit non-zero before any IMAP round-trip:
    fetch_inbox slices uids[-count:], so count<=0 would fetch the whole mailbox."""
    config = _make_config(email_address="a@b.com", email_password="s")
    args = argparse.Namespace(
        email_command="inbox", count=bad_count, show_all=False,
    )
    with patch("maestro.cli.asyncio.run") as run_mock, pytest.raises(SystemExit) as exc:
        cmd_email(args, config)
    assert exc.value.code == 1
    run_mock.assert_not_called()


def test_cmd_email_read(mem_fs: dict[str, str]) -> None:
    from maestro.email import EmailMessage

    msg = EmailMessage(
        uid="42", from_addr="sender@test.com", subject="Read Me",
        body="Email body text", date="2026-04-01",
        message_id="<read@test>", seen=True,
    )
    config = _make_config(email_address="a@b.com", email_password="s")
    args = argparse.Namespace(email_command="read", uid="42")
    with patch("maestro.cli.asyncio.run", return_value=msg):
        output = capture_stdout(cmd_email, args, config)
    assert "sender@test.com" in output
    assert "Read Me" in output
    assert "Email body text" in output


def test_cmd_email_read_requests_mark_seen(mem_fs: dict[str, str]) -> None:
    """The ``read`` branch must ask ``fetch_by_uid`` to flag the message
    ``\\Seen`` (issue #239): opening an *unseen* message should mark it read so
    it stops resurfacing. Guards the ``mark_seen=True`` wiring the CLI-only
    ``asyncio.run`` patch in ``test_cmd_email_read`` never asserts."""
    from maestro.email import EmailMessage

    msg = EmailMessage(
        uid="42", from_addr="sender@test.com", subject="Read Me",
        body="Email body text", date="2026-04-01",
        message_id="<read@test>", seen=False,
    )
    client = MagicMock()
    client.fetch_by_uid = MagicMock(return_value=msg)
    config = _make_config(email_address="a@b.com", email_password="s")
    args = argparse.Namespace(email_command="read", uid="42")
    with patch("maestro.cli._build_email_client", return_value=client), \
         patch("maestro.cli.asyncio.run", return_value=msg):
        output = capture_stdout(cmd_email, args, config)
    client.fetch_by_uid.assert_called_once_with("42", mark_seen=True)
    assert "Read Me" in output
    assert "Email body text" in output


def test_cmd_email_read_not_found(mem_fs: dict[str, str]) -> None:
    config = _make_config(email_address="a@b.com", email_password="s")
    args = argparse.Namespace(email_command="read", uid="999")
    with patch("maestro.cli.asyncio.run", return_value=None), pytest.raises(SystemExit):
        cmd_email(args, config)


def test_cmd_email_no_subcommand(mem_fs: dict[str, str]) -> None:
    config = _make_config(email_address="a@b.com", email_password="s")
    args = argparse.Namespace(email_command=None)
    with pytest.raises(SystemExit):
        cmd_email(args, config)


# ── cmd_notify_pre_deploy ───────────────────────────────────────────


def _pre_deploy_args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "ref": None,
        "from_ref": None,
        "pr": None,
        "run_id": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_extract_pr_numbers_merge_and_squash() -> None:
    assert _extract_pr_numbers("Merge pull request #42 from foo/bar") == [42]
    assert _extract_pr_numbers("Fix dashboard redirect (#123)") == [123]
    assert _extract_pr_numbers("unrelated commit") == []


def test_resolve_pre_deploy_prs_dedupes_and_orders() -> None:
    from maestro.vcs import CommitInfo

    commits = [
        CommitInfo(hash="a", subject="Merge pull request #10 from foo/a", author="x"),
        CommitInfo(hash="b", subject="Another thing (#20)", author="y"),
        CommitInfo(hash="c", subject="dup (#10)", author="z"),
    ]
    with patch("maestro.cli.SubprocessVCS") as mock_vcs_cls:
        mock_vcs_cls.return_value.get_commits_between.return_value = commits
        result = _resolve_pre_deploy_prs(
            from_ref="old", to_ref="new", explicit=[5, 10],
            workspace=Path("/fake"),
        )
    # Explicit first, commit-discovered appended, duplicates removed.
    assert result == [5, 10, 20]


def test_resolve_pre_deploy_prs_no_range_returns_explicit() -> None:
    result = _resolve_pre_deploy_prs(
        from_ref=None, to_ref=None, explicit=[7], workspace=Path("/fake"),
    )
    assert result == [7]


def test_resolve_pre_deploy_prs_vcs_failure_is_tolerated() -> None:
    with patch("maestro.cli.SubprocessVCS", side_effect=RuntimeError("boom")):
        result = _resolve_pre_deploy_prs(
            from_ref="old", to_ref="new", explicit=[1],
            workspace=Path("/fake"),
        )
    assert result == [1]


def test_compose_pre_deploy_content_with_prs() -> None:
    prs = [
        (123, "Add pre-deploy notice", "Long body " * 50),
        (124, "Fix bug", ""),
    ]
    content = _compose_pre_deploy_content("abc1234567890def", "42", prs)
    assert "ref=abc123456789" in content
    assert "run=42" in content
    assert "#123 Add pre-deploy notice" in content
    assert "#124 Fix bug" in content
    # Body truncated with ellipsis
    assert "…" in content


def test_compose_pre_deploy_content_no_prs() -> None:
    content = _compose_pre_deploy_content(None, None, [])
    assert "no PR metadata available" in content
    assert "ref=unknown" in content
    assert "run=manual" in content


def test_compose_pre_deploy_content_enforces_overall_cap() -> None:
    prs = [(i, f"Title {i}", "body " * 100) for i in range(200)]
    content = _compose_pre_deploy_content("abc", "1", prs)
    assert len(content) <= 4000


def test_cmd_notify_pre_deploy_writes_message_and_journal(
    mem_fs: dict[str, str],
) -> None:
    from maestro.vcs import CommitInfo

    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()

    commits = [
        CommitInfo(
            hash="h1", subject="Merge pull request #99 from foo/x", author="a",
        ),
    ]

    with patch("maestro.cli.SubprocessVCS") as mock_vcs_cls, \
         patch("maestro.cli.SubprocessCodeHost") as mock_host_cls:
        mock_vcs_cls.return_value.get_commits_between.return_value = commits
        mock_host_cls.return_value.get_pr_info.return_value = {
            "title": "A title", "body": "A body",
        }
        args = _pre_deploy_args(
            ref="deadbeef", from_ref="cafebabe", run_id="42", pr=[7],
        )
        cmd_notify_pre_deploy(args, config)

    # Message persisted
    messages_path = str(config.memory_dir / "messages.jsonl")
    assert messages_path in mem_fs
    line = mem_fs[messages_path].strip()
    msg = json.loads(line)
    assert msg["sender"] == "thomas"
    assert msg["message_type"] == "info"
    assert msg["read"] is False
    assert msg["message_id"].startswith("pre-deploy-")
    assert "#7" in msg["content"]
    assert "#99 A title" in msg["content"]
    assert "A body" in msg["content"]

    # Milestone recorded as a journal event, never as an inner thought
    assert str(config.memory_dir / "journal.jsonl") not in mem_fs
    events_path = str(config.memory_dir / "events.jsonl")
    entry = json.loads(mem_fs[events_path].strip().splitlines()[-1])
    assert entry["entry_type"] == "milestone"
    assert entry["triggered_by"] == "pre_deploy"
    assert "#99" in entry["content"]

    # get_pr_info called once per unique PR number
    assert mock_host_cls.return_value.get_pr_info.call_count == 2


def test_cmd_notify_pre_deploy_get_pr_info_failure_is_tolerated(
    mem_fs: dict[str, str],
) -> None:
    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()

    with patch("maestro.cli.SubprocessCodeHost") as mock_host_cls:
        mock_host_cls.return_value.get_pr_info.side_effect = RuntimeError("gh down")
        args = _pre_deploy_args(ref="abc", pr=[1, 2])
        cmd_notify_pre_deploy(args, config)

    messages_path = str(config.memory_dir / "messages.jsonl")
    assert messages_path in mem_fs
    msg = json.loads(mem_fs[messages_path].strip())
    # PRs still listed even without titles
    assert "#1" in msg["content"]
    assert "#2" in msg["content"]


def test_cmd_notify_pre_deploy_message_store_failure_still_journals(
    mem_fs: dict[str, str],
) -> None:
    config = _make_config()
    config.ensure_dirs()

    with patch(
        "maestro.cli.MessageStore.save_message", side_effect=OSError("disk full"),
    ):
        args = _pre_deploy_args(ref="abc")
        # Must not raise
        cmd_notify_pre_deploy(args, config)

    # No message file created, but the milestone event is still recorded
    events_path = str(config.memory_dir / "events.jsonl")
    assert events_path in mem_fs
    entry = json.loads(mem_fs[events_path].strip().splitlines()[-1])
    assert entry["triggered_by"] == "pre_deploy"


def test_cmd_notify_pre_deploy_journal_failure_is_tolerated(
    mem_fs: dict[str, str],
) -> None:
    config = _make_config()
    config.ensure_dirs()

    with patch(
        "maestro.cli.Journal.record_event", side_effect=RuntimeError("nope"),
    ):
        args = _pre_deploy_args(ref="abc")
        cmd_notify_pre_deploy(args, config)  # must not raise

    # Message still persisted despite journal failure
    messages_path = str(config.memory_dir / "messages.jsonl")
    assert messages_path in mem_fs


def test_cmd_notify_pre_deploy_code_host_init_failure_is_tolerated(
    mem_fs: dict[str, str],
) -> None:
    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()

    with patch(
        "maestro.cli.SubprocessCodeHost", side_effect=RuntimeError("gh init fail"),
    ):
        args = _pre_deploy_args(ref="abc", pr=[1])
        cmd_notify_pre_deploy(args, config)

    messages_path = str(config.memory_dir / "messages.jsonl")
    assert messages_path in mem_fs
    msg = json.loads(mem_fs[messages_path].strip())
    # PR still listed without title/body since code_host is None
    assert "#1" in msg["content"]


def test_cmd_notify_pre_deploy_unexpected_error_is_tolerated(
    mem_fs: dict[str, str],
) -> None:
    config = _make_config()
    config.ensure_dirs()

    with patch(
        "maestro.cli._resolve_pre_deploy_prs",
        side_effect=RuntimeError("unexpected"),
    ):
        args = _pre_deploy_args(ref="abc")
        # Must not raise — outer exception handler swallows it.
        cmd_notify_pre_deploy(args, config)


def test_cmd_notify_pre_deploy_no_args_uses_defaults(
    mem_fs: dict[str, str],
) -> None:
    config = _make_config()
    config.ensure_dirs()

    args = _pre_deploy_args()
    cmd_notify_pre_deploy(args, config)

    messages_path = str(config.memory_dir / "messages.jsonl")
    assert messages_path in mem_fs
    msg = json.loads(mem_fs[messages_path].strip())
    assert "ref=unknown" in msg["content"]
    assert "run=manual" in msg["content"]
    assert "no PR metadata available" in msg["content"]


def test_main_dispatches_notify_pre_deploy(mem_fs: dict[str, str]) -> None:
    from maestro.cli import main

    with patch("sys.argv", [
        "maestro", "notify-pre-deploy", "--ref", "abc", "--run-id", "7",
    ]), patch("maestro.cli.cmd_notify_pre_deploy") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


# ── cmd_docs (doc-proposals pipeline) ────────────────────────────


def _make_doc_record(
    doc_id: str = "d-001",
    *,
    doc_type: str = "core",
    title: str = "Sample doc",
    linked_task_ids: list[str] | None = None,
    target_path: str | None = None,
    pr_url: str | None = None,
    status: str = "pending",
    error: str | None = None,
    created_at: str = "2026-04-25T00:00:00+00:00",
) -> DocumentRecord:
    return DocumentRecord(
        doc_id=doc_id,
        doc_type=doc_type,  # type: ignore[arg-type]
        title=title,
        linked_task_ids=list(linked_task_ids or ["t-001"]),
        target_path=target_path,
        doc_branch=None,
        pr_url=pr_url,
        bundle_file_names=[],
        reasoning="",
        status=status,  # type: ignore[arg-type]
        error=error,
        created_at=created_at,
    )


def _seed_doc_record(memory: Memory, record: DocumentRecord) -> None:
    """Append one historical document row (the writer was retired, proposal 149)."""
    from dataclasses import asdict

    with open(memory._documents_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record)) + "\n")


def test_cmd_docs_empty(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(
        count=None, doc_type=None, status=None, task=None, json=False,
    )
    output = capture_stdout(cmd_docs, args, config)
    assert "No document proposal records." in output


def test_cmd_docs_default_output(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    _seed_doc_record(memory, _make_doc_record(
        target_path="docs/proposals/core/044-sample.md",
        pr_url="https://github.com/owner/repo/pull/77",
        status="pr_opened",
    ))
    args = argparse.Namespace(
        count=None, doc_type=None, status=None, task=None, json=False,
    )
    output = capture_stdout(cmd_docs, args, config)
    assert "2026-04-25" in output
    assert "pr_opened" in output
    assert "core" in output
    assert "d-001" in output
    assert "Sample doc" in output
    assert "tasks: t-001" in output
    assert "path: docs/proposals/core/044-sample.md" in output
    assert "pr: https://github.com/owner/repo/pull/77" in output


def test_cmd_docs_filters_by_status(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    _seed_doc_record(memory, _make_doc_record(
        doc_id="d-pending", status="pending", title="Still pending",
    ))
    _seed_doc_record(memory, _make_doc_record(
        doc_id="d-failed", status="failed", title="Bad entry",
        error="invalid_linked_task_ids",
    ))
    args = argparse.Namespace(
        count=None, doc_type=None, status="failed", task=None, json=False,
    )
    output = capture_stdout(cmd_docs, args, config)
    assert "Bad entry" in output
    assert "Still pending" not in output
    assert "error: invalid_linked_task_ids" in output


def test_cmd_docs_filters_by_type(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    _seed_doc_record(memory, _make_doc_record(
        doc_id="d-core", doc_type="core", title="Core change",
    ))
    _seed_doc_record(memory, _make_doc_record(
        doc_id="d-research", doc_type="research", title="Research note",
    ))
    args = argparse.Namespace(
        count=None, doc_type="research", status=None, task=None, json=False,
    )
    output = capture_stdout(cmd_docs, args, config)
    assert "Research note" in output
    assert "Core change" not in output


def test_cmd_docs_filters_by_task(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    _seed_doc_record(memory, _make_doc_record(
        doc_id="d-1", linked_task_ids=["t-a", "t-b"], title="Pair",
    ))
    _seed_doc_record(memory, _make_doc_record(
        doc_id="d-2", linked_task_ids=["t-z"], title="Solo",
    ))
    args = argparse.Namespace(
        count=None, doc_type=None, status=None, task="t-b", json=False,
    )
    output = capture_stdout(cmd_docs, args, config)
    assert "Pair" in output
    assert "Solo" not in output


def test_cmd_docs_count_zero_returns_empty(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    _seed_doc_record(memory, _make_doc_record())
    args = argparse.Namespace(
        count=0, doc_type=None, status=None, task=None, json=False,
    )
    output = capture_stdout(cmd_docs, args, config)
    assert "No document proposal records." in output


def test_cmd_docs_count_caps_to_n(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    for i in range(5):
        _seed_doc_record(memory, _make_doc_record(
            doc_id=f"d-{i}", title=f"Doc {i}",
        ))
    args = argparse.Namespace(
        count=2, doc_type=None, status=None, task=None, json=False,
    )
    output = capture_stdout(cmd_docs, args, config)
    assert "Doc 4" in output
    assert "Doc 3" in output
    assert "Doc 0" not in output


def test_cmd_docs_json_output(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    _seed_doc_record(memory, _make_doc_record(doc_id="d-json"))
    args = argparse.Namespace(
        count=None, doc_type=None, status=None, task=None, json=True,
    )
    output = capture_stdout(cmd_docs, args, config)
    rows = json.loads(output)
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["doc_id"] == "d-json"


def test_cmd_docs_truncates_long_error(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    _seed_doc_record(memory, _make_doc_record(
        doc_id="d-err", status="failed", error="x" * 200,
    ))
    args = argparse.Namespace(
        count=None, doc_type=None, status=None, task=None, json=False,
    )
    output = capture_stdout(cmd_docs, args, config)
    # Only the first 80 chars surface in the human view.
    assert "x" * 80 in output
    assert "x" * 100 not in output


def test_cmd_docs_no_created_at_falls_back_to_unknown(
    mem_fs: dict[str, str],
) -> None:
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    _seed_doc_record(memory, _make_doc_record(
        doc_id="d-bare", created_at="",
    ))
    args = argparse.Namespace(
        count=None, doc_type=None, status=None, task=None, json=False,
    )
    output = capture_stdout(cmd_docs, args, config)
    assert "[unknown |" in output


# --- reality-inventory (proposal 136) ----------------------------------------


def _seed_reality_state(config: Config) -> None:
    from maestro.audit_checks import AuditFinding
    from maestro.memory import AuditResult

    Memory(config.memory_dir).append_audit_result(AuditResult(
        iteration=1, timestamp="2026-01-01T00:00:00Z",
        findings=[AuditFinding(
            kind="contradicted_claim", statement="stale claim", rule="stale_entry",
        )],
    ))


def test_reality_inventory_subcommand_registered(mem_fs: dict[str, str]) -> None:
    """main() routes 'reality-inventory' to its handler."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "reality-inventory", "--json"]), \
         patch("maestro.cli.cmd_reality_inventory") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


def test_cmd_reality_inventory_empty(mem_fs: dict[str, str]) -> None:
    """Empty state prints per-kind capability counts and 'No measured problems.'."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(json=False)
    output = capture_stdout(cmd_reality_inventory, args, config)
    assert "Reality Inventory" in output
    assert "  cli: " in output
    assert "  gate: " in output
    assert "No measured problems." in output


def test_cmd_reality_inventory_human_output(mem_fs: dict[str, str]) -> None:
    """A seeded audit finding prints as a measured problem line."""
    config = _make_config()
    config.ensure_dirs()
    _seed_reality_state(config)
    args = argparse.Namespace(json=False)
    output = capture_stdout(cmd_reality_inventory, args, config)
    assert "Measured problems:" in output
    assert "  audit:stale_entry (weight 1): stale claim" in output


def test_cmd_reality_inventory_json_is_read_only(mem_fs: dict[str, str]) -> None:
    """--json emits both keys and writes nothing to the in-memory FS."""
    config = _make_config()
    config.ensure_dirs()
    _seed_reality_state(config)
    before = dict(mem_fs)
    args = argparse.Namespace(json=True)
    data = json.loads(capture_stdout(cmd_reality_inventory, args, config))
    assert mem_fs == before
    assert set(data) == {"shipped_capabilities", "measured_problems"}
    assert "cli:reality-inventory" in data["shipped_capabilities"]
    assert data["shipped_capabilities"] == sorted(data["shipped_capabilities"])
    assert data["measured_problems"] == [{
        "id": "audit:stale_entry", "source": "audit",
        "summary": "stale claim", "weight": 1,
    }]


def test_cmd_reality_inventory_reads_flag_selected_backends(
    mem_fs: dict[str, str],
) -> None:
    """Under sqlite_state_enabled the audit/rejection rows come from the builders."""
    from maestro.audit_checks import AuditFinding
    from maestro.memory import (
        AuditResult,
        JsonlAuditHistoryLedger,
        JsonlRejectionHistoryLedger,
        RejectionRecord,
    )

    config = _make_config(sqlite_state_enabled=True)
    config.ensure_dirs()
    routed_dir = config.memory_dir / "routed"
    routed_dir.mkdir(parents=True, exist_ok=True)
    audit_backend = JsonlAuditHistoryLedger(routed_dir)
    audit_backend.record(AuditResult(
        iteration=1, timestamp="2026-01-01T00:00:00Z",
        findings=[AuditFinding(
            kind="contradicted_claim", statement="routed claim", rule="stale_entry",
        )],
    ))
    rejection_backend = JsonlRejectionHistoryLedger(routed_dir)
    rejection_backend.record(RejectionRecord(
        plan_id="p1", title="t", category="capability", priority="capability",
        rejection_reason="already shipped", timestamp="2026-01-01T00:00:00Z",
        rejection_category="not_needed",
    ))
    args = argparse.Namespace(json=True)
    with patch(
        "maestro.cli._build_audit_history_backend", return_value=audit_backend,
    ) as mock_audit, patch(
        "maestro.cli._build_rejection_history_backend",
        return_value=rejection_backend,
    ) as mock_rejection:
        data = json.loads(capture_stdout(cmd_reality_inventory, args, config))

    mock_audit.assert_called_once_with(config)
    mock_rejection.assert_called_once_with(config)
    ids = [p["id"] for p in data["measured_problems"]]
    assert "audit:stale_entry" in ids
    assert "rejection:not_needed" in ids
