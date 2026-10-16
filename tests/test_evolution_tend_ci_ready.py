"""CI-wait gate, ready-for-review ping and owed-acts observation inside tend;
split out of ``tests/test_evolution_tend.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import evolution_tend
from maestro.ci_diagnosis import CheckRun
from maestro.config import Config
from maestro.evolution import EvolutionEngine, QualityGateResult
from maestro.evolution_tend import TendPRsResult
from maestro.gate_observation import (
    GateFailure,
    GateObservation,
    GateRunObservation,
)
from maestro.memory import EvolutionPlan
from maestro.pr_facts import PrStaleness
from maestro.pr_owed_act import OwedAct
from maestro.remediation_tracker import (
    MERGE_BLOCK_CONFLICTING,
    MERGE_BLOCK_NONE,
)
from maestro.vcs import MergeResult, VCSError
from tests.support.merge_truth import merge_oracle
from tests.test_evolution_tend import (
    FAKE_DIR,
    _ground_vcs,
    _make_engine,
    _plan,
    _pr_state,
)

# ---------------------------------------------------------------------------
# CI-wait gate inside ``_tend_single_pr``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ci_wait_pending_defers_without_merging(
    mem_fs: dict[str, str],
) -> None:
    """When the PR's CI rollup is still pending, the tend body returns
    without merging — pushing now would invalidate the in-flight run."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = ""  # type: ignore[attr-defined]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.deferred_for_ci == 1
    vcs.merge_from.assert_not_called()
    vcs.checkout.assert_any_call("main")


@pytest.mark.asyncio
async def test_ci_wait_failure_routes_into_quality_gates(
    mem_fs: dict[str, str],
) -> None:
    """A ``failure`` CI rollup on the existing head routes through
    ``quality_gates_routed`` with the head_ref so the remediation task
    fixes the broken commit rather than masking it under a main-merge."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.merge_from.assert_not_called()
    assert result.quality_gates_routed == [
        (
            1, "t", "ci_failure_on_head",
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    # Proposal 137: the pre-merge route stamps the full signature it is
    # bouncing off, so a later lap can release the cooldown the moment the
    # head, base or rollup provably changes.
    stamped = engine._memory.get_plan("ep1")
    assert stamped is not None
    assert stamped.last_quality_fix_head_sha == "h"
    assert stamped.last_quality_fix_base_sha == "h"
    assert stamped.last_quality_fix_ci_conclusion == "failure"
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.last_quality_fix_attempt_at is not None


@pytest.mark.asyncio
async def test_conflicting_pr_with_failing_head_routes_conflicts(
    mem_fs: dict[str, str],
) -> None:
    """A PR that is both CONFLICTING and has a ``failure`` head rollup routes
    through conflict resolution, not the pre-merge ``ci_failure_on_head`` path
    (#2440): head CI cannot be validated on a branch that will not merge, so
    conflict routing takes precedence."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=False)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    vcs.merge_from.return_value = MergeResult(
        success=False, conflicted_files=["a.py"],
    )
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.merge_from.assert_called_once()
    vcs.abort_merge.assert_called_once()
    assert result.quality_gates_routed == []
    assert len(result.conflicts_routed) == 1
    assert result.conflicts_routed[0][0] == 1
    assert result.conflicts_routed[0][2] == ["a.py"]


@pytest.mark.asyncio
async def test_failing_head_with_local_commits_merges_and_pushes(
    mem_fs: dict[str, str],
) -> None:
    """Reproduces the #2472 deadlock: a non-conflicting PR with a ``failure``
    head rollup AND local remediation commits ahead of origin must fall through
    to the merge → gates → push path rather than re-routing
    ``quality_gates_routed`` forever, so the trailing ``push`` delivers the
    local fix and lets CI re-run."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"  # branch resolves locally
    vcs.commits_ahead_behind.return_value = (2, 0)  # local fix ahead
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.quality_gates_routed == []
    assert result.merged_updated == 1
    vcs.merge_from.assert_called_once()
    vcs.push.assert_called_once_with("maestro/evolve/ep1")


@pytest.mark.asyncio
async def test_failing_head_no_local_commits_routes_quality_gates(
    mem_fs: dict[str, str],
) -> None:
    """No-local-commits behavior preserved: a ``failure`` head with no commits
    ahead still routes ``quality_gates_routed`` under the cooldown throttle and
    returns after ``checkout("main")`` without merging or pushing (#2472)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)  # no local fix
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert len(result.quality_gates_routed) == 1
    vcs.merge_from.assert_not_called()
    vcs.push.assert_not_called()
    vcs.checkout.assert_any_call("main")


@pytest.mark.asyncio
async def test_failing_head_no_local_commits_in_cooldown_no_reroute(
    mem_fs: dict[str, str],
) -> None:
    """Cooldown still throttles the no-commits route: a ``failure`` head with no
    local commits whose ``last_quality_fix_attempt_at`` is within the cooldown
    window records no append and never pushes (current behavior preserved)."""
    recent_iso = datetime.now(UTC).isoformat()
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)  # no local fix
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    plan = _plan()
    plan.last_quality_fix_attempt_at = recent_iso
    engine._memory.save_evolution_plan(plan)
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.quality_gates_routed == []
    vcs.merge_from.assert_not_called()
    vcs.push.assert_not_called()


@pytest.mark.asyncio
async def test_failing_head_no_local_commits_logs_deferral(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """The load-bearing CI-failure merge-refusal announces itself: a ``failure``
    head with no local commits ahead emits one greppable INFO line naming the
    failing check and the main-merge deferral, while still refusing to merge or
    push (#2836)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)  # no local fix
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    engine._host.get_pr_failing_check_names.return_value = [  # type: ignore[attr-defined]
        "browser-tests",
    ]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }

    with caplog.at_level(logging.INFO, logger="maestro"):
        result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    # Merge-refusal behavior preserved: routed, never merged/pushed.
    assert len(result.quality_gates_routed) == 1
    vcs.merge_from.assert_not_called()
    vcs.push.assert_not_called()
    # The decision is now greppable: one INFO line naming the failing check.
    deferral = [
        r.getMessage()
        for r in caplog.records
        if "head CI failing" in r.getMessage()
    ]
    assert len(deferral) == 1
    assert "PR #1" in deferral[0]
    assert "browser-tests" in deferral[0]
    assert "deferring main-merge" in deferral[0]
    assert "routing quality-gate remediation" in deferral[0]


@pytest.mark.asyncio
async def test_failing_head_no_local_commits_in_cooldown_logs_deferral(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """Even when the routing is cooldown-throttled, the merge-refusal still
    announces itself: a ``failure`` head within the quality-fix cooldown window
    records no append yet emits one INFO line naming the cooldown deferral and
    the failing check (#2836)."""
    recent_iso = datetime.now(UTC).isoformat()
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)  # no local fix
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    engine._host.get_pr_failing_check_names.return_value = [  # type: ignore[attr-defined]
        "browser-tests",
    ]
    plan = _plan()
    plan.last_quality_fix_attempt_at = recent_iso
    engine._memory.save_evolution_plan(plan)
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }

    with caplog.at_level(logging.INFO, logger="maestro"):
        result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    # Cooldown still throttles the append, but the deferral is still announced.
    assert result.quality_gates_routed == []
    vcs.merge_from.assert_not_called()
    vcs.push.assert_not_called()
    deferral = [
        r.getMessage()
        for r in caplog.records
        if "head CI failing" in r.getMessage()
    ]
    assert len(deferral) == 1
    assert "browser-tests" in deferral[0]
    assert "in quality-fix cooldown" in deferral[0]


