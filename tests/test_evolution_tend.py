"""Tests for EvolutionEngine.tend_evolution_prs — mechanical PR tending phase.

Covers reminder cadence, main-branch tamper refusal, clean-merge path,
CHANGES_REQUESTED staleness handling, and orphan branch reconciliation.
Conflict routing lives in ``test_evolution_tend_conflicts.py`` and
post-merge quality-gate routing in ``test_evolution_tend_quality.py``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from maestro import evolution_tend
from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine, QualityGateResult
from maestro.evolution_tend import _PR_REMINDER_HOURS, TendPRsResult
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import EvolutionPlan, Memory
from maestro.notifications.base import NullNotifier
from maestro.vcs import MergeResult, PrMergeState, VCSError
from tests.support.merge_truth import merge_oracle, merge_truth

FAKE_DIR = Path("/fake/tend")


def _make_engine(
    mem_fs: dict[str, str],
    *,
    vcs: MagicMock | None = None,
    code_host: MagicMock | None = None,
    notifier: NullNotifier | None = None,
    config: Config | None = None,
    stub_quality_gates: bool = True,
) -> EvolutionEngine:
    d = FAKE_DIR
    cfg = config or Config(memory_dir=d, workspace=d)
    cfg.ensure_dirs()
    memory = Memory(d)
    consciousness = Consciousness(d)
    consciousness.load_self_model()
    journal = Journal(d)
    issues = IssueTracker(github_repo=None)
    if vcs is None:
        vcs = MagicMock()
        vcs.get_current_branch.return_value = "main"
        vcs.rev_parse.return_value = "hash"
        vcs.commits_ahead_behind.return_value = (0, 0)
        vcs.list_remote_branches.return_value = []
    if code_host is None:
        code_host = MagicMock()
        code_host.get_open_evolution_pr_states.return_value = {}
    # Default the CI-wait gate to "success" so existing tests exercise
    # the merge path. The CI-wait branches are tested explicitly below.
    code_host.get_pr_check_conclusion.return_value = "success"
    # Default the failing-check-name accessor (Fix 4, #2440) to ``[]`` so the
    # ``ci_failure_on_head`` summary stays the bare sentinel unless a test sets
    # names explicitly. Without this, an unconfigured MagicMock returns a
    # truthy non-iterable and breaks the ``", ".join`` in the failure route.
    code_host.get_pr_failing_check_names.return_value = []
    # Default the bug-comment scan to "no threads" so existing tests exercise
    # the merge path without routing a comment-processing remediation. Tests
    # that exercise bug-comment routing set ``review_threads`` explicitly.
    code_host.get_pr_review_threads.return_value = []
    # Default the overflow scan (proposal 064) to "no reviews / no comments"
    # the same way; overflow-routing tests set these explicitly.
    code_host.get_pr_review_bodies.return_value = []
    code_host.get_pr_comment_bodies.return_value = []
    # The router and overflow scan read the fail-loud ``*_strict`` siblings
    # (#4065); default them to "nothing outstanding" too.
    code_host.get_pr_review_threads_strict.return_value = []
    code_host.get_pr_review_bodies_strict.return_value = []
    code_host.get_pr_comment_bodies_strict.return_value = []
    engine = EvolutionEngine(
        memory, consciousness, journal,
        notifier or NullNotifier(),
        issues=issues,
        config=cfg,
        vcs=vcs,
        code_host=code_host,
    )
    # Default the post-merge subprocess gate run to ``passed=True`` so
    # existing tests (which all assume push-after-merge without a gate
    # detour) keep working unchanged. New tests exercising the gate
    # routing pass ``stub_quality_gates=False`` and patch the method.
    if stub_quality_gates:
        engine.run_quality_gates = MagicMock(  # type: ignore[method-assign]
            return_value=QualityGateResult(
                passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True,
                raw_output="",
            ),
        )
    return engine


def _plan(
    *,
    plan_id: str = "ep1",
    status: str = "implemented",
    pr_url: str = "https://github.com/x/y/pull/1",
    last_pr_reminder_at: str | None = None,
    pr_reminder_count: int = 0,
    last_conflict_attempt: str | None = None,
    last_conflict_base_sha: str | None = None,
    last_conflict_head_sha: str | None = None,
    last_conflict_ci_conclusion: str | None = None,
    last_quality_fix_attempt: str | None = None,
    last_quality_fix_head_sha: str | None = None,
    last_quality_fix_base_sha: str | None = None,
    last_quality_fix_ci_conclusion: str | None = None,
    conflict_attempts: int = 0,
    quality_fix_attempts: int = 0,
) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title="sample",
        category="capability",
        rationale="r",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        status=status,  # type: ignore[arg-type]
        pr_url=pr_url,
        created_at="2025-01-01T00:00:00Z",
        last_pr_reminder_at=last_pr_reminder_at,
        pr_reminder_count=pr_reminder_count,
        last_conflict_resolution_attempt_at=last_conflict_attempt,
        last_conflict_resolution_base_sha=last_conflict_base_sha,
        last_conflict_resolution_head_sha=last_conflict_head_sha,
        last_conflict_resolution_ci_conclusion=last_conflict_ci_conclusion,
        last_quality_fix_attempt_at=last_quality_fix_attempt,
        last_quality_fix_head_sha=last_quality_fix_head_sha,
        last_quality_fix_base_sha=last_quality_fix_base_sha,
        last_quality_fix_ci_conclusion=last_quality_fix_ci_conclusion,
        conflict_resolution_signature_attempts=conflict_attempts,
        quality_fix_signature_attempts=quality_fix_attempts,
    )


def _pr_state(
    *,
    head_ref: str = "maestro/evolve/ep1",
    number: int = 1,
    state: str = "OPEN",
    merge_state_status: str = "CLEAN",
    mergeable: str = "MERGEABLE",
    review_decision: str = "",
    created_at: str = "2026-04-20T00:00:00+00:00",
    updated_at: str = "2026-04-22T00:00:00+00:00",
) -> PrMergeState:
    return PrMergeState(
        number=number,
        head_ref=head_ref,
        title="t",
        body="b",
        state=state,
        mergeable=mergeable,
        merge_state_status=merge_state_status,
        review_decision=review_decision,
        created_at=created_at,
        updated_at=updated_at,
        url=f"https://github.com/x/y/pull/{number}",
    )


# ---------------------------------------------------------------------------
# TendPRsResult dataclass
# ---------------------------------------------------------------------------


def test_tend_prs_result_defaults() -> None:
    r = TendPRsResult()
    assert r.tended == 0
    assert r.enumerated is False
    assert r.merged_updated == 0
    assert r.conflicts_cooldown_skipped == 0
    assert r.reminders_sent == 0
    assert r.ready_for_review_sent == 0
    assert r.orphans_repaired == 0
    assert r.skipped_for_review == 0
    assert r.changes_requested_prs == []
    assert r.conflicts_routed == []
    assert r.quality_gates_routed == []
    assert r.settled_pr_numbers == []
    assert r.errors == []


@pytest.mark.asyncio
async def test_tend_records_prs_gone_from_open_set(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 103: PRs whose ledger rows the tend prune drops (they left the
    open evolution-PR set) are recorded in ``settled_pr_numbers`` — the
    on-settlement reconcile drive candidates. A PR still in the open set is not
    recorded; the numbers come back sorted."""
    engine = _make_engine(mem_fs)
    # Seed the per-PR ledger out of order with three PRs; only #9 stays open.
    engine._pr_ledger.observe_head(7, "maestro/evolve/gone-a", "sha7")
    engine._pr_ledger.observe_head(4, "maestro/evolve/gone-b", "sha4")
    engine._pr_ledger.observe_head(9, "maestro/evolve/ep1", "sha9")
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(head_ref="maestro/evolve/ep1", number=9),
    }

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.enumerated is True
    # #7 and #4 left the open set → recorded sorted; #9 is still open → absent.
    assert result.settled_pr_numbers == [4, 7]


@pytest.mark.asyncio
async def test_tend_records_no_settled_prs_when_open_set_unchanged(
    mem_fs: dict[str, str],
) -> None:
    """When every ledger PR is still open upstream, the prune drops nothing and
    ``settled_pr_numbers`` stays empty — no reconcile candidates that lap."""
    engine = _make_engine(mem_fs)
    engine._pr_ledger.observe_head(9, "maestro/evolve/ep1", "sha9")
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(head_ref="maestro/evolve/ep1", number=9),
    }

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.enumerated is True
    assert result.settled_pr_numbers == []


