"""Tests for ``MaestroAgent._run_task_phase`` and the ``run_task`` loop phase."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.agent import MaestroAgent
from maestro.audit_checks import classify_task_shape
from maestro.consciousness import PendingPRRequest
from maestro.escalation_bridge import has_standdown
from maestro.loop.pr_remediation import PrRemediationController
from maestro.memory import TaskRecord
from maestro.pr_ledger import PRLedger
from maestro.pr_remediation_state import PrRemediationState
from maestro.remediation_tracker import (
    MERGE_BLOCK_BEHIND_MAIN,
    MERGE_BLOCK_CONFLICTING,
    JsonlStandDownVerdictLedger,
    find_stand_down,
    record_stand_down,
)
from maestro.task_queue import QueuedTask
from maestro.vcs import PrMergeState, VCSError


@pytest.mark.asyncio
async def test_run_task_phase_drains_user_request_first(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["user goal one"]
    model.pending_pr_requests = [
        PendingPRRequest(directive="pr goal", pr_branch="maestro/evolve/p1"),
    ]
    agent.consciousness.save_self_model(model)
    agent.task_queue.save([QueuedTask(task_id="q1", goal="queue goal")])
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0] == "user goal one"
    assert kwargs["source"] == "external"
    assert kwargs["context"] == "external"
    assert kwargs["linked_question"] is None
    # User-request drain forwards ``pr_branch=None``.
    assert kwargs["pr_branch"] is None

    # Source FIFO was drained head-first BEFORE run() — verify with the
    # post-run on-disk state.
    after = agent.consciousness.load_self_model()
    assert after.pending_user_requests == []
    assert after.pending_pr_requests == [
        PendingPRRequest(directive="pr goal", pr_branch="maestro/evolve/p1"),
    ]
    assert [q.task_id for q in agent.task_queue.load()] == ["q1"]


def _open_pr_state(number: int, head_ref: str) -> PrMergeState:
    """A minimal open ``PrMergeState`` for the human-PR-message routing tests."""
    return PrMergeState(
        number=number,
        head_ref=head_ref,
        title=f"PR {number}",
        body="body",
        state="OPEN",
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision="",
        created_at="2026-06-16T00:00:00Z",
        updated_at="2026-06-16T00:00:00Z",
        url=f"https://github.com/o/r/pull/{number}",
    )


def _seed_pr_suppressions(
    memory_dir: Path, mem_fs: dict[str, str], pr: int, head_sha: str,
) -> None:
    """Seed both suppression stores for *pr* so a release is observable."""
    record_stand_down(
        pr, head_sha, JsonlStandDownVerdictLedger(memory_dir),
        now="2026-06-16T00:00:00+00:00",
    )
    mem_fs[str(memory_dir / "escalation_handoffs.json")] = json.dumps({
        str(pr): {
            "head_sha": head_sha,
            "delivered_at": "2026-06-16T00:00:00+00:00",
            "serviced_comment_ids": [],
        },
    })


@pytest.mark.asyncio
async def test_run_task_phase_user_pr_message_routes_to_remediation(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A user message naming an open own PR is reshaped into a PR-remediation
    run (``Remediate PR #<n>:`` goal + the PR branch) and both suppression
    stores are released before the run."""
    agent = make_agent()
    agent.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "maestro/evolve/ep42": _open_pr_state(2884, "maestro/evolve/ep42"),
        },
    )
    memory_dir = agent.config.memory_dir
    _seed_pr_suppressions(memory_dir, mem_fs, 2884, "sha1")
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["PR #2884 has unresolved comments"]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0].startswith("Remediate PR #2884:")
    assert kwargs["pr_branch"] == "maestro/evolve/ep42"
    assert kwargs["source"] == "external"
    # Both suppression stores released before the run.
    assert find_stand_down(2884, "sha1", agent.stand_down_verdicts) is None
    assert has_standdown(agent.escalation_standdowns, 2884) is False


@pytest.mark.asyncio
async def test_run_task_phase_user_pr_message_flag_off_generic_path(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """With the flag off the message runs as a generic task (no branch, no
    header rewrite) and the suppression stores are left intact."""
    agent = make_agent()
    agent.config.user_pr_message_remediation_enabled = False
    agent.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "maestro/evolve/ep42": _open_pr_state(2884, "maestro/evolve/ep42"),
        },
    )
    memory_dir = agent.config.memory_dir
    _seed_pr_suppressions(memory_dir, mem_fs, 2884, "sha1")
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["PR #2884 has unresolved comments"]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0] == "PR #2884 has unresolved comments"
    assert kwargs["pr_branch"] is None
    agent.evolution.code_host.get_open_evolution_pr_states.assert_not_called()
    # Stores untouched — the flag never reached the router.
    assert find_stand_down(2884, "sha1", agent.stand_down_verdicts) is not None
    assert has_standdown(agent.escalation_standdowns, 2884) is True


@pytest.mark.asyncio
async def test_run_task_phase_user_pr_message_settled_pr_generic_path(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A message naming a PR that is not an open own PR falls through to the
    generic path with the stores left intact."""
    agent = make_agent()
    # No open own PR matches #2884 (it settled / is foreign).
    agent.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        return_value={},
    )
    memory_dir = agent.config.memory_dir
    _seed_pr_suppressions(memory_dir, mem_fs, 2884, "sha1")
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["PR #2884 has unresolved comments"]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0] == "PR #2884 has unresolved comments"
    assert kwargs["pr_branch"] is None
    assert find_stand_down(2884, "sha1", agent.stand_down_verdicts) is not None
    assert has_standdown(agent.escalation_standdowns, 2884) is True


@pytest.mark.asyncio
async def test_run_task_phase_user_message_without_pr_generic_path(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A user message naming no PR runs generically without consulting the
    host."""
    agent = make_agent()
    agent.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        return_value={},
    )
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["please improve the journal formatting"]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0] == "please improve the journal formatting"
    assert kwargs["pr_branch"] is None
    agent.evolution.code_host.get_open_evolution_pr_states.assert_not_called()


@pytest.mark.asyncio
async def test_run_task_phase_falls_through_to_pr_request(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="pr goal alpha", pr_branch="maestro/evolve/p2",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.task_queue.save([QueuedTask(task_id="q1", goal="queue goal")])
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0] == "pr goal alpha"
    assert kwargs["source"] == "external"
    assert kwargs["context"] == "external"
    # PR-remediation drain forwards the entry's ``pr_branch``.
    assert kwargs["pr_branch"] == "maestro/evolve/p2"

    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == []
    # Queue not touched — PR request popped first.
    assert [q.task_id for q in agent.task_queue.load()] == ["q1"]