@pytest.mark.asyncio
async def test_conflicting_failing_head_with_local_commits_routes_conflict(
    mem_fs: dict[str, str],
) -> None:
    """Conflict-routing precedence still holds (#2440), but the gate-validated
    local resolution is now pushed before the conflict is routed (#2479): a
    CONFLICTING PR with a ``failure`` head and local commits ahead
    (``ahead>0``, ``behind==0`` vs ``origin/<branch>``) falls into the merge
    body, which detects the conflict and routes ``conflicts_routed`` — and,
    because the local head strictly extends its own remote and passes the
    gates, ships that head so the stranded resolution is not frozen forever.
    This supersedes the pre-#2479 "never a push" stance from #2472; the head's
    failing CI does not block delivery because the local resolution is
    independently gate-validated and the post-push CI re-run is authoritative."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (2, 0)  # local commits ahead
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(
        success=False, conflicted_files=["a.py"],
    )
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.quality_gates_routed == []
    assert len(result.conflicts_routed) == 1
    assert result.conflicts_routed[0][2] == ["a.py"]
    # #2479 — gate-validated local resolution is delivered despite the
    # re-conflict and the failing head CI; the conflict is still routed.
    vcs.push.assert_called_once_with("maestro/evolve/ep1")


@pytest.mark.asyncio
async def test_failing_head_local_commits_in_cooldown_pushes_without_gates(
    mem_fs: dict[str, str],
) -> None:
    """The escape-hatch fall-through inside the quality-fix cooldown window
    pushes the local commits WITHOUT re-running the local gates (#2472): a
    ``failure`` head with local commits ahead and a recent
    ``last_quality_fix_attempt_at`` skips ``run_quality_gates`` (gated on
    ``not _in_quality_fix_cooldown``) yet still pushes, so the post-push CI
    re-run is the sole validator in that window."""
    recent_iso = datetime.now(UTC).isoformat()
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (2, 0)  # local fix ahead
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs, stub_quality_gates=False)
    engine.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=QualityGateResult(
            passed=False, ruff_ok=False, mypy_ok=False, tests_ok=False,
            raw_output="",
        ),
    )
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    plan = _plan()
    plan.last_quality_fix_attempt_at = recent_iso
    engine._memory.save_evolution_plan(plan)
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Cooldown bypassed the gate run; the fall-through still pushes.
    engine.run_quality_gates.assert_not_called()
    assert result.merged_updated == 1
    vcs.push.assert_called_once_with("maestro/evolve/ep1")


@pytest.mark.asyncio
async def test_ci_failure_on_head_summary_names_failing_checks(
    mem_fs: dict[str, str],
) -> None:
    """A clean-against-main PR with a failing head threads the real failing
    check name(s) into the ``ci_failure_on_head`` gate_summary (#2440)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    engine._host.get_pr_failing_check_names.return_value = [  # type: ignore[attr-defined]
        "quality", "browser-tests",
    ]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.merge_from.assert_not_called()
    assert len(result.quality_gates_routed) == 1
    assert (
        result.quality_gates_routed[0][2]
        == "ci_failure_on_head: quality, browser-tests"
    )


@pytest.mark.asyncio
async def test_ci_failure_on_head_failing_names_fetch_error_falls_back(
    mem_fs: dict[str, str],
) -> None:
    """If the failing-check-name fetch raises, the route still records the bare
    sentinel rather than crashing the mechanical tend phase (#2440)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    engine._host.get_pr_failing_check_names.side_effect = VCSError(  # type: ignore[attr-defined]
        "boom",
    )
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.merge_from.assert_not_called()
    assert len(result.quality_gates_routed) == 1
    assert result.quality_gates_routed[0][2] == "ci_failure_on_head"


@pytest.mark.asyncio
async def test_ci_failure_on_head_within_cooldown_does_not_reroute(
    mem_fs: dict[str, str],
) -> None:
    """A ``failure`` head whose ``last_quality_fix_attempt_at`` is within the
    cooldown window is NOT re-routed: no append, no re-stamp, no merge — yet it
    still checks out main and returns rather than masking the failure (#2110)."""
    recent_iso = datetime.now(UTC).isoformat()
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    plan = _plan()
    plan.last_quality_fix_attempt_at = recent_iso
    engine._memory.save_evolution_plan(plan)
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.quality_gates_routed == []
    vcs.merge_from.assert_not_called()
    # The head's CI is still failing, so the branch must NOT fall through to the
    # merge/push path: ``checkout("main")`` stays OUTSIDE the cooldown guard and
    # must run even when re-routing is suppressed, leaving the working tree on
    # main rather than the PR branch for the next loop iteration (#2110).
    vcs.checkout.assert_called_with("main")
    # The cooldown stamp is preserved, not re-written to ``now``.
    stored = engine._memory.get_plan("ep1")
    assert stored is not None
    assert stored.last_quality_fix_attempt_at == recent_iso


@pytest.mark.asyncio
async def test_ci_failure_on_head_past_cooldown_reroutes(
    mem_fs: dict[str, str],
) -> None:
    """A ``failure`` head whose ``last_quality_fix_attempt_at`` is older than the
    cooldown window routes again — re-stamping and appending — proving the guard
    only suppresses within the window (#2110)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    plan = _plan()
    plan.last_quality_fix_attempt_at = "2025-01-01T00:00:00+00:00"
    engine._memory.save_evolution_plan(plan)
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.merge_from.assert_not_called()
    assert result.quality_gates_routed == [
        (
            1, "t", "ci_failure_on_head",
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    stored = engine._memory.get_plan("ep1")
    assert stored is not None
    assert stored.last_quality_fix_attempt_at != "2025-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_ci_failure_on_head_records_completion_time(
    mem_fs: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CI-failure-on-head quality-fix record stamps the time the routing
    attempt actually resolved, not the iteration-start ``now`` threaded through
    the tend loop (#1679).

    Mirrors ``test_post_merge_gate_failure_records_completion_time`` for the
    sibling record site: a single tend iteration captures one ``now`` at the
    top and reuses it for every open PR, so a later PR's record must reflect
    the wall-clock time elapsed by the time the route resolves — not the stale
    iteration-start ``now``. Unlike the post-merge route, this path never calls
    ``run_quality_gates()``, so the ``get_pr_failing_check_names`` call that
    immediately precedes the record advances a mocked clock to simulate that
    elapsed time. The pre-fix code recording ``now.isoformat()`` would stamp
    ``t0`` and fail the final assertion, so this pins the ``:4246`` site the
    ``past_cooldown`` test's ``!= <old>`` assertion left unguarded.
    """
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    clock = [t0]

    class _ClockDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return clock[0]

    # Every ``datetime.now(UTC)`` in evolution_tend.py now reads the mutable clock:
    # the iteration-start capture sees ``t0``; a fresh call at the record site
    # sees the value the failing-check probe advanced it to.
    monkeypatch.setattr("maestro.evolution_tend.datetime", _ClockDatetime)

    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]

    def _advance_and_list(_pr_number: int) -> list[str]:
        # The tend lap consumes real wall-clock time before reaching the
        # record site; simulate 10 minutes elapsing after the iteration-start
        # ``now`` capture. Returning ``[]`` keeps the bare ``ci_failure_on_head``
        # summary, matching ``test_ci_failure_on_head_past_cooldown_reroutes``.
        clock[0] = t0 + timedelta(minutes=10)
        return []

    engine._host.get_pr_failing_check_names.side_effect = _advance_and_list  # type: ignore[attr-defined]
    plan = _plan()
    # ``None`` → not in cooldown, so the CI-failure route reaches the record.
    plan.last_quality_fix_attempt_at = None
    engine._memory.save_evolution_plan(plan)
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.merge_from.assert_not_called()
    # Routing still fires, exactly like the sibling past-cooldown test.
    assert result.quality_gates_routed == [
        (
            1, "t", "ci_failure_on_head",
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    stored = engine._memory.get_plan("ep1")
    assert stored is not None
    # The record is the routing-completion time (t0 + 10min), NOT iteration
    # -start ``now`` (t0). Under the pre-fix code this equals ``t0.isoformat()``.
    assert stored.last_quality_fix_attempt_at == (
        t0 + timedelta(minutes=10)
    ).isoformat()


@pytest.mark.asyncio
async def test_ci_wait_success_proceeds_to_merge(
    mem_fs: dict[str, str],
) -> None:
    """A ``success`` rollup falls through; the existing merge+push path runs."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    # Default is already "success" via _make_engine; assert explicitly.
    assert engine._host.get_pr_check_conclusion.return_value == "success"  # type: ignore[attr-defined]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    engine._host.get_pr_check_conclusion.assert_called_with(1)  # type: ignore[attr-defined]
    assert result.merged_updated == 1
    vcs.push.assert_called_once_with("maestro/evolve/ep1")


@pytest.mark.asyncio
async def test_ci_wait_no_ci_proceeds_to_merge(
    mem_fs: dict[str, str],
) -> None:
    """A ``"none"`` rollup (no CI configured) falls through like ``success``:
    the merge+push path runs and the PR is not deferred forever (#2109)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.return_value = "none"  # type: ignore[attr-defined]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.deferred_for_ci == 0
    assert result.merged_updated == 1
    vcs.push.assert_called_once_with("maestro/evolve/ep1")


# ---------------------------------------------------------------------------
# Ready-for-review ping — stand-down signal after a clean push
# ---------------------------------------------------------------------------


def _recent_iso() -> str:
    """A timestamp recent enough to suppress the tapered PR reminder."""
    return datetime.now().astimezone().isoformat()


async def _deliver_and_stamp(
    mem_fs: dict[str, str],
    *,
    config: Config | None = None,
) -> tuple[EvolutionEngine, MagicMock, AsyncMock]:
    """Run one delivery sweep so the plan ends carrying a pending head stamp.

    A BEHIND PR merges ``origin/main``, passes gates, and pushes — delivery
    increments ``merged_updated`` and stamps ``ready_for_review_pending_sha``
    (``"h"``, the uniform ``rev_parse`` return) WITHOUT pinging. Returns
    ``(engine, vcs, notify)``; callers run a second (confirm) sweep.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    # Grounded staleness (095): the delivery sweep's BEHIND PR is measured
    # behind + cleanly mergeable, so it reaches merge_from + push and stamps.
    _ground_vcs(vcs, behind=1, merge_clean=True)
    engine = _make_engine(mem_fs, vcs=vcs, config=config)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND",
            mergeable="MERGEABLE",
            updated_at=_recent_iso(),
        ),
    }
    notify = AsyncMock()
    engine._notifier.notify = notify  # type: ignore[method-assign]

    deliver = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert deliver.merged_updated == 1
    assert deliver.ready_for_review_sent == 0
    notify.assert_not_awaited()
    stamped = engine._memory.get_plan("ep1")
    assert stamped is not None
    assert stamped.ready_for_review_pending_sha == "h"
    # Delivery landed; reset the grounded staleness to "current" so the caller's
    # confirmation sweep hits the is_current short-circuit (its CLEAN confirm PR
    # is level with main).
    vcs.commits_ahead_behind.side_effect = None
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.merge_tree_conflicts.return_value = False
    return engine, vcs, notify


def _set_clean_confirm_pr(engine: EvolutionEngine) -> None:
    """Swap the open-PR set to a CLEAN PR for the confirmation sweep."""
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="CLEAN",
            updated_at=_recent_iso(),
        ),
    }


def _pending_sha(engine: EvolutionEngine) -> str | None:
    """Reload the plan and return its ``ready_for_review_pending_sha``."""
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    return plan.ready_for_review_pending_sha


@pytest.mark.asyncio
async def test_ready_for_review_ping_fires_on_clean_push(
    mem_fs: dict[str, str],
) -> None:
    """Delivery stamps; a later clean pass with green CI fires the ping once."""
    engine, _vcs, notify = await _deliver_and_stamp(mem_fs)

    # Confirm pass: the branch is clean again and the delivered head's CI is
    # green — the deferred ping fires and the stamp is cleared.
    _set_clean_confirm_pr(engine)
    engine._host.get_pr_check_conclusion.return_value = "success"  # type: ignore[attr-defined]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.ready_for_review_sent == 1
    notify.assert_awaited_once()
    (sent_message,) = notify.await_args.args
    assert "ready for re-review" in sent_message
    assert _pending_sha(engine) is None


@pytest.mark.asyncio
async def test_ready_for_review_ping_suppressed_when_comments_open(
    mem_fs: dict[str, str],
) -> None:
    """An unresolved comment on the confirm pass blocks the ping; stamp kept.

    Delivery stamped the head, but the confirmation sweep routes an open
    review comment, so "all comments addressed" is false — the ping is withheld
    and the stamp is preserved for the next sweep.
    """
    engine, _vcs, notify = await _deliver_and_stamp(mem_fs)
    _set_clean_confirm_pr(engine)

    def _route(
        _engine: object, plan: EvolutionPlan, pr: object, result: TendPRsResult,
    ) -> bool:
        result.bug_comments_routed.append(
            (pr.number, pr.title, [], plan.plan_id, "", "", pr.head_ref),  # type: ignore[attr-defined]
        )
        return True

    with patch.object(evolution_tend, "_route_bug_comments", side_effect=_route):
        result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.ready_for_review_sent == 0
    notify.assert_not_awaited()
    assert _pending_sha(engine) == "h"


@pytest.mark.asyncio
async def test_ready_for_review_ping_skipped_without_push(
    mem_fs: dict[str, str],
) -> None:
    """A clean, idle PR (no push) never pings — Maestro never tended it."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="CLEAN", updated_at=_recent_iso(),
        ),
    }
    notify = AsyncMock()
    engine._notifier.notify = notify  # type: ignore[method-assign]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.merged_updated == 0
    assert result.ready_for_review_sent == 0
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_for_review_ping_disabled_by_flag(
    mem_fs: dict[str, str],
) -> None:
    """With the flag off, a clean push delivers but stamps nothing — no ping."""
    cfg = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    cfg.pr_ready_for_review_notify_enabled = False
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs, config=cfg)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND",
            mergeable="MERGEABLE",
            updated_at=_recent_iso(),
        ),
    }
    notify = AsyncMock()
    engine._notifier.notify = notify  # type: ignore[method-assign]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.merged_updated == 1
    assert result.ready_for_review_sent == 0
    notify.assert_not_awaited()
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.ready_for_review_pending_sha is None