# ---------------------------------------------------------------------------
# Gate: disabled via config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tend_disabled_returns_empty(mem_fs: dict[str, str]) -> None:
    cfg = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    cfg.tend_prs_enabled = False
    engine = _make_engine(mem_fs, config=cfg)
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result == TendPRsResult()
    engine._vcs.fetch.assert_not_called()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Gate: fetch failure short-circuits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_failure_records_error(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.fetch.side_effect = VCSError("offline")
    engine = _make_engine(mem_fs, vcs=vcs)
    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.errors
    assert "fetch origin failed" in result.errors[0]
    # Pre-enumeration short-circuit: ``tended`` is a transient 0, so the
    # flag stays False and the loop must not adopt the count.
    assert result.enumerated is False
    # Fail-loud: the early-return failure is surfaced at WARNING by the
    # ``_emit_tend_errors`` sink, not swallowed.
    warnings = [
        rec.getMessage() for rec in caplog.records
        if rec.levelno == logging.WARNING
    ]
    assert any("fetch origin failed" in msg for msg in warnings)


# ---------------------------------------------------------------------------
# Main-branch tamper check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proceeds_when_local_main_not_ahead(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.side_effect = lambda ref: {
        "main": "h1",
        "origin/main": "h2",
    }.get(ref)
    # Hashes differ but ahead==0 → behind only. Should still proceed.
    vcs.commits_ahead_behind.return_value = (0, 3)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_open_evolution_pr_states.return_value = {}  # type: ignore[attr-defined]
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.errors == []


def _drifted_vcs() -> MagicMock:
    """A VCS mock whose local ``main`` sits one commit ahead of origin/main."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.side_effect = lambda ref: {
        "main": "local-hash",
        "origin/main": "remote-hash",
    }.get(ref)
    vcs.commits_ahead_behind.return_value = (1, 0)
    vcs.local_main_ahead_commits.return_value = ["deadbeefcafef00d"]
    return vcs


def _reconcile_config() -> Config:
    return Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)


@pytest.mark.asyncio
async def test_reconciles_when_local_main_ahead_all_pushed(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    # Flag on + every ahead commit reachable from an origin ref → heal and tend.
    vcs = _drifted_vcs()
    vcs.remote_refs_containing.return_value = ["maestro/evolve/ep7"]
    engine = _make_engine(mem_fs, vcs=vcs, config=_reconcile_config())
    engine._host.get_open_evolution_pr_states.return_value = {}  # type: ignore[attr-defined]
    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # main was healed back to origin/main and the phase proceeded to enumerate.
    vcs.reset_hard.assert_called_once_with("origin/main")
    # Flag on ⇒ the pre-reconcile fetch prunes so a stale remote-tracking ref
    # cannot mask an unpushed commit as pushed in the reachability check.
    vcs.fetch.assert_called_once_with("origin", prune=True)
    assert result.errors == []
    engine._host.get_open_evolution_pr_states.assert_called_once()  # type: ignore[attr-defined]
    warnings = [
        rec.getMessage() for rec in caplog.records
        if rec.levelno == logging.WARNING
    ]
    assert any("reconciled local main ahead" in msg for msg in warnings)


@pytest.mark.asyncio
async def test_refuses_when_local_main_ahead_unpushed(
    mem_fs: dict[str, str],
) -> None:
    # Flag on but one ahead commit is on no origin ref → preserve it, refuse.
    vcs = _drifted_vcs()
    vcs.remote_refs_containing.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs, config=_reconcile_config())
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.errors
    assert "unpushed local main commit(s)" in result.errors[0]
    assert "local main is ahead" in result.errors[0]
    vcs.reset_hard.assert_not_called()
    engine._host.get_open_evolution_pr_states.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_refuses_when_ahead_enumeration_fails(
    mem_fs: dict[str, str],
) -> None:
    # Flag on but enumerating the drift raises → never reset a half-known tree.
    vcs = _drifted_vcs()
    vcs.local_main_ahead_commits.side_effect = VCSError("rev-list boom")
    engine = _make_engine(mem_fs, vcs=vcs, config=_reconcile_config())
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.errors
    assert "enumerating the drift failed" in result.errors[0]
    vcs.reset_hard.assert_not_called()
    engine._host.get_open_evolution_pr_states.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_refuses_when_reset_hard_fails(
    mem_fs: dict[str, str],
) -> None:
    # Flag on, every ahead commit pushed, but the heal reset itself raises →
    # never proceed on a half-reconciled tree.
    vcs = _drifted_vcs()
    vcs.remote_refs_containing.return_value = ["maestro/evolve/ep7"]
    vcs.reset_hard.side_effect = VCSError("reset boom")
    engine = _make_engine(mem_fs, vcs=vcs, config=_reconcile_config())
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.errors
    assert "healing it to" in result.errors[0]
    engine._host.get_open_evolution_pr_states.assert_not_called()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Reminder cadence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reminder_sent_after_first_threshold(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    old_update = (datetime.now().astimezone() - timedelta(hours=_PR_REMINDER_HOURS[0] + 1)).isoformat()
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(updated_at=old_update),
    }
    notify = AsyncMock()
    engine._notifier.notify = notify  # type: ignore[method-assign]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.reminders_sent == 1
    notify.assert_awaited_once()
    # The reminder timestamp was persisted.
    loaded = engine._memory.get_plan("ep1")
    assert loaded is not None
    assert loaded.last_pr_reminder_at is not None


@pytest.mark.asyncio
async def test_no_reminder_before_threshold(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    fresh_update = datetime.now().astimezone().isoformat()
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(updated_at=fresh_update),
    }
    notify = AsyncMock()
    engine._notifier.notify = notify  # type: ignore[method-assign]
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.reminders_sent == 0
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_reminder_second_tier_uses_72h(mem_fs: dict[str, str]) -> None:
    """After 1 reminder sent, the 72h threshold applies (not 48h or 168h)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)

    # Plan has had 1 reminder already; last reminder was 50h ago.
    # 50h > 48h (tier 0) but < 72h (tier 1) → should NOT fire.
    last_reminder = (datetime.now().astimezone() - timedelta(hours=50)).isoformat()
    engine._memory.save_evolution_plan(
        _plan(last_pr_reminder_at=last_reminder, pr_reminder_count=1),
    )
    old_update = (datetime.now().astimezone() - timedelta(hours=100)).isoformat()
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(updated_at=old_update),
    }
    notify = AsyncMock()
    engine._notifier.notify = notify  # type: ignore[method-assign]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.reminders_sent == 0
    notify.assert_not_awaited()

    # Now set last reminder to 73h ago → should fire.
    engine._memory.update_plan_status(
        "ep1", "implemented",
        last_pr_reminder_at=(datetime.now().astimezone() - timedelta(hours=73)).isoformat(),
    )
    result2 = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result2.reminders_sent == 1
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_reminder_third_tier_reachable(mem_fs: dict[str, str]) -> None:
    """After 2 reminders sent, the 168h (weekly) threshold is reachable."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)

    # 2 reminders already sent; last reminder was 169h ago → > 168h → fires.
    last_reminder = (datetime.now().astimezone() - timedelta(hours=169)).isoformat()
    engine._memory.save_evolution_plan(
        _plan(last_pr_reminder_at=last_reminder, pr_reminder_count=2),
    )
    old_update = (datetime.now().astimezone() - timedelta(hours=300)).isoformat()
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(updated_at=old_update),
    }
    notify = AsyncMock()
    engine._notifier.notify = notify  # type: ignore[method-assign]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.reminders_sent == 1
    notify.assert_awaited_once()

    # Verify the count was persisted as 3.
    loaded = engine._memory.get_plan("ep1")
    assert loaded is not None
    assert loaded.pr_reminder_count == 3


@pytest.mark.asyncio
async def test_reminder_third_tier_not_before_168h(mem_fs: dict[str, str]) -> None:
    """After 2 reminders, 100h since last reminder is not enough (need 168h)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)

    last_reminder = (datetime.now().astimezone() - timedelta(hours=100)).isoformat()
    engine._memory.save_evolution_plan(
        _plan(last_pr_reminder_at=last_reminder, pr_reminder_count=2),
    )
    old_update = (datetime.now().astimezone() - timedelta(hours=300)).isoformat()
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(updated_at=old_update),
    }
    notify = AsyncMock()
    engine._notifier.notify = notify  # type: ignore[method-assign]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.reminders_sent == 0
    notify.assert_not_awaited()


