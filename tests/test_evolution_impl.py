"""Tests for EvolutionEngine.implement() and create_pr() — VCS/CodeHost/query mocked."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import (
    _DIRTY_TREE_STATUS_CHARS,
    EvolutionEngine,
    QualityGateResult,
    _is_non_fast_forward_rejection,
)
from maestro.evolution_quality import (
    _MAX_GATE_BASELINES,
    _SELF_REVIEW_DIFF_MAX_CHARS,
    _is_lgtm,
    _truncate_diff,
)
from maestro.gate_observation import (
    GateFailure,
    GateObservation,
    GateRunObservation,
)
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import EvolutionPlan, Memory, TaskRecord
from maestro.notifications.base import NullNotifier
from maestro.sdk import QueryOptions
from maestro.tool_gating import task_gate_from_hooks
from maestro.vcs import (
    DirtyWorkingTreeError,
    MergeResult,
    PrMergeState,
    VCSError,
    evolution_branch,
)
from tests.conftest import MockQueryResult, arm_entry_clean_tree

FAKE_DIR = Path("/fake/evoimpl")


def _make(
    mem_fs: dict[str, str],
    vcs: MagicMock | None = None,
    host: MagicMock | None = None,
    config: Config | None = None,
) -> tuple[EvolutionEngine, Memory]:
    if (
        vcs is not None
        and vcs.porcelain_status.side_effect is None
        and not isinstance(vcs.porcelain_status.return_value, str)
    ):
        # A bare ``MagicMock`` reads as a dirty tree, which ``create_pr``'s
        # pre-push guard refuses (#4067); default it clean unless pinned.
        vcs.porcelain_status.return_value = ""
    m = Memory(FAKE_DIR)
    c = Consciousness(FAKE_DIR)
    j = Journal(FAKE_DIR)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    e = EvolutionEngine(
        m, c, j, n, issues=issues, github_repo="owner/repo",
        # ``self_review_max_rounds=0`` is the default every pre-existing caller
        # relies on; *config* overrides it wholesale for the tests that need a
        # different flag (the proposal-124 baseline seam wants the observation on).
        config=config or Config(self_review_max_rounds=0),
        vcs=vcs, code_host=host,
    )
    return e, m


def _approved_plan(**overrides: object) -> EvolutionPlan:
    defaults: dict[str, object] = {
        "plan_id": "abc123",
        "title": "Test evolution",
        "category": "capability",
        "rationale": "For testing",
        "proposed_changes": ["Change X"],
        "files_affected": ["maestro/agent.py"],
        "expected_impact": "Improvement",
        "priority": "capability",
        "status": "approved",
        "created_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(overrides)
    return EvolutionPlan(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_implement_success(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    e, _m = _make(mem_fs, vcs=vcs)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)

    # Two branch reads: ``ensure_main_branch`` positions HEAD, then
    # ``assert_on_main_for_mutation`` re-reads the fact it only attempted
    # (#3711). The tree is read once at entry (``porcelain_status``, before
    # any positioning could discard) and once by the commit tail.
    assert vcs.get_current_branch.call_count == 2
    vcs.porcelain_status.assert_called_once()
    vcs.checkout_new_branch.assert_called_once_with("maestro/evolve/abc123")
    vcs.has_changes.assert_called_once()
    vcs.stage_all.assert_called_once()
    vcs.commit.assert_called_once()
    assert "Test evolution" in vcs.commit.call_args[0][0]
    # implement is a tool-using query that runs unbounded; the deterministic
    # per-call ceiling is the SDK budget cap, not a turn count.
    options = mock_q.call_args.kwargs["options"]
    assert options.max_turns is None


async def _grab_implement_prompt(
    mem_fs: dict[str, str], *, require_claude_md_update: bool,
) -> str:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    e, _m = _make(mem_fs, vcs=vcs)
    e._config = Config(
        self_review_max_rounds=0,
        require_claude_md_update=require_claude_md_update,
    )
    plan = _approved_plan()
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)
    return str(mock_q.call_args.kwargs["prompt"])


@pytest.mark.asyncio
async def test_implement_prompt_documentation_is_flag_gated(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 108: the implement prompt's CLAUDE.md mandate is flag-selected.

    With the default (``require_claude_md_update`` False) the prompt carries
    the soft conditional guidance and NOT the "You MUST update CLAUDE.md"
    mandate; with the flag on the legacy hard mandate reappears.
    """
    off = await _grab_implement_prompt(mem_fs, require_claude_md_update=False)
    assert "MUST update CLAUDE.md" not in off
    assert "Core Modules table" not in off
    assert "only if an invariant changed" in off

    on = await _grab_implement_prompt(mem_fs, require_claude_md_update=True)
    assert "MUST update CLAUDE.md" in on
    assert "Core Modules table" in on


@pytest.mark.asyncio
async def test_implement_no_changes_raises(mem_fs: dict[str, str]) -> None:
    """If query produces no file changes, implement should raise RuntimeError."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs, later=False)
    e, _m = _make(mem_fs, vcs=vcs)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")

        with pytest.raises(RuntimeError, match="no file changes"):
            await e.implement(plan)

    # Cleanup should have been attempted
    vcs.checkout.assert_called()
    vcs.delete_branch.assert_called()


@pytest.mark.asyncio
async def test_implement_failure_cleans_up(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    arm_entry_clean_tree(vcs)
    vcs.get_current_branch.return_value = "main"
    e, _m = _make(mem_fs, vcs=vcs)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.side_effect = RuntimeError("query failed")

        with pytest.raises(RuntimeError, match="query failed"):
            await e.implement(plan)

    # Cleanup: checkout original branch + delete feature branch
    vcs.checkout.assert_called_with("main")
    vcs.delete_branch.assert_called_with("maestro/evolve/abc123")


@pytest.mark.asyncio
async def test_implement_cleanup_tolerates_failures(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    arm_entry_clean_tree(vcs)
    vcs.get_current_branch.return_value = "main"
    # First checkout call is the pre-branch positioning to ``main`` (defence-
    # in-depth alongside ``ensure_main_branch`` at entry); subsequent calls
    # are the cleanup path that must tolerate VCSError.
    vcs.checkout.side_effect = [None, VCSError("checkout failed")]
    vcs.delete_branch.side_effect = VCSError("delete failed")
    e, _m = _make(mem_fs, vcs=vcs)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await e.implement(plan)

    # Both cleanup attempts were made despite failures
    vcs.checkout.assert_called()
    vcs.delete_branch.assert_called()


@pytest.mark.asyncio
async def test_implement_refuses_dirty_main_before_cutting_a_branch(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """#3711: uncommitted work on ``main`` must not ride onto the evolution
    branch. ``implement()`` refuses before any branch is cut and before the
    SDK call, and discards nothing — the operator's edit is theirs. The
    refusal quotes the porcelain status so the operator knows what to clear."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.porcelain_status.return_value = " M maestro/agent.py\n?? stray.txt"
    e, _m = _make(mem_fs, vcs=vcs)
    plan = _approved_plan()

    with caplog.at_level(logging.ERROR, logger="maestro"), \
            patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        with pytest.raises(
            DirtyWorkingTreeError, match="uncommitted changes",
        ) as excinfo:
            await e.implement(plan)
        mock_q.assert_not_awaited()
    assert "?? stray.txt" in str(excinfo.value)
    # The refusal is re-raised as an environment fault, so the operator's only
    # legible signal is this ERROR naming what to clear (#3711).
    assert any(
        "?? stray.txt" in r.getMessage() for r in caplog.records
    )

    vcs.checkout_new_branch.assert_not_called()
    vcs.stage_all.assert_not_called()
    vcs.commit.assert_not_called()
    vcs.discard_changes.assert_not_called()


@pytest.mark.asyncio
async def test_implement_refuses_dirty_tree_before_positioning_discards_it(
    mem_fs: dict[str, str],
) -> None:
    """The tree probe must precede ``ensure_main_branch``.

    That helper's non-main arm calls ``discard_changes()`` before checking
    out main, so probing after it would destroy the operator's work and then
    see a clean tree — the refusal would never fire and the promise that
    nothing is discarded would be false.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "maestro/evolve/sibling"
    vcs.porcelain_status.return_value = " M maestro/agent.py"
    e, _m = _make(mem_fs, vcs=vcs)

    with pytest.raises(DirtyWorkingTreeError, match="uncommitted changes"):
        await e.implement(_approved_plan())

    vcs.discard_changes.assert_not_called()
    vcs.checkout.assert_not_called()
    vcs.checkout_new_branch.assert_not_called()


@pytest.mark.asyncio
async def test_implement_dirty_refusal_bounds_the_quoted_status(
    mem_fs: dict[str, str],
) -> None:
    """A tree with hundreds of stray paths must not unbound the log line."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.porcelain_status.return_value = "\n".join(
        f"?? artifacts/stray_{i}.txt" for i in range(500)
    )
    e, _m = _make(mem_fs, vcs=vcs)

    with pytest.raises(DirtyWorkingTreeError) as excinfo:
        await e.implement(_approved_plan())

    quoted = str(excinfo.value).split("commit:\n", 1)[1]
    assert len(quoted) == _DIRTY_TREE_STATUS_CHARS


@pytest.mark.asyncio
async def test_implement_refuses_when_head_is_not_main(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A HEAD that ``ensure_main_branch`` could not move off is refused too —
    cutting from a sibling branch is the isolation break the guard exists for."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "maestro/evolve/other"
    vcs.porcelain_status.return_value = ""
    e, _m = _make(mem_fs, vcs=vcs)

    with caplog.at_level(logging.ERROR, logger="maestro"), \
            pytest.raises(DirtyWorkingTreeError, match="HEAD must be on main"):
        await e.implement(_approved_plan())

    assert any(
        "maestro/evolve/other" in r.getMessage() for r in caplog.records
    )
    vcs.checkout_new_branch.assert_not_called()


@pytest.mark.asyncio
async def test_implement_refuses_when_tree_state_is_unreadable(
    mem_fs: dict[str, str],
) -> None:
    """An unreadable tree is indistinguishable from a dirty one: the probe's
    ``VCSError`` re-raises as the same refusal rather than being guessed past."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.porcelain_status.side_effect = VCSError("porcelain failed")
    e, _m = _make(mem_fs, vcs=vcs)

    with pytest.raises(DirtyWorkingTreeError, match="Could not read"):
        await e.implement(_approved_plan())

    vcs.checkout_new_branch.assert_not_called()


@pytest.mark.asyncio
async def test_implement_proceeds_on_a_clean_main(mem_fs: dict[str, str]) -> None:
    """The companion to the refusals: a clean ``main`` still implements."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    e, _m = _make(mem_fs, vcs=vcs)

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(_approved_plan())

    vcs.checkout_new_branch.assert_called_once_with("maestro/evolve/abc123")
    vcs.commit.assert_called_once()


@pytest.mark.asyncio
async def test_implement_positions_head_on_main_before_checkout_new_branch(
    mem_fs: dict[str, str],
) -> None:
    """Branch-bleed regression for #1867: ``Evolution.implement()`` must always
    position HEAD on ``main`` before ``checkout_new_branch``, even when
    ``base_ref`` is the literal ``"main"`` (no source_task_id, or fallback
    from a missing task branch). Previously the ``if base_ref != "main"``
    guard fell through with HEAD wherever the previous plan left it, so a
    sibling plan's evolution branch could become the new branch's parent."""
    vcs = MagicMock()
    # Simulate a sibling plan having left HEAD on its evolution branch.
    # ``ensure_main_branch`` should switch us to main; the ``else`` branch
    # at the base_ref dispatcher should re-checkout main as defence-in-depth.
    # The positioning read sees the sibling branch; the entry precondition's
    # re-read sees main, as it would after ``ensure_main_branch``'s checkout.
    vcs.get_current_branch.side_effect = ["maestro/evolve/sibling_plan", "main"]
    arm_entry_clean_tree(vcs)
    e, _m = _make(mem_fs, vcs=vcs)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)

    # HEAD must have been forced to main before the new branch was cut.
    main_checkouts = [
        call for call in vcs.checkout.call_args_list
        if call.args == ("main",)
    ]
    assert len(main_checkouts) >= 1
    # And it must have happened *before* checkout_new_branch.
    assert vcs.checkout_new_branch.call_count == 1
    # ``_orig_branch`` is unconditionally ``main`` — no fall-through to a
    # sibling branch is possible.
    assert e._orig_branch == "main"