@pytest.mark.asyncio
async def test_ready_for_review_no_stamp_when_bug_comments_disabled(
    mem_fs: dict[str, str],
) -> None:
    """With comment-tending off, delivery never stamps and the ping never fires.

    The deferred ping's "no comment unaddressed" claim is only meaningful when
    comment-tending is the source of truth, so the delivery stamp is gated on
    ``tend_bug_comments_enabled``.
    """
    cfg = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    cfg.tend_bug_comments_enabled = False
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs, config=cfg)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND",
            mergeable="MERGEABLE",
            updated_at=_recent_iso(),
        ),
    }
    notify = AsyncMock()
    engine._notifier.notify = notify  # type: ignore[method-assign]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.merged_updated == 1
    assert result.ready_for_review_sent == 0
    notify.assert_not_awaited()
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.ready_for_review_pending_sha is None


@pytest.mark.asyncio
async def test_ready_for_review_no_stamp_when_rev_parse_none(
    mem_fs: dict[str, str],
) -> None:
    """A ``None`` delivered-head resolution skips the stamp without crashing."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"

    def _rev_parse(ref: str) -> str | None:
        # The bare branch ref (the delivered-head stamp source) is
        # unresolvable; everything else (origin/*, main) resolves normally.
        return None if ref == "maestro/evolve/ep1" else "h"

    vcs.rev_parse.side_effect = _rev_parse
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND",
            mergeable="MERGEABLE",
            updated_at=_recent_iso(),
        ),
    }
    notify = AsyncMock()
    engine._notifier.notify = notify  # type: ignore[method-assign]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.merged_updated == 1
    assert result.ready_for_review_sent == 0
    notify.assert_not_awaited()
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.ready_for_review_pending_sha is None


@pytest.mark.asyncio
async def test_ready_for_review_confirm_ci_pending_keeps_stamp(
    mem_fs: dict[str, str],
) -> None:
    """CI still pending on the confirm pass: no ping, stamp kept for reprobe."""
    engine, _vcs, notify = await _deliver_and_stamp(mem_fs)
    _set_clean_confirm_pr(engine)
    engine._host.get_pr_check_conclusion.return_value = ""  # type: ignore[attr-defined]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.ready_for_review_sent == 0
    notify.assert_not_awaited()
    assert _pending_sha(engine) == "h"


@pytest.mark.asyncio
async def test_ready_for_review_confirm_ci_error_keeps_stamp(
    mem_fs: dict[str, str],
) -> None:
    """A ``VCSError`` reading CI is treated as pending: no ping, stamp kept."""
    engine, _vcs, notify = await _deliver_and_stamp(mem_fs)
    _set_clean_confirm_pr(engine)
    engine._host.get_pr_check_conclusion.side_effect = VCSError("gh down")  # type: ignore[attr-defined]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.ready_for_review_sent == 0
    notify.assert_not_awaited()
    assert _pending_sha(engine) == "h"


@pytest.mark.asyncio
async def test_ready_for_review_confirm_ci_failure_clears_stamp(
    mem_fs: dict[str, str],
) -> None:
    """A failing delivered head clears the stamp without pinging."""
    engine, _vcs, notify = await _deliver_and_stamp(mem_fs)
    _set_clean_confirm_pr(engine)
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.ready_for_review_sent == 0
    notify.assert_not_awaited()
    assert _pending_sha(engine) is None


@pytest.mark.asyncio
async def test_ready_for_review_confirm_ci_none_fires(
    mem_fs: dict[str, str],
) -> None:
    """No CI configured (``none``) is vacuously green — the ping fires once."""
    engine, _vcs, notify = await _deliver_and_stamp(mem_fs)
    _set_clean_confirm_pr(engine)
    engine._host.get_pr_check_conclusion.return_value = "none"  # type: ignore[attr-defined]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.ready_for_review_sent == 1
    notify.assert_awaited_once()
    assert _pending_sha(engine) is None


@pytest.mark.asyncio
async def test_ready_for_review_confirm_head_mismatch_clears_stamp(
    mem_fs: dict[str, str],
) -> None:
    """A head that advanced past the stamp clears it without pinging."""
    engine, vcs, notify = await _deliver_and_stamp(mem_fs)
    _set_clean_confirm_pr(engine)
    # The branch head advanced since delivery — origin now resolves to a
    # different SHA than the one we stamped, so this is not the head to confirm.
    vcs.rev_parse.return_value = "advanced"

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.ready_for_review_sent == 0
    notify.assert_not_awaited()
    assert _pending_sha(engine) is None


@pytest.mark.asyncio
async def test_ready_for_review_confirm_comment_scan_error_keeps_stamp(
    mem_fs: dict[str, str],
) -> None:
    """A comment-scan fetch error on the confirm pass blocks the ping (strict).

    Even with green CI, an errored thread fetch must not read as "zero
    comments" — the ping is withheld and the stamp kept for the next sweep.
    """
    engine, _vcs, notify = await _deliver_and_stamp(mem_fs)
    _set_clean_confirm_pr(engine)
    engine._host.get_pr_check_conclusion.return_value = "success"  # type: ignore[attr-defined]
    engine._host.get_pr_review_threads_strict.side_effect = VCSError("gh down")  # type: ignore[attr-defined]

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.ready_for_review_sent == 0
    notify.assert_not_awaited()
    assert _pending_sha(engine) == "h"


@pytest.mark.asyncio
async def test_ready_for_review_confirm_dedup_no_second_fire(
    mem_fs: dict[str, str],
) -> None:
    """After the ping fires and clears the stamp, a third clean pass is silent."""
    engine, _vcs, notify = await _deliver_and_stamp(mem_fs)
    _set_clean_confirm_pr(engine)
    engine._host.get_pr_check_conclusion.return_value = "success"  # type: ignore[attr-defined]

    first = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert first.ready_for_review_sent == 1
    notify.assert_awaited_once()
    assert _pending_sha(engine) is None

    # Third (still clean) pass: the stamp is already cleared → no second ping.
    third = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert third.ready_for_review_sent == 0
    notify.assert_awaited_once()
    assert _pending_sha(engine) is None


@pytest.mark.asyncio
async def test_ready_for_review_notify_failure_captured(
    mem_fs: dict[str, str],
) -> None:
    """A notify failure on the confirm ping is captured; the stamp is kept."""
    engine, _vcs, _notify = await _deliver_and_stamp(mem_fs)
    _set_clean_confirm_pr(engine)
    engine._notifier.notify = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("telegram offline"),
    )

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert result.ready_for_review_sent == 0
    assert len(result.errors) == 1
    assert "ready-for-review notify failed" in result.errors[0]
    assert "ep1" in result.errors[0]
    assert _pending_sha(engine) == "h"


@pytest.mark.asyncio
async def test_ready_for_review_notifier_cancelled_keeps_stamp(
    mem_fs: dict[str, str],
) -> None:
    """A cancelled notifier must not burn the ready-for-review retry token.

    The helper swallows ``CancelledError`` from the notifier itself (shutdown
    discipline), so the sweep sees no exception. The stamp is the only retry
    token: clearing it on an undelivered ping would suppress the re-review
    nudge for that head forever, so it must survive and the count must not move.
    """
    engine, _vcs, notify = await _deliver_and_stamp(mem_fs)
    _set_clean_confirm_pr(engine)
    notify.side_effect = asyncio.CancelledError()

    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    notify.assert_awaited_once()
    assert result.ready_for_review_sent == 0
    assert len(result.errors) == 1
    assert "ready-for-review notify cancelled" in result.errors[0]
    assert "ep1" in result.errors[0]
    assert _pending_sha(engine) == "h"


@pytest.mark.asyncio
async def test_ready_for_review_notify_cancelled_propagates(
    mem_fs: dict[str, str],
) -> None:
    """``CancelledError`` from the confirm ping propagates out of the sweep."""
    engine, _vcs, _notify = await _deliver_and_stamp(mem_fs)
    _set_clean_confirm_pr(engine)

    with (
        patch.object(
            evolution_tend, "_send_pr_ready_for_review",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await engine.tend_evolution_prs(settled_oracle=merge_oracle())


@pytest.mark.asyncio
async def test_send_pr_ready_for_review_propagates_notifier_error(
    mem_fs: dict[str, str],
) -> None:
    """A generic ``Exception`` from the ready-for-review notifier propagates.

    Mirrors :func:`test_send_pr_reminder_propagates_notifier_error`: only
    ``asyncio.CancelledError`` is suppressed; everything else surfaces.
    """
    engine = _make_engine(mem_fs)
    engine._notifier.notify = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("telegram offline"),
    )
    with pytest.raises(RuntimeError, match="telegram offline"):
        await evolution_tend._send_pr_ready_for_review(engine, _plan(), _pr_state())


@pytest.mark.asyncio
async def test_send_pr_ready_for_review_suppresses_cancelled(
    mem_fs: dict[str, str],
) -> None:
    """``CancelledError`` (the documented SDK-teardown leak) is swallowed."""
    engine = _make_engine(mem_fs)
    engine._notifier.notify = AsyncMock(  # type: ignore[method-assign]
        side_effect=asyncio.CancelledError(),
    )
    # Must return without raising.
    await evolution_tend._send_pr_ready_for_review(engine, _plan(), _pr_state())


# ── The base-branch gate baseline (proposal 124) ───────────────────────────────


def _tend_baseline_result() -> QualityGateResult:
    """A red gate result carrying an observation, for stubbing the baseline runner."""
    failure = GateFailure(
        tool="mypy", kind="FAILED", location="maestro/x.py",
        identifier="maestro/x.py:1:name-defined", message="",
    )
    return QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=False, tests_ok=True, raw_output="",
        observation=GateRunObservation(
            gates=(
                GateObservation(
                    tool="mypy", kind="FAILED", failures=(failure,), output="",
                ),
            ),
        ),
    )


def _baseline_tend_vcs() -> MagicMock:
    """A VCS on a clean ``main`` already reconciled to ``origin/main``."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "mainhash"
    vcs.get_head_hash.return_value = "mainhash"
    vcs.has_changes.return_value = False
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    return vcs


