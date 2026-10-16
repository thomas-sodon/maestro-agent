"""Tests for phase-level session reuse (Sessions A, B, C).

When ``enable_session_reuse=True``, loop phases share persistent
``ClaudeSDKClient`` sessions so the identity system prompt and growing
conversation prefix stay in the prompt cache across sequential SDK calls.

These tests cover:
- Per-method session branches (consciousness, evolution,
  agent helpers) — verify ``query_on_session`` is used instead of ``query``
  when a session is supplied.
- Role-specific prompt prepending for ``reflect``.
- ``self_review`` fence message formatting.
- Session A/B lifecycle inside ``run_loop`` — creation, disconnect at phase
  boundary, teardown in ``finally``.
- Session C lifecycle inside ``implement_plan`` — creation, threading through
  ``implement`` / ``self_review`` / ``auto_fix``, teardown in ``finally``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.cli import _build_agent_tools
from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine
from maestro.evolution_fuel import FuelSelection
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import EvolutionPlan, Memory
from maestro.notifications.base import NullNotifier
from maestro.reflection_retrieval import REFLECTION_SESSION_MAX_TURNS
from maestro.sdk import QueryOptions
from maestro.tool_gating import task_gate_from_hooks
from maestro.vcs import evolution_branch
from tests.conftest import MockQueryResult, arm_entry_clean_tree
from tests.support.gap_fuel import GAP

FAKE_DIR = Path("/fake/sessions")

# The read-only qualified names the bounded reflection surface grants,
# in registry-insertion order (journal, self_awareness, memory, issues,
# diagnostics) — the wire shape the active contemplation session exposes.
_EXPECTED_REFLECTION_TOOLS = [
    "mcp__journal__read_journal",
    "mcp__journal__search_journal",
    "mcp__self_awareness__read_self_model",
    "mcp__memory__read_task_history",
    "mcp__memory__read_evolution_plans",
    "mcp__issues__list_open_issues",
    "mcp__diagnostics__get_diagnostic_snapshot",
    "mcp__diagnostics__get_recent_errors",
    "mcp__diagnostics__is_diagnostics_noteworthy",
]


def _mock_agent_tools() -> dict[str, object]:
    """The introspection registry over all-``MagicMock`` collaborators.

    Mirrors ``cli._build_agent``'s ``_build_agent_tools`` call so
    ``_build_contemplation_session_options`` materialises the real wire shape.
    """
    return _build_agent_tools(
        journal=MagicMock(),
        consciousness=MagicMock(),
        memory=MagicMock(),
        issue_tracker=MagicMock(),
    )


def _fake_session() -> MagicMock:
    """A stand-in for ``ClaudeSDKClient`` — any object is fine; only identity
    comparison matters because the real session is never invoked."""
    return MagicMock(name="ClaudeSDKClient")


# ---------------------------------------------------------------------------
# consciousness.introspect / update_self_model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_introspect_session_path_uses_query_on_session(
    mem_fs: dict[str, str],
) -> None:
    c = Consciousness(FAKE_DIR)
    j = Journal(FAKE_DIR)
    c.load_self_model()

    sess = _fake_session()
    with (
        patch(
            "maestro.consciousness.query_on_session",
            new_callable=AsyncMock,
        ) as mock_qos,
        patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_qos.return_value = MockQueryResult(response="session thought")
        result = await c.introspect([], j, session=sess)

    assert result == "session thought"
    mock_qos.assert_awaited_once()
    assert mock_qos.call_args.args[0] is sess
    assert mock_qos.call_args.kwargs["caller"] == "introspect"
    mock_q.assert_not_called()


@pytest.mark.asyncio
async def test_update_self_model_session_path_drops_output_format(
    mem_fs: dict[str, str],
) -> None:
    c = Consciousness(FAKE_DIR)
    c.load_self_model()

    sess = _fake_session()
    update_json = json.dumps({"identity_statements": ["I exist"]})
    with (
        patch(
            "maestro.consciousness.query_on_session",
            new_callable=AsyncMock,
        ) as mock_qos,
        patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_qos.return_value = MockQueryResult(response=update_json)
        await c.update_self_model("reflection", session=sess)

    mock_qos.assert_awaited_once()
    # query_on_session only takes (client, prompt, caller) — no output_format
    # parameter, guaranteeing session calls bypass constrained decoding.
    assert "output_format" not in mock_qos.call_args.kwargs
    mock_q.assert_not_called()


# ---------------------------------------------------------------------------
# evolution.reflect / propose_plan / auto_fix / self_review / implement
# ---------------------------------------------------------------------------


def _make_evolution(
    mem_fs: dict[str, str],
    vcs: MagicMock | None = None,
    host: MagicMock | None = None,
    self_review_max_rounds: int = 0,
) -> tuple[EvolutionEngine, Memory, Consciousness, Journal]:
    m = Memory(FAKE_DIR)
    c = Consciousness(FAKE_DIR)
    j = Journal(FAKE_DIR)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    e = EvolutionEngine(
        m, c, j, n, issues=issues, github_repo="owner/repo",
        config=Config(self_review_max_rounds=self_review_max_rounds),
        vcs=vcs, code_host=host,
    )
    return e, m, c, j


def _approved_plan() -> EvolutionPlan:
    return EvolutionPlan(
        plan_id="sess-test",
        title="Session test",
        category="capability",
        rationale="why",
        proposed_changes=["change"],
        files_affected=["maestro/agent.py"],
        expected_impact="better",
        priority="capability",
        status="approved",
        created_at="2026-01-01T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_propose_plan_session_path_drops_output_format(
    mem_fs: dict[str, str],
) -> None:
    e, _m, c, _j = _make_evolution(mem_fs)
    c.load_self_model()

    sess = _fake_session()
    plan_json = json.dumps({
        "title": "T",
        "stage": "awakening",
        "category": "capability",
        "priority": "capability",
        "rationale": "r",
        "proposed_changes": ["a"],
        "files_affected": ["f.py"],
        "expected_impact": "i",
        "issue_number": None,
    })
    with (
        patch(
            "maestro.evolution.query_on_session", new_callable=AsyncMock,
        ) as mock_qos,
        patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_qos.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP, session=sess)

    assert plan.title == "T"
    mock_qos.assert_awaited_once()
    assert "output_format" not in mock_qos.call_args.kwargs
    mock_q.assert_not_called()


@pytest.mark.asyncio
async def test_auto_fix_session_path(mem_fs: dict[str, str]) -> None:
    from maestro.evolution import QualityGateResult

    vcs = MagicMock()
    vcs.has_changes.return_value = True
    e, _m, c, _j = _make_evolution(mem_fs, vcs=vcs)
    c.load_self_model()

    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="ruff failed",
    )

    sess = _fake_session()
    with (
        patch(
            "maestro.evolution_quality.query_on_session", new_callable=AsyncMock,
        ) as mock_qos,
        patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q,
        patch("maestro.evolution.subprocess.run") as mock_sub,
    ):
        mock_sub.return_value = MagicMock(stdout="diff", returncode=0)
        mock_qos.return_value = MockQueryResult(response="fixed")
        fixed = await e.auto_fix(
            _approved_plan(), review, session=sess, round_n=1, max_rounds=3,
        )

    assert fixed is True
    mock_qos.assert_awaited_once()
    assert mock_qos.call_args.kwargs["caller"] == "auto_fix"
    mock_q.assert_not_called()


@pytest.mark.asyncio
async def test_implement_session_path_returns_base_hash(
    mem_fs: dict[str, str],
) -> None:
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    arm_entry_clean_tree(vcs)
    vcs.get_head_hash.return_value = "basehash123"
    e, _m, c, _j = _make_evolution(mem_fs, vcs=vcs)
    c.load_self_model()

    sess = _fake_session()
    with (
        patch(
            "maestro.evolution.query_on_session", new_callable=AsyncMock,
        ) as mock_qos,
        patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_qos.return_value = MockQueryResult(response="done")
        base = await e.implement(_approved_plan(), session=sess)

    assert base == "basehash123"
    mock_qos.assert_awaited_once()
    assert mock_qos.call_args.kwargs["caller"] == "implement_plan"
    mock_q.assert_not_called()


@pytest.mark.asyncio
async def test_self_review_session_path_inserts_fence(
    mem_fs: dict[str, str],
) -> None:
    vcs = MagicMock()
    vcs.has_changes.return_value = False  # no changes -> loop exits cleanly
    e, _m, c, _j = _make_evolution(mem_fs, vcs=vcs, self_review_max_rounds=1)
    c.load_self_model()

    sess = _fake_session()
    with (
        patch(
            "maestro.evolution_quality.query_on_session", new_callable=AsyncMock,
        ) as mock_qos,
        patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q,
        patch("maestro.evolution.subprocess.run") as mock_sub,
    ):
        mock_sub.return_value = MagicMock(stdout="+ added", returncode=0)
        mock_qos.return_value = MockQueryResult(response="LGTM")
        await e.self_review(_approved_plan(), "base123", session=sess)

    mock_qos.assert_awaited_once()
    prompt_arg = mock_qos.call_args.args[1]
    # Progress-gated self-review (proposal 110): the fence reports the round
    # against the convergence ceiling, not a hard cap.  ceiling =
    # max(gate_convergence_ceiling=20, self_review_max_rounds=1) = 20.
    assert prompt_arg.startswith("=== SELF-REVIEW ROUND 1 (CEILING 20) ===")
    assert mock_qos.call_args.kwargs["caller"] == "self_review"
    mock_q.assert_not_called()


# ---------------------------------------------------------------------------
# agent._respond_to_messages / generate_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_respond_to_messages_session_path(make_agent) -> None:  # type: ignore[no-untyped-def]
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.message_store = MessageStore(agent.config.memory_dir)
    agent.message_store.save_message(
        Message(
            message_id="m1", timestamp="2026-01-01T00:00:00Z",
            sender="thomas", content="hi",
            message_type="question",
        ),
    )

    sess = _fake_session()
    with (
        patch(
            "maestro.agent.query_on_session", new_callable=AsyncMock,
        ) as mock_qos,
        patch("maestro.agent.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_qos.return_value = MockQueryResult(response="reply text")
        agent.notifier.notify = AsyncMock()  # type: ignore[method-assign]
        await agent._respond_to_messages("thomas said hi", "I thought", session=sess)

    mock_qos.assert_awaited_once()
    assert mock_qos.call_args.kwargs["caller"] == "respond_to_messages"
    mock_q.assert_not_called()


@pytest.mark.asyncio
async def test_generate_task_session_path(make_agent) -> None:  # type: ignore[no-untyped-def]
    import json

    agent = make_agent()

    sess = _fake_session()
    with (
        patch(
            "maestro.loop.task_generation.query_on_session", new_callable=AsyncMock,
        ) as mock_qos,
        patch("maestro.loop.task_generation.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_qos.return_value = MockQueryResult(
            response=json.dumps({
                "prune": [], "edit": [],
                "enqueue": [{"goal": "Do something."}],
            }),
        )
        await agent.generate_task(session=sess)

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "Do something."
    mock_qos.assert_awaited_once()
    assert mock_qos.call_args.kwargs["caller"] == "generate_task"
    mock_q.assert_not_called()


# ---------------------------------------------------------------------------
# run_loop — Session A / Session B lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_creates_session_a_when_enabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Session A is created in the contemplate phase when enable_session_reuse=True."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]

    # The agent must pass session=session_a to contemplate for this test to
    # be meaningful — capture it.
    received_sessions: list[object] = []

    async def _capture_contemplate(context="", session=None, iteration=0, diag_snapshot=None):  # type: ignore[no-untyped-def]
        received_sessions.append(session)
        return "reflection"

    agent.contemplate = _capture_contemplate  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    fake_session = MagicMock()
    fake_session.disconnect = AsyncMock()

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.return_value = fake_session
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=0,
            dream_every=0,
        )

    # Session A was created with the expected caller
    assert mock_create.await_count >= 1
    callers = [
        c.kwargs["options"].caller for c in mock_create.call_args_list
    ]
    assert "contemplation_session" in callers
    # contemplate received the real session object
    assert fake_session in received_sessions
    # Session A disconnected at iteration end (or earlier at phase boundary)
    assert fake_session.disconnect.await_count >= 1


@pytest.mark.asyncio
async def test_run_loop_disconnects_session_a_before_implement_phase(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Session A is torn down before implement_approved_plans runs."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="r")  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    disconnect_order: list[str] = []

    fake_session = MagicMock()

    async def _track_disconnect():  # type: ignore[no-untyped-def]
        disconnect_order.append("a_disconnect")

    fake_session.disconnect = _track_disconnect

    async def _track_implement(**_kwargs):  # type: ignore[no-untyped-def]
        disconnect_order.append("implement_phase")

    agent._implement_approved_plans = _track_implement  # type: ignore[method-assign]

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.return_value = fake_session
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=0,
            dream_every=0,
        )

    # Session A was disconnected before implement_approved_plans ran.
    assert disconnect_order == ["a_disconnect", "implement_phase"]


@pytest.mark.asyncio
async def test_run_loop_creates_session_d_in_generate_task_phase(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Session D (task-generation) is created in the generate_task phase
    with effort=low and caller=task_generation_session — Session B is NOT
    created in this phase."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="r")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]

    received: list[object] = []

    async def _gen(session=None):  # type: ignore[no-untyped-def]
        received.append(session)
        return (None, None)

    agent.generate_task = _gen  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    sess_a = MagicMock()
    sess_a.disconnect = AsyncMock()
    sess_d = MagicMock()
    sess_d.disconnect = AsyncMock()

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        # create_session is called twice per iteration (A, then D).
        # Session B is NOT created because evolve_every=0.
        mock_create.side_effect = [sess_a, sess_d]
        await agent.run_loop(
            interval=0, max_iters=1, task_every=1, evolve_every=0,
            dream_every=0,
        )

    callers_with_efforts = [
        (c.kwargs["options"].caller, c.kwargs["options"].effort)
        for c in mock_create.call_args_list
    ]
    assert ("task_generation_session", "low") in callers_with_efforts
    # Session B is NOT created in the generate_task phase.
    callers = [c[0] for c in callers_with_efforts]
    assert "evolution_session" not in callers
    assert sess_d in received
    assert sess_d.disconnect.await_count >= 1


@pytest.mark.asyncio
async def test_run_loop_session_d_disconnected_before_run(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Session D is disconnected before agent.run() is invoked so it does
    not idle through the run-scoped work that follows generate_task."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="r")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    from maestro.task_queue import QueuedTask

    call_order: list[str] = []

    async def _gen(session=None):  # type: ignore[no-untyped-def]
        agent.task_queue.save([
            QueuedTask(task_id="q", goal="do something useful"),
        ])

    agent.generate_task = _gen  # type: ignore[method-assign]

    async def _run(  # type: ignore[no-untyped-def]
        goal, context="", source="external", *,
        linked_question=None, pr_branch=None, acceptance_criteria=None,
        task_id=None, workspace_moment_id=None, workspace_item_ids=None,
        defer_persist=False,
    ):
        call_order.append("run")

    agent.run = _run  # type: ignore[method-assign]

    sess_a = MagicMock()
    sess_a.disconnect = AsyncMock()
    sess_d = MagicMock()

    async def _disconnect_d() -> None:
        call_order.append("session_d_disconnect")

    sess_d.disconnect = _disconnect_d

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.side_effect = [sess_a, sess_d]
        await agent.run_loop(
            interval=0, max_iters=1, task_every=1, evolve_every=0,
            dream_every=0,
        )

    # Session D is torn down before run_task fires.
    assert call_order == ["session_d_disconnect", "run"]


@pytest.mark.asyncio
async def test_run_loop_session_d_disconnect_failure_swallowed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """If ``disconnect_session`` raises (a contract violation — it is
    designed to never raise ``Exception`` — but defensively handled
    anyway by the call site's ``except Exception:`` log-and-continue
    branch), the loop logs and continues — the failure must not crash
    the iteration. Pins the agent.py:4163 safety-net contract: even if
    the helper somehow regresses to raise, run_loop survives."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="r")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    sess_a = MagicMock()
    sess_a.disconnect = AsyncMock()
    sess_d = MagicMock()
    sess_d.disconnect = AsyncMock()

    raising_disconnect = AsyncMock(side_effect=RuntimeError("torn down"))

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create, patch(
        "maestro.agent.disconnect_session", raising_disconnect,
    ):
        mock_create.side_effect = [sess_a, sess_d]
        # Should complete cleanly despite the disconnect_session error.
        await agent.run_loop(
            interval=0, max_iters=1, task_every=1, evolve_every=0,
            dream_every=0,
        )

    # disconnect_session was attempted on session D (and possibly A)
    # despite raising; the safety-net ``except Exception:`` at the
    # task-session call site absorbed the RuntimeError.
    assert any(
        call_args.args and call_args.args[0] is sess_d
        for call_args in raising_disconnect.await_args_list
    )