@pytest.mark.asyncio
async def test_implement_source_task_fallback_repositions_on_main(
    mem_fs: dict[str, str],
) -> None:
    """Branch-bleed regression for #1867: when ``source_task_id`` is set but
    the task branch is missing locally, the fall-back to ``main`` must
    explicitly checkout main before ``checkout_new_branch`` instead of
    trusting ambient HEAD."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    # Task branch is gone — rev_parse returns None to simulate the deleted
    # branch path.
    vcs.rev_parse.return_value = None
    e, m = _make(mem_fs, vcs=vcs)
    _record_task_with_branch(m, task_id="t1", branch="maestro/task/t1")
    plan = _approved_plan(source_task_id="t1")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)

    # The branch never existed locally — fall-back path runs with
    # ``base_ref == "main"``. The defence-in-depth ``else: checkout("main")``
    # ensures HEAD is repositioned regardless.
    main_checkouts = [
        call for call in vcs.checkout.call_args_list
        if call.args == ("main",)
    ]
    assert main_checkouts, (
        "Expected at least one checkout('main') call before "
        "checkout_new_branch on the source_task fall-back path"
    )


@pytest.mark.asyncio
async def test_create_pr_refuses_dirty_tree(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An uncommitted edit at push time is refused loudly, never pushed past
    (#4067) — as a plain ``VCSError``, not the pre-branch
    ``DirtyWorkingTreeError`` environment refusal, so the plan-failure path runs."""
    vcs = MagicMock()
    vcs.porcelain_status.return_value = " M maestro/agent.py"
    host = MagicMock()
    e, m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan(status="approved")
    m.save_evolution_plan(plan)

    with caplog.at_level("ERROR", logger="maestro"), \
            pytest.raises(VCSError, match="uncommitted changes") as excinfo:
        await e.create_pr(plan)

    assert not isinstance(excinfo.value, DirtyWorkingTreeError)
    vcs.push.assert_not_called()
    host.create_pr.assert_not_called()
    assert any("maestro/agent.py" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_create_pr_cleanup_failure_logs_warning_and_returns_url(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Branch-bleed regression for #1867: the cleanup checkout at the end of
    ``create_pr`` previously suppressed VCSError silently, hiding the bug
    that produced sibling-branch contamination. The replacement logs a
    warning with the current-branch stamp and still returns the PR URL."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "maestro/evolve/abc123"
    vcs.checkout.side_effect = VCSError("checkout to main failed")
    host = MagicMock()
    host.create_pr.return_value = "https://example/pr/1"
    host.get_default_branch.return_value = "main"

    e, m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan(status="approved")
    m.save_evolution_plan(plan)
    e._orig_branch = "main"

    with caplog.at_level("WARNING", logger="maestro"):
        pr_url = await e.create_pr(plan)

    # PR URL must still be returned — cleanup failure does not roll back.
    assert pr_url == "https://example/pr/1"
    # The new shape stamps the failure with the cleanup target and the
    # current-branch read so telemetry can surface a future incident.
    assert any(
        "create_pr cleanup checkout to main failed" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_create_pr_cleanup_failure_with_unreadable_head_logs_unknown(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the cleanup checkout fails AND the follow-up
    ``get_current_branch`` read also fails (VCS state may be wedged after
    the failed checkout), the warning still fires with ``<unknown>`` as
    the HEAD stamp instead of crashing."""
    vcs = MagicMock()
    # ``get_current_branch`` is called inside the cleanup-failure branch to
    # stamp the current HEAD into the warning. Make it raise so the inner
    # fallback (``current = "<unknown>"``) runs.
    vcs.get_current_branch.side_effect = VCSError("HEAD unreadable")
    vcs.checkout.side_effect = VCSError("checkout to main failed")
    host = MagicMock()
    host.create_pr.return_value = "https://example/pr/2"
    host.get_default_branch.return_value = "main"

    e, m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan(status="approved")
    m.save_evolution_plan(plan)
    e._orig_branch = "main"

    with caplog.at_level("WARNING", logger="maestro"):
        pr_url = await e.create_pr(plan)

    assert pr_url == "https://example/pr/2"
    assert any(
        "create_pr cleanup checkout to main failed" in rec.message
        and "<unknown>" in rec.message
        for rec in caplog.records
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # Each marker pinned in a representative git push stderr,
        # case-insensitively — so a future edit that drops one is caught here
        # directly, not only transitively through ``create_pr``.
        ("! [rejected]        main -> main (non-fast-forward)", True),
        ("Updates were rejected because the remote contains work; fetch first", True),
        ("error: failed to push some refs; [REJECTED] (fetch first)", True),
        ("NON-FAST-FORWARD", True),  # upper-case marker still matches
        # Unrelated push failures must never be misclassified as an orphan.
        ("fatal: Authentication failed for 'https://example/repo'", False),
        ("fatal: unable to access 'origin': Could not resolve host", False),
        ("", False),
    ],
)
def test_is_non_fast_forward_rejection(message: str, expected: bool) -> None:
    """#2813: the push-rejection classifier recognises every stale-orphan
    marker (case-insensitively) and rejects auth/network failures, so the
    reconcile in ``create_pr`` only fires on a genuine non-fast-forward."""
    assert _is_non_fast_forward_rejection(message) is expected


@pytest.mark.asyncio
async def test_create_pr_reconciles_stale_orphan_evolve_branch(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#2813: a non-fast-forward push against a PR-less ``maestro/evolve/*``
    orphan on origin is reconciled — the orphan is deleted and the fresh
    branch is pushed once — instead of crashing the implementation into a
    hard ``failed`` demotion."""
    branch = "maestro/evolve/abc123"
    vcs = MagicMock()
    vcs.get_current_branch.return_value = branch
    # First push rejected non-fast-forward, second (post-delete) succeeds.
    vcs.push.side_effect = [VCSError("! [rejected] (non-fast-forward)"), None]
    vcs.list_remote_branches.return_value = [branch, "main"]
    host = MagicMock()
    host.create_pr.return_value = "https://example/pr/3"
    host.get_default_branch.return_value = "main"
    # No open PR references the orphan branch.
    host.get_open_evolution_pr_states.return_value = {}

    e, m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan(status="approved")
    m.save_evolution_plan(plan)
    e._orig_branch = "main"

    with caplog.at_level("WARNING", logger="maestro"):
        pr_url = await e.create_pr(plan)

    assert pr_url == "https://example/pr/3"
    # The PR-less check consulted open PRs under the evolution prefix, in
    # strict mode so an errored host listing can never read as 'no PR' (#2813).
    host.get_open_evolution_pr_states.assert_called_once_with(
        "maestro/evolve/", strict=True,
    )
    # Origin was refreshed before the presence check reads remote refs, so the
    # check reflects live state rather than a stale last-fetch cache (#2813 r2).
    vcs.fetch.assert_called_once()
    # The orphan was deleted, scoped to the colliding evolve branch.
    vcs.delete_remote_branch.assert_called_once_with(branch)
    # Push attempted twice: initial (rejected) then post-delete retry, both
    # targeting the same branch with upstream tracking.
    assert vcs.push.call_count == 2
    for call in vcs.push.call_args_list:
        assert call.args[0] == branch
        assert call.kwargs["set_upstream"] is True
    assert any(
        "deleting PR-less orphan" in rec.message for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_create_pr_non_fast_forward_with_open_pr_does_not_delete(
    mem_fs: dict[str, str],
) -> None:
    """#2813 safety: when an open PR references the colliding
    ``maestro/evolve/*`` branch, the non-fast-forward push is NOT reconciled
    — the branch is never deleted and the original ``VCSError`` surfaces, so
    the loud failure path is unchanged for a live PR branch."""
    branch = "maestro/evolve/abc123"
    vcs = MagicMock()
    vcs.get_current_branch.return_value = branch
    vcs.push.side_effect = VCSError("! [rejected] (non-fast-forward)")
    vcs.list_remote_branches.return_value = [branch, "main"]
    host = MagicMock()
    host.get_default_branch.return_value = "main"
    # An open PR references the branch → it is a live PR branch, never touched.
    host.get_open_evolution_pr_states.return_value = {
        branch: PrMergeState(
            number=42, head_ref=branch, title="t", body="b", state="OPEN",
            mergeable="MERGEABLE", merge_state_status="CLEAN",
            review_decision="", created_at="", updated_at="", url="",
        ),
    }

    e, m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan(status="approved")
    m.save_evolution_plan(plan)
    e._orig_branch = "main"

    with pytest.raises(VCSError, match="non-fast-forward"):
        await e.create_pr(plan)

    # A branch carrying a live PR is never deleted.
    vcs.delete_remote_branch.assert_not_called()
    # Only the initial push was attempted; no retry after the re-raise.
    assert vcs.push.call_count == 1


@pytest.mark.asyncio
async def test_create_pr_push_error_that_is_not_non_fast_forward_reraises(
    mem_fs: dict[str, str],
) -> None:
    """#2813 scope: a push failure that is NOT a non-fast-forward rejection
    (e.g. auth or network) is never treated as a stale orphan — no host PR
    lookup, no delete — and the ``VCSError`` re-raises unchanged."""
    branch = "maestro/evolve/abc123"
    vcs = MagicMock()
    vcs.get_current_branch.return_value = branch
    vcs.push.side_effect = VCSError("fatal: Authentication failed")
    host = MagicMock()
    host.get_default_branch.return_value = "main"

    e, m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan(status="approved")
    m.save_evolution_plan(plan)
    e._orig_branch = "main"

    with pytest.raises(VCSError, match="Authentication failed"):
        await e.create_pr(plan)

    host.get_open_evolution_pr_states.assert_not_called()
    vcs.delete_remote_branch.assert_not_called()
    assert vcs.push.call_count == 1


@pytest.mark.asyncio
async def test_create_pr_non_fast_forward_but_branch_absent_reraises(
    mem_fs: dict[str, str],
) -> None:
    """#2813 defensive: a non-fast-forward rejection with no open PR but where
    the branch is not present on origin (nothing to reconcile) deletes nothing
    and re-raises the ``VCSError``."""
    branch = "maestro/evolve/abc123"
    vcs = MagicMock()
    vcs.get_current_branch.return_value = branch
    vcs.push.side_effect = VCSError("! [rejected] (non-fast-forward)")
    vcs.list_remote_branches.return_value = ["main"]  # orphan absent
    host = MagicMock()
    host.get_default_branch.return_value = "main"
    host.get_open_evolution_pr_states.return_value = {}

    e, m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan(status="approved")
    m.save_evolution_plan(plan)
    e._orig_branch = "main"

    with pytest.raises(VCSError, match="non-fast-forward"):
        await e.create_pr(plan)

    vcs.delete_remote_branch.assert_not_called()
    assert vcs.push.call_count == 1


@pytest.mark.asyncio
async def test_create_pr_reconcile_fetch_failure_reraises_original(
    mem_fs: dict[str, str],
) -> None:
    """#2813 r2: when the pre-check origin fetch fails the reconcile cannot
    trust a stale remote-ref cache, so it reads no presence, deletes nothing,
    and surfaces the original non-fast-forward rejection — never the fetch
    error — rather than deleting blind."""
    branch = "maestro/evolve/abc123"
    vcs = MagicMock()
    vcs.get_current_branch.return_value = branch
    vcs.push.side_effect = VCSError("! [rejected] (non-fast-forward)")
    vcs.fetch.side_effect = VCSError("fatal: unable to access 'origin'")
    host = MagicMock()
    host.get_default_branch.return_value = "main"
    host.get_open_evolution_pr_states.return_value = {}

    e, m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan(status="approved")
    m.save_evolution_plan(plan)
    e._orig_branch = "main"

    with pytest.raises(VCSError, match="non-fast-forward"):
        await e.create_pr(plan)

    # Refresh failed → no presence read, no delete, no retry push.
    vcs.list_remote_branches.assert_not_called()
    vcs.delete_remote_branch.assert_not_called()
    assert vcs.push.call_count == 1


@pytest.mark.asyncio
async def test_create_pr_reconcile_host_pr_state_failure_reraises_original(
    mem_fs: dict[str, str],
) -> None:
    """#2813 fail-open guard: when the strict host PR-state listing fails
    (transient ``gh`` auth/rate-limit/network error), the branch's PR state
    is UNCONFIRMED, so the reconcile must not treat absence as 'no PR' and
    delete blind — it surfaces the original non-fast-forward rejection, never
    reaching fetch, presence read, or delete. A best-effort ``{}`` would have
    fooled the guard into orphaning a branch that may carry a live PR."""
    branch = "maestro/evolve/abc123"
    vcs = MagicMock()
    vcs.get_current_branch.return_value = branch
    vcs.push.side_effect = VCSError("! [rejected] (non-fast-forward)")
    host = MagicMock()
    host.get_default_branch.return_value = "main"
    # Strict mode raises rather than swallowing the host failure into ``{}``.
    host.get_open_evolution_pr_states.side_effect = VCSError(
        "gh: API rate limit exceeded",
    )

    e, m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan(status="approved")
    m.save_evolution_plan(plan)
    e._orig_branch = "main"

    with pytest.raises(VCSError, match="non-fast-forward"):
        await e.create_pr(plan)

    # PR state unconfirmed → no fetch, no presence read, no delete, no retry.
    host.get_open_evolution_pr_states.assert_called_once_with(
        "maestro/evolve/", strict=True,
    )
    vcs.fetch.assert_not_called()
    vcs.list_remote_branches.assert_not_called()
    vcs.delete_remote_branch.assert_not_called()
    assert vcs.push.call_count == 1


@pytest.mark.asyncio
async def test_implement_recovers_when_already_on_target_branch(mem_fs: dict[str, str]) -> None:
    """If we're already on the evolution branch (stale from prior failure), recovery works."""
    vcs = MagicMock()
    # ``ensure_main_branch`` reads the stale branch and checks out main; the
    # entry precondition's re-read then sees main, as a real tree would.
    vcs.get_current_branch.side_effect = ["maestro/evolve/abc123", "main"]
    arm_entry_clean_tree(vcs)
    # First checkout_new_branch fails (branch exists), second succeeds
    vcs.checkout_new_branch.side_effect = [VCSError("branch exists"), None]
    e, _m = _make(mem_fs, vcs=vcs)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)

    # _orig_branch should have been corrected to "main"
    assert e._orig_branch == "main"
    # Recovery sequence: checkout orig, delete stale, checkout_new_branch again
    assert vcs.checkout_new_branch.call_count == 2


def _record_task_with_branch(mem: Memory, *, task_id: str, branch: str | None,
                              outcome: str = "success") -> None:
    mem.record_task(TaskRecord(
        task_id=task_id, goal="g", plan=[], outcome=outcome,  # type: ignore[arg-type]
        result_summary="r", errors=[], duration_secs=1.0,
        timestamp="2026-01-01", branch=branch,
    ))


@pytest.mark.asyncio
async def test_implement_uses_task_branch_when_source_task_id_set(
    mem_fs: dict[str, str],
) -> None:
    """When source_task_id resolves, implement bases the evolution branch on
    the task branch after merging origin/main into it."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    vcs.rev_parse.return_value = "task_branch_sha"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    e, m = _make(mem_fs, vcs=vcs)
    _record_task_with_branch(m, task_id="t1", branch="maestro/task/t1")
    plan = _approved_plan(source_task_id="t1")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)

    vcs.rev_parse.assert_called_with("refs/heads/maestro/task/t1")
    vcs.fetch.assert_called_with("origin")
    vcs.merge_from.assert_called_once()
    # Order: checkout(task_branch) before checkout_new_branch(evolution_branch)
    checkout_calls = [c.args[0] for c in vcs.checkout.call_args_list]
    assert "maestro/task/t1" in checkout_calls
    vcs.checkout_new_branch.assert_called_once_with("maestro/evolve/abc123")


@pytest.mark.asyncio
async def test_implement_bases_fresh_from_main_when_reuse_disabled(
    mem_fs: dict[str, str],
) -> None:
    """With task_branch_reuse_enabled False, a plan carrying a valid
    source_task_id is still based fresh-from-main: the source_task_id block is
    skipped entirely — no origin/main merge, no task-branch checkout."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    vcs.rev_parse.return_value = "task_branch_sha"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    e, m = _make(mem_fs, vcs=vcs)
    e._config = Config(
        task_branch_reuse_enabled=False, self_review_max_rounds=0,
    )
    _record_task_with_branch(m, task_id="t1", branch="maestro/task/t1")
    plan = _approved_plan(source_task_id="t1")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)

    vcs.merge_from.assert_not_called()
    checkout_calls = [c.args[0] for c in vcs.checkout.call_args_list]
    assert "maestro/task/t1" not in checkout_calls
    vcs.checkout_new_branch.assert_called_once_with("maestro/evolve/abc123")


@pytest.mark.asyncio
async def test_implement_falls_back_when_task_branch_missing(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the task branch is no longer present, implement demotes to
    fresh-from-main and logs the fallback."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    vcs.rev_parse.return_value = None  # branch missing
    e, m = _make(mem_fs, vcs=vcs)
    _record_task_with_branch(m, task_id="t1", branch="maestro/task/t1")
    plan = _approved_plan(source_task_id="t1")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
            caplog.at_level("WARNING", logger="maestro"):
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)

    # No merge attempted — branch was missing.
    vcs.merge_from.assert_not_called()
    # Evolution branch still created from current HEAD (main).
    vcs.checkout_new_branch.assert_called_once_with("maestro/evolve/abc123")
    assert any("task branch missing" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_implement_falls_back_on_merge_conflict_from_main(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A merge conflict aborts the merge and falls back to fresh-from-main."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    vcs.rev_parse.return_value = "task_branch_sha"
    vcs.merge_from.return_value = MergeResult(
        success=False, conflicted_files=["maestro/foo.py"],
    )
    e, m = _make(mem_fs, vcs=vcs)
    _record_task_with_branch(m, task_id="t1", branch="maestro/task/t1")
    plan = _approved_plan(source_task_id="t1")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
            caplog.at_level("WARNING", logger="maestro"):
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)

    vcs.abort_merge.assert_called_once()
    vcs.checkout_new_branch.assert_called_once_with("maestro/evolve/abc123")
    assert any(
        "merge conflict with origin/main" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_implement_no_refinement_changes_does_not_double_commit(
    mem_fs: dict[str, str],
) -> None:
    """On the source-task path, an empty refinement is NOT an error: we
    accept the existing task commits as the entire change."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs, later=False)  # LLM produced no new diff
    vcs.rev_parse.return_value = "task_branch_sha"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    e, m = _make(mem_fs, vcs=vcs)
    _record_task_with_branch(m, task_id="t1", branch="maestro/task/t1")
    plan = _approved_plan(source_task_id="t1")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="no refinement needed")
        await e.implement(plan)  # must NOT raise

    vcs.commit.assert_not_called()


@pytest.mark.asyncio
async def test_implement_retry_reresolves_task_branch(
    mem_fs: dict[str, str],
) -> None:
    """Retry path re-resolves rev_parse so a task branch that vanished
    between attempts cleanly demotes."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    vcs.rev_parse.return_value = "task_branch_sha"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    e, m = _make(mem_fs, vcs=vcs)
    _record_task_with_branch(m, task_id="t1", branch="maestro/task/t1")
    plan = _approved_plan(source_task_id="t1", failure_count=1, status="failed")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)
        # Re-arm for the second attempt: its entry precondition reads a clean
        # tree again, exactly as a real one would after the first attempt's
        # branch was cleaned up.
        arm_entry_clean_tree(vcs)
        await e.implement(plan)

    rev_parse_calls = [c.args[0] for c in vcs.rev_parse.call_args_list]
    assert rev_parse_calls.count("refs/heads/maestro/task/t1") == 2


@pytest.mark.asyncio
async def test_implement_falls_back_when_vcs_error_during_rebase(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A VCSError raised while preparing the task branch (e.g. checkout
    failure) aborts any in-flight merge and falls back to fresh-from-main."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    vcs.rev_parse.return_value = "task_branch_sha"
    # First checkout (to task branch) fails. Subsequent checkouts (orig
    # branch during fallback, then defence-in-depth ``checkout("main")``
    # before ``checkout_new_branch``) must succeed so the rest of implement
    # runs.
    vcs.checkout.side_effect = [
        VCSError("transient checkout failure"),
        None,  # checkout(_orig_branch) recovery
        None,  # checkout("main") defence-in-depth before checkout_new_branch
    ]
    e, m = _make(mem_fs, vcs=vcs)
    _record_task_with_branch(m, task_id="t1", branch="maestro/task/t1")
    plan = _approved_plan(source_task_id="t1")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
            caplog.at_level("WARNING", logger="maestro"):
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)

    vcs.abort_merge.assert_called_once()
    vcs.checkout_new_branch.assert_called_once_with("maestro/evolve/abc123")
    assert any(
        "vcs error during task branch rebase" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_implement_stale_evolution_branch_retry_on_source_task_path(
    mem_fs: dict[str, str],
) -> None:
    """When the evolution branch already exists from a prior failed attempt
    AND a source_task_id is set, the retry path repositions HEAD on the task
    branch before recreating the evolution branch."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    vcs.rev_parse.return_value = "task_branch_sha"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    # First checkout_new_branch fails (stale), retry succeeds.
    vcs.checkout_new_branch.side_effect = [VCSError("branch exists"), None]
    e, m = _make(mem_fs, vcs=vcs)
    _record_task_with_branch(m, task_id="t1", branch="maestro/task/t1")
    plan = _approved_plan(source_task_id="t1")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)

    # The retry path must reposition HEAD on the task branch before
    # recreating the evolution branch — otherwise we'd lose the rebase.
    checkout_calls = [c.args[0] for c in vcs.checkout.call_args_list]
    assert checkout_calls.count("maestro/task/t1") >= 2
    assert vcs.checkout_new_branch.call_count == 2


@pytest.mark.asyncio
async def test_implement_appends_task_lineage_to_pr_body(
    mem_fs: dict[str, str],
) -> None:
    """create_pr surfaces the source task id in the PR body when set."""
    vcs = MagicMock()
    host = MagicMock()
    host.create_pr.return_value = "https://github.com/owner/repo/pulls/123"
    e, m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan(source_task_id="t1")
    m.save_evolution_plan(plan)

    await e.create_pr(plan)

    body = host.create_pr.call_args[0][1]
    assert "Builds on task t1" in body
    assert "branch reused from prior self-directed work" in body


@pytest.mark.asyncio
async def test_create_pr_omits_lineage_when_no_source_task_id(
    mem_fs: dict[str, str],
) -> None:
    """Without source_task_id, the lineage line stays out of the PR body."""
    vcs = MagicMock()
    host = MagicMock()
    host.create_pr.return_value = "https://github.com/owner/repo/pulls/124"
    e, m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan()
    m.save_evolution_plan(plan)

    await e.create_pr(plan)

    body = host.create_pr.call_args[0][1]
    assert "Builds on task" not in body


@pytest.mark.asyncio
async def test_create_pr_omits_lineage_when_reuse_disabled(
    mem_fs: dict[str, str],
) -> None:
    """With task_branch_reuse_enabled False, the PR body omits the reuse
    lineage line even when source_task_id is set — the branch was not reused."""
    vcs = MagicMock()
    host = MagicMock()
    host.create_pr.return_value = "https://github.com/owner/repo/pulls/125"
    e, m = _make(mem_fs, vcs=vcs, host=host)
    e._config = Config(
        task_branch_reuse_enabled=False, self_review_max_rounds=0,
    )
    plan = _approved_plan(source_task_id="t1")
    m.save_evolution_plan(plan)

    await e.create_pr(plan)

    body = host.create_pr.call_args[0][1]
    assert "Builds on task" not in body


@pytest.mark.asyncio
async def test_create_pr_success(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    host = MagicMock()
    host.create_pr.return_value = "https://github.com/owner/repo/pulls/99"
    e, m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan()
    m.save_evolution_plan(plan)

    pr_url = await e.create_pr(plan)

    assert pr_url == "https://github.com/owner/repo/pulls/99"
    vcs.push.assert_called_once_with("maestro/evolve/abc123", set_upstream=True)
    host.create_pr.assert_called_once()
    # Verify title
    title = host.create_pr.call_args[0][0]
    assert title == "[Maestro] Test evolution"
    # Plan should be updated to implemented
    saved = m.get_plan(plan.plan_id)
    assert saved is not None
    assert saved.status == "implemented"
    assert saved.pr_url == pr_url


@pytest.mark.asyncio
async def test_create_pr_clears_stale_rejection_reason(mem_fs: dict[str, str]) -> None:
    """create_pr must clear rejection_reason from a previous failed attempt."""
    vcs = MagicMock()
    host = MagicMock()
    host.create_pr.return_value = "https://github.com/owner/repo/pulls/101"
    e, m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan(rejection_reason="Implementation failed: SDK exploded")
    m.save_evolution_plan(plan)

    await e.create_pr(plan)

    saved = m.get_plan(plan.plan_id)
    assert saved is not None
    assert saved.status == "implemented"
    assert saved.rejection_reason is None


@pytest.mark.asyncio
async def test_create_pr_with_issue_number(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    host = MagicMock()
    host.create_pr.return_value = "https://github.com/owner/repo/pulls/100"
    e, m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan(issue_number=42)
    m.save_evolution_plan(plan)

    await e.create_pr(plan)

    # Verify the PR body includes "Closes #42"
    body = host.create_pr.call_args[0][1]
    assert "Closes #42" in body


@pytest.mark.asyncio
async def test_create_pr_push_failure(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.push.side_effect = VCSError("push failed")
    e, _m = _make(mem_fs, vcs=vcs)
    plan = _approved_plan()

    with pytest.raises(VCSError):
        await e.create_pr(plan)


@pytest.mark.asyncio
async def test_create_pr_gh_failure(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    host = MagicMock()
    host.create_pr.side_effect = VCSError("gh pr create failed")
    e, _m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan()

    with pytest.raises(VCSError):
        await e.create_pr(plan)

    # Push should have succeeded before gh failed
    vcs.push.assert_called_once()


@pytest.mark.asyncio
async def test_create_pr_no_draft_flag(mem_fs: dict[str, str]) -> None:
    """create_pr should not pass a draft parameter to the code host."""
    vcs = MagicMock()
    host = MagicMock()
    host.create_pr.return_value = "https://github.com/owner/repo/pull/99"
    e, _m = _make(mem_fs, vcs=vcs, host=host)
    plan = _approved_plan()

    await e.create_pr(plan)

    assert "draft" not in host.create_pr.call_args[1]


# --- ensure_main_branch() tests ---


def test_ensure_main_branch_already_on_main(mem_fs: dict[str, str]) -> None:
    """No checkout when already on main."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    e, _m = _make(mem_fs, vcs=vcs)

    e.ensure_main_branch()

    vcs.get_current_branch.assert_called_once()
    vcs.checkout.assert_not_called()


def test_ensure_main_branch_switches_from_evolution_branch(
    mem_fs: dict[str, str],
) -> None:
    """Switches to main when on a stale evolution branch."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "maestro/evolve/abc123"
    e, _m = _make(mem_fs, vcs=vcs)

    e.ensure_main_branch()

    vcs.checkout.assert_called_once_with("main")


def test_ensure_main_branch_switches_from_any_non_main(
    mem_fs: dict[str, str],
) -> None:
    """Switches to main from any non-main branch."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "feature/something"
    e, _m = _make(mem_fs, vcs=vcs)

    e.ensure_main_branch()

    vcs.checkout.assert_called_once_with("main")


def test_ensure_main_branch_tolerates_get_branch_failure(
    mem_fs: dict[str, str],
) -> None:
    """Does not raise when get_current_branch fails."""
    vcs = MagicMock()
    vcs.get_current_branch.side_effect = VCSError("detached HEAD")
    e, _m = _make(mem_fs, vcs=vcs)

    e.ensure_main_branch()  # should not raise

    vcs.checkout.assert_not_called()


def test_ensure_main_branch_tolerates_checkout_failure(
    mem_fs: dict[str, str],
) -> None:
    """Does not raise when checkout fails."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "maestro/evolve/old"
    vcs.checkout.side_effect = VCSError("checkout failed")
    e, _m = _make(mem_fs, vcs=vcs)

    e.ensure_main_branch()  # should not raise

    vcs.checkout.assert_called_once_with("main")


# ------------------------------------------------------------------
# Merged from test_bug_hunt11.py — implement_plan reviewed_at preservation
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_implement_plan_preserves_reviewed_at_on_failure(
    mem_fs: dict[str, str],
) -> None:
    """When implementation fails and plan is demoted to 'failed',
    reviewed_at must not be overwritten with current time."""
    from maestro.agent import MaestroAgent
    from maestro.contributions import ContributionTracker
    from maestro.executor import Executor
    from maestro.planner import Planner

    d = Path("/fake/evoimpl_reviewedat")
    config = MagicMock()
    config.memory_dir = d
    config.workspace = d
    config.pid_path = d / "maestro.pid"
    config.enable_session_reuse = False
    config.sqlite_state_enabled = False
    config.max_implementation_attempts = 3
    config.auto_fix_max_rounds = 3

    m = Memory(d)
    c = Consciousness(d)
    j = Journal(d)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    executor = Executor()
    planner = Planner()
    evolution = EvolutionEngine(
        m, c, j, n, issues=issues, config=config,
    )
    contributions = ContributionTracker(d, d)

    vcs = MagicMock()
    vcs.has_changes.return_value = False
    agent = MaestroAgent(
        config=config,
        consciousness=c,
        journal=j,
        memory=m,
        executor=executor,
        planner=planner,
        evolution=evolution,
        notifier=n,
        contributions=contributions,
        vcs=vcs,
    )

    original_reviewed_at = "2024-06-15T10:30:00+00:00"
    plan = _approved_plan(
        plan_id="plan-reviewedat",
        reviewed_at=original_reviewed_at,
    )
    m.save_evolution_plan(plan)

    with patch.object(evolution, "implement", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
         patch.object(evolution, "cleanup_branch"), \
         pytest.raises(RuntimeError, match="boom"):
        await agent.implement_plan("plan-reviewedat")

    updated = m.get_plan("plan-reviewedat")
    assert updated is not None
    assert updated.status == "failed"
    assert updated.reviewed_at == original_reviewed_at


# --- discard_changes in cleanup paths ---


@pytest.mark.asyncio
async def test_implement_failure_calls_discard_changes(mem_fs: dict[str, str]) -> None:
    """discard_changes() is called before checkout when implement() fails."""
    vcs = MagicMock()
    arm_entry_clean_tree(vcs)
    vcs.get_current_branch.return_value = "main"
    e, _m = _make(mem_fs, vcs=vcs)
    plan = _approved_plan()

    call_order: list[str] = []
    vcs.discard_changes.side_effect = lambda: call_order.append("discard")
    orig_checkout = vcs.checkout.side_effect

    def track_checkout(branch: str) -> None:
        call_order.append(f"checkout:{branch}")
        if orig_checkout:
            orig_checkout(branch)

    vcs.checkout.side_effect = track_checkout

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.side_effect = RuntimeError("query failed")
        with pytest.raises(RuntimeError, match="query failed"):
            await e.implement(plan)

    assert "discard" in call_order
    # ``discard`` must precede the *cleanup* checkout. The pre-branch
    # positioning checkout to ``main`` (line 1042 — defence-in-depth alongside
    # ``ensure_main_branch`` at entry) fires before query and is unrelated to
    # the cleanup contract.
    discard_idx = call_order.index("discard")
    checkout_idx = next(
        i for i, v in enumerate(call_order)
        if v.startswith("checkout:") and i > discard_idx
    )
    assert discard_idx < checkout_idx


def test_cleanup_branch_calls_discard_changes(mem_fs: dict[str, str]) -> None:
    """cleanup_branch() calls discard_changes() before checkout."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    e, _m = _make(mem_fs, vcs=vcs)

    e.cleanup_branch("abc123")

    vcs.discard_changes.assert_called_once()


def test_release_branch_checks_out_orig_without_deleting(
    mem_fs: dict[str, str],
) -> None:
    """release_branch() discards, checks out the original branch, never deletes."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "feature"
    e, _m = _make(mem_fs, vcs=vcs)
    e._orig_branch = "feature"

    e.release_branch("abc123")

    vcs.discard_changes.assert_called_once()
    vcs.checkout.assert_called_once_with("feature")
    vcs.delete_branch.assert_not_called()


def test_release_branch_falls_back_to_main_when_orig_is_evolve_branch(
    mem_fs: dict[str, str],
) -> None:
    """When the recorded original branch is the evolve branch itself, release
    to main instead."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    e, _m = _make(mem_fs, vcs=vcs)
    e._orig_branch = "maestro/evolve/abc123"

    e.release_branch("abc123")

    vcs.checkout.assert_called_once_with("main")
    vcs.delete_branch.assert_not_called()


def test_release_branch_logs_error_when_head_stays_on_evolve_branch(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed checkout that strands HEAD on the evolve branch is loud."""
    vcs = MagicMock()
    vcs.checkout.side_effect = VCSError("checkout failed")
    vcs.get_current_branch.return_value = "maestro/evolve/abc123"
    e, _m = _make(mem_fs, vcs=vcs)
    e._orig_branch = "main"

    with caplog.at_level(logging.ERROR, logger="maestro.evolution"):
        e.release_branch("abc123")

    assert "failed to release HEAD" in caplog.text


def test_cleanup_branch_releases_then_deletes(mem_fs: dict[str, str]) -> None:
    """cleanup_branch() releases HEAD before deleting the evolve branch."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    e, _m = _make(mem_fs, vcs=vcs)
    e._orig_branch = "main"

    e.cleanup_branch("abc123")

    vcs.checkout.assert_called_once_with("main")
    vcs.delete_branch.assert_called_once_with("maestro/evolve/abc123")


def test_ensure_main_branch_calls_discard_changes(mem_fs: dict[str, str]) -> None:
    """ensure_main_branch() calls discard_changes() when not on main."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "maestro/evolve/old"
    e, _m = _make(mem_fs, vcs=vcs)

    e.ensure_main_branch()

    vcs.discard_changes.assert_called_once()
    vcs.checkout.assert_called_once_with("main")


def test_ensure_main_branch_skips_discard_when_on_main(
    mem_fs: dict[str, str],
) -> None:
    """ensure_main_branch() does NOT call discard_changes() when already on main."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    e, _m = _make(mem_fs, vcs=vcs)

    e.ensure_main_branch()

    vcs.discard_changes.assert_not_called()
    vcs.checkout.assert_not_called()


@pytest.mark.asyncio
async def test_implement_passes_mcp_servers(mem_fs: dict[str, str]) -> None:
    """MCP servers should be forwarded to the SDK query during implementation."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    mcp = {"github": {"command": "npx", "args": ["-y", "pkg"], "env": {"T": "x"}}}

    m = Memory(FAKE_DIR)
    c = Consciousness(FAKE_DIR)
    j = Journal(FAKE_DIR)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    e = EvolutionEngine(
        m, c, j, n, issues=issues, github_repo="owner/repo",
        config=Config(self_review_max_rounds=0), vcs=vcs, code_host=MagicMock(), mcp_servers=mcp,
    )
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)

    opts = mock_q.call_args.kwargs.get("options")
    assert opts is not None
    assert opts.mcp_servers == mcp


@pytest.mark.asyncio
async def test_implement_forwards_disallowed_tools(mem_fs: dict[str, str]) -> None:
    """The no-session implement query carries the read-only external-MCP denial.

    Regression: the bare-``query`` else-branch attaches ``_mcp_servers`` (serena
    included while on) and must also forward ``external_mcp_disallowed_tools()``,
    or serena's ``execute_shell_command`` would be offered and could bypass the
    Bash-scoped action gate (proposal 084 read-only trust boundary).
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)

    m = Memory(FAKE_DIR)
    c = Consciousness(FAKE_DIR)
    j = Journal(FAKE_DIR)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    e = EvolutionEngine(
        m, c, j, n, issues=issues, github_repo="owner/repo",
        config=Config(self_review_max_rounds=0, serena_mcp_enabled=True),
        vcs=vcs, code_host=MagicMock(),
    )
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)

    opts = mock_q.call_args.kwargs.get("options")
    assert opts is not None
    assert opts.disallowed_tools is not None
    assert "mcp__serena__execute_shell_command" in opts.disallowed_tools


@pytest.mark.asyncio
async def test_implement_disallowed_tools_none_when_serena_off(
    mem_fs: dict[str, str],
) -> None:
    """Serena off → no disallow list on the implement query (byte-identical)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)

    m = Memory(FAKE_DIR)
    c = Consciousness(FAKE_DIR)
    j = Journal(FAKE_DIR)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    e = EvolutionEngine(
        m, c, j, n, issues=issues, github_repo="owner/repo",
        config=Config(self_review_max_rounds=0),
        vcs=vcs, code_host=MagicMock(),
    )
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)

    opts = mock_q.call_args.kwargs.get("options")
    assert opts is not None
    assert opts.disallowed_tools is None


# --- Self-review tests ---


def _make_with_review(
    mem_fs: dict[str, str],
    vcs: MagicMock | None = None,
    host: MagicMock | None = None,
    max_rounds: int = 2,
) -> tuple[EvolutionEngine, Memory]:
    m = Memory(FAKE_DIR)
    c = Consciousness(FAKE_DIR)
    j = Journal(FAKE_DIR)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    e = EvolutionEngine(
        m, c, j, n, issues=issues, github_repo="owner/repo",
        config=Config(self_review_max_rounds=max_rounds),
        vcs=vcs, code_host=host,
    )
    return e, m


@pytest.mark.asyncio
async def test_self_review_lgtm_first_round(mem_fs: dict[str, str]) -> None:
    """Self-review says LGTM on first round — no extra commits."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # implement commits (True); the LGTM round leaves a clean tree (False).
    vcs.has_changes.side_effect = [True, False]
    arm_entry_clean_tree(vcs)
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+new line"
    e, _m = _make_with_review(mem_fs, vcs=vcs)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution_quality.query", mock_q):
        mock_q.side_effect = [
            MockQueryResult(response="implemented"),  # implement query
            MockQueryResult(response="LGTM"),          # self-review round 1
        ]
        base_hash = await e.implement(plan)
        await e.self_review(plan, base_hash)

    assert mock_q.call_count == 2
    # Only one commit (implementation), no review fix commits
    assert vcs.commit.call_count == 1


@pytest.mark.asyncio
async def test_self_review_forwards_disallowed_tools(
    mem_fs: dict[str, str],
) -> None:
    """The no-session self-review query carries the read-only external-MCP denial.

    Regression: the bare-``query`` else-branch attaches ``_mcp_servers`` (serena
    included while on) and must also forward ``external_mcp_disallowed_tools()``
    so serena's ``execute_shell_command`` cannot bypass the Bash action gate.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.has_changes.return_value = True
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+new line"
    e, _m = _make_with_review(mem_fs, vcs=vcs)
    e._config = Config(self_review_max_rounds=2, serena_mcp_enabled=True)
    plan = _approved_plan()

    with patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base123")

    opts = mock_q.call_args.kwargs.get("options")
    assert opts is not None
    assert opts.disallowed_tools is not None
    assert "mcp__serena__execute_shell_command" in opts.disallowed_tools


@pytest.mark.asyncio
async def test_self_review_commits_edits_on_lgtm_round(
    mem_fs: dict[str, str],
) -> None:
    """A reviewer that edits files and ends on LGTM in the same round gets its
    edits committed before the green terminal (#4067)."""
    vcs = MagicMock()
    vcs.has_changes.return_value = True
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+new line"
    e, _m = _make_with_review(mem_fs, vcs=vcs)
    plan = _approved_plan()

    with patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(
            response="Fixed the missing guard.\n\nLGTM",
        )
        result = await e.self_review(plan, base_hash="base123")

    assert result is True
    assert mock_q.call_count == 1
    vcs.stage_all.assert_called_once()
    vcs.commit.assert_called_once_with("maestro: self-review fixes (round 1)")


@pytest.mark.asyncio
async def test_self_review_lgtm_clean_tree_does_not_commit(
    mem_fs: dict[str, str],
) -> None:
    """An approving round over a clean tree commits nothing."""
    vcs = MagicMock()
    vcs.has_changes.return_value = False
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+new line"
    e, _m = _make_with_review(mem_fs, vcs=vcs)
    plan = _approved_plan()

    with patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        result = await e.self_review(plan, base_hash="base123")

    assert result is True
    vcs.stage_all.assert_not_called()
    vcs.commit.assert_not_called()


@pytest.mark.asyncio
async def test_self_review_fix_then_lgtm(mem_fs: dict[str, str]) -> None:
    """First review round finds issues and fixes, second round LGTM."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # has_changes: True for implement check, True for review round 1 fixes,
    # False for the clean LGTM round 2
    arm_entry_clean_tree(vcs)
    vcs.has_changes.side_effect = [True, True, False]
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+new line"
    e, _m = _make_with_review(mem_fs, vcs=vcs)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution_quality.query", mock_q):
        mock_q.side_effect = [
            MockQueryResult(response="implemented"),           # implement
            MockQueryResult(response="Found issues, fixed"),   # review round 1
            MockQueryResult(response="LGTM"),                  # review round 2
        ]
        base_hash = await e.implement(plan)
        await e.self_review(plan, base_hash)

    assert mock_q.call_count == 3
    # Two commits: implementation + review fixes
    assert vcs.commit.call_count == 2
    assert "self-review fixes" in vcs.commit.call_args_list[1][0][0]


@pytest.mark.asyncio
async def test_self_review_max_rounds_exhausted(mem_fs: dict[str, str]) -> None:
    """A review that makes progress then stalls terminates ``False`` (proposal
    110).

    ``self_review_max_rounds`` is no longer a hard cap: the loop runs while each
    round commits a fix and stops only on a no-progress stall — a round with no
    LGTM and no committed fix — which the caller routes to the loud fail-loud
    terminal.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # implement (True) → round 1 commits a fix (True) → round 2 makes no
    # change and does not approve (False): the stall terminal.
    arm_entry_clean_tree(vcs)
    vcs.has_changes.side_effect = [True, True, False]
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+line"
    e, _m = _make_with_review(mem_fs, vcs=vcs, max_rounds=2)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution_quality.query", mock_q):
        mock_q.side_effect = [
            MockQueryResult(response="implemented"),           # implement
            MockQueryResult(response="Issues found, fixed"),   # review round 1
            MockQueryResult(response="Still issues, no fix"),  # review round 2
        ]
        base_hash = await e.implement(plan)
        result = await e.self_review(plan, base_hash)  # should not raise

    assert result is False
    assert mock_q.call_count == 3
    # 2 commits: implementation + round-1 fix (round 2 stalled, no commit).
    assert vcs.commit.call_count == 2


@pytest.mark.asyncio
async def test_self_review_grinds_past_max_rounds(
    mem_fs: dict[str, str],
) -> None:
    """A review that keeps committing fixes grinds *past* ``self_review_max_rounds``
    (proposal 110). With the default ceiling (``gate_convergence_ceiling`` = 20)
    three fix rounds under the old cap of 2 are no longer cut off, and a final
    LGTM converges to ``True``."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # implement + three fix rounds each commit a change (True); the LGTM round
    # leaves a clean tree (False), so it commits nothing.
    arm_entry_clean_tree(vcs)
    vcs.has_changes.side_effect = [True, True, True, True, False]
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+line"
    # max_rounds=2 (the old hard cap) — the loop must run past it.
    e, _m = _make_with_review(mem_fs, vcs=vcs, max_rounds=2)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution_quality.query", mock_q):
        mock_q.side_effect = [
            MockQueryResult(response="implemented"),         # implement
            MockQueryResult(response="Fixed round 1"),       # review round 1
            MockQueryResult(response="Fixed round 2"),       # review round 2
            MockQueryResult(response="Fixed round 3"),       # review round 3
            MockQueryResult(response="LGTM"),                # review round 4
        ]
        base_hash = await e.implement(plan)
        result = await e.self_review(plan, base_hash)

    assert result is True
    # implement + 4 review rounds (3 fixes past the old cap of 2, then LGTM).
    assert mock_q.call_count == 5
    # 4 commits: implementation + 3 review-fix rounds.
    assert vcs.commit.call_count == 4


@pytest.mark.asyncio
async def test_self_review_stall_returns_false_immediately(
    mem_fs: dict[str, str],
) -> None:
    """A first round with no LGTM and no committed fix stalls immediately —
    ``self_review`` returns ``False`` after a single review round, without
    grinding out to the ceiling (proposal 110)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # implement (True) → round 1 makes no change and does not approve (False).
    arm_entry_clean_tree(vcs)
    vcs.has_changes.side_effect = [True, False]
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+line"
    # A high ceiling (default 20) proves the stall — not the ceiling — stops it.
    e, _m = _make_with_review(mem_fs, vcs=vcs, max_rounds=2)
    # Flag off (the ``_make_with_review`` default): the review terminal is a pure
    # pass-through and must issue NO ``run_quality_gates`` call — the off-path is
    # provably byte-identical, not merely believed to be (proposal 124).
    e.run_quality_gates = MagicMock()  # type: ignore[method-assign]
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution_quality.query", mock_q):
        mock_q.side_effect = [
            MockQueryResult(response="implemented"),
            MockQueryResult(response="Issues found but could not fix"),
        ]
        base_hash = await e.implement(plan)
        result = await e.self_review(plan, base_hash)

    assert result is False
    # Only one review round fired — the stall short-circuited the loop.
    assert mock_q.call_count == 2
    # Only the implementation commit; the stalled round committed nothing.
    assert vcs.commit.call_count == 1
    # Flag off ⇒ the gate is never re-read on the stall path.
    e.run_quality_gates.assert_not_called()


@pytest.mark.asyncio
async def test_self_review_ceiling_backstop_returns_false(
    mem_fs: dict[str, str],
) -> None:
    """When the reviewer always commits a fix but never approves, the generous
    ceiling backstop terminates the loop at ``ceiling`` rounds with ``False``
    (proposal 110). ``ceiling`` folds ``self_review_max_rounds`` into
    ``gate_convergence_ceiling`` via ``max`` — here both are low so the backstop
    is observable."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # implement + two fix rounds; ceiling = max(2, 1) = 2 stops the second.
    arm_entry_clean_tree(vcs)
    vcs.has_changes.side_effect = [True, True, True]
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+line"
    e, _m = _make_with_review(mem_fs, vcs=vcs)
    # ceiling = max(gate_convergence_ceiling=2, self_review_max_rounds=1) = 2.
    e._config = Config(self_review_max_rounds=1, gate_convergence_ceiling=2)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution_quality.query", mock_q):
        mock_q.side_effect = [
            MockQueryResult(response="implemented"),       # implement
            MockQueryResult(response="Fixed but issues remain 1"),  # round 1
            MockQueryResult(response="Fixed but issues remain 2"),  # round 2
        ]
        base_hash = await e.implement(plan)
        result = await e.self_review(plan, base_hash)

    assert result is False
    # Exactly ceiling (2) review rounds ran before the backstop tripped.
    assert mock_q.call_count == 3
    # 3 commits: implementation + 2 review-fix rounds.
    assert vcs.commit.call_count == 3


def _clean_review() -> QualityGateResult:
    """A green gate result whose observation reports every gate CLEAN (proposal 124)."""
    return QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True, raw_output="",
        observation=GateRunObservation(
            gates=(
                GateObservation(tool="ruff", kind="CLEAN", failures=(), output=""),
                GateObservation(tool="mypy", kind="CLEAN", failures=(), output=""),
                GateObservation(tool="tests", kind="CLEAN", failures=(), output=""),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_self_review_verify_grace_buys_a_round_flag_on(
    mem_fs: dict[str, str],
) -> None:
    """Flag on: a verify-only round against a green gate buys a second round.

    Proposal 124 — the regression this change exists for. A first round with no
    ``LGTM`` and no committed fix would return ``False`` immediately under the
    old tree-dirtiness proxy; with the gate read at the terminal and the branch
    green, it buys one further review round, then stalls once the bounded
    verify-only grace is spent.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # Two verify-only rounds: neither approves, neither leaves the tree dirty.
    vcs.has_changes.side_effect = [False, False]
    vcs.get_head_hash.return_value = "head999"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+line"
    e, _m = _make_with_review(mem_fs, vcs=vcs)
    e._config = Config(self_review_max_rounds=2, gate_observation_enabled=True)
    e.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_clean_review(),
    )
    plan = _approved_plan()

    with patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q:
        mock_q.side_effect = [
            MockQueryResult(response="Reviewed — nothing to change (round 1)"),
            MockQueryResult(response="Reviewed — nothing to change (round 2)"),
        ]
        result = await e.self_review(plan, base_hash="base123")

    assert result is False
    # A SECOND review round ran — the grace converted the would-be demotion.
    assert mock_q.call_count == 2
    # The grace budget is one, so the gate is re-read exactly once across the call.
    assert e.run_quality_gates.call_count == 1
    # No fixes were committed on either verify-only round.
    assert vcs.commit.call_count == 0

    # The grace-bought round's prompt carries the measured-green gate fact the
    # gate read paid for (proposal 124) — the first (verify) round does not.
    round1_prompt = mock_q.call_args_list[0].kwargs["prompt"]
    round2_prompt = mock_q.call_args_list[1].kwargs["prompt"]
    assert "## Gate Status" not in round1_prompt
    assert "## Gate Status" in round2_prompt
    assert "GREEN" in round2_prompt

    # Exactly one ``self_review_terminal`` row, at the grace-spent stall exit,
    # whose ``detail`` pins the grace accounting — ``grace_spent`` is
    # ``_VERIFY_ONLY_REVIEW_ROUND_BUDGET - verify_grace_remaining``, so a sign
    # inversion (invisible to a mere line-coverage assertion) would fail here.
    terminal_rows = [
        row for row in e._gate_activations.read_all()
        if row.gate == "self_review_terminal"
    ]
    assert len(terminal_rows) == 1
    assert terminal_rows[0].action == "stuck"
    assert terminal_rows[0].detail == "rounds=2 grace_spent=1"


@pytest.mark.asyncio
async def test_self_review_verify_grace_red_gate_stays_stuck_flag_on(
    mem_fs: dict[str, str],
) -> None:
    """Flag on: grace does not rescue a red gate — one read, then stall.

    The gate read fires on the would-be-stuck round, finds a red gate, and the
    round stays a stall — so no second review round runs and only one extra gate
    run is paid.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.has_changes.side_effect = [False]
    vcs.get_head_hash.return_value = "head999"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+line"
    e, _m = _make_with_review(mem_fs, vcs=vcs)
    e._config = Config(self_review_max_rounds=2, gate_observation_enabled=True)
    e.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_baseline_result("maestro/x.py:1:name-defined"),
    )
    plan = _approved_plan()

    with patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q:
        mock_q.side_effect = [
            MockQueryResult(response="Found a problem, could not fix it"),
        ]
        result = await e.self_review(plan, base_hash="base123")

    assert result is False
    # No second round — a red gate does not buy a continuation.
    assert mock_q.call_count == 1
    assert e.run_quality_gates.call_count == 1
    assert vcs.commit.call_count == 0


@pytest.mark.asyncio
async def test_self_review_self_committed_fix_is_progress_flag_on(
    mem_fs: dict[str, str],
) -> None:
    """Flag on: a reviewer that commits its own fix is progress, not a stall.

    Proposal 124's second motivating case. The action gate scopes the review
    agent's ``Bash`` to the evolve branch, so an agent that stages and commits
    its own fix hands back a *clean* tree — ``has_changes()`` sees nothing. With
    HEAD advanced past the round's start, that round is a committed-fix
    continuation, not a would-be stall: no verify-only grace is spent (so the
    gate is never re-read) and the branch is never demoted for it. The next
    round approves and the review proceeds.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # Round 1 leaves a clean tree (the reviewer committed its own fix), and so
    # does the approving round 2…
    vcs.has_changes.side_effect = [False, False]
    # …but HEAD advanced between the round's start ("h0") and its terminal
    # check ("h1"); round 2 starts on the advanced head and approves.
    vcs.get_head_hash.side_effect = ["h0", "h1", "h1"]
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+line"
    e, _m = _make_with_review(mem_fs, vcs=vcs)
    e._config = Config(self_review_max_rounds=2, gate_observation_enabled=True)
    # Must never be called: a self-committed fix is progress on its own, so the
    # would-be-stuck gate re-read never fires.
    e.run_quality_gates = MagicMock()  # type: ignore[method-assign]
    plan = _approved_plan()

    with patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q:
        mock_q.side_effect = [
            MockQueryResult(response="Fixed and committed it myself"),
            MockQueryResult(response="LGTM"),
        ]
        result = await e.self_review(plan, base_hash="base123")

    assert result is True
    # A second review round ran — the self-commit was counted as progress.
    assert mock_q.call_count == 2
    # No grace was spent, so the gate was never re-read.
    e.run_quality_gates.assert_not_called()
    # ``self_review`` committed nothing — the reviewer already committed its fix.
    assert vcs.commit.call_count == 0
    # The green terminal row records ``grace_spent=0`` — the self-committed
    # round bought its continuation for free, not out of the verify-only grace.
    terminal_rows = [
        row for row in e._gate_activations.read_all()
        if row.gate == "self_review_terminal"
    ]
    assert len(terminal_rows) == 1
    assert terminal_rows[0].action == "green"
    assert terminal_rows[0].detail == "rounds=2 grace_spent=0"


@pytest.mark.asyncio
async def test_self_review_self_committed_fix_ignored_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Flag off: a self-committed fix (advanced HEAD) is NOT progress — stall.

    The head-advance detection is behind ``[loop] gate_observation_enabled``, so
    flag-off the loop is byte-identical to its pre-124 behaviour: the same round
    (clean tree, advanced HEAD, no ``LGTM``) is a stall on the first round, and
    the gate is never re-read.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.has_changes.side_effect = [False]
    # HEAD advances, but flag-off the ``and`` short-circuits before it is even
    # re-read: only the round-start read happens.
    vcs.get_head_hash.side_effect = ["h0"]
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+line"
    e, _m = _make_with_review(mem_fs, vcs=vcs, max_rounds=2)
    # Flag off (the ``_make_with_review`` default): the review terminal issues
    # no ``run_quality_gates`` call at all.
    e.run_quality_gates = MagicMock()  # type: ignore[method-assign]
    plan = _approved_plan()

    with patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q:
        mock_q.side_effect = [
            MockQueryResult(response="Fixed and committed it myself"),
        ]
        result = await e.self_review(plan, base_hash="base123")

    assert result is False
    # Only one round — the stall short-circuited the loop.
    assert mock_q.call_count == 1
    # Flag off ⇒ the gate is never re-read, and no head-advance progress credit.
    e.run_quality_gates.assert_not_called()
    assert vcs.commit.call_count == 0


@pytest.mark.asyncio
async def test_self_review_terminal_offloaded_to_thread(
    mem_fs: dict[str, str],
) -> None:
    """The review terminal's gate re-read runs via ``asyncio.to_thread``.

    ``_review_terminal``'s flag-on gate re-read is a full ruff+mypy+pytest
    sweep, so ``self_review`` must hand it to ``asyncio.to_thread`` rather than
    stall the event loop while the implementation session may still be live —
    the same convention ``_observe_baseline``'s callers use. Wrap the real
    ``to_thread`` and assert the bound ``_review_terminal`` is the callable
    handed to it (proposal 124).
    """
    import asyncio as _asyncio

    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.has_changes.side_effect = [False, False]
    vcs.get_head_hash.return_value = "head999"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+line"
    e, _m = _make_with_review(mem_fs, vcs=vcs)
    e._config = Config(self_review_max_rounds=2, gate_observation_enabled=True)
    e.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_clean_review(),
    )
    plan = _approved_plan()

    real_to_thread = _asyncio.to_thread

    async def tracking_to_thread(func: object, /, *args: object, **kwargs: object) -> object:
        tracking_to_thread.calls.append(  # type: ignore[attr-defined]
            getattr(func, "__name__", repr(func)),
        )
        return await real_to_thread(func, *args, **kwargs)  # type: ignore[arg-type]

    tracking_to_thread.calls = []  # type: ignore[attr-defined]

    with (
        patch("maestro.evolution.asyncio.to_thread", tracking_to_thread),
        patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_q.side_effect = [
            MockQueryResult(response="Reviewed — nothing to change (round 1)"),
            MockQueryResult(response="Reviewed — nothing to change (round 2)"),
        ]
        result = await e.self_review(plan, base_hash="base123")

    assert result is False
    # The terminal decision — carrying the blocking gate re-read — was offloaded.
    assert "_review_terminal" in tracking_to_thread.calls  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_self_review_exhausted_suppresses_warning(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-converging review returns False and stays silent — proposal 110
    retired the legacy "proceeding" WARNING; the loud terminal now lives
    unconditionally in ``agent.implement_plan``, so no proceed-without-LGTM
    WARNING may fire.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # implement (True) → round 1 fix (True) → round 2 stall (False).
    arm_entry_clean_tree(vcs)
    vcs.has_changes.side_effect = [True, True, False]
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+line"
    e, _m = _make_with_review(mem_fs, vcs=vcs, max_rounds=2)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution_quality.query", mock_q):
        mock_q.side_effect = [
            MockQueryResult(response="implemented"),          # implement
            MockQueryResult(response="Issues found, fixed"),  # review round 1
            MockQueryResult(response="Still issues, no fix"),  # review round 2
        ]
        base_hash = await e.implement(plan)
        with caplog.at_level("WARNING", logger="maestro"):
            result = await e.self_review(plan, base_hash)

    assert result is False
    assert not any(
        "proceeding" in str(rec.msg).lower() for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_self_review_skipped_when_zero(mem_fs: dict[str, str]) -> None:
    """Self-review is skipped when self_review_max_rounds=0."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    vcs.get_head_hash.return_value = "base123"
    e, _m = _make_with_review(mem_fs, vcs=vcs, max_rounds=0)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)

    # Only one query call (implementation), no self-review
    # (self_review would be called by the caller only if max_rounds > 0)
    assert mock_q.call_count == 1
    vcs.get_diff.assert_not_called()


@pytest.mark.asyncio
async def test_self_review_diff_passed_to_prompt(mem_fs: dict[str, str]) -> None:
    """The diff content is included in the self-review prompt."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/maestro/agent.py b/maestro/agent.py\n+unique_marker_xyz"
    e, _m = _make_with_review(mem_fs, vcs=vcs, max_rounds=1)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution_quality.query", mock_q):
        mock_q.side_effect = [
            MockQueryResult(response="implemented"),  # implement
            MockQueryResult(response="LGTM"),          # review
        ]
        base_hash = await e.implement(plan)
        await e.self_review(plan, base_hash)

    # The second query call (self-review) should contain the diff
    review_call = mock_q.call_args_list[1]
    review_prompt = review_call.args[0] if review_call.args else review_call.kwargs["prompt"]
    assert "unique_marker_xyz" in review_prompt
    assert "Test evolution" in review_prompt


@pytest.mark.asyncio
async def test_self_review_diff_truncated_once_with_accurate_marker(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A >50K diff is truncated exactly once — by ``_truncate_diff`` — not
    twice.  ``self_review`` requests the uncapped diff (``max_chars=0``), the
    prompt carries ``_truncate_diff``'s marker with the true omitted count, and
    the truncation WARNING fires.  Before the fix, ``get_diff`` pre-capped at
    the same 50K, so ``_truncate_diff`` never fired and both the marker and the
    WARNING were silently skipped."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    vcs.get_head_hash.return_value = "base123"
    big_diff = "diff --git a/foo.py b/foo.py\n" + ("+x\n" * 20_000)
    assert len(big_diff) > 50_000
    vcs.get_diff.return_value = big_diff
    e, _m = _make_with_review(mem_fs, vcs=vcs, max_rounds=1)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution_quality.query", mock_q):
        mock_q.side_effect = [
            MockQueryResult(response="implemented"),  # implement
            MockQueryResult(response="LGTM"),          # review round 1
        ]
        base_hash = await e.implement(plan)
        with caplog.at_level("WARNING", logger="maestro"):
            await e.self_review(plan, base_hash)

    # self_review requested the uncapped diff so _truncate_diff is the sole
    # truncation authority (every get_diff call passes max_chars=0).
    assert vcs.get_diff.call_args_list
    for call in vcs.get_diff.call_args_list:
        assert call.kwargs.get("max_chars") == 0

    # The prompt carries _truncate_diff's marker with the true omitted count.
    expected_omitted = len(big_diff) - 2 * (50_000 // 2)
    expected_marker = (
        f"[... {expected_omitted} chars truncated by self-review diff cap ...]"
    )
    review_call = mock_q.call_args_list[1]
    review_prompt = (
        review_call.args[0] if review_call.args else review_call.kwargs["prompt"]
    )
    assert expected_marker in review_prompt

    # The truncation WARNING fired (it was silently skipped before the fix).
    assert any(
        "diff truncated from" in str(rec.msg) for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_self_review_skips_when_diff_empty(mem_fs: dict[str, str]) -> None:
    """If get_diff returns empty, self-review is skipped gracefully."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = ""
    e, _m = _make_with_review(mem_fs, vcs=vcs)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        base_hash = await e.implement(plan)
        await e.self_review(plan, base_hash)

    # Only implementation query, no review query since diff was empty
    assert mock_q.call_count == 1


@pytest.mark.asyncio
async def test_self_review_no_changes_no_commit(mem_fs: dict[str, str]) -> None:
    """A reviewer that reports issues but commits no fix is a stall: no extra
    commit is created and ``self_review`` returns ``False`` (proposal 110 — a
    round with no LGTM and no change is the no-progress terminal)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # First has_changes for implement check = True,
    # second for review round 1 = False (no fixes made) → stall.
    arm_entry_clean_tree(vcs)
    vcs.has_changes.side_effect = [True, False]
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+line"
    e, _m = _make_with_review(mem_fs, vcs=vcs, max_rounds=1)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution_quality.query", mock_q):
        mock_q.side_effect = [
            MockQueryResult(response="implemented"),
            MockQueryResult(response="Found issues but could not fix"),
        ]
        base_hash = await e.implement(plan)
        result = await e.self_review(plan, base_hash)

    assert result is False
    # Only one commit (implementation), no fix commit
    assert vcs.commit.call_count == 1


def test_truncate_diff_under_budget_returns_unchanged() -> None:
    diff = "diff --git a/foo.py b/foo.py\n+small change\n"
    assert _truncate_diff(diff) == diff


def test_truncate_diff_over_budget_keeps_head_and_tail() -> None:
    head = "AAA_HEAD_MARKER"
    tail = "ZZZ_TAIL_MARKER"
    middle = "M" * (_SELF_REVIEW_DIFF_MAX_CHARS * 2)
    diff = head + middle + tail
    truncated = _truncate_diff(diff)

    assert head in truncated
    assert tail in truncated
    assert "chars truncated" in truncated
    # Final size is bounded: cap + marker line (~80 chars), well under 2x cap.
    assert len(truncated) < _SELF_REVIEW_DIFF_MAX_CHARS + 200


def test_truncate_diff_respects_custom_cap() -> None:
    diff = "x" * 1000
    truncated = _truncate_diff(diff, max_chars=200)
    assert len(truncated) < 400
    assert "chars truncated" in truncated


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("LGTM", True),
        ("lgtm", True),
        ("LGTM.", True),
        ("LGTM!", True),
        ("  lgtm  ", True),
        ("This is not LGTM yet because ...", False),
        ("not LGTM", False),
        ("LGTM is not appropriate here", False),
        ("This change is not an LGTM", False),
        ("", False),
        ("\n\n", False),
        ("Looks good\nLGTM", True),
        ("LGTM\n\n", True),
        ("LGTM but with caveats", False),
        ("First line is LGTM\nbut second line says NO", False),
    ],
)
def test_is_lgtm(response: str, expected: bool) -> None:
    """The LGTM matcher must reject substring impostors and accept clean verdicts."""
    assert _is_lgtm(response) is expected


@pytest.mark.asyncio
async def test_self_review_rejects_substring_lgtm(mem_fs: dict[str, str]) -> None:
    """A reviewer that explicitly says 'not LGTM' must not be treated as approval.

    Regression for the substring-match bug: previously ``"LGTM" in response_text``
    accepted any response containing the literal token, including
    ``"This is not LGTM yet ..."``.  The fix only matches an LGTM verdict on
    the last non-empty line.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # has_changes: True after implement; round 1 commits a fix (True) so the
    # progress-gated loop continues; round 2 makes no change (False) and stalls.
    # Neither review response is an LGTM despite containing the substring.
    arm_entry_clean_tree(vcs)
    vcs.has_changes.side_effect = [True, True, False]
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+line"
    e, _m = _make_with_review(mem_fs, vcs=vcs, max_rounds=2)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution_quality.query", mock_q):
        mock_q.side_effect = [
            MockQueryResult(response="implemented"),
            MockQueryResult(
                response="This is not LGTM yet because the test coverage is missing.",
            ),
            MockQueryResult(
                response="Still not an LGTM — the docstring is misleading.",
            ),
        ]
        base_hash = await e.implement(plan)
        result = await e.self_review(plan, base_hash)

    # All three queries fired (implement + 2 review rounds), proving the loop
    # did NOT short-circuit on the substring match; the round-2 stall then
    # terminates the review without approval.
    assert result is False
    assert mock_q.call_count == 3


@pytest.mark.asyncio
async def test_self_review_accepts_lgtm_on_last_line(mem_fs: dict[str, str]) -> None:
    """A multi-line response whose last line is LGTM is accepted as approval."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # implement commits (True); the LGTM round leaves a clean tree (False).
    vcs.has_changes.side_effect = [True, False]
    arm_entry_clean_tree(vcs)
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+line"
    e, _m = _make_with_review(mem_fs, vcs=vcs, max_rounds=2)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution_quality.query", mock_q):
        mock_q.side_effect = [
            MockQueryResult(response="implemented"),
            MockQueryResult(
                response=(
                    "I reviewed the diff against the plan.\n"
                    "Everything checks out: completeness, correctness, tests.\n"
                    "LGTM"
                ),
            ),
        ]
        base_hash = await e.implement(plan)
        await e.self_review(plan, base_hash)

    # Only two queries (implement + 1 review round) — second round skipped.
    assert mock_q.call_count == 2
    # Only one commit — no review-fix commit.
    assert vcs.commit.call_count == 1


@pytest.mark.asyncio
async def test_self_review_truncates_large_diff(mem_fs: dict[str, str]) -> None:
    """An oversized diff is truncated before being injected into the prompt."""
    head = "diff --git a/foo.py b/foo.py\nUNIQUE_HEAD_TOKEN\n"
    tail = "\nUNIQUE_TAIL_TOKEN\n"
    middle_filler = "+" + ("X" * _SELF_REVIEW_DIFF_MAX_CHARS) + "\n"
    huge_diff = head + middle_filler + tail

    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = huge_diff
    e, _m = _make_with_review(mem_fs, vcs=vcs, max_rounds=1)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution_quality.query", mock_q):
        mock_q.side_effect = [
            MockQueryResult(response="implemented"),
            MockQueryResult(response="LGTM"),
        ]
        base_hash = await e.implement(plan)
        await e.self_review(plan, base_hash)

    review_call = mock_q.call_args_list[1]
    review_prompt = review_call.args[0] if review_call.args else review_call.kwargs["prompt"]
    # Both ends of the diff are still visible to the reviewer.
    assert "UNIQUE_HEAD_TOKEN" in review_prompt
    assert "UNIQUE_TAIL_TOKEN" in review_prompt
    # The truncation marker is present so the reviewer knows the diff was clipped.
    assert "chars truncated by self-review diff cap" in review_prompt
    # The prompt is bounded — well under 2x the diff cap to leave room for
    # the rest of the prompt scaffolding.
    assert len(review_prompt) < 2 * _SELF_REVIEW_DIFF_MAX_CHARS


@pytest.mark.asyncio
async def test_self_review_accepts_lgtm_with_trailing_punctuation(
    mem_fs: dict[str, str],
) -> None:
    """The LGTM regex tolerates a trailing period or exclamation mark."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+line"
    e, _m = _make_with_review(mem_fs, vcs=vcs, max_rounds=1)
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution_quality.query", mock_q):
        mock_q.side_effect = [
            MockQueryResult(response="implemented"),
            MockQueryResult(response="lgtm!"),
        ]
        base_hash = await e.implement(plan)
        await e.self_review(plan, base_hash)

    assert mock_q.call_count == 2


def test_create_issue_delegates_to_issue_tracker(mem_fs: dict[str, str]) -> None:
    """``EvolutionEngine.create_issue`` is a thin passthrough to the
    injected ``IssueTracker`` — regression guard covering the wrapper
    body (hit via the failed-plan terminal path in production)."""
    e, _m = _make(mem_fs)
    e._issues = MagicMock()
    e._issues.create.return_value = 42

    result = e.create_issue("title", "body", labels=["bug"])

    assert result == 42
    e._issues.create.assert_called_once_with("title", "body", labels=["bug"])


# --- Action-gate installation on the sessionless fallbacks (issue #2914) ---
#
# ``implementation`` is declared repo-mutating in ``action_gated_phases()``, but
# that declaration only constrains which *provider* may serve the phase. Both
# guard-carrying providers derive the actual gate from ``QueryOptions.hooks``:
# the Anthropic path installs the ``PreToolUse`` callback only when
# ``options.hooks is not None``, and ``cli_paced`` injects no ``--settings``
# without it. So a ``QueryOptions`` granting ``Bash`` with no ``hooks=`` is
# un-gated on *every* provider — the implementation lineage could run
# ``gh pr merge`` or push to ``main`` from ``Bash``, the exact human-in-the-loop
# bypass the gate exists to close. These three cover the ``session is None``
# arms taken when ``enable_session_reuse`` is off; the session arms inherit the
# gate from the impl session's own options.


def _assert_gated_to_plan_branch(
    options: QueryOptions, plan_id: str, config: Config,
) -> None:
    """Assert *options* installs the task action-gate scoped to *plan_id*.

    ``allowed_branches`` must be the plan's own evolve branch, not ``None``:
    the gate fails *closed* on an empty set, which would deny every push and
    strand the implementation phase's own work. ``strip_redirections`` is
    checked against *config* rather than today's default, so the assertion pins
    the *wiring* (the lever is forwarded) and survives a default flip.
    """
    gate = task_gate_from_hooks(options.hooks)
    assert gate is not None
    assert gate.allowed_branches == frozenset({evolution_branch(plan_id)})
    assert gate.current_branch == evolution_branch(plan_id)
    assert gate.strip_redirections == config.gate_strip_redirections_enabled


@pytest.mark.asyncio
async def test_implement_bare_query_carries_action_gate(
    mem_fs: dict[str, str],
) -> None:
    """The no-session ``implement`` query installs the #2280 Bash action-gate."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    e, _m = _make(mem_fs, vcs=vcs, host=MagicMock())
    plan = _approved_plan()

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(plan)

    _assert_gated_to_plan_branch(
        mock_q.call_args.kwargs["options"], plan.plan_id, e._config,
    )


@pytest.mark.asyncio
async def test_self_review_bare_query_carries_action_gate(
    mem_fs: dict[str, str],
) -> None:
    """The no-session ``self_review`` query installs the Bash action-gate."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.has_changes.return_value = True
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+new line"
    e, _m = _make_with_review(mem_fs, vcs=vcs)
    plan = _approved_plan()

    with patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base123")

    _assert_gated_to_plan_branch(
        mock_q.call_args.kwargs["options"], plan.plan_id, e._config,
    )


@pytest.mark.asyncio
async def test_auto_fix_bare_query_carries_action_gate(
    mem_fs: dict[str, str],
) -> None:
    """The no-session ``_run_auto_fix_query`` installs the Bash action-gate.

    Reached through the public ``auto_fix`` entry point — the gate-grinding
    round is the last un-gated link in the implementation lineage.
    """
    vcs = MagicMock()
    vcs.has_changes.return_value = True
    e, _m = _make(mem_fs, vcs=vcs, host=MagicMock())
    plan = _approved_plan()
    failing = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="Ruff issues:\nE501 line too long",
    )

    with (
        patch("maestro.evolution.subprocess.run") as mock_sub,
        patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff", stderr="",
        )
        mock_q.return_value = MockQueryResult(response="fixed")
        await e.auto_fix(plan, failing, round_n=1, max_rounds=3)

    _assert_gated_to_plan_branch(
        mock_q.call_args.kwargs["options"], plan.plan_id, e._config,
    )


@pytest.mark.asyncio
async def test_auto_fix_prompt_steers_verify_through_container_lease(
    mem_fs: dict[str, str],
) -> None:
    """The auto_fix prompt routes a verify run through the container-safe lease.

    #3760: a bare host-sized ``pytest -n auto`` inside the loop's memory-limited
    cgroup oversubscribes it into an OOM stall, so the fix prompt steers any
    model-issued verify run through ``scripts/quality-gates.sh`` / the ``maestro
    gate-lease`` wrapper (which size pytest-xdist to the container) and never
    toward a bare host-sized parallel run.
    """
    vcs = MagicMock()
    vcs.has_changes.return_value = True
    e, _m = _make(mem_fs, vcs=vcs, host=MagicMock())
    plan = _approved_plan()
    failing = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="Ruff issues:\nE501 line too long",
    )

    with (
        patch("maestro.evolution.subprocess.run") as mock_sub,
        patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff", stderr="",
        )
        mock_q.return_value = MockQueryResult(response="fixed")
        await e.auto_fix(plan, failing, round_n=1, max_rounds=3)

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "maestro gate-lease" in prompt
    assert "scripts/quality-gates.sh" in prompt
    # Never steer toward a bare host-sized parallel run.
    assert "-n auto" not in prompt


# ── The base-branch gate baseline (proposal 124) ───────────────────────────────


def _observing_config() -> Config:
    """The ``_make`` config with the gate-observation flag on."""
    return Config(self_review_max_rounds=0, gate_observation_enabled=True)


def _baseline_vcs(head: str | None) -> MagicMock:
    """A VCS standing on *head* with a clean working tree — the seam's happy path.

    Both conditions are load-bearing: the seam observes the *tree*, so it banks a
    row for a commit only while HEAD is that commit **and** nothing uncommitted
    sits on top of it.
    """
    vcs = MagicMock()
    vcs.get_head_hash.return_value = head
    vcs.has_changes.return_value = False
    return vcs


def _baseline_result(identifier: str) -> QualityGateResult:
    """A red gate result carrying a one-failure observation, for stubbing the runner."""
    failure = GateFailure(
        tool="mypy", kind="FAILED", location="maestro/x.py",
        identifier=identifier, message="",
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


def test_observe_baseline_records_only_while_head_stands_on_the_sha(
    mem_fs: dict[str, str],
) -> None:
    """The gate runs, and the observation is banked under *sha*, on a HEAD match."""
    vcs = _baseline_vcs("base1")
    e, _m = _make(mem_fs, vcs=vcs, config=_observing_config())
    e.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_baseline_result("maestro/x.py:1:name-defined"),
    )

    observed = e._observe_baseline("base1")

    assert observed is not None
    assert observed.signature() == frozenset({"mypy:maestro/x.py:1:name-defined"})
    assert e._gate_baselines["base1"] is observed
    e.run_quality_gates.assert_called_once_with()


def test_observe_baseline_refuses_a_head_that_is_not_the_sha(
    mem_fs: dict[str, str],
) -> None:
    """A HEAD mismatch runs no gate and moves no tree.

    The seam never checks anything out — that is what keeps it from pulling a
    mid-flight implementation tree out from under its own session — so a base it
    does not already stand on is simply not observed.
    """
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "somewhere-else"
    e, _m = _make(mem_fs, vcs=vcs, config=_observing_config())
    e.run_quality_gates = MagicMock()  # type: ignore[method-assign]

    assert e._observe_baseline("base1") is None
    assert e._gate_baselines == {}
    e.run_quality_gates.assert_not_called()
    vcs.checkout.assert_not_called()
    vcs.checkout_new_branch.assert_not_called()


def test_observe_baseline_short_circuits_an_empty_sha(mem_fs: dict[str, str]) -> None:
    """An empty key never reaches the VCS — the ``or ""`` coercion can hand it one."""
    vcs = MagicMock()
    e, _m = _make(mem_fs, vcs=vcs, config=_observing_config())
    e.run_quality_gates = MagicMock()  # type: ignore[method-assign]

    assert e._observe_baseline("") is None
    vcs.get_head_hash.assert_not_called()
    e.run_quality_gates.assert_not_called()


def test_observe_baseline_is_served_from_cache_on_a_repeat(
    mem_fs: dict[str, str],
) -> None:
    """A second call for the same base re-runs nothing — a commit cannot go stale."""
    vcs = _baseline_vcs("base1")
    e, _m = _make(mem_fs, vcs=vcs, config=_observing_config())
    e.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_baseline_result("maestro/x.py:1:name-defined"),
    )

    first = e._observe_baseline("base1")
    second = e._observe_baseline("base1")

    assert first is second
    e.run_quality_gates.assert_called_once_with()
    assert vcs.get_head_hash.call_count == 1


def test_observe_baseline_evicts_the_oldest_entry_past_the_bound(
    mem_fs: dict[str, str],
) -> None:
    """The map is bounded to ``_MAX_GATE_BASELINES``, dropping the oldest inserted."""
    vcs = _baseline_vcs(None)
    e, _m = _make(mem_fs, vcs=vcs, config=_observing_config())
    shas = [f"base{n}" for n in range(_MAX_GATE_BASELINES + 2)]
    e.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        side_effect=[_baseline_result(f"maestro/x.py:{n}:c") for n in range(len(shas))],
    )

    for sha in shas:
        vcs.get_head_hash.return_value = sha
        e._observe_baseline(sha)

    assert len(e._gate_baselines) == _MAX_GATE_BASELINES
    assert list(e._gate_baselines) == shas[-_MAX_GATE_BASELINES:]


def test_observe_baseline_is_inert_with_the_flag_off(mem_fs: dict[str, str]) -> None:
    """Flag-off the seam issues no subprocess and reads no HEAD — the rollback path."""
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "base1"
    e, _m = _make(mem_fs, vcs=vcs)
    e.run_quality_gates = MagicMock()  # type: ignore[method-assign]

    assert e._observe_baseline("base1") is None
    assert e._gate_baselines == {}
    e.run_quality_gates.assert_not_called()
    vcs.get_head_hash.assert_not_called()


def test_observe_baseline_banks_nothing_when_the_runner_yields_no_observation(
    mem_fs: dict[str, str],
) -> None:
    """A result with no observation is not cached, so a later call can still observe.

    Unreachable with the flag on, but ``QualityGateResult.observation`` is typed
    optional and banking a ``None`` would poison the key for good.
    """
    vcs = _baseline_vcs("base1")
    e, _m = _make(mem_fs, vcs=vcs, config=_observing_config())
    e.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=QualityGateResult(
            passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True, raw_output="",
        ),
    )

    assert e._observe_baseline("base1") is None
    assert e._gate_baselines == {}


@pytest.mark.asyncio
async def test_implement_baselines_the_base_it_stands_on(
    mem_fs: dict[str, str],
) -> None:
    """``implement`` captures its own base: HEAD equals ``base_hash`` by construction.

    The branch is freshly created with no commits yet, so the seam's HEAD check
    passes and a ``source_task_id`` base — the one no tend lap ever visits — gets
    a baseline of its own.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.get_head_hash.return_value = "basehash"
    # Clean at the capture point — the branch was just cut and the session has
    # not written anything — and dirty from the session's first edit onward,
    # which is what the later commit arm reads.
    arm_entry_clean_tree(vcs)
    tree_reads = iter([False])
    vcs.has_changes.side_effect = lambda: next(tree_reads, True)
    e, _m = _make(mem_fs, vcs=vcs, config=_observing_config())
    e.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=_baseline_result("maestro/x.py:1:name-defined"),
    )

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="implemented")
        await e.implement(_approved_plan())

    assert list(e._gate_baselines) == ["basehash"]


def test_observe_baseline_refuses_a_dirty_tree(mem_fs: dict[str, str]) -> None:
    """An uncommitted edit on top of the base is not the base — bank nothing.

    ``run_quality_gates`` observes the working tree, not the object the key names,
    and ``ensure_main_branch`` only discards changes when HEAD is *off* ``main``,
    so dirty-on-``main`` reaches this seam. Banking there would let a red caused
    by local junk later excuse a branch's identical red as ``INHERITED``.
    """
    vcs = _baseline_vcs("base1")
    vcs.has_changes.return_value = True
    e, _m = _make(mem_fs, vcs=vcs, config=_observing_config())
    e.run_quality_gates = MagicMock()  # type: ignore[method-assign]

    assert e._observe_baseline("base1") is None
    assert e._gate_baselines == {}
    e.run_quality_gates.assert_not_called()


def test_observe_baseline_refuses_an_unreadable_tree_state(
    mem_fs: dict[str, str],
) -> None:
    """A ``git status`` that will not run yields no baseline, and raises nothing.

    Unreadable is treated exactly like dirty — the same fail-safe
    ``UNATTRIBUTABLE`` a cache miss already produces. The seam warms a cache for a
    verdict that has a "no evidence" arm, so it must never abort the mechanical
    tend lap that calls it.
    """
    vcs = _baseline_vcs("base1")
    vcs.has_changes.side_effect = VCSError("git status failed")
    e, _m = _make(mem_fs, vcs=vcs, config=_observing_config())
    e.run_quality_gates = MagicMock()  # type: ignore[method-assign]

    assert e._observe_baseline("base1") is None
    assert e._gate_baselines == {}
    e.run_quality_gates.assert_not_called()


# ------------------------------------------------------------------
# Acceptance-criteria count gate (#3712)
# ------------------------------------------------------------------


def _agent_for_criteria_gate(
    mem_fs: dict[str, str], workdir: Path,
) -> tuple[object, Memory]:
    """Build a MaestroAgent over *workdir* with every external mocked."""
    from maestro.agent import MaestroAgent
    from maestro.contributions import ContributionTracker
    from maestro.executor import Executor
    from maestro.planner import Planner

    config = MagicMock()
    config.memory_dir = workdir
    config.workspace = workdir
    config.pid_path = workdir / "maestro.pid"
    config.enable_session_reuse = False
    config.sqlite_state_enabled = False
    config.max_implementation_attempts = 3
    config.auto_fix_max_rounds = 3

    m = Memory(workdir)
    c = Consciousness(workdir)
    j = Journal(workdir)
    n = NullNotifier()
    evolution = EvolutionEngine(
        m, c, j, n, issues=IssueTracker(github_repo=None), config=config,
    )
    # No PR already on the evolve branch (#4553 retry-door lookup).
    evolution.find_existing_pr = MagicMock(return_value=None)  # type: ignore[method-assign]
    vcs = MagicMock()
    vcs.has_changes.return_value = False
    agent = MaestroAgent(
        config=config,
        consciousness=c,
        journal=j,
        memory=m,
        executor=Executor(),
        planner=Planner(),
        evolution=evolution,
        notifier=n,
        contributions=ContributionTracker(workdir, workdir),
        vcs=vcs,
    )
    return agent, m


def _write_over_cap_plan(
    mem_fs: dict[str, str], workdir: Path, m: Memory, plan_id: str,
    *, count: int, status: str = "approved", failure_count: int = 0,
) -> None:
    """Persist a legacy plan carrying *count* criteria, bypassing the write door.

    ``save_evolution_plan`` refuses an over-cap list by design, so the legacy
    row is written the only way it can exist in the wild: already on disk.
    """
    import json

    plan = _approved_plan(
        plan_id=plan_id, status=status, failure_count=failure_count,
    )
    m.save_evolution_plan(plan)
    path = str(workdir / "evolution_plans.jsonl")
    rows = [
        json.loads(line)
        for line in mem_fs[path].splitlines() if line.strip()
    ]
    for row in rows:
        if row.get("plan_id") == plan_id:
            row["acceptance_criteria"] = [f"criterion {i}" for i in range(count)]
    mem_fs[path] = "".join(json.dumps(r) + "\n" for r in rows)


@pytest.mark.asyncio
async def test_implement_plan_disposes_over_cap_approved_plan(
    mem_fs: dict[str, str],
) -> None:
    """A legacy over-cap approved plan is retired obsolete, never implemented.

    The sole prompt renderer refuses an over-cap criteria list; without this
    gate that refusal would escape from deep inside ``implement`` and crash the
    loop phase every iteration. Disposal is zero-SDK and self-extinguishing —
    a human must re-scope the contract.
    """
    from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS

    d = Path("/fake/evoimpl_criteria_cap")
    agent, m = _agent_for_criteria_gate(mem_fs, d)
    _write_over_cap_plan(
        mem_fs, d, m, "plan-overcap",
        count=ACCEPTANCE_CRITERIA_MAX_ITEMS + 3,
    )

    with patch.object(
        agent.evolution, "implement", new_callable=AsyncMock,
    ) as impl:
        await agent.implement_plan("plan-overcap")

    # No SDK work was bought and no branch was cut.
    impl.assert_not_awaited()
    updated = m.get_plan("plan-overcap")
    assert updated is not None
    assert updated.status == "obsolete"
    assert str(ACCEPTANCE_CRITERIA_MAX_ITEMS) in (updated.obsolete_reason or "")


@pytest.mark.asyncio
async def test_implement_plan_disposes_over_cap_failed_plan(
    mem_fs: dict[str, str],
) -> None:
    """A ``failed`` over-cap plan is retired ``obsolete`` too (#4123).

    Left ``failed`` it would stay retry-eligible — the gate bumps no
    ``failure_count`` — and re-occupy the implementation slot forever.
    """
    from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS

    d = Path("/fake/evoimpl_criteria_cap_failed")
    agent, m = _agent_for_criteria_gate(mem_fs, d)
    _write_over_cap_plan(
        mem_fs, d, m, "plan-overcap-failed",
        count=ACCEPTANCE_CRITERIA_MAX_ITEMS + 1, status="failed",
    )

    with patch.object(
        agent.evolution, "implement", new_callable=AsyncMock,
    ) as impl:
        await agent.implement_plan("plan-overcap-failed")

    impl.assert_not_awaited()
    updated = m.get_plan("plan-overcap-failed")
    assert updated is not None
    assert updated.status == "obsolete"
    assert str(ACCEPTANCE_CRITERIA_MAX_ITEMS) in (updated.obsolete_reason or "")


@pytest.mark.asyncio
async def test_over_cap_failed_plan_releases_retry_slot(
    mem_fs: dict[str, str],
) -> None:
    """A disposed over-cap ``failed`` plan stops starving its failed siblings.

    With a cap of 1 and ascending ``failure_count`` ordering, the over-cap
    plan (count 1) is selected ahead of a normal failed plan (count 2). Once
    disposed it leaves the selector, so the next pass reaches the sibling, and
    the refusal is journaled exactly once.
    """
    from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS

    d = Path("/fake/evoimpl_criteria_cap_slot")
    agent, m = _agent_for_criteria_gate(mem_fs, d)
    config: Any = agent.config  # type: ignore[attr-defined]
    config.max_implementations_per_iteration = 1
    config.max_open_evolution_prs = 0
    _write_over_cap_plan(
        mem_fs, d, m, "overcap",
        count=ACCEPTANCE_CRITERIA_MAX_ITEMS + 1, status="failed",
        failure_count=1,
    )
    m.save_evolution_plan(
        _approved_plan(plan_id="normal", status="failed", failure_count=2),
    )

    real_implement_plan = agent.implement_plan  # type: ignore[attr-defined]
    reached: list[str] = []

    async def _spy(plan_id: str) -> None:
        reached.append(plan_id)
        if plan_id == "overcap":
            await real_implement_plan(plan_id)

    with (
        patch.object(agent, "_advance_slice_rollouts", new_callable=AsyncMock),
        patch.object(agent, "_wip_owns_slot", return_value=False),
        patch.object(agent, "_pre_mint_generation_verdict", return_value=None),
        patch.object(agent, "_slice_defers_implementation", return_value=False),
        patch(
            "maestro.loop.plan_holds.gate_environment_verdict",
            return_value=None,
        ),
        patch(
            "maestro.loop.plan_holds.plan_on_inherited_hold",
            return_value=False,
        ),
        patch.object(agent.evolution, "ensure_main_branch"),  # type: ignore[attr-defined]
        patch.object(agent, "implement_plan", side_effect=_spy),
    ):
        await agent._implement_approved_plans()  # type: ignore[attr-defined]
        overcap = m.get_plan("overcap")
        assert overcap is not None
        assert overcap.status == "obsolete"
        await agent._implement_approved_plans()  # type: ignore[attr-defined]

    assert reached == ["overcap", "normal"]
    notes = [
        e for e in agent.journal.read_events_by_type("evolution_note")  # type: ignore[attr-defined]
        if e.triggered_by == "criteria_cap_gate:overcap"
    ]
    assert len(notes) == 1


@pytest.mark.asyncio
async def test_over_cap_disposal_logs_error_when_write_does_not_persist(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused ``mark_plan_obsolete`` is logged loudly, not reported done."""
    from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS

    d = Path("/fake/evoimpl_criteria_cap_nopersist")
    agent, m = _agent_for_criteria_gate(mem_fs, d)
    _write_over_cap_plan(
        mem_fs, d, m, "plan-stuck",
        count=ACCEPTANCE_CRITERIA_MAX_ITEMS + 1,
    )

    with (
        patch.object(m, "mark_plan_obsolete", return_value=False),
        patch.object(
            agent.evolution, "implement", new_callable=AsyncMock,
        ) as impl,
        caplog.at_level(logging.ERROR, logger="maestro"),
    ):
        await agent.implement_plan("plan-stuck")

    impl.assert_not_awaited()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "plan-stuck" in errors[0].getMessage()
    assert "approved" in errors[0].getMessage()


def test_dispose_over_cap_plan_logs_missing_plan(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A plan that vanished before the flip is named ``missing`` in the log."""
    from maestro.loop.plan_refusal import dispose_over_cap_plan
    from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS

    plan = MagicMock()
    plan.plan_id = "gone"
    plan.acceptance_criteria = ["c"] * (ACCEPTANCE_CRITERIA_MAX_ITEMS + 1)
    memory = MagicMock()
    memory.mark_plan_obsolete.return_value = False
    memory.get_plan.return_value = None

    with caplog.at_level(logging.ERROR, logger="maestro"):
        refused = dispose_over_cap_plan(
            plan, memory=memory, journal=MagicMock(),
        )

    assert refused is True
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "missing" in errors[0].getMessage()


@pytest.mark.parametrize("status", ["rejected", "implemented", "blocked"])
def test_dispose_over_cap_plan_concurrent_terminal_move_is_benign(
    caplog: pytest.LogCaptureFixture, status: str,
) -> None:
    """A plan a concurrent writer moved out of the retry pool logs INFO.

    The refusal is not journaled either: only a persisted flip journals.
    """
    from maestro.loop.plan_refusal import dispose_over_cap_plan
    from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS

    plan = MagicMock()
    plan.plan_id = "raced"
    plan.acceptance_criteria = ["c"] * (ACCEPTANCE_CRITERIA_MAX_ITEMS + 1)
    memory = MagicMock()
    memory.mark_plan_obsolete.return_value = False
    memory.get_plan.return_value = MagicMock(status=status)
    journal = MagicMock()

    with caplog.at_level(logging.INFO, logger="maestro"):
        refused = dispose_over_cap_plan(
            plan, memory=memory, journal=journal,
        )

    assert refused is True
    journal.record_event.assert_not_called()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == []
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert status in infos[0].getMessage()


@pytest.mark.asyncio
async def test_implement_plan_admits_at_cap_plan(mem_fs: dict[str, str]) -> None:
    """An at-cap plan passes the gate — the bound is inclusive."""
    from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS

    d = Path("/fake/evoimpl_criteria_at_cap")
    agent, m = _agent_for_criteria_gate(mem_fs, d)
    plan = _approved_plan(
        plan_id="plan-atcap",
        acceptance_criteria=[
            f"criterion {i}" for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS)
        ],
    )
    m.save_evolution_plan(plan)

    with patch.object(
        agent.evolution, "implement", new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ), patch.object(agent.evolution, "cleanup_branch"), \
            pytest.raises(RuntimeError, match="boom"):
        await agent.implement_plan("plan-atcap")

    # Reached implementation (and failed there) rather than being disposed.
    updated = m.get_plan("plan-atcap")
    assert updated is not None
    assert updated.status == "failed"


def test_over_cap_criteria_reason_boundary(mem_fs: dict[str, str]) -> None:
    """The reason is ``None`` at the cap and names the count above it."""
    from maestro.loop.plan_refusal import over_cap_criteria_reason
    from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS

    at_cap = _approved_plan(
        acceptance_criteria=[
            f"c{i}" for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS)
        ],
    )
    assert over_cap_criteria_reason(at_cap) is None

    over_cap = _approved_plan(
        acceptance_criteria=[
            f"c{i}" for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS + 1)
        ],
    )
    reason = over_cap_criteria_reason(over_cap)
    assert reason is not None
    assert str(ACCEPTANCE_CRITERIA_MAX_ITEMS + 1) in reason
    assert str(ACCEPTANCE_CRITERIA_MAX_ITEMS) in reason


def test_journal_plan_refusal_survives_a_journal_failure(
    mem_fs: dict[str, str], caplog: Any,
) -> None:
    """A journal-write failure logs loudly and never reaches the caller.

    The refusal has already been decided and persisted by the time this runs,
    so raising here would turn a clean pre-flight refusal into a loop crash.
    """
    import logging

    from maestro.loop.plan_refusal import journal_plan_refusal

    j = MagicMock()
    j.record_event.side_effect = OSError("journal is gone")

    with caplog.at_level(logging.WARNING):
        journal_plan_refusal(
            j, content="refused", triggered_by="scope_gate:ep1",
        )

    assert any(
        "plan-refusal journal entry" in r.getMessage()
        and "scope_gate:ep1" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_self_review_own_session_routes_through_single_use_call(
    mem_fs: dict[str, str],
) -> None:
    """Flag on, no session: every round runs via ``single_use_call`` with the
    plan's action-gate hooks and the fence-free prompt, never bare ``query``."""
    vcs = MagicMock()
    vcs.has_changes.side_effect = [True, False]
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+new line"
    e, _m = _make_with_review(mem_fs, vcs=vcs)
    e._config = Config(
        self_review_max_rounds=2, self_review_own_session_enabled=True,
    )
    plan = _approved_plan()
    gate = {"PreToolUse": ["gate"]}

    with (
        patch.object(e, "action_gate_hooks", return_value=gate) as mock_gate,
        patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q,
        patch(
            "maestro.evolution_quality.single_use_call", new_callable=AsyncMock,
        ) as mock_single,
    ):
        mock_single.side_effect = [
            MockQueryResult(response="Found issues, fixed"),
            MockQueryResult(response="LGTM"),
        ]
        result = await e.self_review(plan, base_hash="base123")

    assert result is True
    mock_q.assert_not_called()
    assert mock_single.await_count == 2
    vcs.commit.assert_called_once_with("maestro: self-review fixes (round 1)")
    call = mock_single.await_args
    assert call.args[0] == "self_review"
    assert "you just completed" not in call.args[1]
    opts = call.kwargs["options"]
    assert opts.hooks is gate
    mock_gate.assert_called_with(plan)
    assert opts.effort is None
    assert opts.caller is None
    assert opts.cwd == str(e._config.workspace)


@pytest.mark.asyncio
async def test_self_review_own_session_flag_ignored_with_session(
    mem_fs: dict[str, str],
) -> None:
    """A supplied session still takes the same-session arm with the flag on."""
    vcs = MagicMock()
    vcs.has_changes.return_value = False
    vcs.get_head_hash.return_value = "base123"
    vcs.get_diff.return_value = "diff --git a/foo.py b/foo.py\n+new line"
    e, _m = _make_with_review(mem_fs, vcs=vcs)
    e._config = Config(
        self_review_max_rounds=2, self_review_own_session_enabled=True,
    )
    session = MagicMock()

    with (
        patch(
            "maestro.evolution_quality.query_on_session", new_callable=AsyncMock,
        ) as mock_qos,
        patch(
            "maestro.evolution_quality.single_use_call", new_callable=AsyncMock,
        ) as mock_single,
    ):
        mock_qos.return_value = MockQueryResult(response="LGTM")
        result = await e.self_review(_approved_plan(), "base123", session=session)

    assert result is True
    mock_single.assert_not_called()
    assert "you just completed" in mock_qos.await_args.args[1]
