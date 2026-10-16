"""Tests for the merge-readiness check at task completion.

Covers ``contributions.check_merge_clean`` (the zero-side-effect
``git merge-tree`` probe) and its wiring into ``MaestroAgent.run`` —
the ``merge_clean`` task-history field plus the auto-promotion
``next_task_directive`` bridge for cleanly-mergeable branches.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.contributions import check_merge_clean
from tests.conftest import MockQueryResult

# ── check_merge_clean — return-path coverage ──────────────────────────


def test_clean_merge_returns_true() -> None:
    """exit 0 → clean merge → True."""
    completed = MagicMock(returncode=0)
    with patch(
        "maestro.contributions.subprocess.run", return_value=completed,
    ) as run:
        result = check_merge_clean("maestro/task/abc")
    assert result is True
    # The probe is the side-effect-free write-tree form against origin/main.
    args = run.call_args.args[0]
    assert args == [
        "git", "merge-tree", "--write-tree", "origin/main", "maestro/task/abc",
    ]


def test_conflict_returns_false() -> None:
    """exit 1 → merge conflicts → False."""
    completed = MagicMock(returncode=1)
    with patch(
        "maestro.contributions.subprocess.run", return_value=completed,
    ):
        result = check_merge_clean("maestro/task/abc")
    assert result is False


def test_timeout_returns_none() -> None:
    """TimeoutExpired → unknown → None."""
    with patch(
        "maestro.contributions.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        result = check_merge_clean("maestro/task/abc")
    assert result is None


def test_os_error_returns_none() -> None:
    """OSError (e.g. git missing) → unknown → None."""
    with patch(
        "maestro.contributions.subprocess.run",
        side_effect=OSError("no git"),
    ):
        result = check_merge_clean("maestro/task/abc")
    assert result is None


def test_unexpected_exit_returns_none() -> None:
    """Any other exit code (e.g. 128 — bad ref) → unknown → None."""
    completed = MagicMock(returncode=128)
    with patch(
        "maestro.contributions.subprocess.run", return_value=completed,
    ):
        result = check_merge_clean("maestro/task/abc", target="origin/dev")
    assert result is None


def test_probe_runs_in_configured_workspace() -> None:
    """🐛 #2640: the probe must scope git to the caller's workspace.

    Without a ``cwd`` argument, ``git merge-tree`` runs in the process's
    launch directory, not ``config.workspace``. When the two differ (a
    supported TOML configuration) the probe runs against the wrong dir /
    a non-repo → exit 128 → ``None`` → no branch is ever flagged clean
    and the whole promotion feature silently no-ops. The probe must
    accept a ``cwd`` and forward it to ``subprocess.run``.
    """
    completed = MagicMock(returncode=0)
    with patch(
        "maestro.contributions.subprocess.run", return_value=completed,
    ) as run:
        result = check_merge_clean("maestro/task/abc", cwd="/configured/workspace")
    assert result is True
    assert run.call_args.kwargs["cwd"] == "/configured/workspace"


def test_unicode_decode_error_returns_none() -> None:
    """💡 #2640: UnicodeDecodeError must be swallowed as unknown → None.

    ``subprocess.run(..., text=True)`` decodes git output with the locale
    encoding and can raise ``UnicodeDecodeError`` (a subclass of
    ``ValueError``, NOT ``OSError``) — e.g. a non-ASCII conflicting path,
    or any non-ASCII bytes under a POSIX/ASCII locale. The docstring
    promises the probe returns ``None`` for every failure mode and never
    lets an error escape; today this exception slips past
    ``except (TimeoutExpired, OSError)`` and propagates out of ``run()``,
    skipping the task record and crashing the iteration.
    """
    with patch(
        "maestro.contributions.subprocess.run",
        side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
    ):
        result = check_merge_clean("maestro/task/abc")
    assert result is None


# ── agent.run wiring ──────────────────────────────────────────────────


def _patch_queries():  # type: ignore[no-untyped-def]
    """Patch planner/executor/agent SDK calls for a one-step success run."""
    plan_response = json.dumps([{"description": "Step 1"}])
    plan = patch("maestro.planner.query", new_callable=AsyncMock)
    exec_ = patch("maestro.executor.query", new_callable=AsyncMock)
    agent_q = patch("maestro.agent.query", new_callable=AsyncMock)
    return plan_response, plan, exec_, agent_q


@pytest.mark.asyncio
async def test_merge_clean_field_in_task_history(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A successful kept-branch run records the mocked merge_clean verdict."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    plan_response, plan, exec_, agent_q = _patch_queries()

    with plan as mock_plan, exec_ as mock_exec, agent_q, patch(
        "maestro.loop.task_delivery.check_merge_clean", return_value=True,
    ) as mock_check:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step done")
        record = await agent.run("goal with branch")

    assert record.outcome == "success"
    assert record.branch is not None
    # Scoped to the configured workspace, not the process launch dir (#2640).
    mock_check.assert_called_once_with(
        record.branch, cwd=str(agent.config.workspace),
    )
    assert record.merge_clean is True
    # And the field round-trips through task_history.jsonl.
    persisted = agent.memory.get_task_record(record.task_id)
    assert persisted is not None
    assert persisted.merge_clean is True


@pytest.mark.asyncio
async def test_directive_set_on_clean(make_agent) -> None:  # type: ignore[no-untyped-def]
    """merge_clean True + empty directive → promotion hint is queued."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    # Precondition: no directive queued.
    model = agent.consciousness.load_self_model()
    assert not model.next_task_directive
    plan_response, plan, exec_, agent_q = _patch_queries()

    with plan as mock_plan, exec_ as mock_exec, agent_q, patch(
        "maestro.loop.task_delivery.check_merge_clean", return_value=True,
    ):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step done")
        record = await agent.run("goal with branch")

    updated = agent.consciousness.load_self_model()
    assert updated.next_task_directive == (
        f"Branch {record.branch} merges cleanly — "
        f"promote via source_task_id={record.task_id}"
    )