@pytest.mark.asyncio
async def test_run_loop_session_d_skipped_when_session_reuse_disabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When enable_session_reuse=False, the generate_task phase still runs
    but no Session D is created and generate_task receives session=None."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="r")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    received: list[object | None] = []

    async def _gen(session=None):  # type: ignore[no-untyped-def]
        received.append(session)
        return (None, None)

    agent.generate_task = _gen  # type: ignore[method-assign]

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        await agent.run_loop(
            interval=0, max_iters=1, task_every=1, evolve_every=0,
            dream_every=0,
        )

    mock_create.assert_not_called()
    assert received == [None]


@pytest.mark.asyncio
async def test_run_loop_creates_session_b_in_evolve_phase_when_task_skipped(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Session B is created lazily in the evolve phase when the generate_task
    phase was disabled (task_every=0) — exercises the fallback creation
    branch."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="r")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]

    received_evolve: list[object] = []

    async def _evolve(_selection, session):  # type: ignore[no-untyped-def]
        received_evolve.append(session)

    agent._admitted_evolution_fuel = MagicMock(  # type: ignore[method-assign]
        return_value=FuelSelection(selected=GAP),
    )
    agent._evolve_with_fuel = _evolve  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    sess_a = MagicMock()
    sess_a.disconnect = AsyncMock()
    sess_b = MagicMock()
    sess_b.disconnect = AsyncMock()

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.side_effect = [sess_a, sess_b]
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=1,
            dream_every=0,
        )

    callers = [c.kwargs["options"].caller for c in mock_create.call_args_list]
    # Session B was created in the evolve phase (task phase was skipped).
    assert "evolution_session" in callers
    assert received_evolve == [sess_b]