def _baseline_tend_host() -> MagicMock:
    """A host carrying one open evolution PR — the worklist the capture needs."""
    host = MagicMock()
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/ep1": _pr_state(),
    }
    return host


@pytest.mark.asyncio
async def test_tend_baselines_the_origin_main_head_once(
    mem_fs: dict[str, str],
) -> None:
    """Tend captures a baseline of ``origin/main``, then is served from cache.

    ``ensure_main_branch`` has already put HEAD on ``main`` and the reconcile has
    healed it to ``origin/main``, so the seam's HEAD check passes on exactly the
    laps where the tree really is the base. The cache is what makes this one gate
    run per new head rather than one per lap.
    """
    engine = _make_engine(
        mem_fs,
        vcs=_baseline_tend_vcs(),
        code_host=_baseline_tend_host(),
        config=Config(
            memory_dir=FAKE_DIR, workspace=FAKE_DIR, gate_observation_enabled=True,
        ),
        stub_quality_gates=False,
    )
    engine.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_tend_baseline_result(),
    )

    await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert list(engine._gate_baselines) == ["mainhash"]
    engine.run_quality_gates.assert_called_once_with()


@pytest.mark.asyncio
async def test_tend_captures_no_baseline_without_an_open_pr(
    mem_fs: dict[str, str],
) -> None:
    """An empty worklist pays nothing — the common quiet lap runs no gate sweep.

    The baseline exists for the consumers that attribute an open PR branch's red
    against the base it was cut from, and ``implement`` captures its own base
    regardless, so a lap with no open evolution PR has nothing to warm the cache
    for. Keeping the sweep out of it is what stops a top-priority, every-iteration,
    burn-rate-exempt phase from growing a multi-minute synchronous head.
    """
    engine = _make_engine(
        mem_fs,
        vcs=_baseline_tend_vcs(),
        config=Config(
            memory_dir=FAKE_DIR, workspace=FAKE_DIR, gate_observation_enabled=True,
        ),
        stub_quality_gates=False,
    )
    engine.run_quality_gates = MagicMock()  # type: ignore[method-assign]

    await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert engine._gate_baselines == {}
    engine.run_quality_gates.assert_not_called()