@pytest.mark.asyncio
async def test_run_task_phase_pr_request_with_none_branch(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A legacy-shaped PR entry (no pr_branch) forwards ``pr_branch=None``."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [PendingPRRequest(directive="legacy pr goal")]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    _, kwargs = agent.run.await_args
    assert kwargs["pr_branch"] is None


@pytest.mark.asyncio
async def test_run_task_phase_drains_multi_kb_pr_goal_intact(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A multi-KB rich PR-remediation prompt drains and reaches
    :meth:`run` unmodified (no truncation, no header rewrite).

    Regression for the case where ``pending_pr_requests`` carries the
    multi-section prompt composed by
    :meth:`MaestroAgent._route_pr_feedback_to_pending_requests`.
    """
    agent = make_agent()
    rich_goal = (
        "Remediate PR #42: Refactor planner\n\n"
        "## Problem\n"
        "A reviewer requested changes...\n\n"
        "## Original Evolution Plan\n"
        f"- Rationale: {'X' * 2000}\n\n"
        "## Original PR Description\n"
        f"{'Y' * 1500}\n\n"
        "## Remediation Guidance\n"
        "Branch: maestro/evolve/ep42\n"
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=rich_goal,
            pr_branch="maestro/evolve/ep42",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0] == rich_goal
    assert kwargs["pr_branch"] == "maestro/evolve/ep42"


@pytest.mark.asyncio
async def test_run_task_phase_falls_through_to_queue_head(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    agent.task_queue.save([
        QueuedTask(
            task_id="first",
            goal="explore the codebase",
            linked_question="What am I?",
            workspace_moment_id="cm-77",
            workspace_item_ids=["wi-1", "wi-2"],
        ),
        QueuedTask(task_id="second", goal="other"),
    ])
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0] == "explore the codebase"
    assert kwargs["source"] == "self_directed"
    assert kwargs["context"] == "self_directed"
    assert kwargs["linked_question"] == "What am I?"
    # Queue-head drain forwards ``pr_branch=None``.
    assert kwargs["pr_branch"] is None
    # The conscious-access citation (proposal 130) on the popped head is
    # forwarded to ``run`` so it rides onto the completed TaskRecord.
    assert kwargs["workspace_moment_id"] == "cm-77"
    assert kwargs["workspace_item_ids"] == ["wi-1", "wi-2"]

    remaining = agent.task_queue.load()
    assert [q.task_id for q in remaining] == ["second"]


@pytest.mark.asyncio
async def test_run_task_phase_attaches_premise_report_via_context(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A self-directed goal citing a missing ``.py`` path gets a premise block.

    The block rides the advisory ``context`` channel handed to :meth:`run`
    (which reaches the planner) — advisory, not a drop — so the drifted
    premise is surfaced without touching ``goal`` (#2336).
    """
    agent = make_agent()
    original_goal = "Tighten the retry backoff in maestro/does_not_exist.py"
    agent.task_queue.save([
        QueuedTask(task_id="q1", goal=original_goal),
    ])
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert kwargs["source"] == "self_directed"
    # The report rides ``context`` (planner-only, never recorded), naming the
    # missing path and preserving the ``self_directed`` context prefix.
    passed_context = kwargs["context"]
    assert passed_context.startswith("self_directed")
    assert "## PREMISE REPORT" in passed_context
    assert "- maestro/does_not_exist.py" in passed_context
    # ``goal`` — recorded verbatim as ``TaskRecord.goal`` — stays clean: no
    # premise boilerplate leaks into it.
    passed_goal = args[0]
    assert passed_goal == original_goal
    assert "## PREMISE REPORT" not in passed_goal


@pytest.mark.asyncio
async def test_run_task_phase_premise_report_does_not_flip_recorded_shape(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The premise report must not perturb the recorded goal's shape (#2336).

    Regression: the report boilerplate contains the word "create", which
    ``classify_task_shape`` matches as ``construction``. Were the report
    concatenated into the recorded ``goal``, the goal-shape sensors
    (``format_task_shape_summary`` / ``predict_task_outcome``, both keyed on
    ``TaskRecord.goal``) would durably misclassify affected tasks. Routing it
    through ``context`` keeps the recorded goal — and its classified shape —
    unchanged.
    """
    agent = make_agent()
    original_goal = "Tighten the retry backoff in maestro/does_not_exist.py"
    # The bare goal is not construction-shaped; the report boilerplate is.
    assert classify_task_shape(original_goal) != "construction"
    agent.task_queue.save([
        QueuedTask(task_id="q1", goal=original_goal),
    ])
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    args, _ = agent.run.await_args
    recorded_goal = args[0]
    assert classify_task_shape(recorded_goal) == classify_task_shape(original_goal)
    assert classify_task_shape(recorded_goal) != "construction"


@pytest.mark.asyncio
async def test_run_task_phase_no_premise_report_when_files_present(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A self-directed goal whose referenced files all exist is passed through."""
    agent = make_agent()
    workspace = str(agent.config.workspace)
    mem_fs[f"{workspace}/maestro/sdk.py"] = "x = 1\n"
    agent.task_queue.save([
        QueuedTask(
            task_id="q1",
            goal="Tighten the retry backoff in maestro/sdk.py",
        ),
    ])
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert kwargs["source"] == "self_directed"
    assert args[0] == "Tighten the retry backoff in maestro/sdk.py"
    assert "## PREMISE REPORT" not in args[0]
    # No missing files → the context channel carries only the bare source.
    assert kwargs["context"] == "self_directed"


@pytest.mark.asyncio
async def test_run_task_phase_no_premise_report_for_pr_request(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A non-self-directed (PR-request) head is never given a premise block.

    Even though its directive references a missing ``.py`` path, the
    ``source == "self_directed"`` guard leaves external goals untouched.
    """
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Fix maestro/does_not_exist.py",
            pr_branch="maestro/evolve/p9",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert kwargs["source"] == "external"
    assert args[0] == "Fix maestro/does_not_exist.py"
    assert "## PREMISE REPORT" not in args[0]
    # The premise guard is scoped to ``self_directed``; external context is bare.
    assert kwargs["context"] == "external"


def _partial_lap(pr: int, blocker: str) -> TaskRecord:
    """Build a ``partial`` remediation-lap record for *pr* carrying *blocker*.

    Mirrors the ``result_summary`` ``run()`` stamps on an unlanded lap:
    ``(merge blocker: <CODE>)`` — the field
    :meth:`MaestroAgent._mechanical_standdown_verdict` parses the blocker from.
    A real :class:`TaskRecord` (not a stand-in) is returned so the same mocked
    ``get_recent_tasks`` also feeds the outcome-prediction ``asdict`` path on
    the admit branches without tripping its dataclass requirement.
    """
    return TaskRecord(
        task_id=f"t{pr}",
        goal=f"Remediate PR #{pr}: resolve conflicts",
        plan=[],
        outcome="partial",
        result_summary=(
            f"Resolved on branch, awaiting tend push — PR #{pr} not yet "
            f"landed (merge blocker: {blocker})."
        ),
        errors=[],
        duration_secs=1.0,
        timestamp="2026-06-21T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_run_task_phase_owned_lineage_routes_as_remediation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A lapped owned (CONFLICTING) lineage that clears the dequeue gates is
    served as a remediation task — not diverted to a directive (proposal 092).

    The owned-relap divert is retired: finishing the owned PR outranks handing
    control back to task generation, where the ``wip_primacy`` gate would name
    the same PR anyway. A CONFLICTING lineage with prior ``partial`` laps that
    passes ``dequeue_gates`` now routes straight through to ``run`` — no
    ``next_task_directive`` write and no early return."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.next_task_directive = ""
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #2727: resolve conflicts",
            pr_branch="maestro/evolve/p2727",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent._pr_remediation.dequeue_gates = AsyncMock(  # type: ignore[method-assign]
        return_value=False,
    )
    # A CONFLICTING (owned) lineage with multiple prior partial laps — the exact
    # shape the retired divert converted into a reconcile-or-close directive. It
    # must now route as an ordinary remediation task instead.
    agent.memory.get_recent_tasks = MagicMock(  # type: ignore[method-assign]
        return_value=[
            _partial_lap(2727, MERGE_BLOCK_CONFLICTING),
            _partial_lap(2727, MERGE_BLOCK_CONFLICTING),
        ],
    )
    agent.run = AsyncMock()  # type: ignore[method-assign]
    agent._record_remediation_outcome = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    # Routed as remediation: run() executed the goal, the entry was popped, and
    # no reconcile-or-close directive was written.
    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0] == "Remediate PR #2727: resolve conflicts"
    assert kwargs["source"] == "external"
    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == []
    assert after.next_task_directive == ""


@pytest.mark.asyncio
async def test_run_task_phase_noop_when_no_runnable_work(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_task_phase_skips_whitespace_only_goal(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A pending request that is whitespace-only is drained but not executed."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["   "]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_not_awaited()
    # The whitespace-only request was still drained off the FIFO so the next
    # iteration doesn't keep stalling on it.
    after = agent.consciousness.load_self_model()
    assert after.pending_user_requests == []


@pytest.mark.asyncio
async def test_run_task_phase_persists_drain_before_run(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A crash inside ``run()`` must not let the same goal re-fire."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["urgent thing"]
    agent.consciousness.save_self_model(model)

    seen_during_run: list[list[str]] = []

    async def _record_then_raise(*_a, **_k):  # type: ignore[no-untyped-def]
        # Read the persisted self-model from inside run() to confirm the
        # drain landed before execution started.
        seen_during_run.append(
            list(agent.consciousness.load_self_model().pending_user_requests),
        )
        raise RuntimeError("boom")

    agent.run = AsyncMock(side_effect=_record_then_raise)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await agent._run_task_phase()

    assert seen_during_run == [[]]
    after = agent.consciousness.load_self_model()
    assert after.pending_user_requests == []


def test_has_runnable_work_true_for_user_request(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["x"]
    agent.consciousness.save_self_model(model)
    assert agent._has_runnable_work() is True


def test_has_runnable_work_true_for_pr_request(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [PendingPRRequest(directive="fix pr")]
    agent.consciousness.save_self_model(model)
    assert agent._has_runnable_work() is True


def test_has_runnable_work_true_for_queue(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    agent.task_queue.save([QueuedTask(task_id="x", goal="g")])
    assert agent._has_runnable_work() is True


def test_has_runnable_work_false_when_all_empty(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    assert agent._has_runnable_work() is False


def test_phase_will_run_run_task_gated_by_runnable_work(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    # No work — phase should be skipped.
    assert (
        agent._phase_will_run(
            "run_task",
            iteration=1, smoke=False, first_iteration_after_restart=False,
            task_every=3, evolve_every=5, dream_every=10,
            audit_self_every=10,
        )
        is False
    )
    # Add work — phase should run.
    agent.task_queue.save([QueuedTask(task_id="q", goal="g")])
    assert (
        agent._phase_will_run(
            "run_task",
            iteration=1, smoke=False, first_iteration_after_restart=False,
            task_every=3, evolve_every=5, dream_every=10,
            audit_self_every=10,
        )
        is True
    )


def test_phase_will_run_run_task_skipped_under_smoke(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    agent.task_queue.save([QueuedTask(task_id="q", goal="g")])
    assert (
        agent._phase_will_run(
            "run_task",
            iteration=1, smoke=True, first_iteration_after_restart=False,
            task_every=3, evolve_every=5, dream_every=10,
            audit_self_every=10,
        )
        is False
    )


def test_phase_will_run_run_task_skipped_under_budget_throttle(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    agent.task_queue.save([QueuedTask(task_id="q", goal="g")])
    # Force the union throttle on. ``budget_throttle_active`` returns True
    # when any of its inputs are active; setting the burn-rate entry stamp
    # is the cheapest mock.
    agent._budget_throttle_entered_at = "2025-01-01T00:00:00+00:00"
    assert (
        agent._phase_will_run(
            "run_task",
            iteration=1, smoke=False, first_iteration_after_restart=False,
            task_every=3, evolve_every=5, dream_every=10,
            audit_self_every=10,
        )
        is False
    )


@pytest.mark.asyncio
async def test_run_loop_fires_run_task_each_iteration_with_pending_user_request(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A queued user request must execute even when task_every cadence forbids generate_task."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["urgent goal"]
    agent.consciousness.save_self_model(model)

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock()  # type: ignore[method-assign]
    agent.run = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # task_every=999 ensures generate_task is NOT called on the single
    # iteration; run_task must still fire because pending_user_requests
    # has content.
    await agent.run_loop(
        interval=0, max_iters=1, task_every=999, evolve_every=999,
    )

    agent.generate_task.assert_not_called()
    agent.run.assert_awaited_once()
    args, _ = agent.run.await_args
    assert args[0] == "urgent goal"


@pytest.mark.asyncio
async def test_run_task_phase_forwards_queued_acceptance_criteria(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A queued task carrying ``acceptance_criteria`` must forward them into
    ``MaestroAgent.run`` so the criteria-anchored outcome-classification path
    (``_assess_criteria_outcome``) is reachable from the only production task
    entry point. Without this forwarding the criteria gate is dead code at
    runtime — exactly the regression this test pins down."""
    agent = make_agent()
    criteria = [
        "Feature X is implemented in module.py",
        "Tests cover all new branches",
    ]
    agent.task_queue.save([
        QueuedTask(
            task_id="crit-1",
            goal="implement feature X",
            acceptance_criteria=criteria,
        ),
    ])
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    _args, kwargs = agent.run.await_args
    assert kwargs["acceptance_criteria"] == criteria
    assert kwargs["source"] == "self_directed"


def test_run_task_phase_references_acceptance_criteria_in_source() -> None:
    """Static guard: ``_run_task_phase`` must mention ``acceptance_criteria``
    in its body. The criteria-anchored classification path in ``agent.run()``
    is only reachable in production when ``_run_task_phase`` forwards the
    kwarg. This guard pins the plumbing in place against silent regressions
    (the original defect was exactly this kwarg going missing)."""
    src = inspect.getsource(MaestroAgent._run_task_phase)
    assert "acceptance_criteria" in src, (
        "_run_task_phase has no acceptance_criteria reference — "
        "the criteria-anchored outcome path added to agent.run() is "
        "unreachable from the only production task entry point."
    )


@pytest.mark.asyncio
async def test_run_task_phase_consumes_queue_head_on_completion(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The popped queue head is consumed (not restored) once ``run`` returns —
    graceful shutdown no longer re-queues a task; ``run_task`` runs the task to
    completion like every other phase (084, retiring 057)."""
    agent = make_agent()
    agent.task_queue.save([
        QueuedTask(task_id="head", goal="the goal"),
        QueuedTask(task_id="behind", goal="other goal"),
    ])

    async def _run(*_a, **_k):  # type: ignore[no-untyped-def]
        return MagicMock(outcome="success", result_summary="", key_findings="")

    agent.run = _run  # type: ignore[method-assign]

    await agent._run_task_phase()

    assert [q.task_id for q in agent.task_queue.load()] == ["behind"]


@pytest.mark.asyncio
async def test_remediation_no_commit_records_noop(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A PR remediation that leaves the branch HEAD unchanged is a no-op,
    regardless of the LLM's success-framed summary (#2440)."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #55: fix it",
            pr_branch="maestro/evolve/ep55",
        ),
    ]
    agent.consciousness.save_self_model(model)
    # Identical HEAD before/after → the remediation committed nothing.
    agent._vcs.rev_parse = MagicMock(return_value="samehash")
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            task_id=MagicMock(),
            outcome="success",
            result_summary="All done — fixed everything!",
            key_findings="Resolved the issue.",
        ),
    )

    await agent._run_task_phase()

    assert agent.remediation_tracker._data["55"] == [True]


@pytest.mark.asyncio
async def test_remediation_with_commit_records_non_noop(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A remediation that advances the branch HEAD defers to the free-text
    no-op classifier; a substantive summary records a non-no-op (#2440)."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #55: fix it",
            pr_branch="maestro/evolve/ep55",
        ),
    ]
    agent.consciousness.save_self_model(model)
    # The pre-execution merge-block and stand-down gates are exercised
    # separately; neutralise them here so their head-resolution does not consume
    # the ``rev_parse`` side_effect sized for the post-run HEAD-advance check.
    agent._gate_pr_remediation_merge_block = (  # type: ignore[method-assign]
        AsyncMock(return_value=False)
    )
    agent._gate_pr_remediation_stood_down = (  # type: ignore[method-assign]
        AsyncMock(return_value=False)
    )
    # Three ``rev_parse`` consumers in order: the owned-relap divert's head
    # resolution (#2864; discarded here — an empty history diverts nothing), then
    # the post-run HEAD-advance check's before/after pair. HEAD advances between
    # the latter two: a real commit was made on the branch.
    agent._vcs.rev_parse = MagicMock(
        side_effect=["origin-head", "before", "after"],
    )
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            task_id=MagicMock(),
            outcome="success",
            result_summary="Committed a real fix.",
            key_findings="Patched the bug.",
        ),
    )

    await agent._run_task_phase()

    assert agent.remediation_tracker._data["55"] == [False]


@pytest.mark.asyncio
async def test_remediation_no_commit_trips_gate_and_escalates(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """After ``threshold`` consecutive no-commit remediations the repetition
    gate refuses to re-route and the stuck PR is escalated (#2440)."""
    agent = make_agent()
    # Pre-seed one no-op so this run trips the (threshold-2) gate.
    agent.remediation_tracker.record_outcome(55, noop=True)
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #55: fix it",
            pr_branch="maestro/evolve/ep55",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent._vcs.rev_parse = MagicMock(return_value="samehash")
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            task_id=MagicMock(),
            outcome="success",
            result_summary="Looks good!",
            key_findings="",
        ),
    )
    agent._escalate_stuck_pr = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    assert agent.remediation_tracker._data["55"] == [True, True]
    assert agent.remediation_tracker.should_reroute(55) is False
    # No items context this lap → the escalation gets ``items_remaining=None``
    # and falls back to the git merge-block CTA (#2744).
    agent._escalate_stuck_pr.assert_awaited_once_with(
        55, "maestro/evolve/ep55", items_remaining=None,
    )


@pytest.mark.asyncio
async def test_remediation_malformed_pr_header_skips_recording(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A directive starting with the remediation prefix but with no parseable
    PR number records nothing (defensive guard) (#2440)."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #notanumber: stuff",
            pr_branch="maestro/evolve/epx",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent._vcs.rev_parse = MagicMock(return_value="h")
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            task_id=MagicMock(), outcome="success",
            result_summary="", key_findings="",
        ),
    )

    await agent._run_task_phase()

    assert agent.remediation_tracker._data == {}


@pytest.mark.asyncio
async def test_remediation_non_pr_task_skips_outcome_recording(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A self-directed (non-PR) task never touches the remediation tracker."""
    agent = make_agent()
    agent.task_queue.save([QueuedTask(task_id="t", goal="explore")])
    agent._vcs.rev_parse = MagicMock(return_value="h")
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            task_id=MagicMock(), outcome="success",
            result_summary="", key_findings="",
        ),
    )

    await agent._run_task_phase()

    assert agent.remediation_tracker._data == {}


@pytest.mark.asyncio
async def test_remediation_host_noop_counts_as_noop(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A remediation that committed locally but the host stamp gate found never
    landed is a no-op from the host's perspective, even with a substantive
    summary — the gate reuses ``run()``'s verdict without re-fetching (#2457)."""
    agent = make_agent()
    # Simulate run()'s stamp gate having downgraded this remediation: it set
    # the host verdict before _record_remediation_outcome runs.
    agent._last_remediation_merge_block = "CI_FAILING"
    # A real commit landed on the branch (HEAD advanced) and the summary reads
    # substantive — only the host verdict marks this a no-op.
    agent._vcs.rev_parse = MagicMock(return_value="after")

    record = MagicMock(
        result_summary="Committed a real fix.",
        key_findings="Patched the failing check.",
    )
    await agent._record_remediation_outcome(
        goal="Remediate PR #77: fix CI",
        pr_branch="maestro/evolve/ep77",
        record=record,
        head_before="before",
    )

    assert agent.remediation_tracker._data["77"] == [True]


@pytest.mark.asyncio
async def test_remediation_host_noop_stuck_reason_carries_merge_truth_provenance(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When the pre-stamp merge-truth gate (087 step 5) stamped a ``source`` and
    ``observed_at``, the repetition-gate stuck reason carries that provenance —
    not just the block reason — so the tracker records how merge state was read."""
    agent = make_agent()
    agent._last_remediation_merge_block = "CI_FAILING"
    agent._last_remediation_merge_source = "host_api"
    agent._last_remediation_merge_observed_at = 12345.0
    agent._vcs.rev_parse = MagicMock(return_value="after")
    agent.pr_ledger.record_attempt = MagicMock()  # type: ignore[method-assign]

    record = MagicMock(
        result_summary="Committed a real fix.",
        key_findings="Patched the failing check.",
    )
    await agent._record_remediation_outcome(
        goal="Remediate PR #77: fix CI",
        pr_branch="maestro/evolve/ep77",
        record=record,
        head_before="before",
    )

    agent.pr_ledger.record_attempt.assert_called_once()
    stuck_reason = agent.pr_ledger.record_attempt.call_args.kwargs["stuck_reason"]
    assert "merge-blocked: CI_FAILING" in stuck_reason
    assert "source=host_api" in stuck_reason
    assert "observed_at=12345" in stuck_reason


@pytest.mark.asyncio
async def test_remediation_landed_commit_is_not_noop(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When the host verdict is clear (no stamp-gate downgrade), a real commit
    with a substantive summary records a non-no-op (#2457 regression guard)."""
    agent = make_agent()
    agent._last_remediation_merge_block = None
    agent._vcs.rev_parse = MagicMock(return_value="after")

    record = MagicMock(
        result_summary="Committed a real fix.",
        key_findings="Patched the failing check.",
    )
    await agent._record_remediation_outcome(
        goal="Remediate PR #78: fix CI",
        pr_branch="maestro/evolve/ep78",
        record=record,
        head_before="before",
    )

    assert agent.remediation_tracker._data["78"] == [False]


@pytest.mark.asyncio
async def test_branch_resolved_lap_trips_gate_and_escalates(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A BRANCH_RESOLVED lap (host stamp gate downgraded a committed-but-unlanded
    remediation) counts as a host no-op, and a second consecutive one trips the
    repetition gate and escalates — driven by the classified state, not by any
    marker substring in the summary (proposal 070)."""
    agent = make_agent()
    # Pre-seed one no-op so this run trips the (threshold-2) gate.
    agent.remediation_tracker.record_outcome(88, noop=True)
    # run()'s stamp gate downgraded this lap: merge-block reason set, a real
    # commit landed (HEAD advanced), and the summary is substantive and carries
    # no marker substring — only the classified BRANCH_RESOLVED state makes it a
    # host no-op.
    agent._last_remediation_merge_block = "CI_FAILING"
    agent._vcs.rev_parse = MagicMock(return_value="after")
    agent._escalate_stuck_pr = AsyncMock()  # type: ignore[method-assign]

    record = MagicMock(
        result_summary="Committed a real fix on the branch.",
        key_findings="Patched the failing check.",
    )
    await agent._record_remediation_outcome(
        goal="Remediate PR #88: fix CI",
        pr_branch="maestro/evolve/ep88",
        record=record,
        head_before="before",
    )

    assert agent._last_remediation_state is PrRemediationState.BRANCH_RESOLVED
    assert agent.remediation_tracker._data["88"] == [True, True]
    assert agent.remediation_tracker.should_reroute(88) is False
    # A host (BRANCH_RESOLVED) latch carries no items context, so the
    # escalation gets ``items_remaining=None`` and names the git blocker (#2744).
    agent._escalate_stuck_pr.assert_awaited_once_with(
        88, "maestro/evolve/ep88", items_remaining=None,
    )


@pytest.mark.asyncio
async def test_remediation_reuses_stashed_threads_remaining_noop(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When ``run()``'s completeness gate already counted remaining eligible
    items this lap, the repetition gate reuses that count for its thread no-op
    instead of re-querying the host — a positive count is a no-op even with a
    real commit and a clean host verdict (#2663)."""
    agent = make_agent()
    agent._last_remediation_merge_block = None
    agent._last_remediation_items_remaining = 2
    agent._vcs.rev_parse = MagicMock(return_value="after")
    threads_spy = MagicMock(return_value=[])
    agent.evolution.code_host.get_pr_review_threads = threads_spy  # type: ignore[attr-defined]

    record = MagicMock(
        result_summary="Committed a real fix.",
        key_findings="Patched two of the three.",
    )
    await agent._record_remediation_outcome(
        goal="Remediate PR #80: address comments",
        pr_branch="maestro/evolve/ep80",
        record=record,
        head_before="before",
    )

    assert agent.remediation_tracker._data["80"] == [True]
    threads_spy.assert_not_called()


@pytest.mark.asyncio
async def test_remediation_reuses_stashed_zero_threads_progress(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A stashed zero remaining count means the single pass addressed every
    item — a committed remediation with a clean host verdict records progress,
    and the host is not re-queried for the thread check (#2663)."""
    agent = make_agent()
    agent._last_remediation_merge_block = None
    agent._last_remediation_items_remaining = 0
    agent._vcs.rev_parse = MagicMock(return_value="after")
    threads_spy = MagicMock(return_value=[])
    agent.evolution.code_host.get_pr_review_threads = threads_spy  # type: ignore[attr-defined]

    record = MagicMock(
        result_summary="Committed a real fix.",
        key_findings="Addressed every thread.",
    )
    await agent._record_remediation_outcome(
        goal="Remediate PR #81: address comments",
        pr_branch="maestro/evolve/ep81",
        record=record,
        head_before="before",
    )

    assert agent.remediation_tracker._data["81"] == [False]
    threads_spy.assert_not_called()


@pytest.mark.asyncio
async def test_remediation_text_noop_with_commit_uses_text_stuck_reason(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When the host verdict is clear and a commit landed but the text
    summary matches a noop indicator, the stuck_reason is 'text-classified noop'
    — covers the agent.py else branch in the stuck_reason derivation. The
    budget is shrunk to 1 so this single attempt latches stuck and the
    derived reason is actually persisted and asserted."""
    agent = make_agent()
    agent.pr_ledger = PRLedger(
        agent.config.memory_dir, max_attempts_per_head=1,
    )
    agent._last_remediation_merge_block = None
    # HEAD changed (a commit was made).
    agent._vcs.rev_parse = MagicMock(return_value="after")

    record = MagicMock(
        result_summary="already passes — no changes needed",
        key_findings="nothing to do",
    )
    await agent._record_remediation_outcome(
        goal="Remediate PR #79: fix CI",
        pr_branch="maestro/evolve/ep79",
        record=record,
        head_before="before",
    )
    # text_noop=True via the "already passes" indicator in result_summary.
    assert agent.remediation_tracker._data["79"] == [True]
    entry = agent.pr_ledger.get(79)
    assert entry is not None
    assert entry.last_outcome == "noop"
    assert entry.stuck is True
    assert entry.stuck_reason == "text-classified noop"


@pytest.mark.asyncio
async def test_comment_progress_lap_reopens_latched_pr(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A comment-only lap that delivers a terminal reply — strictly fewer
    outstanding items than the prior lap — resets the latched repetition gate
    and grants a fresh PR-ledger budget, so the PR is re-routable for its
    still-open threads even though the lap made no branch commit (#2744)."""
    agent = make_agent()
    # Latch the repetition gate on two no-progress comment laps and record a
    # baseline of 3 outstanding items.
    agent.remediation_tracker.record_outcome(80, noop=True, items_remaining=3)
    agent.remediation_tracker.record_outcome(80, noop=True, items_remaining=3)
    assert agent.remediation_tracker.should_reroute(80) is False
    # Exhaust the PR-ledger budget at the current head too.
    agent.pr_ledger = PRLedger(
        agent.config.memory_dir, max_attempts_per_head=2,
    )
    for _ in range(2):
        agent.pr_ledger.record_attempt(
            80, outcome="noop", stuck_reason="no commit produced",
            branch="maestro/evolve/ep80",
        )
    assert agent.pr_ledger.can_attempt(80) is False
    # This lap delivered a terminal reply: the stashed count dropped 3 → 2 and
    # no branch commit was made (HEAD unchanged before/after run()).
    agent._last_remediation_merge_block = None
    agent._last_remediation_items_remaining = 2
    agent._vcs.rev_parse = MagicMock(return_value="same")
    agent._escalate_stuck_pr = AsyncMock()  # type: ignore[method-assign]

    record = MagicMock(
        result_summary="Delivered a terminal reply to one thread.",
        key_findings="Addressed one of the three.",
    )
    await agent._record_remediation_outcome(
        goal="Remediate PR #80: address comments",
        pr_branch="maestro/evolve/ep80",
        record=record,
        head_before="same",
    )

    # The latch cleared: a single fresh no-op replaces the latched history.
    assert agent.remediation_tracker._data["80"] == [True]
    assert agent.remediation_tracker.should_reroute(80) is True
    # The new baseline carried forward for the next lap's comparison.
    assert agent.remediation_tracker.last_items_remaining(80) == 2
    # The PR-ledger budget was refreshed (grant then one charged attempt).
    assert agent.pr_ledger.can_attempt(80) is True
    # should_reroute is True, so no escalation fires this lap.
    agent._escalate_stuck_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_items_count_credits_no_progress(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A lap whose §4d scan failed leaves the count ``None`` (#4065): no
    tracker reset, no fresh ledger grant, and the prior baseline of 3 holds."""
    agent = make_agent()
    agent.remediation_tracker.record_outcome(84, noop=True, items_remaining=3)
    agent._last_remediation_merge_block = None
    agent._last_remediation_items_remaining = None
    agent._vcs.rev_parse = MagicMock(return_value="same")
    agent._escalate_stuck_pr = AsyncMock()  # type: ignore[method-assign]
    agent.remediation_tracker.reset = MagicMock()  # type: ignore[method-assign]
    agent.pr_ledger.grant_fresh_attempts = MagicMock()  # type: ignore[method-assign]

    record = MagicMock(
        result_summary="Scan failed.", key_findings="Unknown.",
    )
    await agent._record_remediation_outcome(
        goal="Remediate PR #84: address comments",
        pr_branch="maestro/evolve/ep84",
        record=record,
        head_before="same",
    )

    agent.remediation_tracker.reset.assert_not_called()
    agent.pr_ledger.grant_fresh_attempts.assert_not_called()
    assert agent.remediation_tracker.last_items_remaining(84) == 3


@pytest.mark.parametrize("fail_threads", [True, False])
@pytest.mark.asyncio
async def test_unknown_items_fallback_failed_reread_counts_as_noop(
    make_agent,  # type: ignore[no-untyped-def]
    fail_threads: bool,
) -> None:
    """The ``None``-count fallback re-read is fail-loud too (#4065): a failed
    thread read, or an incomplete overflow scan, keeps ``items_noop`` True even
    on a commit-bearing lap."""
    agent = make_agent()
    agent._last_remediation_merge_block = None
    agent._last_remediation_items_remaining = None
    agent._vcs.rev_parse = MagicMock(return_value="after")
    agent._escalate_stuck_pr = AsyncMock()  # type: ignore[method-assign]
    host = agent.evolution.code_host
    if fail_threads:
        host.get_pr_review_threads_strict = MagicMock(
            side_effect=VCSError("gh timed out"),
        )
    else:
        host.get_pr_review_bodies_strict = MagicMock(
            side_effect=VCSError("secondary rate limit"),
        )

    record = MagicMock(
        result_summary="Pushed a fix.", key_findings="One commit.",
    )
    await agent._record_remediation_outcome(
        goal="Remediate PR #85: address comments",
        pr_branch="maestro/evolve/ep85",
        record=record,
        head_before="before",
    )

    assert agent.remediation_tracker._data["85"] == [True]


@pytest.mark.asyncio
async def test_comment_no_progress_lap_latches_and_escalates_items(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A comment lap whose outstanding-item count does not decrease still
    counts as a no-op and latches the gate exactly as before (#2440 / #2663),
    and the escalation is handed the items context so it can name the
    review-items blocker rather than a transient git snapshot (#2744)."""
    agent = make_agent()
    # One prior no-op established the baseline of 3 outstanding items.
    agent.remediation_tracker.record_outcome(82, noop=True, items_remaining=3)
    agent._last_remediation_merge_block = None
    # This lap still reports 3 — no terminal reply delivered.
    agent._last_remediation_items_remaining = 3
    agent._vcs.rev_parse = MagicMock(return_value="same")
    agent._escalate_stuck_pr = AsyncMock()  # type: ignore[method-assign]

    record = MagicMock(
        result_summary="Could not resolve the threads this pass.",
        key_findings="Three still outstanding.",
    )
    await agent._record_remediation_outcome(
        goal="Remediate PR #82: address comments",
        pr_branch="maestro/evolve/ep82",
        record=record,
        head_before="same",
    )

    # No reset: the second no-op latches the gate.
    assert agent.remediation_tracker._data["82"] == [True, True]
    assert agent.remediation_tracker.should_reroute(82) is False
    # The baseline stays at the unchanged count.
    assert agent.remediation_tracker.last_items_remaining(82) == 3
    # The escalation receives the outstanding-items count so it names the
    # review-items cause, not a transient git merge-block snapshot.
    agent._escalate_stuck_pr.assert_awaited_once_with(
        82, "maestro/evolve/ep82", items_remaining=3,
    )


# ──────────────────────────────────────────────────────────────────────
# Pre-execution merge-block gate — skip SDK spend on Tend-PR-owned blockers
# ──────────────────────────────────────────────────────────────────────


def _pr_state(branch: str, *, mergeable: str, merge_state: str) -> PrMergeState:
    """Build a minimal ``PrMergeState`` for the named branch."""
    return PrMergeState(
        number=55, head_ref=branch, title="t", body="b", state="OPEN",
        mergeable=mergeable, merge_state_status=merge_state,
        review_decision="", created_at="", updated_at="", url="",
    )


@pytest.mark.asyncio
async def test_pr_gate_skips_on_behind_main(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A PR whose branch trails ``origin/main`` is skipped before any SDK
    spend: a zero-cost no-op is recorded, the repetition tracker counts it,
    and the named BEHIND_MAIN blocker is escalated — ``run`` is never called."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #55: fix it",
            pr_branch="maestro/evolve/ep55",
        ),
    ]
    agent.consciousness.save_self_model(model)
    # Branch is two commits behind origin/main → classify → BEHIND_MAIN.
    agent._vcs.commits_ahead_behind.return_value = (0, 2)
    agent.run = AsyncMock()  # type: ignore[method-assign]
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    # No SDK session was opened.
    agent.run.assert_not_awaited()
    # The blocker was surfaced by name.
    agent._notify_best_effort.assert_awaited_once()
    message = agent._notify_best_effort.await_args.args[0]
    assert "PR #55" in message
    assert MERGE_BLOCK_BEHIND_MAIN in message
    # Counted in the separate merge-block class, NOT the ``should_reroute``
    # outcome list, so the skip never exiles the PR's still-unattempted
    # comment/overflow work (#2796).
    assert agent.remediation_tracker.merge_block_skip_count(55) == 1
    assert "55" not in agent.remediation_tracker._data
    # A zero-cost merge-block TaskRecord was persisted.
    tasks = agent.memory.get_recent_tasks(5)
    assert len(tasks) == 1
    assert "[merge-block]" in (tasks[0].key_findings or "")
    assert MERGE_BLOCK_BEHIND_MAIN in (tasks[0].key_findings or "")
    # The PR request was drained and not re-added.
    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == []


@pytest.mark.asyncio
async def test_pr_gate_awaiting_push_runnable(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """AWAITING_PUSH is owned (proposal 098): an unpushed on-branch resolution is
    my own next act, not Tend's, so it is not in the Tend-clearable set and the
    request stays runnable — the lap reaches ``run`` instead of being skipped at
    zero cost, and no ``[merge-block]`` no-op is recorded."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #55: fix it",
            pr_branch="maestro/evolve/ep55",
        ),
    ]
    agent.consciousness.save_self_model(model)
    # Same probe state as the flag-off case: current vs origin/main, three
    # local-only commits not yet pushed → AWAITING_PUSH.
    agent._vcs.commits_ahead_behind.side_effect = [(0, 0), (3, 0)]
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            task_id="abc", outcome="success",
            result_summary="", key_findings="",
        ),
    )
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    # The dequeue red-root read (#4216) would consume the scripted probe
    # sequence above; this test is about the merge-block classification.
    agent._pr_remediation._diagnose_red_root_on_pr_head = MagicMock()

    await agent._run_task_phase()

    # Runnable: the lap reached the session entry point rather than deferring.
    agent.run.assert_awaited_once()
    assert agent.run.await_args.args[0] == "Remediate PR #55: fix it"
    # The merge-block gate did not fire.
    agent._notify_best_effort.assert_not_awaited()
    assert agent.remediation_tracker.merge_block_skip_count(55) == 0
    tasks = agent.memory.get_recent_tasks(5)
    assert all("[merge-block]" not in (t.key_findings or "") for t in tasks)


@pytest.mark.asyncio
async def test_pr_gate_skips_on_branch_resolved_costume(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A PR resolved-on-branch ≥ threshold times without landing is skipped by
    the costume-signature gate before any SDK spend: ``run`` is never called,
    a zero-cost ``[branch-resolved]`` ``partial`` no-op is recorded, and the
    costume is escalated once — exercising the dequeue-face wiring return."""
    from datetime import UTC, datetime

    from maestro.memory import TaskRecord

    agent = make_agent()
    # Two prior resolved-on-branch partial laps for PR #55 → at threshold (2).
    for i in range(2):
        agent.memory.record_task(TaskRecord(
            task_id=f"prior{i}",
            goal="Remediate PR #55: fix it",
            plan=[],
            outcome="partial",
            result_summary="Resolved on branch, awaiting tend push",
            errors=[],
            duration_secs=0.0,
            timestamp=datetime.now(UTC).isoformat(),
            source="external",
            key_findings="Resolved on branch, awaiting tend push",
        ))
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #55: fix it",
            pr_branch="maestro/evolve/ep55",
        ),
    ]
    agent.consciousness.save_self_model(model)
    # Clean host state → merge-block gate passes through (NONE) so the
    # branch-resolved gate downstream is reached.
    agent._vcs.commits_ahead_behind.return_value = (0, 0)
    agent.run = AsyncMock()  # type: ignore[method-assign]
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    # No SDK session was opened.
    agent.run.assert_not_awaited()
    # The costume was escalated once, naming the PR.
    agent._notify_best_effort.assert_awaited_once()
    message = agent._notify_best_effort.await_args.args[0]
    assert "PR #55" in message
    # A zero-cost ``[branch-resolved]`` partial no-op was persisted.
    tasks = agent.memory.get_recent_tasks(5)
    assert "[branch-resolved]" in (tasks[-1].key_findings or "")
    assert tasks[-1].outcome == "partial"
    # The PR request was drained and not re-added.
    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == []


@pytest.mark.asyncio
async def test_pr_gate_proceeds_on_conflicting(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A genuinely conflicting PR is NOT gated — the LLM remediation must run
    to resolve the conflict, so the gate returns control to the normal path."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #55: fix it",
            pr_branch="maestro/evolve/ep55",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent._vcs.commits_ahead_behind.return_value = (0, 0)
    agent.evolution._host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/ep55": _pr_state(
            "maestro/evolve/ep55", mergeable="CONFLICTING", merge_state="DIRTY",
        ),
    }
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            task_id="abc", outcome="success",
            result_summary="", key_findings="",
        ),
    )
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    # The remediation flow ran; the gate did not fire.
    agent.run.assert_awaited_once()
    agent._notify_best_effort.assert_not_awaited()
    args, kwargs = agent.run.await_args
    assert args[0] == "Remediate PR #55: fix it"
    assert kwargs["pr_branch"] == "maestro/evolve/ep55"


@pytest.mark.parametrize("merge_state", ["DIRTY", ""])
@pytest.mark.asyncio
async def test_pr_gate_proceeds_on_behind_and_conflicting(
    make_agent,  # type: ignore[no-untyped-def]
    merge_state: str,
) -> None:
    """A PR branch that is simultaneously behind ``origin/main`` AND conflicting
    must NOT be skipped as BEHIND_MAIN — the conflict needs LLM resolution, and
    catching up only reproduces it (#2521). The probe reports CONFLICTING and
    the gate proceeds whether the host signals the conflict via
    ``merge_state == DIRTY`` (classifier surface) or only via the separate
    ``mergeable == CONFLICTING`` field with ``merge_state`` not yet ``DIRTY``
    (probe-override surface)."""
    agent = make_agent()
    # Two commits behind origin/main, and the host reports the branch
    # conflicting. Both probe calls (origin/main, origin/branch) see behind=2.
    agent._vcs.commits_ahead_behind.return_value = (0, 2)
    agent.evolution._host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/ep55": _pr_state(
            "maestro/evolve/ep55",
            mergeable="CONFLICTING",
            merge_state=merge_state,
        ),
    }
    agent.evolution._host.is_pr_merged.return_value = False
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

    landed_ok, reason = await agent._probe_pr_merge_block(
        55, "maestro/evolve/ep55",
    )
    assert landed_ok is False
    assert reason == MERGE_BLOCK_CONFLICTING

    gated = await agent._gate_pr_remediation_merge_block(
        "Remediate PR #55: fix it", "maestro/evolve/ep55", "external",
    )
    # The gate does NOT skip — the LLM remediation path must run.
    assert gated is False
    # No zero-cost no-op was recorded and no merge-block escalation fired.
    assert "55" not in agent.remediation_tracker._data
    agent._notify_best_effort.assert_not_awaited()
    assert agent.memory.get_recent_tasks(5) == []


@pytest.mark.asyncio
async def test_pr_gate_proceeds_on_behind_and_local_probe_conflict(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A branch behind ``origin/main`` whose *local* merge-tree probe conflicts
    must be routed to remediation, never stamped ``[merge-block]`` — even though
    the host ``mergeable`` field reads clean (the flapping-field bug 080 fixes).
    The merge-block gate proceeds (returns False) and records no no-op (#080)."""
    agent = make_agent()
    # Two commits behind origin/main; host reports the branch cleanly mergeable.
    agent._vcs.commits_ahead_behind.return_value = (0, 2)
    agent.evolution._host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/ep55": _pr_state(
            "maestro/evolve/ep55", mergeable="MERGEABLE", merge_state="CLEAN",
        ),
    }
    agent.evolution._host.is_pr_merged.return_value = False
    # The deterministic local probe — not the host field — finds the conflict.
    agent._vcs.merge_tree_conflicts.return_value = True
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

    landed_ok, reason = await agent._probe_pr_merge_block(
        55, "maestro/evolve/ep55",
    )
    assert landed_ok is False
    assert reason == MERGE_BLOCK_CONFLICTING

    gated = await agent._gate_pr_remediation_merge_block(
        "Remediate PR #55: fix it", "maestro/evolve/ep55", "external",
    )
    # The gate does NOT skip — the LLM remediation path must run.
    assert gated is False
    assert "55" not in agent.remediation_tracker._data
    agent._notify_best_effort.assert_not_awaited()
    assert agent.memory.get_recent_tasks(5) == []


@pytest.mark.asyncio
async def test_pr_gate_proceeds_when_pr_already_merged(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When the host reports the PR merged, the probe reports it landed, so the
    gate does not fire and the normal (drain) path proceeds."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #55: fix it",
            pr_branch="maestro/evolve/ep55",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.evolution._host.is_pr_merged.return_value = True
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            task_id="abc", outcome="success",
            result_summary="", key_findings="",
        ),
    )
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    agent._notify_best_effort.assert_not_awaited()


@pytest.mark.asyncio
async def test_pr_gate_returns_false_for_non_remediation_directive(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A directive that is not a ``Remediate PR #<n>:`` goal never probes the
    host and never gates — the helper returns False on the regex miss."""
    agent = make_agent()
    gated = await agent._gate_pr_remediation_merge_block(
        "Build a new feature", "maestro/evolve/x", "external",
    )
    assert gated is False
    # No host probe was attempted.
    agent.evolution._host.is_pr_merged.assert_not_called()


@pytest.mark.asyncio
async def test_pr_gate_returns_false_for_unparseable_pr_number(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A remediation-prefixed directive with no parseable PR number is a regex
    miss — the gate returns False without probing the host."""
    agent = make_agent()
    gated = await agent._gate_pr_remediation_merge_block(
        "Remediate PR #notanumber: stuff", "maestro/evolve/x", "external",
    )
    assert gated is False
    agent.evolution._host.is_pr_merged.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# SDK-session boundary proof — the gate spends zero budget before run()
#
# The skip tests above mock ``run()`` (the high-level remediation entry).
# These tests go one level deeper and patch the SDK-session primitives
# ``create_session`` / ``query_on_session`` (where every PR remediation
# session is actually opened — see the findings report), proving the gate
# returns *before* either is ever invoked.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pr_gate_behind_main_opens_no_sdk_session(
    make_agent,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BEHIND_MAIN proof at the SDK boundary: neither ``create_session`` nor
    ``query_on_session`` is invoked, the named ``classify_merge_block``
    escalation is routed, and a no-op ``TaskRecord`` is persisted and counted —
    no budget is spent re-doing work Tend PR owns."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #91: fix CI",
            pr_branch="maestro/evolve/ep91",
        ),
    ]
    agent.consciousness.save_self_model(model)
    # Branch trails origin/main by four commits → classify → BEHIND_MAIN.
    agent._vcs.commits_ahead_behind.return_value = (0, 4)
    create_session = AsyncMock()
    query_on_session = AsyncMock()
    monkeypatch.setattr("maestro.agent.create_session", create_session)
    monkeypatch.setattr("maestro.agent.query_on_session", query_on_session)
    # ``run`` stays mocked as a regression backstop: if the gate failed to
    # short-circuit, ``run`` would be awaited (caught below) even though it
    # never reaches the real session primitives.
    agent.run = AsyncMock()  # type: ignore[method-assign]
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    # No SDK session was created and no session-attached query was issued.
    create_session.assert_not_called()
    query_on_session.assert_not_called()
    agent.run.assert_not_awaited()
    # The named classify_merge_block escalation was routed.
    agent._notify_best_effort.assert_awaited_once()
    message = agent._notify_best_effort.await_args.args[0]
    assert "PR #91" in message
    assert MERGE_BLOCK_BEHIND_MAIN in message
    # A zero-cost merge-block TaskRecord was persisted; the skip is counted in
    # the separate merge-block class, NOT the ``should_reroute`` outcome list,
    # so it never exiles the PR's still-unattempted comment/overflow work
    # (#2796).
    assert agent.remediation_tracker.merge_block_skip_count(91) == 1
    assert "91" not in agent.remediation_tracker._data
    tasks = agent.memory.get_recent_tasks(5)
    assert len(tasks) == 1
    # git-merge-truth shows the PR unlanded, so the zero-cost skip records
    # `partial`, never a false `success` (Trust-git-over-stamps, #2543).
    assert tasks[0].outcome == "partial"
    assert tasks[0].duration_secs == 0.0
    assert "[merge-block]" in (tasks[0].key_findings or "")
    assert MERGE_BLOCK_BEHIND_MAIN in (tasks[0].result_summary or "")


@pytest.mark.asyncio
async def test_pr_gate_actionable_reason_opens_sdk_session(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Control: an actionable blocker (failing CI, which the LLM must fix) is
    NOT gated — the gate returns control and the remediation reaches ``run``,
    the session entry point, with no zero-cost no-op recorded and no
    escalation."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive="Remediate PR #91: fix CI",
            pr_branch="maestro/evolve/ep91",
        ),
    ]
    agent.consciousness.save_self_model(model)
    # Even with origin/main and pushed, but CI reports failure → classify
    # CI_FAILING, an actionable reason the LLM must remediate.
    agent._vcs.commits_ahead_behind.return_value = (0, 0)
    agent.evolution._host.get_pr_check_conclusion.return_value = "failure"
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            task_id="abc", outcome="success",
            result_summary="", key_findings="",
        ),
    )
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    # The remediation proceeded to the session entry point.
    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0] == "Remediate PR #91: fix CI"
    assert kwargs["pr_branch"] == "maestro/evolve/ep91"
    # The gate did not fire: it routed no escalation and persisted no
    # zero-cost ``[merge-block]`` no-op record. (The normal post-run
    # repetition tracker still records the mocked run's outcome — that is the
    # standard remediation path, distinct from the gate.)
    agent._notify_best_effort.assert_not_awaited()
    tasks = agent.memory.get_recent_tasks(5)
    assert all("[merge-block]" not in (t.key_findings or "") for t in tasks)