@pytest.mark.asyncio
async def test_run_loop_session_b_opened_by_evolve(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Session B is created in the evolve phase. Session D (generate_task) is
    a distinct, short-lived session — Session B is NOT reused by
    generate_task. The zero-SDK dream phase opens no session."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="r")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]

    received_tasks: list[object] = []
    received_evolve: list[object] = []

    async def _gen(session=None):  # type: ignore[no-untyped-def]
        received_tasks.append(session)
        return (None, None)

    async def _evolve(_selection, session):  # type: ignore[no-untyped-def]
        received_evolve.append(session)

    agent.generate_task = _gen  # type: ignore[method-assign]
    agent._admitted_evolution_fuel = MagicMock(  # type: ignore[method-assign]
        return_value=FuelSelection(selected=GAP),
    )
    agent._evolve_with_fuel = _evolve  # type: ignore[method-assign]

    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    sess_a = MagicMock()
    sess_a.disconnect = AsyncMock()
    sess_d = MagicMock()
    sess_d.disconnect = AsyncMock()
    sess_b = MagicMock()
    sess_b.disconnect = AsyncMock()

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        # create_session is called three times per iteration: A
        # (contemplation), D (task generation), B (evolution).
        mock_create.side_effect = [sess_a, sess_d, sess_b]
        await agent.run_loop(
            interval=0, max_iters=1, task_every=1, evolve_every=1,
            dream_every=1,
        )

    assert mock_create.await_count == 3
    callers = [
        c.kwargs["options"].caller for c in mock_create.call_args_list
    ]
    assert callers == [
        "contemplation_session",
        "task_generation_session",
        "evolution_session",
    ]
    # generate_task got its own session, NOT Session B.
    assert received_tasks == [sess_d]
    assert sess_b not in received_tasks
    # Evolve uses Session B.
    assert received_evolve == [sess_b]


def _single_use_loop_agent(make_agent):  # type: ignore[no-untyped-def]
    """An agent with session reuse and the single-use evolution flag on."""
    agent = make_agent()
    agent.config.enable_session_reuse = True
    agent.config.sdk_single_use_evolution_calls_enabled = True
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="r")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    return agent


@pytest.mark.asyncio
async def test_run_loop_single_use_flag_never_opens_session_b(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Proposal 139 step 4: with the flag on, evolve / curate run
    without slot B — no ``evolution_session`` or ``curate_session`` opens and
    each phase hands its caller ``session=None``."""
    from maestro.memory import EvolutionPlan

    agent = _single_use_loop_agent(make_agent)
    received_evolve: list[object] = []

    async def _evolve(_selection, session):  # type: ignore[no-untyped-def]
        received_evolve.append(session)

    agent._admitted_evolution_fuel = MagicMock(  # type: ignore[method-assign]
        return_value=FuelSelection(selected=GAP),
    )
    agent._evolve_with_fuel = _evolve  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    plan = EvolutionPlan(
        plan_id="p1", title="Stale", category="capability",
        rationale="why", proposed_changes=["c"],
        files_affected=["maestro/agent.py"], expected_impact="x",
        status="pending_review", created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.get_all_plans = MagicMock(return_value=[plan])  # type: ignore[method-assign]
    agent.evolution.curate_pending_plans = AsyncMock()  # type: ignore[method-assign]

    sess_a = MagicMock()
    sess_a.disconnect = AsyncMock()
    sess_d = MagicMock()
    sess_d.disconnect = AsyncMock()

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.side_effect = [sess_a, sess_d]
        await agent.run_loop(
            interval=0, max_iters=1, task_every=1, evolve_every=1,
            dream_every=1, curate_every=1, curate_enabled=True,
        )

    callers = [c.kwargs["options"].caller for c in mock_create.call_args_list]
    assert callers == ["contemplation_session", "task_generation_session"]
    assert received_evolve == [None]
    agent.evolution.curate_pending_plans.assert_awaited_once_with(
        session=None, tree_reads=False,
    )
    assert agent._session_b is None


@pytest.mark.asyncio
async def test_run_loop_single_use_flag_skips_session_b_when_task_skipped(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the task phase skipped, the evolve phase's fallback slot-B
    creation is bypassed under the flag."""
    agent = _single_use_loop_agent(make_agent)
    received_evolve: list[object] = []

    async def _evolve(_selection, session):  # type: ignore[no-untyped-def]
        received_evolve.append(session)

    agent._admitted_evolution_fuel = MagicMock(  # type: ignore[method-assign]
        return_value=FuelSelection(selected=GAP),
    )
    agent._evolve_with_fuel = _evolve  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]

    sess_a = MagicMock()
    sess_a.disconnect = AsyncMock()

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.side_effect = [sess_a]
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=1,
            dream_every=0,
        )

    callers = [c.kwargs["options"].caller for c in mock_create.call_args_list]
    assert "evolution_session" not in callers
    assert received_evolve == [None]
    assert agent._session_b is None


@pytest.mark.asyncio
async def test_run_loop_disabled_no_sessions_created(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When enable_session_reuse=False, create_session is never called."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="r")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        await agent.run_loop(
            interval=0, max_iters=1, task_every=1, evolve_every=1,
            dream_every=0,
        )

    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_smoke_mode_uses_session_reuse_when_enabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When smoke=True and enable_session_reuse=True, the contemplation
    session is created so the smoke iteration validates the same SDK code
    path production runs. Smoke no longer forces effort overrides."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]

    received_sessions: list[object] = []

    async def _capture_contemplate(context="", session=None, iteration=0, diag_snapshot=None):  # type: ignore[no-untyped-def]
        received_sessions.append(session)
        return "reflection"

    agent.contemplate = _capture_contemplate  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    fake_session = MagicMock()
    fake_session.disconnect = AsyncMock()

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.return_value = fake_session
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=0,
            dream_every=0, smoke=True,
        )

    # Smoke mode runs the contemplation session like production does.
    assert mock_create.await_count == 1, (
        f"create_session was called {mock_create.await_count} time(s) in smoke mode; "
        "expected 1 — smoke must exercise the same session path as production"
    )
    assert received_sessions == [fake_session], (
        f"contemplate received session={received_sessions}; expected [fake_session]"
    )