@pytest.mark.asyncio
async def test_directive_preserved_when_set(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An already-populated directive is never overwritten by the bridge."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "existing directive"
    agent.consciousness.save_self_model(model)
    plan_response, plan, exec_, agent_q = _patch_queries()

    with plan as mock_plan, exec_ as mock_exec, agent_q, patch(
        "maestro.loop.task_delivery.check_merge_clean", return_value=True,
    ):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step done")
        await agent.run("goal with branch")

    updated = agent.consciousness.load_self_model()
    assert updated.next_task_directive == "existing directive"


@pytest.mark.asyncio
async def test_directive_skipped_when_promotion_pressure_disabled(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """Proposal 116, step 6: with the branch-promotion-pressure master switch off,
    a cleanly-mergeable kept branch writes no promotion directive, even though the
    slot is empty (the empty-slot guard is preserved only for the flag-on path)."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    agent.config.branch_promotion_pressure_enabled = False
    model = agent.consciousness.load_self_model()
    assert not model.next_task_directive
    plan_response, plan, exec_, agent_q = _patch_queries()

    with plan as mock_plan, exec_ as mock_exec, agent_q, patch(
        "maestro.loop.task_delivery.check_merge_clean", return_value=True,
    ):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step done")
        record = await agent.run("goal with branch")

    assert record.merge_clean is True
    updated = agent.consciousness.load_self_model()
    assert not updated.next_task_directive


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [False, None])
async def test_directive_untouched_when_not_clean(  # type: ignore[no-untyped-def]
    make_agent, verdict,
) -> None:
    """✏️ #2640: a non-True verdict + empty directive leaves it empty.

    Acceptance criterion 3 requires the promotion directive to be set
    *only* when ``merge_clean`` is exactly ``True``. The existing tests
    only cover the ``True`` paths; a regression such as
    ``if merge_clean is not None`` would fabricate a bogus promotion
    directive on conflicting (``False``) or unknown (``None``) branches
    and nothing would catch it. Start from an empty directive, run a
    successful kept-branch task whose probe returns ``False``/``None``,
    and assert the directive stays empty.
    """
    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    # Precondition: no directive queued.
    model = agent.consciousness.load_self_model()
    assert not model.next_task_directive
    plan_response, plan, exec_, agent_q = _patch_queries()

    with plan as mock_plan, exec_ as mock_exec, agent_q, patch(
        "maestro.loop.task_delivery.check_merge_clean", return_value=verdict,
    ):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step done")
        record = await agent.run("goal with branch")

    assert record.merge_clean is verdict
    updated = agent.consciousness.load_self_model()
    assert not updated.next_task_directive