@pytest.mark.asyncio
async def test_tend_captures_no_baseline_with_the_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Flag-off the capture site is inert — no extra subprocess, no cached fact."""
    engine = _make_engine(
        mem_fs,
        vcs=_baseline_tend_vcs(),
        code_host=_baseline_tend_host(),
        stub_quality_gates=False,
    )
    engine.run_quality_gates = MagicMock()  # type: ignore[method-assign]

    await engine.tend_evolution_prs(settled_oracle=merge_oracle())

    assert engine._gate_baselines == {}
    engine.run_quality_gates.assert_not_called()


def test_settled_oracle_is_required_on_both_merge_readers() -> None:
    """#2958: neither merge-state reader may default its chokepoint oracle.

    A defaulted ``settled_oracle`` would let a caller that forgot to thread the
    chokepoint down silently observe *no* merge state — a degradation the
    merge-truth structural guard cannot catch, since it forbids *calling* a raw
    merge primitive rather than skipping the observation.  Pin both signatures
    so the default cannot come back.
    """
    for method in (
        EvolutionEngine.check_merged_plans,
        EvolutionEngine.tend_evolution_prs,
        evolution_tend._reconcile_orphan_branches,
    ):
        param = inspect.signature(method).parameters["settled_oracle"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# owed_acts_observed — the measured owed act carried out of the walk (135)
# ---------------------------------------------------------------------------


def _refutable_config() -> Config:
    """A tend config with the proposal-135 observation switched on."""
    cfg = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, refutable_standdown_enabled=True,
    )
    cfg.ensure_dirs()
    return cfg


@pytest.mark.asyncio
async def test_owed_acts_observed_populated_with_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 137: the measurement is un-gated, so the default (flag-off)
    config still populates the field for every open own PR the walk visits.

    The *release* path keeps its own guard at its call site, so the flag-off
    deployment footprint is unchanged — the measurement is simply present.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=False)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    assert engine._config.refutable_standdown_enabled is False
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            number=42, merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.owed_acts_observed == {42: "code-owed"}
    assert result.owned_blockers_observed == {42: MERGE_BLOCK_CONFLICTING}


@pytest.mark.asyncio
async def test_owed_acts_observed_conflicting_reads_code_owed(
    mem_fs: dict[str, str],
) -> None:
    """The refuting reading: a behind-and-conflicting PR is owned code work.

    This is the case the whole release path exists for — the walk measures it
    without any extra host call, off the staleness reading plus the ``pr``
    fields already in hand.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=False)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs, config=_refutable_config())
    engine._memory.save_evolution_plan(_plan(last_conflict_attempt=None))
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            number=42, merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.owed_acts_observed == {42: "code-owed"}
    # The blocker rides out beside the act under the same guard, so the
    # withdrawal message can name the concrete reason the claim no longer holds.
    assert result.owned_blockers_observed == {42: MERGE_BLOCK_CONFLICTING}