# ---------------------------------------------------------------------------
# _gate_allowed_branches — the owned-writable-branch producer (proposal 093)
# ---------------------------------------------------------------------------


def test_gate_allowed_branches_none_when_branchless(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A branchless run injects ``None`` (fail-closed) — the host is never even
    consulted."""
    agent = make_agent()
    agent.evolution.code_host.get_open_evolution_pr_states = MagicMock()
    assert agent._gate_allowed_branches(None) is None
    agent.evolution.code_host.get_open_evolution_pr_states.assert_not_called()


def test_gate_allowed_branches_host_failure_collapses(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The host read fails (best-effort ``{}``): the confirmed set is empty, so
    the result collapses to the singleton run branch."""
    agent = make_agent()
    agent.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        return_value={},
    )
    agent.pr_ledger.observe_head(42, "maestro/evolve/ep42", "sha")
    result = agent._gate_allowed_branches("maestro/task/t1")
    assert result == frozenset({"maestro/task/t1"})


def test_gate_allowed_branches_confirms_evolve_heads(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The set widens to the run branch plus every ``maestro/evolve/*`` head
    confirmed by *both* the live open-PR list and the ledger; a host-only head,
    a ledger-only head, and ``main`` are all excluded."""
    agent = make_agent()
    agent.evolution.code_host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "maestro/evolve/ep1": _open_pr_state(1, "maestro/evolve/ep1"),
            "maestro/evolve/host-only": _open_pr_state(
                2, "maestro/evolve/host-only",
            ),
            "main": _open_pr_state(3, "main"),
        },
    )
    agent.pr_ledger.observe_head(1, "maestro/evolve/ep1", "sha1")
    agent.pr_ledger.observe_head(9, "maestro/evolve/ledger-only", "sha9")
    agent.pr_ledger.observe_head(3, "main", "sha3")
    result = agent._gate_allowed_branches("maestro/task/t1")
    assert result == frozenset({"maestro/task/t1", "maestro/evolve/ep1"})


def _failed_branch_record(outcome: str) -> TaskRecord:
    """A prior task-history row whose branch is ``maestro/task/16db56580b86``."""
    return TaskRecord(
        task_id="16db56580b86",
        goal="wire X",
        plan=[],
        outcome=outcome,
        result_summary="",
        errors=[],
        duration_secs=0.0,
        timestamp="2026-09-24T00:00:00+00:00",
        source="self_directed",
        branch="maestro/task/16db56580b86",
    )


@pytest.mark.asyncio
async def test_queue_head_naming_failed_branch_is_not_run(
    make_agent,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A popped head re-attacking a failed branch is dropped, not run (#4049).

    The generation-time gate only skips curation; the pop is the door the
    costly re-attempt actually passes through, so it must refuse there.
    """
    agent = make_agent()
    agent.memory.record_task(_failed_branch_record("partial"))
    agent.task_queue.save([
        QueuedTask(task_id="q1", goal="Finish maestro/task/16db56580b86: wire X"),
    ])
    agent.run = AsyncMock()  # type: ignore[method-assign]
    activations: list[dict[str, str]] = []
    monkeypatch.setattr(
        "maestro.task_gates.audit_checks.log_gate_activation",
        lambda _backend, **kwargs: activations.append(kwargs),
    )

    await agent._run_task_phase()

    agent.run.assert_not_awaited()
    assert agent.task_queue.load() == []
    assert len(activations) == 1
    assert activations[0]["gate"] == "no_repeat_failed_branch_target"
    assert activations[0]["action"] == "rejected"
    events = agent.journal.read_recent_events(5)
    assert events[-1].triggered_by == "no_repeat_failed_branch_target_gate"


@pytest.mark.asyncio
async def test_queue_head_naming_successful_branch_still_runs(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A head naming a branch whose run succeeded is not refused at pop."""
    agent = make_agent()
    agent.memory.record_task(_failed_branch_record("success"))
    agent.task_queue.save([
        QueuedTask(task_id="q1", goal="Finish maestro/task/16db56580b86: wire X"),
    ])
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, _kwargs = agent.run.await_args
    assert args[0] == "Finish maestro/task/16db56580b86: wire X"


# ---------------------------------------------------------------------------
# Transactional dequeue (#4403): a drain must not revert concurrent writes
# ---------------------------------------------------------------------------


def _interleave(agent, mutate):  # type: ignore[no-untyped-def]
    """Commit a second writer's edit via the blind door, as Telegram would."""
    other = agent.consciousness.load_self_model()
    mutate(other)
    agent.consciousness.save_self_model(other)


@pytest.mark.asyncio
async def test_run_task_phase_user_dequeue_preserves_concurrent_writes(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["first"]
    agent.consciousness.save_self_model(model)
    real_load = agent.consciousness.load_self_model
    fired: list[bool] = []

    def _load_then_interleave():  # type: ignore[no-untyped-def]
        snapshot = real_load()
        if not fired:
            fired.append(True)

            def _add(m):  # type: ignore[no-untyped-def]
                m.pending_user_requests.append("second")
                m.next_task_directive = "d"

            _interleave(agent, _add)
        return snapshot

    agent.consciousness.load_self_model = _load_then_interleave  # type: ignore[method-assign]
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.consciousness.load_self_model = real_load  # type: ignore[method-assign]
    after = real_load()
    assert after.pending_user_requests == ["second"]
    assert after.next_task_directive == "d"
    agent.run.assert_awaited_once()
    assert agent.run.await_args.args[0] == "first"


@pytest.mark.asyncio
async def test_run_task_phase_user_dequeue_already_drained_yields(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["first"]
    agent.consciousness.save_self_model(model)
    agent.consciousness.dequeue_user_request = MagicMock(  # type: ignore[method-assign]
        return_value=None,
    )
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_not_awaited()


def _seed_pr_queue(agent):  # type: ignore[no-untyped-def]
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [PendingPRRequest("a", "b1")]
    agent.consciousness.save_self_model(model)
    agent._pr_remediation.dequeue_gates = AsyncMock(  # type: ignore[method-assign]
        return_value=False,
    )
    agent._dispose_mechanical_standdown = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )
    agent.run = AsyncMock()  # type: ignore[method-assign]
    agent._record_remediation_outcome = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_run_task_phase_pr_dequeue_preserves_concurrent_writes(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    _seed_pr_queue(agent)

    def _unheld(queue, index, _recent):  # type: ignore[no-untyped-def]
        def _add(m):  # type: ignore[no-untyped-def]
            m.pending_pr_requests.append(PendingPRRequest("c", "b2"))
            m.next_task_directive = "d"

        _interleave(agent, _add)
        return 0

    agent._pr_remediation.next_unheld_pr_index = _unheld  # type: ignore[method-assign]

    await agent._run_task_phase()

    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == [PendingPRRequest("c", "b2")]
    assert after.next_task_directive == "d"
    agent.run.assert_awaited_once()
    assert agent.run.await_args.args[0] == "a"


@pytest.mark.asyncio
async def test_run_task_phase_pr_dequeue_selected_entry_gone_yields_slot(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    _seed_pr_queue(agent)
    streak = agent._consecutive_remediation

    def _unheld(queue, index, _recent):  # type: ignore[no-untyped-def]
        def _swap(m):  # type: ignore[no-untyped-def]
            m.pending_pr_requests = [PendingPRRequest("c", "b2")]

        _interleave(agent, _swap)
        return 0

    agent._pr_remediation.next_unheld_pr_index = _unheld  # type: ignore[method-assign]

    await agent._run_task_phase()

    agent.run.assert_not_awaited()
    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == [PendingPRRequest("c", "b2")]
    assert agent._consecutive_remediation == streak


@pytest.mark.parametrize(
    ("owner", "method"),
    [
        (MaestroAgent, "_run_task_phase"),
        (PrRemediationController, "_dispose_mechanical_standdown"),
    ],
)
def test_request_drains_never_blind_save_self_model(owner: type, method: str) -> None:
    """#4403: a drain must route through the transactional dequeue doors, so a
    blind ``save_self_model`` of a stale snapshot cannot reappear."""
    import ast
    import textwrap

    src = textwrap.dedent(inspect.getsource(getattr(owner, method)))
    calls = [
        node.func.attr
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "save_self_model" not in calls
    assert any(name.startswith("dequeue_") for name in calls)


# --- proposal 157 W1: stream requests -----------------------------------------



@pytest.mark.asyncio
async def test_unprefixed_request_passes_none_and_routes(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    agent.config.user_pr_message_remediation_enabled = True
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["fix the parser"]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]
    agent._pr_remediation.route_user_pr_message = MagicMock(  # type: ignore[method-assign]
        return_value=None,
    )

    await agent._run_task_phase()

    assert "stream_id" not in agent.run.await_args.kwargs
    agent._pr_remediation.route_user_pr_message.assert_called_once_with("fix the parser")


@pytest.mark.asyncio
async def test_queue_head_passes_no_stream_id(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    agent.task_queue.save([QueuedTask(task_id="q1", goal="queue goal")])
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    assert "stream_id" not in agent.run.await_args.kwargs


@pytest.mark.asyncio
async def test_pr_request_passes_no_stream_id(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [PendingPRRequest(directive="stream:acme-tool pr goal")]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    await agent._run_task_phase()

    assert "stream_id" not in agent.run.await_args.kwargs