# ---------------------------------------------------------------------------
# CHANGES_REQUESTED shortcut
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changes_requested_is_skipped(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", review_decision="CHANGES_REQUESTED",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.skipped_for_review == 1
    assert result.changes_requested_prs == [
        (
            1, "t", "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    # No merge attempted while reviewer blocks the PR.
    vcs.merge_from.assert_not_called()


# ---------------------------------------------------------------------------
# Clean PR path — nothing to do
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_pr_does_nothing(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(merge_state_status="CLEAN"),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.tended == 1
    # Listing succeeded, so ``tended`` is authoritative for this pass.
    assert result.enumerated is True
    assert result.merged_updated == 0
    vcs.merge_from.assert_not_called()


# ---------------------------------------------------------------------------
# Behind — clean merge + push
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_behind_branch_merged_and_pushed(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.merged_updated == 1
    vcs.checkout.assert_any_call("maestro/evolve/ep1")
    vcs.reset_hard.assert_any_call("origin/maestro/evolve/ep1")
    vcs.merge_from.assert_called_once()
    vcs.push.assert_called_once_with("maestro/evolve/ep1")


# ---------------------------------------------------------------------------
# Grounded behind-ness gate — observe_pr_staleness catch-up decision (095)
# ---------------------------------------------------------------------------


def _staleness_vcs(*, behind: int, merge_clean: bool) -> MagicMock:
    """VCS mock for the grounded staleness gate (proposal 095).

    ``origin/maestro/evolve/ep1`` trails ``origin/main`` by *behind* commits
    and merges cleanly per *merge_clean*.  Local ``main`` is level with
    ``origin/main`` (so the tamper check is skipped) and the branch carries no
    local commits ahead of its origin ref, so the only signal under test is the
    grounded ``observe_pr_staleness`` fact.  The per-ref-pair
    ``commits_ahead_behind`` side-effect matches the ``_drifted_vcs`` style.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # main == origin/main → the main-tamper check is skipped entirely.
    vcs.rev_parse.return_value = "h"
    vcs.list_remote_branches.return_value = []

    def _cab(a: str, b: str) -> tuple[int, int]:
        # The adapter measures origin/<branch> against origin/main; every other
        # probe (tamper, local-commits-ahead) is level.
        if a == "origin/maestro/evolve/ep1" and b == "origin/main":
            return (0, behind)
        return (0, 0)

    vcs.commits_ahead_behind.side_effect = _cab
    # merge_tree_conflicts True == conflicting; cleanly_mergeable = not that.
    vcs.merge_tree_conflicts.return_value = not merge_clean
    return vcs


def _ground_vcs(
    vcs: MagicMock,
    *,
    behind: int = 1,
    merge_clean: bool = True,
) -> None:
    """Make proposal-095's grounded staleness observer see every open PR head
    branch as *behind* commits behind ``origin/main`` (clean or conflicting per
    *merge_clean*).

    Proposal 095 retired the host ``merge_state_status`` proxy: the tend
    catch-up decision now measures ``commits_behind`` / ``merge_tree_conflicts``
    vs ``origin/main`` (``observe_pr_staleness``). Shared-builder tests that
    expressed "PR behind main" via ``merge_state_status="BEHIND"`` / ``"DIRTY"``
    wire that intent here instead. Only the observer's
    ``commits_ahead_behind("origin/<branch>", "origin/main")`` probe (any
    ``origin/*`` head) is intercepted; every other call — the main-tamper check,
    the local-commits-ahead probe, the pre-merge reset gate — keeps the value
    the test already configured, so a behind-but-at-parity pre-merge-reset test
    and a local-commits-ahead escape-hatch test both retain their intent.
    """
    prior_return = vcs.commits_ahead_behind.return_value
    prior_side = vcs.commits_ahead_behind.side_effect

    def _cab(a: str, b: str) -> tuple[int, int]:
        if b == "origin/main" and a.startswith("origin/") and a != "origin/main":
            return (0, behind)
        if callable(prior_side):
            return prior_side(a, b)
        return prior_return

    vcs.commits_ahead_behind.side_effect = _cab
    vcs.merge_tree_conflicts.return_value = not merge_clean


def _grounded_config() -> Config:
    return Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)


def test_observe_pr_staleness_adapter_populates_base_head_sha(
    mem_fs: dict[str, str],
) -> None:
    """The adapter feeds ``PrStaleness.base_head_sha`` from
    ``rev_parse("origin/main")`` — the argument-less base oracle reads the base
    the branch was measured against, from the same local-git seam it owns
    (proposal 099 step 2)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # Distinct SHAs per ref so the assertion proves the base comes from
    # ``origin/main`` specifically, not the branch head.
    vcs.rev_parse.side_effect = lambda ref: (
        "basemainsha099" if ref == "origin/main" else "branchsha000"
    )
    vcs.commits_ahead_behind.return_value = (0, 3)
    vcs.merge_tree_conflicts.return_value = False
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)

    staleness = engine._observe_pr_staleness("maestro/evolve/ep1")

    assert staleness.base_head_sha == "basemainsha099"
    vcs.rev_parse.assert_any_call("origin/main")


@pytest.mark.asyncio
async def test_grounded_behind_but_host_clean_reaches_merge(
    mem_fs: dict[str, str],
) -> None:
    """Flag on: a PR the host proxy calls clean (UNKNOWN/MERGEABLE) but that is
    measurably behind ``origin/main`` with a clean merge-tree is NOT skipped —
    it reaches ``merge_from("origin/main")`` + push instead of the
    ``looks_clean`` no-op the host proxy would trigger."""
    vcs = _staleness_vcs(behind=3, merge_clean=True)
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs, config=_grounded_config())
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="UNKNOWN", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.merged_updated == 1
    vcs.merge_from.assert_called_once()
    vcs.push.assert_called_once_with("maestro/evolve/ep1")


@pytest.mark.asyncio
async def test_grounded_zero_behind_is_noop_even_when_host_behind(
    mem_fs: dict[str, str],
) -> None:
    """Flag on: ``commits_behind == 0`` is a no-op even when the stale host
    status still says BEHIND — ``merge_from`` is not called."""
    vcs = _staleness_vcs(behind=0, merge_clean=True)
    engine = _make_engine(mem_fs, vcs=vcs, config=_grounded_config())
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.merged_updated == 0
    vcs.merge_from.assert_not_called()


@pytest.mark.asyncio
async def test_grounded_behind_and_conflicting_routes_conflicts(
    mem_fs: dict[str, str],
) -> None:
    """Flag on: a behind-and-conflicting branch (measured merge-tree conflict)
    still falls through to ``_tend_single_pr`` and routes ``conflicts_routed``
    — the routing invariant is unchanged; the local ``merge_from`` result
    stays the source of truth even though the host proxy reads clean."""
    vcs = _staleness_vcs(behind=2, merge_clean=False)
    vcs.merge_from.return_value = MergeResult(
        success=False, conflicted_files=["hot.py"],
    )
    engine = _make_engine(mem_fs, vcs=vcs, config=_grounded_config())
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="UNKNOWN", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.abort_merge.assert_called_once()
    assert result.conflicts_routed == [
        (
            1, "t", ["hot.py"],
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    assert result.merged_updated == 0


# ---------------------------------------------------------------------------
# Merge-queue cutover kill-switch — tend_main_merge_enabled off (proposal 128)
# ---------------------------------------------------------------------------


def _tend_off_config() -> Config:
    """Config with the tend ``origin/main`` merge-and-push kill-switch off."""
    return Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, tend_main_merge_enabled=False,
    )


@pytest.mark.asyncio
async def test_tend_main_merge_off_cleanly_behind_is_noop(
    mem_fs: dict[str, str],
) -> None:
    """Flag off: a cleanly-behind PR with no local commits is a no-op.

    The tend phase does not merge ``origin/main`` or push, and ``merged_updated``
    stays 0 — this is exactly the eager main-resync the merge-queue cutover
    eliminates. The tapered reminder (owned by the outer loop, ahead of
    ``_tend_single_pr``) still fires, so the flag scopes only the merge-and-push.
    """
    vcs = _staleness_vcs(behind=3, merge_clean=True)
    engine = _make_engine(mem_fs, vcs=vcs, config=_tend_off_config())
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.merged_updated == 0
    vcs.merge_from.assert_not_called()
    vcs.push.assert_not_called()
    # The reminder role survives — it runs in the outer loop, ahead of the
    # gated merge-and-push.
    assert result.reminders_sent == 1


@pytest.mark.asyncio
async def test_tend_main_merge_off_behind_and_conflicting_routes(
    mem_fs: dict[str, str],
) -> None:
    """Flag off: a behind, not-cleanly-mergeable PR still routes a remediation.

    The conflict is detected by the in-memory ``merge_tree_conflicts`` probe — no
    working-tree ``merge_from`` (and so no ``abort_merge``) — and
    ``conflicts_routed`` is populated with an advisory (empty) file list so the
    downstream remediation re-derives the conflict on-branch.
    """
    vcs = _staleness_vcs(behind=2, merge_clean=False)
    engine = _make_engine(mem_fs, vcs=vcs, config=_tend_off_config())
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="UNKNOWN", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.merge_from.assert_not_called()
    vcs.abort_merge.assert_not_called()
    assert result.conflicts_routed == [
        (
            1, "t", [],
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    assert result.merged_updated == 0


@pytest.mark.asyncio
async def test_tend_main_merge_off_delivers_local_commits(
    mem_fs: dict[str, str],
) -> None:
    """Flag off: a branch strictly extending its own remote head is still pushed.

    An unpushed remediation commit run()'s direct push did not deliver is a
    legitimate fix delivery, not a redundant main-resync, so the flag-off path
    pushes it — without any ``origin/main`` merge.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.list_remote_branches.return_value = []
    vcs.merge_tree_conflicts.return_value = False

    def _cab(a: str, b: str) -> tuple[int, int]:
        # Level with main (behind == 0) but one local commit ahead of the
        # branch's own remote head.
        if a == "maestro/evolve/ep1" and b == "origin/maestro/evolve/ep1":
            return (1, 0)
        return (0, 0)

    vcs.commits_ahead_behind.side_effect = _cab
    engine = _make_engine(mem_fs, vcs=vcs, config=_tend_off_config())
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.merge_from.assert_not_called()
    vcs.push.assert_called_once_with("maestro/evolve/ep1")
    assert result.merged_updated == 0
    assert result.conflicts_routed == []


@pytest.mark.asyncio
async def test_tend_main_merge_off_local_commits_push_failure_recorded(
    mem_fs: dict[str, str],
) -> None:
    """Flag off: a failed delivery push of local commits is recorded, not raised.

    The push is fail-soft (mirroring the flag-on delivery pushes) — a
    ``VCSError`` lands on ``result.errors`` and the lap continues rather than
    aborting the whole tend sweep.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.list_remote_branches.return_value = []
    vcs.merge_tree_conflicts.return_value = False

    def _cab(a: str, b: str) -> tuple[int, int]:
        if a == "maestro/evolve/ep1" and b == "origin/maestro/evolve/ep1":
            return (1, 0)
        return (0, 0)

    vcs.commits_ahead_behind.side_effect = _cab
    vcs.push.side_effect = VCSError("push rejected")
    engine = _make_engine(mem_fs, vcs=vcs, config=_tend_off_config())
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.merge_from.assert_not_called()
    vcs.push.assert_called_once_with("maestro/evolve/ep1")
    assert result.merged_updated == 0
    assert any(
        "push of local commits failed" in err for err in result.errors
    )


@pytest.mark.asyncio
async def test_tend_main_merge_off_diverged_head_reconciled_then_pushed(
    mem_fs: dict[str, str],
) -> None:
    """Flag off: a branch diverged from its OWN remote head is reconciled first.

    When the branch is both ahead of and behind ``origin/<branch>`` (a diverged
    own-remote head — e.g. a GitHub "Update branch" merge), pushing without
    reconciling is rejected non-fast-forward every lap and — being the ``if``
    arm — the conflict route never fires, stranding the PR (#2508/#2568). The
    flag-off path must absorb ``origin/<branch>`` (a working-tree ``merge_from``
    of the branch's own remote head, NOT an ``origin/main`` resync) before the
    fast-forward push.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.list_remote_branches.return_value = []
    vcs.merge_tree_conflicts.return_value = False
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])

    def _cab(a: str, b: str) -> tuple[int, int]:
        # Diverged from its own remote head: one commit ahead, two behind.
        if a == "maestro/evolve/ep1" and b == "origin/maestro/evolve/ep1":
            return (1, 2)
        return (0, 0)

    vcs.commits_ahead_behind.side_effect = _cab
    engine = _make_engine(mem_fs, vcs=vcs, config=_tend_off_config())
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Reconcile merged the branch's OWN remote head (never origin/main).
    vcs.merge_from.assert_called_once_with(
        "origin/maestro/evolve/ep1",
        message="maestro: reconcile diverged origin/maestro/evolve/ep1",
    )
    vcs.push.assert_called_once_with("maestro/evolve/ep1")
    assert result.merged_updated == 0
    assert result.conflicts_routed == []
    assert result.errors == []


@pytest.mark.asyncio
async def test_tend_main_merge_off_diverged_head_conflict_routes_no_push(
    mem_fs: dict[str, str],
) -> None:
    """Flag off: a diverged head that conflicts on reconcile routes, never pushes.

    When absorbing ``origin/<branch>`` conflicts with the local commits, the
    reconcile aborts the merge and routes a remediation (via
    ``_reconcile_remote_pr_head``) instead of pushing a diverged head — the
    non-fast-forward push that would otherwise loop forever never fires.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.list_remote_branches.return_value = []
    vcs.merge_tree_conflicts.return_value = False
    vcs.merge_from.return_value = MergeResult(
        success=False, conflicted_files=["maestro/x.py"],
    )

    def _cab(a: str, b: str) -> tuple[int, int]:
        if a == "maestro/evolve/ep1" and b == "origin/maestro/evolve/ep1":
            return (1, 2)
        return (0, 0)

    vcs.commits_ahead_behind.side_effect = _cab
    engine = _make_engine(mem_fs, vcs=vcs, config=_tend_off_config())
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.abort_merge.assert_called_once()
    vcs.push.assert_not_called()
    assert result.conflicts_routed == [
        (
            1, "t", ["maestro/x.py"],
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    assert result.merged_updated == 0


@pytest.mark.asyncio
async def test_tend_main_merge_off_diverged_head_gate_red_rewinds_and_routes(
    mem_fs: dict[str, str],
) -> None:
    """Flag off: a gate-red reconcile merge is rewound and routed, never pushed.

    Reconciling a diverged own-remote head creates a NEW merge commit that has
    never faced the gates. Mirroring the flag-on divergence guard's
    rewind-on-red, the flag-off path validates that merge locally: on red it
    rewinds to the pre-reconcile head (so a later lap re-gates through the same
    arm rather than the ``behind == 0`` fast-forward push blind-delivering it)
    and routes a quality-gate remediation instead of pushing the unvalidated
    merge to CI.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.list_remote_branches.return_value = []
    vcs.merge_tree_conflicts.return_value = False
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])

    def _cab(a: str, b: str) -> tuple[int, int]:
        if a == "maestro/evolve/ep1" and b == "origin/maestro/evolve/ep1":
            return (1, 2)
        return (0, 0)

    vcs.commits_ahead_behind.side_effect = _cab
    engine = _make_engine(
        mem_fs, vcs=vcs, config=_tend_off_config(), stub_quality_gates=False,
    )
    engine.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=QualityGateResult(
            passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
            raw_output="ruff: E501",
        ),
    )
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Reconcile ran; gates ran and went red → rewind the merge and route.
    vcs.merge_from.assert_called_once_with(
        "origin/maestro/evolve/ep1",
        message="maestro: reconcile diverged origin/maestro/evolve/ep1",
    )
    engine.run_quality_gates.assert_called_once_with()
    vcs.reset_hard.assert_called_once_with("h")
    vcs.push.assert_not_called()
    assert result.merged_updated == 0
    assert result.conflicts_routed == []
    assert result.quality_gates_routed == [
        (
            1, "t", "ruff failed",
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    # Proposal 137: the reconcile arm stamps the same signature as the other
    # two quality-fix routes.
    stamped = engine._memory.get_plan("ep1")
    assert stamped is not None
    assert stamped.last_quality_fix_head_sha == "h"
    assert stamped.last_quality_fix_base_sha == "h"
    assert stamped.last_quality_fix_ci_conclusion == "success"


@pytest.mark.asyncio
async def test_tend_main_merge_off_diverged_head_in_cooldown_rewinds_no_push(
    mem_fs: dict[str, str],
) -> None:
    """Flag off: within the quality-fix cooldown the reconcile merge is rewound.

    A recent quality-fix attempt cools the gate down, so the (expensive) gate is
    skipped — but the unvalidated reconcile merge must still never be delivered:
    it is rewound to the pre-reconcile head and the lap ends without a push or a
    fresh remediation route (mirroring the flag-on clean-merge cooldown, #2110).
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.list_remote_branches.return_value = []
    vcs.merge_tree_conflicts.return_value = False
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])

    def _cab(a: str, b: str) -> tuple[int, int]:
        if a == "maestro/evolve/ep1" and b == "origin/maestro/evolve/ep1":
            return (1, 2)
        return (0, 0)

    vcs.commits_ahead_behind.side_effect = _cab
    engine = _make_engine(
        mem_fs, vcs=vcs, config=_tend_off_config(), stub_quality_gates=False,
    )
    engine.run_quality_gates = MagicMock()  # type: ignore[method-assign]
    plan = _plan()
    # A quality-fix attempt one minute ago keeps the cooldown active.
    plan.last_quality_fix_attempt_at = (
        datetime.now(UTC) - timedelta(minutes=1)
    ).isoformat()
    engine._memory.save_evolution_plan(plan)
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.merge_from.assert_called_once_with(
        "origin/maestro/evolve/ep1",
        message="maestro: reconcile diverged origin/maestro/evolve/ep1",
    )
    engine.run_quality_gates.assert_not_called()
    vcs.reset_hard.assert_called_once_with("h")
    vcs.push.assert_not_called()
    assert result.quality_gates_routed == []
    assert result.merged_updated == 0


# ---------------------------------------------------------------------------
# Planless (retired/obsolete) PR tending — tend_planless_prs_enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_implemented_plan_without_pr_url_skipped(
    mem_fs: dict[str, str],
) -> None:
    """The ``implemented``-plan ``pr_url`` orphan guard still skips an
    implemented plan with an open PR upstream but no recorded ``pr_url`` — the
    guard is now nested under the status check, but its behavior is unchanged."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan(pr_url=""))  # implemented, no pr_url
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.tended == 0
    vcs.merge_from.assert_not_called()


@pytest.mark.asyncio
async def test_obsolete_plan_open_pr_still_tended(mem_fs: dict[str, str]) -> None:
    """An open evolution PR whose plan was retired to ``obsolete`` (via
    ``mark_plan_obsolete(allow_implemented=True)``) still flows through the
    full mechanical tend body: a BEHIND branch merges ``origin/main`` and an
    unresolved bug thread routes a remediation entry. The reminder-stamp write
    goes through ``update_plan_tend_fields`` so the forbidden ``obsolete``
    status write never raises ``ValueError`` out of the never-raising phase."""
    from tests.test_tend_bug_comments import _thread

    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())  # status defaults to implemented
    assert engine._memory.mark_plan_obsolete(
        "ep1", "retired mid-review", allow_implemented=True,
    ) is True
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    engine._host.get_pr_review_threads_strict.return_value = [  # type: ignore[attr-defined]
        _thread(comment_id=11, path="maestro/agent.py"),
    ]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.tended == 1
    assert result.merged_updated == 1
    # The reminder-stamp write ran on the obsolete plan without raising.
    assert result.reminders_sent == 1
    assert len(result.bug_comments_routed) == 1
    assert result.errors == []  # no ValueError escaped the tend phase
    vcs.merge_from.assert_called_once()
    vcs.push.assert_called_once_with("maestro/evolve/ep1")
    # The tend-field writes never touched status — the plan is still obsolete.
    reloaded = engine._memory.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.status == "obsolete"
    assert reloaded.obsolete_reason == "retired mid-review"


@pytest.mark.asyncio
async def test_obsolete_plan_open_pr_skipped_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """With ``tend_planless_prs_enabled=False`` the same retired-plan PR is
    skipped (byte-for-byte the old implemented-only worklist) and no exception
    escapes."""
    from tests.test_tend_bug_comments import _thread

    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    cfg = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR,
        tend_planless_prs_enabled=False,
    )
    engine = _make_engine(mem_fs, vcs=vcs, config=cfg)
    engine._memory.save_evolution_plan(_plan())  # status defaults to implemented
    assert engine._memory.mark_plan_obsolete(
        "ep1", "retired mid-review", allow_implemented=True,
    ) is True
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    engine._host.get_pr_review_threads_strict.return_value = [  # type: ignore[attr-defined]
        _thread(comment_id=11, path="maestro/agent.py"),
    ]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.tended == 0
    assert result.merged_updated == 0
    assert result.bug_comments_routed == []
    assert result.errors == []
    vcs.merge_from.assert_not_called()


# ---------------------------------------------------------------------------
# DIRTY — cooldown skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dirty_branch_in_cooldown(mem_fs: dict[str, str]) -> None:
    recent = datetime.now().astimezone().isoformat()
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=False)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(
        _plan(last_conflict_attempt=recent),
    )
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.conflicts_cooldown_skipped == 1
    vcs.merge_from.assert_not_called()


@pytest.mark.asyncio
async def test_dirty_branch_in_cooldown_with_local_commits_pushes(
    mem_fs: dict[str, str],
) -> None:
    """A DIRTY/CONFLICTING PR inside the conflict cooldown but with local
    commits ahead of origin must NOT be skipped — a prior remediation task
    committed the resolution locally and run()'s direct push did not deliver
    it, so the tend body runs and Tend PR's trailing ``push`` delivers it as
    fallback. Mirrors the ``looks_clean``-with-local-commits escape hatch."""
    recent = datetime.now().astimezone().isoformat()
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"  # branch resolves locally
    vcs.commits_ahead_behind.return_value = (2, 0)  # local resolution ahead
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(
        _plan(last_conflict_attempt=recent),
    )
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.conflicts_cooldown_skipped == 0
    assert result.merged_updated == 1
    vcs.push.assert_called_once_with("maestro/evolve/ep1")


# ---------------------------------------------------------------------------
# DIRTY — budget-standby skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_implemented_plan_with_pr_closed_upstream_is_skipped(
    mem_fs: dict[str, str],
) -> None:
    """When ``status == 'implemented'`` and ``pr_url`` is set but the PR is
    not in the open-PR map (closed or merged upstream), the tender skips
    the plan without raising — ``check_merged_plans`` will reconcile the
    plan's terminal state on its own.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    # Default ``_plan()`` has status="implemented" and a non-empty pr_url.
    engine._memory.save_evolution_plan(_plan())
    # Open-PR map is empty — branch ``maestro/evolve/ep1`` is not present.
    engine._host.get_open_evolution_pr_states.return_value = {}  # type: ignore[attr-defined]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    # Plan was skipped: no merge attempted, no reminder, no error.
    assert result.tended == 0
    assert result.merged_updated == 0
    assert result.errors == []
    vcs.merge_from.assert_not_called()


# ---------------------------------------------------------------------------
# Orphan branch reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_branch_detected(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """An 'approved' orphan is only logged — no state change, so honest
    accounting credits no repair (#2569)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    # Remote has a branch but there's no open PR for it.
    vcs.list_remote_branches.return_value = [
        "main", "maestro/evolve/ep_orphan",
    ]
    engine = _make_engine(mem_fs, vcs=vcs)
    # Plan exists in approved state — expected to be re-implemented later.
    engine._memory.save_evolution_plan(
        _plan(plan_id="ep_orphan", status="approved", pr_url=""),
    )
    engine._host.get_open_evolution_pr_states.return_value = {}  # type: ignore[attr-defined]

    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Log-only arm: the orphan is flagged but nothing is mutated, so it must
    # not be credited as a repair.
    assert result.orphans_repaired == 0
    loaded = engine._memory.get_plan("ep_orphan")
    assert loaded is not None
    assert loaded.status == "approved"
    warnings = [
        rec.getMessage() for rec in caplog.records
        if rec.levelno == logging.WARNING
    ]
    assert any("flagged for re-implementation" in msg for msg in warnings)


@pytest.mark.asyncio
async def test_orphan_implemented_plan_reset_to_approved(
    mem_fs: dict[str, str],
) -> None:
    """An 'implemented' plan with no pr_url whose branch is *not* on
    origin/main is reset to 'approved' (a genuine repair)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = [
        "main", "maestro/evolve/ep_stranded",
    ]
    # Branch is genuinely un-landed — not yet an ancestor of origin/main —
    # so the stranded plan is re-queued rather than dismissed as landed.
    vcs.is_ancestor.return_value = False
    engine = _make_engine(mem_fs, vcs=vcs)
    # Plan is 'implemented' but has no PR (crash between implement and create_pr).
    engine._memory.save_evolution_plan(
        _plan(plan_id="ep_stranded", status="implemented", pr_url=""),
    )
    engine._host.get_open_evolution_pr_states.return_value = {}  # type: ignore[attr-defined]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.orphans_repaired == 1
    loaded = engine._memory.get_plan("ep_stranded")
    assert loaded is not None
    assert loaded.status == "approved"


@pytest.mark.asyncio
async def test_orphan_ignored_when_plan_not_relevant(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = [
        "main", "maestro/evolve/ep_stale",
    ]
    engine = _make_engine(mem_fs, vcs=vcs)
    # Plan exists but has been rejected — ignore orphan (no rescue needed).
    engine._memory.save_evolution_plan(
        _plan(plan_id="ep_stale", status="rejected", pr_url=""),
    )
    engine._host.get_open_evolution_pr_states.return_value = {}  # type: ignore[attr-defined]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.orphans_repaired == 0


@pytest.mark.asyncio
async def test_orphan_merged_pr_defers_flip_not_reflagged(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A merged PR's lingering branch is *not* re-flagged and *not* flipped
    inline — the flip is deferred to ``check_merged_plans`` (#2569).

    Until the iteration-cadence ``check_merged_plans`` phase catches up, a
    merged plan is still ``implemented`` with a ``pr_url`` and its branch is no
    longer in the open-PR set, so the orphan scan used to log an
    ``Orphan ... flagged for re-implementation`` warning and credit a false
    repair on every lap. The merge-aware scan recognises the merge via the same
    host read, suppresses the warning and the false repair credit, but leaves
    the plan ``implemented`` so ``check_merged_plans`` still owns the flip —
    and, with it, the merge side-effects (VersionRecord, evolution_count bump,
    milestone journal) that ``agent.py`` drives off ``check_result.merged``.
    Flipping inline here would move the plan out of ``implemented``, making
    ``check_merged_plans`` skip it and drop those side-effects (review concern).
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = [
        "main", "maestro/evolve/ep_merged",
    ]
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(
        _plan(
            plan_id="ep_merged",
            status="implemented",
            pr_url="https://github.com/thomas-sodon/maestro-agent/pull/2550",
        ),
    )
    # PR merged → gone from the open-PR set, but the chokepoint reports landed.
    engine._host.get_open_evolution_pr_states.return_value = {}  # type: ignore[attr-defined]

    with caplog.at_level(logging.INFO, logger="maestro"):
        result = await engine.tend_evolution_prs(
            settled_oracle=merge_oracle(
                {2550: merge_truth(
                    2550, settled=True, branch_landed=True, state="MERGED",
                )},
            ),
        )

    loaded = engine._memory.get_plan("ep_merged")
    assert loaded is not None
    # Deferred, not flipped: the plan stays 'implemented'+pr_url so
    # check_merged_plans (the side-effect owner) still processes it next cycle.
    assert loaded.status == "implemented"
    assert loaded.pr_url == "https://github.com/thomas-sodon/maestro-agent/pull/2550"
    # No false repair credited, and no re-implementation warning emitted for
    # already-landed work.
    assert result.orphans_repaired == 0
    warnings = [
        rec.getMessage() for rec in caplog.records
        if rec.levelno == logging.WARNING
    ]
    assert not any("flagged for re-implementation" in msg for msg in warnings)
    # The deferral is logged at INFO so the suppression is observable.
    infos = [
        rec.getMessage() for rec in caplog.records
        if rec.levelno == logging.INFO
    ]
    assert any("deferring flip to check_merged_plans" in msg for msg in infos)


@pytest.mark.asyncio
async def test_orphan_deferred_merge_still_reaches_check_merged_plans(
    mem_fs: dict[str, str],
) -> None:
    """The deferred flip preserves the merge side-effect pipeline (#2569).

    Because the orphan scan leaves a merged lingering-branch plan
    ``implemented``+``pr_url`` instead of flipping it, ``check_merged_plans``
    still detects the merge on its next run and reports the plan in
    ``result.merged`` — the list ``agent.py`` iterates to write the VersionRecord,
    bump ``evolution_count``, and journal the milestone. Had the orphan scan
    flipped inline, ``check_merged_plans`` would have skipped the now-``merged``
    plan and those side-effects would never fire.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = [
        "main", "maestro/evolve/ep_deferred",
    ]
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(
        _plan(
            plan_id="ep_deferred",
            status="implemented",
            pr_url="https://github.com/thomas-sodon/maestro-agent/pull/2560",
        ),
    )
    engine._host.get_open_evolution_pr_states.return_value = {}  # type: ignore[attr-defined]
    oracle = merge_oracle(
        {2560: merge_truth(2560, settled=True, branch_landed=True, state="MERGED")},
    )

    # 1) Orphan scan (inside tend) defers: the plan is left implemented.
    await engine.tend_evolution_prs(settled_oracle=oracle)
    deferred = engine._memory.get_plan("ep_deferred")
    assert deferred is not None
    assert deferred.status == "implemented"

    # 2) The very next check_merged_plans phase detects the merge and reports
    #    the plan — this is the list agent.py drives the side-effects off.
    merge_result = engine.check_merged_plans(settled_oracle=oracle)
    assert "ep_deferred" in merge_result.merged
    flipped = engine._memory.get_plan("ep_deferred")
    assert flipped is not None
    assert flipped.status == "merged"


@pytest.mark.asyncio
async def test_orphan_implemented_pr_not_merged_only_logs_no_credit(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """An 'implemented' orphan with a pr_url whose PR is *not* merged falls to
    the log-only arm: it is flagged but not mutated, so it credits no repair
    (honest accounting, #2569)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = [
        "main", "maestro/evolve/ep_open",
    ]
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(
        _plan(
            plan_id="ep_open",
            status="implemented",
            pr_url="https://github.com/thomas-sodon/maestro-agent/pull/2551",
        ),
    )
    engine._host.get_open_evolution_pr_states.return_value = {}  # type: ignore[attr-defined]

    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = await engine.tend_evolution_prs(settled_oracle=merge_oracle({}))

    # Not merged and still has a pr_url → log-only arm; status is untouched and
    # no repair is credited.
    assert result.orphans_repaired == 0
    loaded = engine._memory.get_plan("ep_open")
    assert loaded is not None
    assert loaded.status == "implemented"
    warnings = [
        rec.getMessage() for rec in caplog.records
        if rec.levelno == logging.WARNING
    ]
    assert any("flagged for re-implementation" in msg for msg in warnings)


@pytest.mark.asyncio
async def test_orphan_implemented_no_pr_url_already_landed_flips_merged(
    mem_fs: dict[str, str],
) -> None:
    """An 'implemented' plan with no pr_url whose branch is already on
    origin/main is flipped to 'merged' — never reset — so no re-implementation
    session runs for work already landed (#2569, acceptance criterion 2)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = [
        "main", "maestro/evolve/ep_landed",
    ]
    # Branch head is already an ancestor of origin/main → work landed.
    vcs.is_ancestor.return_value = True
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(
        _plan(plan_id="ep_landed", status="implemented", pr_url=""),
    )
    engine._host.get_open_evolution_pr_states.return_value = {}  # type: ignore[attr-defined]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # The zero-API ancestry check is consulted against the remote refs.
    vcs.is_ancestor.assert_any_call(
        "origin/maestro/evolve/ep_landed", "origin/main",
    )
    loaded = engine._memory.get_plan("ep_landed")
    assert loaded is not None
    assert loaded.status == "merged"
    # A stranded plan that turns out to be landed is a genuine reconcile action.
    assert result.orphans_repaired == 1


@pytest.mark.asyncio
async def test_orphan_implemented_malformed_pr_url_falls_to_log_arm(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed ``pr_url`` that cannot yield a PR number skips the merge
    check and falls to the log-only arm rather than crashing the scan."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = [
        "main", "maestro/evolve/ep_badurl",
    ]
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(
        _plan(
            plan_id="ep_badurl",
            status="implemented",
            pr_url="https://github.com/x/y/pull/not-a-number",
        ),
    )
    engine._host.get_open_evolution_pr_states.return_value = {}  # type: ignore[attr-defined]
    oracle = MagicMock()

    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = await engine.tend_evolution_prs(settled_oracle=oracle)

    # Merge check never runs (no parseable number); the chokepoint oracle is
    # never consulted, the plan is untouched, and no repair is credited.
    oracle.assert_not_called()
    assert result.orphans_repaired == 0
    loaded = engine._memory.get_plan("ep_badurl")
    assert loaded is not None
    assert loaded.status == "implemented"
    warnings = [
        rec.getMessage() for rec in caplog.records
        if rec.levelno == logging.WARNING
    ]
    assert any("flagged for re-implementation" in msg for msg in warnings)


@pytest.mark.asyncio
async def test_orphan_merge_check_host_error_isolated(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A host failure during the merge check isolates to that branch (logged),
    falling through to the log-only arm rather than aborting the whole scan."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = [
        "main", "maestro/evolve/ep_hosterr",
    ]
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(
        _plan(
            plan_id="ep_hosterr",
            status="implemented",
            pr_url="https://github.com/x/y/pull/2552",
        ),
    )
    engine._host.get_open_evolution_pr_states.return_value = {}  # type: ignore[attr-defined]
    oracle = MagicMock(side_effect=RuntimeError("host offline"))

    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = await engine.tend_evolution_prs(settled_oracle=oracle)

    # The failure is surfaced at WARNING and the plan is left untouched (today's
    # behavior), crediting no repair — the scan is not aborted.
    assert result.orphans_repaired == 0
    loaded = engine._memory.get_plan("ep_hosterr")
    assert loaded is not None
    assert loaded.status == "implemented"
    warnings = [
        rec.getMessage() for rec in caplog.records
        if rec.levelno == logging.WARNING
    ]
    assert any("merge check failed" in msg for msg in warnings)
    assert any("flagged for re-implementation" in msg for msg in warnings)


# ---------------------------------------------------------------------------
# _parse_iso — small helper
# ---------------------------------------------------------------------------


def test_parse_iso_invalid_string_returns_none() -> None:
    assert EvolutionEngine._parse_iso("not-a-date") is None


def test_parse_iso_naive_datetime_gets_utc() -> None:
    parsed = EvolutionEngine._parse_iso("2026-04-22T00:00:00")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# _should_send_reminder — no candidates short-circuit
# ---------------------------------------------------------------------------


def test_should_send_reminder_no_candidates(mem_fs: dict[str, str]) -> None:
    engine = _make_engine(mem_fs)
    plan = _plan()  # last_pr_reminder_at is None
    pr = _pr_state(updated_at="")  # also blank
    assert evolution_tend._should_send_reminder(engine, plan, pr, datetime.now().astimezone()) is False


# ---------------------------------------------------------------------------
# _send_pr_reminder — generic notifier exceptions propagate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_pr_reminder_propagates_notifier_error(
    mem_fs: dict[str, str],
) -> None:
    """A generic ``Exception`` from the reminder notifier propagates.

    Only ``asyncio.CancelledError`` (the documented SDK-teardown leak)
    is suppressed; everything else is a bug that must surface.
    """
    engine = _make_engine(mem_fs)
    engine._notifier.notify = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("telegram offline"),
    )
    with pytest.raises(RuntimeError, match="telegram offline"):
        await evolution_tend._send_pr_reminder(engine, _plan(), _pr_state())


# ---------------------------------------------------------------------------
# tend_evolution_prs — reminder failure isolated per plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tend_evolution_prs_isolates_reminder_failure_per_plan(
    mem_fs: dict[str, str],
) -> None:
    """A reminder failure on one plan must not abort the for-loop.

    Regression for #1587: an unguarded notifier blip on plan A used to
    propagate out of ``tend_evolution_prs``, silently skipping every
    subsequent plan (no merge, no conflict resolution, no quality-fix,
    no orphan reconciliation). The typed-return wrapper at the call
    site captures the failure into ``result.errors`` so the loop
    continues to plan B.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan(plan_id="ep1"))
    engine._memory.save_evolution_plan(
        _plan(plan_id="ep2", pr_url="https://github.com/x/y/pull/2"),
    )
    old_update = (
        datetime.now().astimezone()
        - timedelta(hours=_PR_REMINDER_HOURS[0] + 1)
    ).isoformat()
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            head_ref="maestro/evolve/ep1", number=1, updated_at=old_update,
        ),
        "maestro/evolve/ep2": _pr_state(
            head_ref="maestro/evolve/ep2", number=2, updated_at=old_update,
        ),
    }
    notify = AsyncMock(side_effect=[RuntimeError("telegram offline"), None])
    engine._notifier.notify = notify  # type: ignore[method-assign]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    # The second plan's reminder still went out — loop did not abort.
    assert notify.await_count == 2
    assert result.reminders_sent == 1
    assert len(result.errors) == 1
    assert "ep1" in result.errors[0]
    # Persistence is gated on success — ep1 did not advance, ep2 did.
    loaded_ep1 = engine._memory.get_plan("ep1")
    loaded_ep2 = engine._memory.get_plan("ep2")
    assert loaded_ep1 is not None
    assert loaded_ep2 is not None
    assert loaded_ep1.last_pr_reminder_at is None
    assert loaded_ep1.pr_reminder_count == 0
    assert loaded_ep2.last_pr_reminder_at is not None
    assert loaded_ep2.pr_reminder_count == 1


@pytest.mark.asyncio
async def test_tend_evolution_prs_reminder_cancelled_error_propagates(
    mem_fs: dict[str, str],
) -> None:
    """``CancelledError`` from the reminder must still propagate.

    The new typed-return wrapper has an explicit
    ``except asyncio.CancelledError: raise`` arm so shutdown semantics
    survive the isolation: a ``BaseException`` from the notify boundary
    is never folded into ``result.errors``.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    old_update = (
        datetime.now().astimezone()
        - timedelta(hours=_PR_REMINDER_HOURS[0] + 1)
    ).isoformat()
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(updated_at=old_update),
    }
    with (
        patch.object(
            evolution_tend, "_send_pr_reminder",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await engine.tend_evolution_prs(settled_oracle=merge_oracle())


@pytest.mark.asyncio
async def test_reminder_notifier_cancelled_keeps_plan_retryable(
    mem_fs: dict[str, str],
) -> None:
    """A cancelled notifier must not stamp an unsent reminder as delivered.

    ``_send_pr_reminder`` swallows ``CancelledError`` raised by the notifier
    itself, so the caller sees no exception. Gating the durable write on the
    helper's delivery result is what keeps ``last_pr_reminder_at`` unset and
    ``pr_reminder_count`` at zero, so the taper does not push the next nudge
    further out for a PR Thomas was never told about.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    old_update = (
        datetime.now().astimezone()
        - timedelta(hours=_PR_REMINDER_HOURS[0] + 1)
    ).isoformat()
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(updated_at=old_update),
    }
    notify = AsyncMock(side_effect=asyncio.CancelledError())
    engine._notifier.notify = notify  # type: ignore[method-assign]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    notify.assert_awaited_once()
    assert result.reminders_sent == 0
    assert len(result.errors) == 1
    assert "reminder cancelled for plan ep1" in result.errors[0]
    reloaded = engine._memory.get_plan("ep1")
    assert reloaded is not None
    assert reloaded.last_pr_reminder_at is None
    assert reloaded.pr_reminder_count == 0


@pytest.mark.asyncio
async def test_tend_evolution_prs_continues_to_merge_path_after_reminder_failure(
    mem_fs: dict[str, str],
) -> None:
    """A reminder failure does not skip the same plan's merge path.

    The wrapper isolates only the reminder's persistence step; control
    falls through to the existing merge / conflict-resolution block so
    a stale ``BEHIND`` PR still gets ``origin/main`` merged in even
    when its tapered reminder failed to send.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    old_update = (
        datetime.now().astimezone()
        - timedelta(hours=_PR_REMINDER_HOURS[0] + 1)
    ).isoformat()
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", updated_at=old_update,
        ),
    }
    tend_single = AsyncMock(return_value=None)
    with (
        patch.object(
            evolution_tend, "_send_pr_reminder",
            new=AsyncMock(side_effect=RuntimeError("telegram offline")),
        ),
        patch.object(evolution_tend, "_tend_single_pr", new=tend_single),
    ):
        result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    tend_single.assert_awaited_once()
    assert len(result.errors) == 1
    assert "ep1" in result.errors[0]
    assert result.reminders_sent == 0


# ---------------------------------------------------------------------------
# Host failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_listing_failure_records_error(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    code_host = MagicMock()
    code_host.get_open_evolution_pr_states.side_effect = VCSError("gh down")
    engine = _make_engine(mem_fs, vcs=vcs, code_host=code_host)
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.errors
    assert "listing open PRs failed" in result.errors[0]
    code_host.get_open_evolution_pr_states.assert_called_with(
        "maestro/evolve/", strict=True,
    )


@pytest.mark.asyncio
async def test_failed_pr_listing_preserves_ledger_and_enumeration(
    mem_fs: dict[str, str],
) -> None:
    """#4153: a host that returns ``{}`` best-effort but raises under
    ``strict`` must not have its failure read as an empty open set — the
    stuck latch, attempt budget and escalation stamp survive, and nothing
    is reported settled."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)

    def _list(prefix: str, *, strict: bool = False) -> dict[str, object]:
        if strict:
            raise VCSError("HTTP 403: API rate limit exceeded")
        return {}

    code_host = MagicMock()
    code_host.get_open_evolution_pr_states.side_effect = _list
    engine = _make_engine(mem_fs, vcs=vcs, code_host=code_host)
    engine._memory.save_evolution_plan(
        _plan(pr_url="https://github.com/x/y/pull/42"),
    )
    engine._pr_ledger.observe_head(42, "maestro/evolve/ep1", "sha42")
    for _ in range(engine._config.pr_remediation_max_attempts_per_head):
        engine._pr_ledger.record_attempt(
            42, outcome="futile", stuck_reason="conflict",
        )
    engine._pr_ledger.mark_escalated(42)
    before = engine._pr_ledger.get(42)
    assert before is not None
    assert before.stuck is True

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.enumerated is False
    assert result.settled_pr_numbers == []
    assert "listing open PRs failed" in result.errors[0]
    after = engine._pr_ledger.get(42)
    assert after is not None
    assert after.stuck is True
    assert after.attempts_on_head == before.attempts_on_head
    assert after.escalated_at == before.escalated_at


# ---------------------------------------------------------------------------
# tend_evolution_prs — closed PR state is skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_open_pr_is_skipped(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(state="CLOSED"),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.tended == 0
    vcs.merge_from.assert_not_called()


# ---------------------------------------------------------------------------
# tend_evolution_prs — unexpected exception recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tend_evolution_prs_unexpected_exception_propagates(
    mem_fs: dict[str, str],
) -> None:
    """An unexpected exception during single-PR tending propagates.

    Fail-loud: a programming bug or OSError is a real defect — letting
    the loop's iteration handler classify or crash it preserves the
    crash-evidence chain. The ``finally``-driven cleanup still runs
    so the next pass finds a sane working tree, even though no swallow
    records the failure on ``result.errors`` any more.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=False)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    with (
        patch.object(
            evolution_tend, "_tend_single_pr",
            new=AsyncMock(side_effect=RuntimeError("disk full")),
        ),
        pytest.raises(RuntimeError, match="disk full"),
    ):
        await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # ``finally`` still ran the working-tree cleanup before propagation.
    vcs.abort_merge.assert_called()
    vcs.discard_changes.assert_called()


@pytest.mark.asyncio
async def test_tend_evolution_prs_iterates_oldest_pr_first(
    mem_fs: dict[str, str],
) -> None:
    """Plans are tended in PR-``created_at`` ascending order.

    The JSONL insertion order of plans is incidental — under any
    interruption (exception, CancelledError, mid-iteration budget flip)
    the older PRs must have had their chance first, since those are the
    PRs most likely to be drifting from ``main`` and needing help.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=False)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    # Persist in deliberately reversed order — newest plan first in JSONL.
    engine._memory.save_evolution_plan(
        _plan(plan_id="ep_new", pr_url="https://github.com/x/y/pull/3"),
    )
    engine._memory.save_evolution_plan(
        _plan(plan_id="ep_mid", pr_url="https://github.com/x/y/pull/2"),
    )
    engine._memory.save_evolution_plan(
        _plan(plan_id="ep_old", pr_url="https://github.com/x/y/pull/1"),
    )
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep_new": _pr_state(
            head_ref="maestro/evolve/ep_new",
            number=3,
            merge_state_status="DIRTY",
            mergeable="CONFLICTING",
            created_at="2026-04-20T00:00:00+00:00",
        ),
        "maestro/evolve/ep_mid": _pr_state(
            head_ref="maestro/evolve/ep_mid",
            number=2,
            merge_state_status="DIRTY",
            mergeable="CONFLICTING",
            created_at="2026-04-10T00:00:00+00:00",
        ),
        "maestro/evolve/ep_old": _pr_state(
            head_ref="maestro/evolve/ep_old",
            number=1,
            merge_state_status="DIRTY",
            mergeable="CONFLICTING",
            created_at="2026-04-01T00:00:00+00:00",
        ),
    }
    seen: list[str] = []

    async def _record(
        _engine: object, *, plan: EvolutionPlan, **_kwargs: object,
    ) -> None:
        seen.append(plan.plan_id)

    with patch.object(evolution_tend, "_tend_single_pr", new=AsyncMock(side_effect=_record)):
        await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert seen == ["ep_old", "ep_mid", "ep_new"]


@pytest.mark.asyncio
async def test_cancelled_error_runs_cleanup_then_propagates(
    mem_fs: dict[str, str],
) -> None:
    """A ``CancelledError`` mid-PR-tending must clean the working tree
    before propagating, so the next startup does not find a half-merged
    branch.

    Regression for the gap where the broad ``except Exception`` cleanup
    block did not catch ``BaseException`` subclasses, leaving the loop
    branch dirty after a SIGTERM during the mechanical merge probe.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=False)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    with (
        patch.object(
            evolution_tend, "_tend_single_pr",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.abort_merge.assert_called()
    vcs.discard_changes.assert_called()
    vcs.checkout.assert_any_call("main")


# ---------------------------------------------------------------------------
# _tend_single_pr — checkout fallback to origin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_fallback_to_origin(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    # First checkout of the branch fails (branch not local) — the fallback
    # calls checkout("origin/<branch>") and then checkout_new_branch.
    # The final checkout("main") at end of _tend_single_pr also succeeds.
    def _checkout(ref: str) -> None:
        if ref == "maestro/evolve/ep1":
            raise VCSError("no local branch")
        return None
    vcs.checkout.side_effect = _checkout
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(merge_state_status="BEHIND"),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.merged_updated == 1
    vcs.checkout_new_branch.assert_called_once_with("maestro/evolve/ep1")


@pytest.mark.asyncio
async def test_checkout_fallback_failure_records_error(
    mem_fs: dict[str, str],
) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    # Both the primary and origin checkouts fail.
    vcs.checkout.side_effect = VCSError("no branch")
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(merge_state_status="BEHIND"),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.errors
    assert "could not checkout" in result.errors[0]
    vcs.merge_from.assert_not_called()


# ---------------------------------------------------------------------------
# _tend_single_pr — reset_hard failure is tolerated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_hard_failure_is_tolerated(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.reset_hard.side_effect = VCSError("reset failed")
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(merge_state_status="BEHIND"),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Merge still proceeded despite the reset failure.
    assert result.merged_updated == 1


# ---------------------------------------------------------------------------
# _tend_single_pr — merge_from raises VCSError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_from_vcs_error_records(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.side_effect = VCSError("git merge exploded")
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(merge_state_status="BEHIND"),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.errors
    assert "merge failed" in result.errors[0]


# ---------------------------------------------------------------------------
# _tend_single_pr — push after clean merge fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_after_clean_merge_fails(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    vcs.push.side_effect = VCSError("remote rejected")
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(merge_state_status="BEHIND"),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.merged_updated == 0
    assert result.errors
    assert "push after clean merge failed" in result.errors[0]


# ---------------------------------------------------------------------------
# _tend_single_pr — push failure fetches and retries (#2568)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tend_push_failure_fetches_and_retries(
    mem_fs: dict[str, str],
) -> None:
    """#2568 — a push rejected because origin advanced between the
    divergence measurement and the push is retried once after a
    best-effort fetch, delivering the merge in the same tend lap
    instead of deferring to the next."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    vcs.push.side_effect = [VCSError("remote rejected"), None]
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(merge_state_status="BEHIND"),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert vcs.push.call_count == 2
    # Exactly one bare fetch("origin") — the pre-retry fetch (the sweep's
    # own opening fetch passes prune=True and is not counted here).
    retry_fetches = [
        c for c in vcs.fetch.call_args_list if c == call("origin")
    ]
    assert len(retry_fetches) == 1
    assert result.errors == []
    assert result.merged_updated == 1


@pytest.mark.asyncio
async def test_tend_push_retry_fetch_failure_tolerated(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """#2568 — the pre-retry fetch is best-effort: a fetch failure logs a
    WARNING and the push retry still runs (and may still succeed)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    vcs.push.side_effect = [VCSError("remote rejected"), None]

    def _fetch(
        remote: str = "origin", ref: str | None = None,
        *, prune: bool = False,
    ) -> None:
        # Fail only the bare pre-retry fetch("origin"); the sweep's own
        # opening fetch (prune=True) must succeed so tending proceeds.
        if prune or ref is not None:
            return
        raise VCSError("fetch boom")

    vcs.fetch.side_effect = _fetch
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(merge_state_status="BEHIND"),
    }
    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert vcs.push.call_count == 2
    warnings = [
        rec.getMessage() for rec in caplog.records
        if rec.levelno == logging.WARNING
    ]
    assert any(
        "Fetch before tend push retry failed" in msg for msg in warnings
    )
    assert result.errors == []
    assert result.merged_updated == 1


@pytest.mark.asyncio
async def test_tend_push_failure_retry_also_fails_logs_error(
    mem_fs: dict[str, str],
) -> None:
    """#2568 — when the post-fetch retry also fails, the error is recorded
    exactly as before the retry existed (the
    ``test_push_after_clean_merge_fails`` behaviour, preserved) so the
    Tend PR fallback still applies."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    vcs.push.side_effect = VCSError("remote rejected")
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(merge_state_status="BEHIND"),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert vcs.push.call_count == 2
    # The pre-retry fetch("origin") ran exactly once (the sweep's opening
    # fetch passes prune=True and is not counted here).
    retry_fetches = [
        c for c in vcs.fetch.call_args_list if c == call("origin")
    ]
    assert len(retry_fetches) == 1
    assert result.merged_updated == 0
    assert result.errors
    assert "push after clean merge failed" in result.errors[0]


# ---------------------------------------------------------------------------
# _tend_single_pr — unexpected conflict on a branch we thought was clean
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_scan_list_remote_branches_failure(
    mem_fs: dict[str, str],
) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.side_effect = RuntimeError("remote unreachable")
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_open_evolution_pr_states.return_value = {}  # type: ignore[attr-defined]
    # Doesn't raise; errors list is still empty (orphan scan is best-effort).
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.orphans_repaired == 0
    assert result.errors == []


def _failing_gates() -> QualityGateResult:
    return QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="ruff: E501 line too long",
    )


# ---------------------------------------------------------------------------
# TendPRsResult — stale-review counter defaults to 0
# ---------------------------------------------------------------------------


def test_tend_prs_result_stale_review_default_zero() -> None:
    r = TendPRsResult()
    assert r.stale_review_resumed == 0