@pytest.mark.asyncio
async def test_owed_acts_observed_measured_before_conflict_cooldown_skip(
    mem_fs: dict[str, str],
) -> None:
    """A PR the walk skips for conflict cooldown is still measured.

    The cooldown skip is exactly a lap where the latch would otherwise go stale
    unnoticed, so the observation must sit *before* the ``continue``.
    """
    recent = datetime.now().astimezone().isoformat()
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=False)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs, config=_refutable_config())
    engine._memory.save_evolution_plan(_plan(last_conflict_attempt=recent))
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            number=42, merge_state_status="DIRTY", mergeable="CONFLICTING",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.conflicts_cooldown_skipped == 1
    assert result.owed_acts_observed == {42: "code-owed"}


@pytest.mark.asyncio
async def test_owed_acts_observed_measured_before_is_current_short_circuit(
    mem_fs: dict[str, str],
) -> None:
    """A current, clean PR the walk short-circuits is measured too.

    Proposal 137: the measured ``success`` rollup reaches the oracle, so this
    reads the oracle's green verdict rather than the blind ``wait-ci`` the
    previously-hardcoded empty check-run sequence forced.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=0, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs, config=_refutable_config())
    engine._host.get_pr_check_conclusion.return_value = "success"  # type: ignore[attr-defined]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(number=42),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.owed_acts_observed == {42: "handoff-ready"}


@pytest.mark.asyncio
async def test_owed_acts_observed_concluded_red_reads_code_owed(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 137's point: a behind-main PR whose rollup concluded ``failure``
    records ``code-owed``, not ``wait-ci``.

    This is the case the hardcoded empty check-run sequence could not express —
    a self-clearable CI red read as *the clock is running* (someone else's move)
    instead of *I must write code*.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs, config=_refutable_config())
    engine._host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(number=42),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.owed_acts_observed == {42: "code-owed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("conclusion", ["", "pending"])
async def test_owed_acts_observed_unconcluded_rollup_reads_wait_ci(
    mem_fs: dict[str, str], conclusion: str,
) -> None:
    """The fail-safe direction: an in-flight or unreported rollup still reads
    ``wait-ci``, which the validity oracle reads as *holds*."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=0, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs, config=_refutable_config())
    engine._host.get_pr_check_conclusion.return_value = conclusion  # type: ignore[attr-defined]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(number=42),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.owed_acts_observed == {42: "wait-ci"}


@pytest.mark.asyncio
async def test_owed_acts_observed_vcs_error_degrades_to_wait_ci(
    mem_fs: dict[str, str],
) -> None:
    """A ``VCSError`` from the hoisted fetch degrades to ``""`` — the same
    best-effort degrade the two in-body fetch sites used — so the PR reads
    ``wait-ci`` rather than crashing the walk."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=0, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs, config=_refutable_config())
    engine._host.get_pr_check_conclusion.side_effect = VCSError("gh down")  # type: ignore[attr-defined]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(number=42),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.owed_acts_observed == {42: "wait-ci"}


@pytest.mark.asyncio
@pytest.mark.parametrize("behind", [0, 1])
async def test_owed_act_observation_fetches_the_rollup_once_per_pr(
    mem_fs: dict[str, str], behind: int,
) -> None:
    """Proposal 137's stated cost bound: exactly one rollup fetch per open own
    PR per lap.

    Asserted for both shapes — ``behind == 0`` short-circuits at ``is_current``
    (which previously paid nothing, and now pays this one call), and
    ``behind == 1`` reaches ``_tend_single_pr`` (which previously fetched it
    itself, so it pays nothing extra). This retires the former
    flag-on-equals-flag-off premise, which only held while the observation was
    measuring blind.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=behind, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs, config=_refutable_config())
    engine._memory.save_evolution_plan(_plan(last_conflict_attempt=None))
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(number=42),
    }
    await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert engine._host.get_pr_check_conclusion.call_count == 1  # type: ignore[attr-defined]
    engine._host.get_pr_check_conclusion.assert_called_once_with(42)  # type: ignore[attr-defined]