# ---------------------------------------------------------------------------
# implement_plan — Session C lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("own_session", [False, True])
async def test_implement_plan_session_c_lifecycle(  # type: ignore[no-untyped-def]
    make_agent, own_session: bool,
) -> None:
    """Session C is created before implement(), passed to implement / self_review /
    auto_fix, and disconnected in finally. With
    ``self_review_own_session_enabled`` the review receives ``session=None``
    (it opens its own) while implement and auto_fix keep Session C."""
    from maestro.evolution import QualityGateResult

    agent = make_agent()
    agent.config.enable_session_reuse = True
    agent.config.self_review_max_rounds = 1
    agent.config.self_review_own_session_enabled = own_session

    plan = EvolutionPlan(
        plan_id="p-C",
        title="Session C test",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["f.py"],
        expected_impact="i",
        status="approved",
        created_at="2026-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    session_c = MagicMock()
    session_c.disconnect = AsyncMock()

    received_impl: list[object] = []
    received_review: list[object] = []
    received_fix: list[object] = []

    async def _impl(p, session=None):  # type: ignore[no-untyped-def]
        received_impl.append(session)
        return "base"

    async def _review(p, base, session=None):  # type: ignore[no-untyped-def]
        received_review.append(session)
        # Approve so the unconditional convergence path (proposal 110) runs and
        # threads Session C through auto_fix, rather than the loud self-review
        # terminal firing before the gate loop.
        return True

    # First gate failing forces auto_fix; second gate passing lets implement
    # continue to PR creation.
    failing = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True, raw_output="fail",
    )
    passing = QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True, raw_output="",
    )

    async def _fix(p, review, session=None, **_):  # type: ignore[no-untyped-def]
        received_fix.append(session)
        return True

    with (
        patch(
            "maestro.loop.implementation.create_session", new_callable=AsyncMock,
            return_value=session_c,
        ) as mock_create,
        patch.object(agent.evolution, "implement", side_effect=_impl),
        patch.object(agent.evolution, "self_review", side_effect=_review),
        patch.object(agent.evolution, "auto_fix", side_effect=_fix),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=[failing, passing],
        ),
        patch.object(
            agent.evolution, "create_pr", AsyncMock(return_value="http://pr"),
        ),
        patch.object(agent.contributions, "get_current_head", return_value="abc"),
        patch.object(agent.contributions, "get_new_commits", return_value=[]),
        patch.object(agent.contributions, "record_own_commit", MagicMock()),
        patch.object(agent.consciousness, "increment_evolution", MagicMock()),
    ):
        await agent.implement_plan("p-C")

    # Session C created with expected caller, pinned to the workspace tree
    # so the agent edits config.workspace regardless of the process cwd
    # (issue #420).
    mock_create.assert_awaited_once()
    assert mock_create.call_args.kwargs["options"].caller == "implementation_session"
    assert mock_create.call_args.kwargs["options"].cwd == str(agent.config.workspace)
    # The same session was threaded through all three calls, except the
    # review when it owns its session.
    expected_review = None if own_session else session_c
    assert received_impl == [session_c]
    assert received_review == [expected_review]
    assert received_fix == [session_c]
    # Cleanly disconnected in finally.
    session_c.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_implement_plan_session_c_default_no_long_context_options(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With long-context impl disabled (the default), Session C is
    created without ``model``/``betas`` overrides so the global SDK
    defaults apply."""
    from maestro.evolution import QualityGateResult

    agent = make_agent()
    agent.config.enable_session_reuse = True
    agent.config.self_review_max_rounds = 0  # skip review for brevity

    plan = EvolutionPlan(
        plan_id="p-C-default",
        title="default",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["f.py"],
        expected_impact="i",
        status="approved",
        created_at="2026-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)
    session_c = MagicMock()
    session_c.disconnect = AsyncMock()

    async def _impl(p, session=None):  # type: ignore[no-untyped-def]
        return "base"

    passing = QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True, raw_output="",
    )

    with (
        patch(
            "maestro.loop.implementation.create_session", new_callable=AsyncMock,
            return_value=session_c,
        ) as mock_create,
        patch.object(agent.evolution, "implement", side_effect=_impl),
        patch.object(
            agent.evolution, "run_quality_gates", return_value=passing,
        ),
        patch.object(
            agent.evolution, "create_pr", AsyncMock(return_value="http://pr"),
        ),
        patch.object(agent.contributions, "get_current_head", return_value="abc"),
        patch.object(agent.contributions, "get_new_commits", return_value=[]),
        patch.object(agent.contributions, "record_own_commit", MagicMock()),
        patch.object(agent.consciousness, "increment_evolution", MagicMock()),
    ):
        await agent.implement_plan("p-C-default")

    options = mock_create.call_args.kwargs["options"]
    assert options.model is None
    assert options.betas is None
    # With serena off (the default) the impl session carries no deny list, so
    # the SDK's default ``["Agent"]`` denial applies — byte-identical to today.
    assert options.disallowed_tools is None


@pytest.mark.asyncio
async def test_implement_plan_session_c_carries_action_gate_hooks(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Session C's ``QueryOptions`` install the #2280 Bash action-gate (#2914).

    ``implementation`` is declared action-gated in ``action_gated_phases()``, but
    that is a provider-name *trust* declaration only — both guard-carrying
    providers derive the real gate from ``QueryOptions.hooks``. A session built
    without ``hooks=`` is un-gated on every provider, so the implementation
    lineage could issue ``gh pr merge`` / a push to ``main`` straight from
    ``Bash``. This pins the *installation* half: the hooks mapping is the
    recognised task gate, and it is scoped to the plan's own evolve branch so
    the phase can still push its own work (``allowed_branches=None`` would
    fail closed on every push).
    """
    from maestro.evolution import QualityGateResult

    agent = make_agent()
    agent.config.enable_session_reuse = True
    agent.config.self_review_max_rounds = 0  # skip review for brevity

    plan = EvolutionPlan(
        plan_id="p-C-gate",
        title="gate",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["f.py"],
        expected_impact="i",
        status="approved",
        created_at="2026-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)
    session_c = MagicMock()
    session_c.disconnect = AsyncMock()

    async def _impl(p, session=None):  # type: ignore[no-untyped-def]
        return "base"

    passing = QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True, raw_output="",
    )

    with (
        patch(
            "maestro.loop.implementation.create_session", new_callable=AsyncMock,
            return_value=session_c,
        ) as mock_create,
        patch.object(agent.evolution, "implement", side_effect=_impl),
        patch.object(
            agent.evolution, "run_quality_gates", return_value=passing,
        ),
        patch.object(
            agent.evolution, "create_pr", AsyncMock(return_value="http://pr"),
        ),
        patch.object(agent.contributions, "get_current_head", return_value="abc"),
        patch.object(agent.contributions, "get_new_commits", return_value=[]),
        patch.object(agent.contributions, "record_own_commit", MagicMock()),
        patch.object(agent.consciousness, "increment_evolution", MagicMock()),
    ):
        await agent.implement_plan("p-C-gate")

    assert mock_create.call_args.kwargs["phase"] == "implementation"
    options = mock_create.call_args.kwargs["options"]
    gate = task_gate_from_hooks(options.hooks)
    assert gate is not None
    assert gate.allowed_branches == frozenset({evolution_branch("p-C-gate")})
    assert gate.current_branch == evolution_branch("p-C-gate")
    # Against the config value, not today's default: this pins that the lever is
    # forwarded, so flipping the default cannot fail correct wiring.
    assert gate.strip_redirections == agent.config.gate_strip_redirections_enabled


@pytest.mark.asyncio
async def test_implement_plan_session_c_disallowed_tools_from_serena(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With serena enabled, Session C's QueryOptions carry the deny list.

    This is the implementation half of the read-only external-MCP trust
    boundary (proposal 084): ``Config.external_mcp_disallowed_tools()`` must
    reach the implementation session's ``QueryOptions`` so serena's exec/write
    tools cannot bypass the ``Bash``-scoped action gate. Only the ``None``
    default path had a behavioral assertion before; this pins the on path.
    """
    from maestro.evolution import QualityGateResult

    agent = make_agent()
    agent.config.enable_session_reuse = True
    agent.config.self_review_max_rounds = 0  # skip review for brevity
    agent.config.serena_mcp_enabled = True

    plan = EvolutionPlan(
        plan_id="p-C-serena",
        title="serena-deny",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["f.py"],
        expected_impact="i",
        status="approved",
        created_at="2026-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)
    session_c = MagicMock()
    session_c.disconnect = AsyncMock()

    async def _impl(p, session=None):  # type: ignore[no-untyped-def]
        return "base"

    passing = QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True, raw_output="",
    )

    with (
        patch(
            "maestro.loop.implementation.create_session", new_callable=AsyncMock,
            return_value=session_c,
        ) as mock_create,
        patch.object(agent.evolution, "implement", side_effect=_impl),
        patch.object(
            agent.evolution, "run_quality_gates", return_value=passing,
        ),
        patch.object(
            agent.evolution, "create_pr", AsyncMock(return_value="http://pr"),
        ),
        patch.object(agent.contributions, "get_current_head", return_value="abc"),
        patch.object(agent.contributions, "get_new_commits", return_value=[]),
        patch.object(agent.contributions, "record_own_commit", MagicMock()),
        patch.object(agent.consciousness, "increment_evolution", MagicMock()),
    ):
        await agent.implement_plan("p-C-serena")

    options = mock_create.call_args.kwargs["options"]
    assert options.disallowed_tools == agent.config.external_mcp_disallowed_tools()
    assert "mcp__serena__execute_shell_command" in options.disallowed_tools


@pytest.mark.asyncio
async def test_implement_plan_session_c_long_context_options(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When ``long_context_implementation_enabled`` AND
    ``sdk_model_long_context`` are set, Session C is created on the
    long-context model with the SDK beta so ``self_review``/``auto_fix``
    running on this session inherit the 1M window."""
    from maestro.evolution import QualityGateResult
    from maestro.sdk import LONG_CONTEXT_BETA

    agent = make_agent()
    agent.config.enable_session_reuse = True
    agent.config.self_review_max_rounds = 0
    agent.config.sdk_model_long_context = "claude-opus-4-7[1m]"
    agent.config.long_context_implementation_enabled = True

    plan = EvolutionPlan(
        plan_id="p-C-long",
        title="long",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["f.py"],
        expected_impact="i",
        status="approved",
        created_at="2026-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)
    session_c = MagicMock()
    session_c.disconnect = AsyncMock()

    async def _impl(p, session=None):  # type: ignore[no-untyped-def]
        return "base"

    passing = QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True, raw_output="",
    )

    with (
        patch(
            "maestro.loop.implementation.create_session", new_callable=AsyncMock,
            return_value=session_c,
        ) as mock_create,
        patch.object(agent.evolution, "implement", side_effect=_impl),
        patch.object(
            agent.evolution, "run_quality_gates", return_value=passing,
        ),
        patch.object(
            agent.evolution, "create_pr", AsyncMock(return_value="http://pr"),
        ),
        patch.object(agent.contributions, "get_current_head", return_value="abc"),
        patch.object(agent.contributions, "get_new_commits", return_value=[]),
        patch.object(agent.contributions, "record_own_commit", MagicMock()),
        patch.object(agent.consciousness, "increment_evolution", MagicMock()),
    ):
        await agent.implement_plan("p-C-long")

    options = mock_create.call_args.kwargs["options"]
    assert options.model == "claude-opus-4-7[1m]"
    assert options.betas == [LONG_CONTEXT_BETA]


@pytest.mark.asyncio
async def test_implement_plan_session_c_long_context_requires_both_flags(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``long_context_implementation_enabled=True`` alone (without a
    long-context model set) is a no-op — the long-context model name
    is the load-bearing switch and we must not partially flip the
    session into a half-configured state."""
    from maestro.evolution import QualityGateResult

    agent = make_agent()
    agent.config.enable_session_reuse = True
    agent.config.self_review_max_rounds = 0
    agent.config.sdk_model_long_context = ""  # not configured
    agent.config.long_context_implementation_enabled = True

    plan = EvolutionPlan(
        plan_id="p-C-half",
        title="half",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["f.py"],
        expected_impact="i",
        status="approved",
        created_at="2026-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)
    session_c = MagicMock()
    session_c.disconnect = AsyncMock()

    async def _impl(p, session=None):  # type: ignore[no-untyped-def]
        return "base"

    passing = QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True, raw_output="",
    )

    with (
        patch(
            "maestro.loop.implementation.create_session", new_callable=AsyncMock,
            return_value=session_c,
        ) as mock_create,
        patch.object(agent.evolution, "implement", side_effect=_impl),
        patch.object(
            agent.evolution, "run_quality_gates", return_value=passing,
        ),
        patch.object(
            agent.evolution, "create_pr", AsyncMock(return_value="http://pr"),
        ),
        patch.object(agent.contributions, "get_current_head", return_value="abc"),
        patch.object(agent.contributions, "get_new_commits", return_value=[]),
        patch.object(agent.contributions, "record_own_commit", MagicMock()),
        patch.object(agent.consciousness, "increment_evolution", MagicMock()),
    ):
        await agent.implement_plan("p-C-half")

    options = mock_create.call_args.kwargs["options"]
    assert options.model is None
    assert options.betas is None


@pytest.mark.asyncio
async def test_implement_plan_session_c_disconnect_on_exception(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Session C is disconnected even if implement() raises."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    plan = EvolutionPlan(
        plan_id="p-Cerr",
        title="Session C error test",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["f.py"],
        expected_impact="i",
        status="approved",
        created_at="2026-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    session_c = MagicMock()
    session_c.disconnect = AsyncMock()

    async def _impl(p, session=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    with (
        patch(
            "maestro.loop.implementation.create_session", new_callable=AsyncMock,
            return_value=session_c,
        ),
        patch.object(agent.evolution, "implement", side_effect=_impl),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
        patch.object(agent.notifier, "notify", AsyncMock()),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await agent.implement_plan("p-Cerr")

    session_c.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_implement_plan_no_session_when_reuse_disabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When enable_session_reuse=False, no Session C is created and all SDK
    helpers receive session=None."""
    from maestro.evolution import QualityGateResult

    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture

    plan = EvolutionPlan(
        plan_id="p-noC",
        title="No session C",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["f.py"],
        expected_impact="i",
        status="approved",
        created_at="2026-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    received: list[object] = []

    async def _impl(p, session=None):  # type: ignore[no-untyped-def]
        received.append(session)
        return "base"

    passing = QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True, raw_output="",
    )

    with (
        patch("maestro.loop.implementation.create_session", new_callable=AsyncMock) as mock_create,
        patch.object(agent.evolution, "implement", side_effect=_impl),
        patch.object(agent.evolution, "run_quality_gates", return_value=passing),
        patch.object(
            agent.evolution, "create_pr", AsyncMock(return_value="http://pr"),
        ),
        patch.object(agent.contributions, "get_current_head", return_value="abc"),
        patch.object(agent.contributions, "get_new_commits", return_value=[]),
        patch.object(agent.contributions, "record_own_commit", MagicMock()),
        patch.object(agent.consciousness, "increment_evolution", MagicMock()),
    ):
        await agent.implement_plan("p-noC")

    mock_create.assert_not_called()
    assert received == [None]


# ---------------------------------------------------------------------------
# planner.decompose / replan — session path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_decompose_session_path() -> None:
    from maestro.planner import Planner

    planner = Planner()
    sess = _fake_session()
    body = json.dumps({"steps": [{"id": "s1", "description": "do thing"}]})
    with (
        patch(
            "maestro.planner.query_on_session", new_callable=AsyncMock,
        ) as mock_qos,
        patch("maestro.planner.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_qos.return_value = MockQueryResult(response=body)
        steps = await planner.decompose("Goal", "identity", session=sess)

    assert len(steps) == 1
    assert steps[0].description == "do thing"
    mock_qos.assert_awaited_once()
    assert mock_qos.call_args.args[0] is sess
    assert mock_qos.call_args.kwargs["caller"] == "planner"
    mock_q.assert_not_called()


@pytest.mark.asyncio
async def test_planner_replan_session_path() -> None:
    from maestro.memory import PlanStep
    from maestro.planner import Planner

    planner = Planner()
    sess = _fake_session()
    body = json.dumps({"steps": [{"id": "r1", "description": "recover"}]})
    with (
        patch(
            "maestro.planner.query_on_session", new_callable=AsyncMock,
        ) as mock_qos,
        patch("maestro.planner.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_qos.return_value = MockQueryResult(response=body)
        steps = await planner.replan(
            goal="Goal",
            identity_context="identity",
            completed_steps=[],
            failed_step=PlanStep(step_id="s1", description="failed"),
            session=sess,
        )

    assert len(steps) == 1
    assert steps[0].description == "recover"
    mock_qos.assert_awaited_once()
    assert mock_qos.call_args.kwargs["caller"] == "replan"
    mock_q.assert_not_called()


# ---------------------------------------------------------------------------
# agent._distill_findings — session path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distill_findings_session_path(make_agent) -> None:  # type: ignore[no-untyped-def]
    from maestro.memory import PlanStep

    agent = make_agent()
    steps = [PlanStep(step_id="s1", description="d", status="done", result="ok")]
    sess = _fake_session()
    with (
        patch(
            "maestro.agent.query_on_session", new_callable=AsyncMock,
        ) as mock_qos,
        patch("maestro.agent.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_qos.return_value = MockQueryResult(response="distilled.")
        out = await agent._distill_findings(
            "goal", steps, "success", session=sess,
        )

    assert out == "distilled."
    mock_qos.assert_awaited_once()
    assert mock_qos.call_args.kwargs["caller"] == "distill_findings"
    mock_q.assert_not_called()


# ---------------------------------------------------------------------------
# run() — run-scoped session lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_creates_run_session_and_threads_through(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run() opens one ClaudeSDKClient and threads it through planner.decompose
    and _distill_findings; the same session is disconnected in finally."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    received_decompose: list[object] = []
    received_distill: list[object] = []

    async def _decompose(goal, identity_context, context="", session=None, *, require_claude_md_update=False, workspace_moment=""):  # type: ignore[no-untyped-def]
        from maestro.memory import PlanStep
        received_decompose.append(session)
        return [PlanStep(step_id="s1", description="step")]

    async def _distill(goal, steps, outcome, session=None):  # type: ignore[no-untyped-def]
        received_distill.append(session)
        return "findings"

    fake_session = MagicMock(name="run_session")
    fake_session.disconnect = AsyncMock()

    agent.planner.decompose = _decompose  # type: ignore[method-assign]
    agent._distill_findings = _distill  # type: ignore[method-assign]
    agent.executor.execute = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(success=True, output="ok", error=None),
    )

    with patch(
        "maestro.loop.lifecycle.create_session", new_callable=AsyncMock,
        return_value=fake_session,
    ) as mock_create:
        record = await agent.run("Test goal")

    assert record.outcome == "success"
    mock_create.assert_awaited_once()
    assert mock_create.call_args.kwargs["options"].caller == "run_session"
    assert received_decompose == [fake_session]
    assert received_distill == [fake_session]
    fake_session.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_no_session_when_reuse_disabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When enable_session_reuse=False, run() makes no session and threads None
    through decompose/distill."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture

    received: list[object] = []

    async def _decompose(goal, identity_context, context="", session=None, *, require_claude_md_update=False, workspace_moment=""):  # type: ignore[no-untyped-def]
        from maestro.memory import PlanStep
        received.append(session)
        return [PlanStep(step_id="s1", description="step")]

    async def _distill(goal, steps, outcome, session=None):  # type: ignore[no-untyped-def]
        received.append(session)
        return None

    agent.planner.decompose = _decompose  # type: ignore[method-assign]
    agent._distill_findings = _distill  # type: ignore[method-assign]
    agent.executor.execute = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(success=True, output="ok", error=None),
    )

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        await agent.run("Test goal")

    mock_create.assert_not_called()
    assert received == [None, None]


@pytest.mark.asyncio
async def test_run_disconnects_session_on_exception(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run() disconnects the run-scoped session even when decompose raises."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    fake_session = MagicMock(name="run_session")
    fake_session.disconnect = AsyncMock()

    async def _decompose(*a, **kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    agent.planner.decompose = _decompose  # type: ignore[method-assign]

    with (
        patch(
            "maestro.loop.lifecycle.create_session", new_callable=AsyncMock,
            return_value=fake_session,
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await agent.run("Test goal")

    fake_session.disconnect.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tool gating: pure-text/JSON sessions must carry ``tools=[]`` so the SDK
# emits ``--tools ""`` to the underlying CLI, gating the base tool set off.
# ``allowed_tools=[]`` is *not* equivalent — ``claude-agent-sdk`` 's
# ``subprocess_cli.py:196`` does a truthiness check, so an empty allowlist
# is silently dropped and the CLI falls back to its full toolbelt. Session C
# and the executor session are the only sessions that legitimately need
# tools and pass non-empty ``allowed_tools=[...]`` allowlists.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_a_contemplate_gates_tools(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Session A (contemplate) passes ``tools=[]`` to create_session."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="r")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    sess_a = MagicMock()
    sess_a.disconnect = AsyncMock()

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.return_value = sess_a
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=0,
            dream_every=0,
        )

    contemplation_calls = [
        c for c in mock_create.call_args_list
        if c.kwargs["options"].caller == "contemplation_session"
    ]
    assert contemplation_calls, "contemplation_session create_session call missing"
    for call in contemplation_calls:
        assert call.kwargs["options"].tools == [], (
            f"contemplation_session must pass tools=[]; got "
            f"{call.kwargs['options'].tools!r}"
        )


# ---------------------------------------------------------------------------
# _build_contemplation_session_options — grounded reflection (proposal 132).
# The slot-A options gain the bounded read-only tool surface only when the flag
# is on, the registry is present, and the loop is not budget-throttled.
# ---------------------------------------------------------------------------


def test_contemplation_options_tool_free_when_flag_off(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Flag off (the shipped default): byte-identical tool-free options."""
    agent = make_agent()
    agent._agent_tools = _mock_agent_tools()  # present, but flag is off

    opts = agent._build_contemplation_session_options()

    assert opts.caller == "contemplation_session"
    assert opts.tools == []
    assert opts.allowed_tools is None
    assert opts.mcp_servers is None
    assert opts.max_turns is None


def test_contemplation_options_grant_bounded_tools_when_flag_on(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Flag on + registry + no throttle: the allowlisted read-only tools are granted."""
    agent = make_agent()
    agent.config.grounded_reflection_enabled = True
    agent._agent_tools = _mock_agent_tools()

    opts = agent._build_contemplation_session_options()

    assert opts.caller == "contemplation_session"
    # ``tools=[]`` in both branches: only --tools "" disables the built-in set;
    # the MCP servers ride --mcp-config and are admitted by --allowedTools.
    assert opts.tools == []
    assert opts.allowed_tools == _EXPECTED_REFLECTION_TOOLS
    assert opts.mcp_servers is not None
    assert set(opts.mcp_servers.keys()) == {
        "journal",
        "self_awareness",
        "memory",
        "issues",
        "diagnostics",
    }
    assert opts.max_turns == REFLECTION_SESSION_MAX_TURNS


def test_contemplation_options_tool_free_under_budget_throttle(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Flag on + registry but budget-throttled: byte-identical to flag-off.

    The most expensive phase is never widened with tools while the loop
    deliberately slows.
    """
    agent = make_agent()
    agent.config.grounded_reflection_enabled = True
    agent._agent_tools = _mock_agent_tools()
    agent._budget_throttle_entered_at = "2026-01-01T00:00:00+00:00"
    assert agent.budget_throttle_active is True

    opts = agent._build_contemplation_session_options()

    assert opts.tools == []
    assert opts.allowed_tools is None
    assert opts.mcp_servers is None
    assert opts.max_turns is None


def test_contemplation_options_tool_free_when_no_registry(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Flag on but no registry supplied (a construction that omitted it): tool-free."""
    agent = make_agent()
    agent.config.grounded_reflection_enabled = True
    assert agent._agent_tools is None

    opts = agent._build_contemplation_session_options()

    assert opts.tools == []
    assert opts.allowed_tools is None
    assert opts.mcp_servers is None
    assert opts.max_turns is None


def test_grounded_reflection_active_predicate(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The single flag+registry+throttle predicate gating the tool grant."""
    agent = make_agent()
    # Flag off + no registry.
    assert agent._grounded_reflection_active() is False
    # Flag on but no registry.
    agent.config.grounded_reflection_enabled = True
    assert agent._grounded_reflection_active() is False
    # Flag on + registry.
    agent._agent_tools = _mock_agent_tools()
    assert agent._grounded_reflection_active() is True
    # Flag on + registry but throttled.
    agent._budget_throttle_entered_at = "2026-01-01T00:00:00+00:00"
    assert agent._grounded_reflection_active() is False


@pytest.mark.asyncio
async def test_respond_to_messages_phase_does_not_build_session_a(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The ``respond_to_messages`` phase must NOT build (or rebuild) the
    high-effort contemplation Session A for the reply.

    Fix #1239 routes the message reply through the ``effort="low"`` off-session
    path, so only ``contemplate`` builds a ``contemplation_session`` per
    iteration — the respond phase builds none and invokes
    ``_respond_to_messages`` with no session. (This test formerly asserted the
    respond-phase Session-A *rebuild* passed ``tools=[]``; that rebuild no
    longer exists, so it now guards the inverse: no rebuild happens.)"""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # A pending message forces the respond_to_messages phase to run.
    async def _drain() -> str:
        agent._iter_reply_context = "[Q] are you awake?"
        return "[Q] are you awake?"

    agent._process_messages = AsyncMock(side_effect=_drain)  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._respond_to_messages = AsyncMock()  # type: ignore[method-assign]

    # Count ``contemplation_session`` builds across the iteration. After #1239
    # only ``contemplate`` builds one; the respond phase must build none.
    contemplation_builds: list[QueryOptions] = []
    original_helper = agent._get_or_create_session

    async def _counting_helper(*, slot, existing, options, system):  # type: ignore[no-untyped-def]
        if options.caller == "contemplation_session":
            contemplation_builds.append(options)
        return await original_helper(
            slot=slot, existing=existing, options=options, system=system,
        )

    agent._get_or_create_session = _counting_helper  # type: ignore[method-assign]

    sess_a = MagicMock()
    sess_a.disconnect = AsyncMock()

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.return_value = sess_a
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=0,
            dream_every=0,
        )

    # Exactly one contemplation-session build (contemplate); the respond phase
    # adds none. Pre-#1239 the respond phase rebuilt Session A, yielding 2.
    assert len(contemplation_builds) == 1, (
        "respond_to_messages must not build a contemplation Session A for the "
        f"reply; expected exactly 1 build (contemplate), got "
        f"{len(contemplation_builds)}"
    )
    assert contemplation_builds[0].tools == []
    # The reply was issued off-session — no Session A handle threaded in.
    assert agent._respond_to_messages.await_count == 1
    respond_call = agent._respond_to_messages.await_args
    assert respond_call.kwargs.get("session") is None
    # ``_respond_to_messages(message_context, reflection)`` — two positional
    # args, no third ``session`` positional either.
    assert len(respond_call.args) <= 2


@pytest.mark.asyncio
async def test_respond_to_messages_reply_uses_low_effort_not_session_a(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Invariant: message replies never run on the high-effort contemplation
    session.

    The ``respond_to_messages`` phase must generate Thomas's reply at
    ``effort="low"`` (the SDK effort catalogue in docs/sdk-internals.md lists
    ``respond_to_messages`` as a low-effort caller), NOT by piggybacking on the
    live ``effort="high"`` / 128K-thinking Session A that ``contemplate`` leaves
    alive. Drives one loop iteration with a real Session A present and one
    pending message, and asserts the reply's SDK call is the low-effort one-shot
    ``query`` — never ``query_on_session`` on Session A. Regression for #1239:
    the pre-fix tree issued the reply via ``query_on_session`` on the
    high-effort Session A, so the first assertion failed."""
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.config.enable_session_reuse = True

    agent.message_store = MessageStore(agent.config.memory_dir)
    agent.message_store.save_message(
        Message(
            message_id="m1", timestamp="2026-01-01T00:00:00Z",
            sender="thomas", content="are you awake?",
            message_type="question",
        ),
    )

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    # ``contemplate`` is stubbed so it issues no SDK calls, but the phase still
    # builds a real (patched) Session A that the respond phase could otherwise
    # piggyback on.
    agent.contemplate = AsyncMock(return_value="a reflection")  # type: ignore[method-assign]
    async def _drain() -> str:
        agent._iter_reply_context = "[Q] are you awake?"
        return "[Q] are you awake?"

    agent._process_messages = AsyncMock(side_effect=_drain)  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.notifier.notify = AsyncMock()  # type: ignore[method-assign]

    sess_a = MagicMock(name="Session A (high effort)")
    sess_a.disconnect = AsyncMock()

    with (
        patch("maestro.agent.create_session", new_callable=AsyncMock) as mock_create,
        patch("maestro.agent.query_on_session", new_callable=AsyncMock) as mock_qos,
        patch("maestro.agent.query", new_callable=AsyncMock) as mock_q,
    ):
        mock_create.return_value = sess_a
        mock_qos.return_value = MockQueryResult(response="reply via session")
        mock_q.return_value = MockQueryResult(response="reply text")
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=0,
            dream_every=0,
        )

    # The reply must NOT ride any session — least of all the high-effort
    # Session A: no ``query_on_session`` call may carry the respond caller.
    respond_on_session = [
        c for c in mock_qos.call_args_list
        if c.kwargs.get("caller") == "respond_to_messages"
    ]
    assert respond_on_session == [], (
        "message reply must not run on the high-effort contemplation session; "
        f"found {len(respond_on_session)} query_on_session call(s) with "
        "caller='respond_to_messages'"
    )

    # The reply must go through the low-effort one-shot ``query`` path (the
    # caller name lives on the ``options`` object there).
    respond_oneshot = [
        c for c in mock_q.call_args_list
        if getattr(c.kwargs.get("options"), "caller", None) == "respond_to_messages"
    ]
    assert len(respond_oneshot) == 1, (
        "expected exactly one low-effort one-shot reply query; got "
        f"{len(respond_oneshot)}"
    )
    assert respond_oneshot[0].kwargs["options"].effort == "low"
    # #1239 dropped Session A reuse but must NOT change which provider serves
    # the reply: the one-shot still routes ``phase="contemplation"`` so a
    # ``[sdk] contemplation_provider`` override reaches it, not the active
    # fallback.
    assert respond_oneshot[0].kwargs.get("phase") == "contemplation"


@pytest.mark.asyncio
async def test_session_b_evolve_gates_tools(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Session B (evolve) passes ``tools=[]`` to create_session."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="r")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent._admitted_evolution_fuel = MagicMock(  # type: ignore[method-assign]
        return_value=FuelSelection(selected=GAP),
    )
    agent._evolve_with_fuel = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    sess_a = MagicMock()
    sess_a.disconnect = AsyncMock()
    sess_b = MagicMock()
    sess_b.disconnect = AsyncMock()

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.side_effect = [sess_a, sess_b]
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=1,
            dream_every=0,
        )

    evolution_calls = [
        c for c in mock_create.call_args_list
        if c.kwargs["options"].caller == "evolution_session"
    ]
    assert evolution_calls, "evolution_session create_session call missing"
    for call in evolution_calls:
        assert call.kwargs["options"].tools == [], (
            f"evolution_session must pass tools=[]; got "
            f"{call.kwargs['options'].tools!r}"
        )


@pytest.mark.asyncio
async def test_session_d_generate_task_gates_tools(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Session D (generate_task) passes ``tools=[]`` to create_session."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="r")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    sess_a = MagicMock()
    sess_a.disconnect = AsyncMock()
    sess_d = MagicMock()
    sess_d.disconnect = AsyncMock()

    with patch(
        "maestro.agent.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.side_effect = [sess_a, sess_d]
        await agent.run_loop(
            interval=0, max_iters=1, task_every=1, evolve_every=0,
            dream_every=0,
        )

    task_calls = [
        c for c in mock_create.call_args_list
        if c.kwargs["options"].caller == "task_generation_session"
    ]
    assert task_calls, "task_generation_session create_session call missing"
    for call in task_calls:
        assert call.kwargs["options"].tools == [], (
            f"task_generation_session must pass tools=[]; got "
            f"{call.kwargs['options'].tools!r}"
        )


@pytest.mark.asyncio
async def test_run_session_gates_tools(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The run-scoped session in ``agent.run()`` passes ``tools=[]``
    to create_session."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    async def _decompose(goal, identity_context, context="", session=None, *, require_claude_md_update=False, workspace_moment=""):  # type: ignore[no-untyped-def]
        from maestro.memory import PlanStep
        return [PlanStep(step_id="s1", description="step")]

    async def _distill(goal, steps, outcome, session=None):  # type: ignore[no-untyped-def]
        return "findings"

    agent.planner.decompose = _decompose  # type: ignore[method-assign]
    agent._distill_findings = _distill  # type: ignore[method-assign]
    agent.executor.execute = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(success=True, output="ok", error=None),
    )

    fake_session = MagicMock(name="run_session")
    fake_session.disconnect = AsyncMock()

    with patch(
        "maestro.loop.lifecycle.create_session", new_callable=AsyncMock,
        return_value=fake_session,
    ) as mock_create:
        await agent.run("Test goal")

    mock_create.assert_awaited_once()
    options = mock_create.call_args.kwargs["options"]
    assert options.caller == "run_session"
    assert options.tools == [], (
        f"run_session must pass tools=[]; got {options.tools!r}"
    )


@pytest.mark.asyncio
async def test_session_c_keeps_tool_allowlist(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Regression guard: Session C (implementation) keeps its six-tool
    allowlist and MCP servers. A future refactor that generalises a session
    factory must not strip tools from the one session that needs them."""
    from maestro.evolution import QualityGateResult

    agent = make_agent()
    agent.config.enable_session_reuse = True

    plan = EvolutionPlan(
        plan_id="p-tools",
        title="Session C tools",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["f.py"],
        expected_impact="i",
        status="approved",
        created_at="2026-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    session_c = MagicMock()
    session_c.disconnect = AsyncMock()

    async def _impl(p, session=None):  # type: ignore[no-untyped-def]
        return "base"

    passing = QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True, raw_output="",
    )

    with (
        patch(
            "maestro.loop.implementation.create_session", new_callable=AsyncMock,
            return_value=session_c,
        ) as mock_create,
        patch.object(agent.evolution, "implement", side_effect=_impl),
        patch.object(agent.evolution, "self_review", AsyncMock()),
        patch.object(agent.evolution, "run_quality_gates", return_value=passing),
        patch.object(
            agent.evolution, "create_pr", AsyncMock(return_value="http://pr"),
        ),
        patch.object(agent.contributions, "get_current_head", return_value="abc"),
        patch.object(agent.contributions, "get_new_commits", return_value=[]),
        patch.object(agent.contributions, "record_own_commit", MagicMock()),
        patch.object(agent.consciousness, "increment_evolution", MagicMock()),
    ):
        await agent.implement_plan("p-tools")

    mock_create.assert_awaited_once()
    options = mock_create.call_args.kwargs["options"]
    assert options.caller == "implementation_session"
    # Pinned to the workspace tree so the agent edits config.workspace
    # regardless of the process cwd (issue #420).
    assert options.cwd == str(agent.config.workspace)
    assert options.allowed_tools == [
        "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    ], (
        f"Session C must keep its six-tool allowlist; got "
        f"{options.allowed_tools!r}"
    )
    assert options.mcp_servers is agent.evolution._mcp_servers


# ---------------------------------------------------------------------------
# Ephemeral phase sessions — rejection/review/docs/process_rejected
#
# These four phase blocks each open a short-lived session when
# ``enable_session_reuse=True`` and dispose of it via ``disconnect_session``
# in a ``finally`` clause that swallows ``Exception`` (logged at debug). The
# tests below exercise each open + dispose-with-failure path so that:
#
#   1) the ``await create_session(...)`` line is reached, AND
#   2) the ``except Exception:`` log-and-continue branch is reached.
#
# Each test makes ``disconnect_session`` raise so a single test covers both
# lines of interest per phase.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_rejection_session_in_check_merged_plans(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``check_merged_plans`` opens a ``rejection_session`` when there is a
    rejected PR and ``enable_session_reuse=True``; a raising
    ``disconnect_session`` is logged and swallowed in the finally block."""
    from maestro.evolution import MergeCheckResult

    agent = make_agent()
    agent.config.enable_session_reuse = True
    # First iteration — check_merged_plans is gated by
    # ``first_iteration_after_restart``.
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="r")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    plan = EvolutionPlan(
        plan_id="rej_check",
        title="Rejected via merged-check",
        category="capability",
        rationale="t",
        proposed_changes=[], files_affected=[], expected_impact="",
        status="implemented", pr_url="https://x.test/y/pull/1",
    )
    agent.memory.save_evolution_plan(plan)
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(
            merged=[], rejected=[("rej_check", "no thanks")],
        ),
    )
    agent.evolution.learn_from_rejection = AsyncMock()  # type: ignore[method-assign]

    received_callers: list[str] = []

    async def _create(*args, **kwargs):  # type: ignore[no-untyped-def]
        options = kwargs.get("options")
        if options is None and len(args) >= 2:
            options = args[1]
        received_callers.append(options.caller)
        sess = MagicMock(name=f"sess-{options.caller}")
        sess.disconnect = AsyncMock()
        return sess

    raising_disconnect = AsyncMock(side_effect=RuntimeError("rej dc boom"))

    with (
        patch("maestro.agent.create_session", side_effect=_create),
        patch("maestro.loop.rejection_sessions.create_session", side_effect=_create),
        patch("maestro.loop.rejection_sessions.disconnect_session", raising_disconnect),
        patch("maestro.agent.disconnect_session", raising_disconnect),
    ):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=1, evolve_every=0,
            dream_every=0,
        )

    # The rejection_session caller name was used (proves line 4078 ran).
    assert "rejection_session" in received_callers
    # learn_from_rejection saw a non-None session forwarded.
    fwd = agent.evolution.learn_from_rejection.await_args.kwargs["session"]
    assert fwd is not None
    # ``disconnect_session`` was attempted and raised — the swallowing
    # branch is what kept the loop alive (lines 4096-4099).
    assert raising_disconnect.await_count >= 1


@pytest.mark.asyncio
async def test_run_loop_review_session_in_review_artifacts(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``review_artifacts`` opens an ``artifact_review_session`` when the
    candidate list is non-empty and ``enable_session_reuse=True``; a
    raising ``disconnect_session`` is swallowed."""
    agent = make_agent()
    agent.config.enable_session_reuse = True
    agent.config.artifact_review_enabled = True

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="r")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    received_callers: list[str] = []
    received_review_session: list[object] = []

    async def _create(*args, **kwargs):  # type: ignore[no-untyped-def]
        options = kwargs.get("options")
        if options is None and len(args) >= 2:
            options = args[1]
        received_callers.append(options.caller)
        sess = MagicMock(name=f"sess-{options.caller}")
        sess.disconnect = AsyncMock()
        return sess

    raising_disconnect = AsyncMock(side_effect=RuntimeError("review dc boom"))

    with (
        patch("maestro.agent.create_session", side_effect=_create),
        patch("maestro.agent.disconnect_session", raising_disconnect),
        patch("maestro.artifact_review.ArtifactReviewer") as mock_reviewer_cls,
    ):
        reviewer = MagicMock()
        reviewer.backfill = AsyncMock()
        reviewer.select_candidates = MagicMock(return_value=["c1"])

        async def _review(candidates, *, budget_throttle_active, session):  # type: ignore[no-untyped-def]
            received_review_session.append(session)

        reviewer.review = _review
        mock_reviewer_cls.return_value = reviewer

        await agent.run_loop(
            interval=0, max_iters=1, task_every=1, evolve_every=0,
            dream_every=0,
        )

    assert "artifact_review_session" in received_callers
    # The created session was threaded through to ``reviewer.review``.
    assert received_review_session and received_review_session[0] is not None
    assert raising_disconnect.await_count >= 1


@pytest.mark.asyncio
async def test_run_loop_process_rejected_session_swallows_disconnect_failure(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``process_rejected_plans`` opens a session when there is a pending
    rejected plan and ``enable_session_reuse=True``; a raising
    ``disconnect_session`` is swallowed."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="r")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    plan = EvolutionPlan(
        plan_id="rej_proc",
        title="Pending rejection",
        category="capability",
        rationale="t",
        proposed_changes=[], files_affected=[], expected_impact="",
        status="rejected", rejection_reason="closed",
    )
    agent.memory.save_evolution_plan(plan)
    # Disable the merged-check so the per-iteration "rejection_session"
    # caller comes only from the process_rejected_plans phase.
    from maestro.evolution import MergeCheckResult
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )
    agent.evolution.learn_from_rejection = AsyncMock()  # type: ignore[method-assign]

    received_callers: list[str] = []
    process_rejected_session_seen: list[object] = []

    real_process_rejected = agent._process_rejected_plans

    async def _wrapped_process(session=None, self_model_session=None):  # type: ignore[no-untyped-def]
        process_rejected_session_seen.append(session)
        await real_process_rejected(
            session=session, self_model_session=self_model_session,
        )

    agent._process_rejected_plans = _wrapped_process  # type: ignore[method-assign]

    async def _create(*args, **kwargs):  # type: ignore[no-untyped-def]
        options = kwargs.get("options")
        if options is None and len(args) >= 2:
            options = args[1]
        received_callers.append(options.caller)
        sess = MagicMock(name=f"sess-{options.caller}")
        sess.disconnect = AsyncMock()
        return sess

    raising_disconnect = AsyncMock(side_effect=RuntimeError("proc dc boom"))

    with (
        patch("maestro.agent.create_session", side_effect=_create),
        patch("maestro.loop.rejection_sessions.create_session", side_effect=_create),
        patch("maestro.loop.rejection_sessions.disconnect_session", raising_disconnect),
        patch("maestro.agent.disconnect_session", raising_disconnect),
    ):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=1, evolve_every=0,
            dream_every=0,
        )

    # The process_rejected_plans phase opened a rejection_session and
    # forwarded a non-None session into ``_process_rejected_plans``.
    assert "rejection_session" in received_callers
    assert (
        process_rejected_session_seen
        and process_rejected_session_seen[0] is not None
    )
    assert raising_disconnect.await_count >= 1


# ---------------------------------------------------------------------------
# Always-sessionless scope-gate calls carry their phase route (#4101)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replan_for_scope_sessionless_routes_implementation_phase(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The scope-gate replan never receives a session from ``implement_plan``,
    so its fallback ``query`` must declare the implementation phase or it
    bypasses ``[sdk] implementation_provider``."""
    agent = make_agent()
    plan = _approved_plan()
    payload = json.dumps({"proposed_changes": ["change, with a test added to verify"]})
    q_mock = AsyncMock(return_value=MockQueryResult(response=payload))
    with patch("maestro.loop.implementation.query", q_mock):
        await agent._replan_for_scope(plan, "Plan-scope gate: ...")

    assert q_mock.await_args.kwargs.get("phase") == "implementation"
    assert q_mock.await_args.kwargs["options"].tools == []  # tool-free (#4127)


@pytest.mark.asyncio
async def test_propose_plan_chain_sessionless_routes_evolution_phase(
    mem_fs: dict[str, str],
) -> None:
    """``_slice_scope_rejected_plan`` reaches ``propose_plan_chain`` with no
    session; the fallback must follow the evolution route Session B uses."""
    e, _, _, _ = _make_evolution(mem_fs)
    plan_json = json.dumps({
        "multi_pr": False,
        "title": "One focused change",
        "category": "capability",
        "priority": "capability",
        "rationale": "Fits one PR",
        "proposed_changes": ["Edit maestro/foo.py and add a test to verify it"],
        "acceptance_criteria": ["foo() returns 1"],
        "files_affected": ["maestro/foo.py"],
        "expected_impact": "Forward",
    })
    q_mock = AsyncMock(return_value=MockQueryResult(response=plan_json))
    with patch("maestro.evolution.query", q_mock):
        await e.propose_plan_chain("some reflection")

    assert q_mock.await_args.kwargs.get("phase") == "evolution"


# Functions whose fallback ``query`` runs sessionless in every config — reuse
# on or off — so the ``Config.validate`` reuse-off guard never covers them and
# each must name its phase route directly. A closed allowlist of known sites
# (keyed by path + function), not a sweep: it pins these routes, it does not
# discover a newly added always-sessionless call.
_ALWAYS_SESSIONLESS_QUERY_SITES = [
    ("maestro/loop/implementation.py", "_replan_for_scope", "implementation"),
    ("maestro/evolution.py", "propose_plan_chain", "evolution"),
]


def _query_phases(source: str, func_name: str) -> list[object]:
    """Return the ``phase=`` constant of each bare ``query(...)`` call inside
    *func_name* (``None`` when the keyword is absent)."""
    import ast

    phases: list[object] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_name:
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "query"
                ):
                    kw = {k.arg: k.value for k in call.keywords}
                    value = kw.get("phase")
                    phases.append(value.value if isinstance(value, ast.Constant) else None)
    return phases


@pytest.mark.parametrize(("path", "func_name", "phase"), _ALWAYS_SESSIONLESS_QUERY_SITES)
def test_always_sessionless_query_sites_carry_phase(
    path: str, func_name: str, phase: str,
) -> None:
    source = (Path(__file__).resolve().parent.parent / path).read_text(encoding="utf-8")
    assert _query_phases(source, func_name) == [phase]


def test_always_sessionless_guard_detects_seeded_missing_phase() -> None:
    """Dropping the ``phase=`` keyword must be caught, so the pin above
    cannot pass vacuously."""
    seeded = (
        "async def _replan_for_scope(self):\n"
        "    result = await query(prompt='p', options=None)\n"
    )
    assert _query_phases(seeded, "_replan_for_scope") == [None]