def test_observe_owed_act_returns_empty_for_a_settled_pr(
    mem_fs: dict[str, str],
) -> None:
    """A PR pruned from the ledger this same lap reads ``settled`` → ``""``.

    ``classify_pr_owed_act`` short-circuits to ``None`` for a settled PR — owed
    nothing — and settled is exactly the case the merge/close pruner already
    releases, so the empty string keeps it out of the refutation map.
    """
    engine = _make_engine(mem_fs, config=_refutable_config())
    result = TendPRsResult(settled_pr_numbers=[42])
    staleness = PrStaleness(
        branch="maestro/evolve/ep1",
        commits_behind=3,
        cleanly_mergeable=False,
        needs_catchup=True,
        base_head_sha="base",
    )
    assert evolution_tend._observe_owed_act(
        engine,
        _pr_state(number=42, mergeable="CONFLICTING"), staleness, result, "",
    ) == ("", MERGE_BLOCK_CONFLICTING)


def test_observe_owed_act_threads_routed_comment_count(
    mem_fs: dict[str, str],
) -> None:
    """The unresolved-thread count is read back off the ``bug_comments_routed``
    entry the same iteration appended — the same ``_eligible_bug_comments``
    selection, with no second thread fetch. Only this PR's entry counts."""
    engine = _make_engine(mem_fs, config=_refutable_config())
    result = TendPRsResult(
        bug_comments_routed=[
            (42, "t", [(1, "a.py", "bug", "bug"), (2, "b.py", "nit", "nit")],
             "ep1", "body", "url", "maestro/evolve/ep1"),
            (43, "t", [(3, "c.py", "bug", "bug")],
             "ep2", "body", "url", "maestro/evolve/ep2"),
        ],
    )
    staleness = PrStaleness(
        branch="maestro/evolve/ep1",
        commits_behind=0,
        cleanly_mergeable=True,
        needs_catchup=False,
        base_head_sha="base",
    )
    with patch(
        "maestro.evolution_tend.classify_pr_owed_act",
        return_value=OwedAct.CODE_OWED,
    ) as classify:
        evolution_tend._observe_owed_act(engine, _pr_state(number=42), staleness, result, "")
    assert classify.call_args.kwargs["unresolved_threads"] == 2


def test_observe_owed_act_current_but_conflicting_reads_code_owed(
    mem_fs: dict[str, str],
) -> None:
    """A *current* branch the host reports conflicting still reads ``code-owed``.

    Regression: the empty check conclusion makes ``classify_merge_block`` return
    its ``CI_PENDING`` rung for every PR that is neither behind nor host-``DIRTY``
    — the absence of a CI reading, not a measured pending rollup. Left standing
    it sat outside the conflict refinement's ``(NONE, BEHIND_MAIN)`` set, so a
    ``commits_behind == 0`` PR whose branch no longer merges cleanly reported
    ``wait-ci`` and could never refute a handoff latch.
    """
    engine = _make_engine(mem_fs, config=_refutable_config())
    staleness = PrStaleness(
        branch="maestro/evolve/ep1",
        commits_behind=0,
        cleanly_mergeable=False,
        needs_catchup=False,
        base_head_sha="base",
    )
    assert evolution_tend._observe_owed_act(
        engine,
        _pr_state(number=42), staleness, TendPRsResult(), "",
    ) == ("code-owed", MERGE_BLOCK_CONFLICTING)


def test_observe_owed_act_unresolved_thread_reads_code_owed(
    mem_fs: dict[str, str],
) -> None:
    """A clean, current PR with an open routed thread reads ``code-owed``.

    Regression: an unresolved AI review thread is owned, code-clearable work, so
    it must refute a ``handoff-ready`` latch. ``classify_owned_blocker`` promotes
    only the thread-promotable blockers (``AWAITING_APPROVAL`` / ``NONE``), so
    while the composed blocker stayed the ``CI_PENDING`` sentinel the promotion
    was unreachable and the routed count was inert.
    """
    engine = _make_engine(mem_fs, config=_refutable_config())
    result = TendPRsResult(
        bug_comments_routed=[
            (42, "t", [(1, "a.py", "bug", "bug")],
             "ep1", "body", "url", "maestro/evolve/ep1"),
        ],
    )
    staleness = PrStaleness(
        branch="maestro/evolve/ep1",
        commits_behind=0,
        cleanly_mergeable=True,
        needs_catchup=False,
        base_head_sha="base",
    )
    assert evolution_tend._observe_owed_act(
        engine,
        _pr_state(number=42), staleness, result, "",
    ) == ("code-owed", MERGE_BLOCK_NONE)


def test_observe_owed_act_clean_current_pr_without_threads_holds(
    mem_fs: dict[str, str],
) -> None:
    """The conservative direction survives both fixes: with no owned blocker and
    no concluded CI in hand a clean, current PR still reads ``wait-ci``, which
    the validity oracle reads as *holds* — so collapsing the ``CI_PENDING``
    sentinel to ``NONE`` widened refutation only for owned blockers."""
    engine = _make_engine(mem_fs, config=_refutable_config())
    staleness = PrStaleness(
        branch="maestro/evolve/ep1",
        commits_behind=0,
        cleanly_mergeable=True,
        needs_catchup=False,
        base_head_sha="base",
    )
    assert evolution_tend._observe_owed_act(
        engine,
        _pr_state(number=42), staleness, TendPRsResult(), "",
    ) == ("wait-ci", MERGE_BLOCK_NONE)


def test_observe_owed_act_passes_the_measured_check_runs_to_the_oracle(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 137: the rollup conclusion reaches the oracle as a real
    ``CheckRun`` sequence, not the hardcoded empty tuple.

    Uses the same ``classify_pr_owed_act`` patch seam the routed-comment-count
    test uses, so the assertion is on the argument the chokepoint actually
    receives rather than only on the verdict it yields.
    """
    engine = _make_engine(mem_fs, config=_refutable_config())
    staleness = PrStaleness(
        branch="maestro/evolve/ep1",
        commits_behind=0,
        cleanly_mergeable=True,
        needs_catchup=False,
        base_head_sha="base",
    )
    with patch(
        "maestro.evolution_tend.classify_pr_owed_act",
        return_value=OwedAct.CODE_OWED,
    ) as classify:
        evolution_tend._observe_owed_act(
            engine,
            _pr_state(number=42), staleness, TendPRsResult(), "failure",
        )
    assert classify.call_args.kwargs["check_runs"] == (
        CheckRun(name="ci", conclusion="failure"),
    )


@pytest.mark.parametrize("conclusion", ["", "pending"])
def test_observe_owed_act_collapses_the_ci_pending_rung_unconditionally(
    mem_fs: dict[str, str], conclusion: str,
) -> None:
    """The ``CI_PENDING -> NONE`` collapse covers the *measured* pending rollup
    too, not just the unmeasurable ``""``.

    The CI axis is carried by the check-run sequence (both map to ``RUNNING``
    -> ``wait-ci``), so leaving the rung on the blocker axis would only serve to
    swallow the conflict/thread refinements below.
    """
    engine = _make_engine(mem_fs, config=_refutable_config())
    staleness = PrStaleness(
        branch="maestro/evolve/ep1",
        commits_behind=0,
        cleanly_mergeable=True,
        needs_catchup=False,
        base_head_sha="base",
    )
    assert evolution_tend._observe_owed_act(
        engine,
        _pr_state(number=42), staleness, TendPRsResult(), conclusion,
    ) == ("wait-ci", MERGE_BLOCK_NONE)


def test_observe_owed_act_conflict_survives_a_pending_rollup(
    mem_fs: dict[str, str],
) -> None:
    """Regression: a measured-conflicting PR with CI still in flight reads
    ``code-owed``/``CONFLICTING``, not ``wait-ci``/``CI_PENDING``.

    The pending rung is not thread-promotable and is outside the conflict
    refinement's set, so leaving it standing downgraded an owned, code-clearable
    conflict to *the clock is running*.
    """
    engine = _make_engine(mem_fs, config=_refutable_config())
    staleness = PrStaleness(
        branch="maestro/evolve/ep1",
        commits_behind=0,
        cleanly_mergeable=False,
        needs_catchup=False,
        base_head_sha="base",
    )
    assert evolution_tend._observe_owed_act(
        engine,
        _pr_state(number=42), staleness, TendPRsResult(), "pending",
    ) == ("code-owed", MERGE_BLOCK_CONFLICTING)


def test_observe_owed_act_thread_promotion_survives_a_pending_rollup(
    mem_fs: dict[str, str],
) -> None:
    """The other refinement the standing rung swallowed: an open review thread
    on a clean PR with CI in flight promotes to owned work."""
    engine = _make_engine(mem_fs, config=_refutable_config())
    staleness = PrStaleness(
        branch="maestro/evolve/ep1",
        commits_behind=0,
        cleanly_mergeable=True,
        needs_catchup=False,
        base_head_sha="base",
    )
    result = TendPRsResult()
    result.bug_comments_routed.append((42, "t", [{"body": "b"}]))
    assert evolution_tend._observe_owed_act(
        engine,
        _pr_state(number=42), staleness, result, "pending",
    )[0] == "code-owed"


@pytest.mark.parametrize(
    ("commits_behind", "conclusion", "merge_state_status", "mergeable",
     "cleanly_mergeable", "threads"),
    [
        (0, "pending", "BLOCKED", "MERGEABLE", True, 1),
        (0, "", "CLEAN", "MERGEABLE", True, 1),
        (0, "", "CLEAN", "MERGEABLE", True, 0),
        (3, "success", "UNKNOWN", "UNKNOWN", False, 0),
        (0, "success", "BLOCKED", "CONFLICTING", True, 0),
        (0, "failure", "CLEAN", "MERGEABLE", False, 0),
        (2, "success", "CLEAN", "MERGEABLE", True, 0),
        (0, "success", "BLOCKED", "MERGEABLE", True, 0),
    ],
)
def test_observe_owed_act_parity_with_pr_owed_act_cli(
    mem_fs: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    commits_behind: int,
    conclusion: str,
    merge_state_status: str,
    mergeable: str,
    cleanly_mergeable: bool,
    threads: int,
) -> None:
    """Regression (#4048): the tend walk and ``maestro pr-owed-act`` read the
    same owed act off identical measured facts."""
    import argparse

    from maestro.cli import cmd_pr_owed_act

    engine = _make_engine(mem_fs, config=_refutable_config())
    pr = _pr_state(
        number=42, merge_state_status=merge_state_status, mergeable=mergeable,
    )
    staleness = PrStaleness(
        branch=pr.head_ref,
        commits_behind=commits_behind,
        cleanly_mergeable=cleanly_mergeable,
        needs_catchup=commits_behind > 0 and cleanly_mergeable,
        base_head_sha="base",
    )
    result = TendPRsResult()
    thread_nodes = [
        {
            "id": f"PRRT_{i}",
            "isResolved": False,
            "isOutdated": False,
            "comments": {
                "nodes": [
                    {
                        "databaseId": i + 1,
                        "path": "maestro/cli.py",
                        "body": "🐛 **Bug**",
                        "author": {"login": "github-actions[bot]"},
                    },
                ],
            },
        }
        for i in range(threads)
    ]
    if threads:
        result.bug_comments_routed.append((42, "t", [{"body": "b"}] * threads))
    loop_act, _ = evolution_tend._observe_owed_act(engine, pr, staleness, result, conclusion)

    host = MagicMock()
    host.get_open_evolution_pr_states.return_value = {pr.head_ref: pr}
    host.get_pr_check_conclusion.return_value = conclusion
    host.get_pr_review_threads_strict.return_value = thread_nodes
    with patch("maestro.cli.SubprocessCodeHost", return_value=host), \
            patch("maestro.cli.SubprocessVCS"), \
            patch("maestro.pr_facts.observe_pr_staleness", return_value=staleness):
        cmd_pr_owed_act(argparse.Namespace(), engine._config)

    assert f"#42 {loop_act}:" in capsys.readouterr().out


def test_observe_owed_act_conflict_outranks_a_concluded_red(
    mem_fs: dict[str, str],
) -> None:
    """Regression: the blocker a measured-conflicting *red* PR reports is the
    conflict, not the CI red it is downstream of.

    The owed act is ``code-owed`` either way, but the withdrawal message a
    refuted release sends names this blocker, so it must name the real cause.
    """
    engine = _make_engine(mem_fs, config=_refutable_config())
    staleness = PrStaleness(
        branch="maestro/evolve/ep1",
        commits_behind=0,
        cleanly_mergeable=False,
        needs_catchup=False,
        base_head_sha="base",
    )
    assert evolution_tend._observe_owed_act(
        engine,
        _pr_state(number=42), staleness, TendPRsResult(), "failure",
    ) == ("code-owed", MERGE_BLOCK_CONFLICTING)
