"""Tests for the CI-pending remediation-slot yield gate.

``MaestroAgent._gate_pr_remediation_ci_pending_hold`` is the refusal-by-correctness
answer to the CI-pending owned-PR monoculture: when a queued remediation targets
an owned open PR whose *sole* live blocker is CI still running — no merge
conflict, no failing quality gate, no unaddressed AI-finding thread — there is
nothing to fix. Opening a session and reconcile-pushing would only reset the very
CI clock the loop is waiting on (``push-resets-the-ci-clock``). The gate yields
the slot *before the pop* in :meth:`_run_task_phase`: the PR is left queued (no
eviction), no SDK session opens, and no stand-down / escalation / close is
written — CI concludes on its own clock.

The gate body — :meth:`PrRemediationController.gate_ci_pending_hold`, which
``MaestroAgent._gate_pr_remediation_ci_pending_hold`` delegates to — is
exercised against a duck-typed agent stub with the readiness observation stubbed; a single integration test
drives :meth:`_run_task_phase` end-to-end to pin the no-evict / no-session
contract. All file I/O goes through the ``mem_fs`` fixture.

The flagged one-shot green-handoff arm (#3630, ``[loop]
``ci_pending_green_handoff_enabled``) is exercised on the same stub with the
real JSONL ledger backends over ``mem_fs`` and an injected gate runner, so a
delivery is asserted on its three durable artifacts (one message row, one
``pr_number``-keyed stand-down, one ``HANDOFF_DELIVERED`` deed) and no
subprocess ever runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from maestro import escalation_bridge
from maestro.audit_checks import JsonlGateActivationLedger, measure_gate_triggers
from maestro.consciousness import PendingPRRequest
from maestro.deed_ledger import DeedType, JsonlDeedLedger
from maestro.escalation_bridge import JsonEscalationLedger
from maestro.loop.pr_remediation import PrRemediationController
from maestro.messaging import MessageStore
from maestro.pr_facts import PRReadiness
from maestro.remediation_tracker import JsonlStandDownVerdictLedger
from maestro.vcs import VCSError

FAKE_DIR = Path("/fake/ci-hold")
_ACTIVATIONS = str(FAKE_DIR / "gate_activations.jsonl")


def _readiness(
    *,
    ci_state: str = "PENDING",
    landed: bool = False,
    settled: bool | None = None,
    behind: bool = False,
    conflicting: bool = False,
    threads: int = 0,
    overflow: int = 0,
) -> PRReadiness:
    """A CI-pending-hold-shaped readiness fact with per-dimension overrides."""
    return PRReadiness(
        pr_number=7,
        branch_landed=landed,
        # A landed PR is always settled; ``settled=True`` with ``landed=False``
        # expresses a human-closed, unmerged PR.
        settled=landed if settled is None else settled,
        branch_behind=behind,
        branch_conflicting=conflicting,
        ci_state=ci_state,  # type: ignore[arg-type]
        unresolved_threads=threads,
        unaddressed_overflow=overflow,
        awaiting_approval=False,
        # ``ready_and_waiting`` is irrelevant to this gate (it keys on CI
        # *pending*, never *green*); pin it False so a stray read never masks a
        # bug.
        ready_and_waiting=False,
        source="host_api",
        observed_at=100.0,
    )


# Reverse of ``ci_state_from_conclusion``: the raw host rollup string the gate's
# cheap first read (``get_pr_check_conclusion``) must return for a given CIState.
_CONCLUSION_FOR = {
    "PENDING": "",
    "GREEN": "success",
    "FAILING": "failure",
    "NONE": "none",
}


def _gate_stub(
    *, enabled: bool = True, readiness: PRReadiness | None = None,
    **overrides: Any,
) -> SimpleNamespace:
    """Minimal duck-typed agent exposing only what the CI-pending gate touches.

    The host ``get_pr_check_conclusion`` rollup is wired to match the readiness
    fact's ``ci_state`` so the gate's cheap CI-first read agrees with the
    aggregated observation it consults only for a genuinely CI-pending head.
    """
    fact = readiness if readiness is not None else _readiness()
    stub = SimpleNamespace(
        config=SimpleNamespace(
            ci_pending_hold_dequeue_enabled=enabled,
            # The green-handoff arm is opt-in; default it off so every
            # pre-existing test exercises today's byte-identical hold path.
            ci_pending_green_handoff_enabled=False,
            memory_dir=FAKE_DIR,
        ),
        memory=MagicMock(),
        remediation_tracker=MagicMock(),
        # The gate appends its ``[ci-pending-hold]`` row through this backend seam
        # (proposal 106); the default JSONL adapter over ``FAKE_DIR`` writes the
        # same ``gate_activations.jsonl`` these tests assert against.
        gate_activations=JsonlGateActivationLedger(FAKE_DIR),
        evolution=SimpleNamespace(
            code_host=SimpleNamespace(
                get_pr_check_conclusion=MagicMock(
                    return_value=_CONCLUSION_FOR[fact.ci_state],
                ),
            ),
        ),
        observe_pr_readiness=MagicMock(return_value=fact),
    )
    for name, value in overrides.items():
        setattr(stub, name, value)
    return stub


def _call(stub: SimpleNamespace, entry: PendingPRRequest) -> bool:
    return PrRemediationController(stub).gate_ci_pending_hold(entry)  # type: ignore[arg-type]


def _entry(
    directive: str = "Remediate PR #7: fix CI", branch: str | None = "b7",
) -> PendingPRRequest:
    return PendingPRRequest(directive=directive, pr_branch=branch)


# --- Criterion 1 & 3: the fire branch yields the slot, one activation ---------


def test_ci_pending_sole_blocker_yields_slot(mem_fs: dict[str, str]) -> None:
    """Sole live blocker is CI pending → gate fires (returns True).

    Yielding the slot means: no ``TaskRecord`` (no eviction bookkeeping), no
    repetition no-op / stand-down (``remediation_tracker`` untouched), and no
    close — only a single ``[ci-pending-hold]`` activation is written.
    """
    stub = _gate_stub()
    assert _call(stub, _entry()) is True
    stub.observe_pr_readiness.assert_called_once_with(7, "b7")
    # No eviction record, no stand-down / repetition no-op, no escalation.
    stub.memory.record_task.assert_not_called()
    stub.remediation_tracker.record_outcome.assert_not_called()


def test_ci_pending_fire_writes_exactly_one_activation(
    mem_fs: dict[str, str],
) -> None:
    """Each firing appends exactly one ``[ci-pending-hold]`` record."""
    stub = _gate_stub()
    assert _call(stub, _entry()) is True
    assert _ACTIVATIONS in mem_fs
    records = [
        json.loads(line)
        for line in mem_fs[_ACTIVATIONS].strip().splitlines()
    ]
    assert len(records) == 1
    assert records[0]["gate"] == "ci_pending_hold"
    assert "[ci-pending-hold]" in records[0]["detail"]
    # The canonical gate name is countable by the measure tool.
    assert measure_gate_triggers(
        JsonlGateActivationLedger(FAKE_DIR), n=100,
    ) == {"ci_pending_hold": 1}


# --- Criterion 2: any self-clearable blocker → no fire ------------------------


def test_merge_conflict_does_not_fire(mem_fs: dict[str, str]) -> None:
    """A merge conflict is self-clearable — the gate falls through."""
    stub = _gate_stub(readiness=_readiness(conflicting=True))
    assert _call(stub, _entry()) is False
    assert _ACTIVATIONS not in mem_fs


def test_failing_quality_gate_does_not_fire(mem_fs: dict[str, str]) -> None:
    """CI ``FAILING`` (a failing quality gate) is self-clearable — no fire.

    The cheap CI-first read sees ``FAILING`` and short-circuits before the
    heavier readiness observation.
    """
    stub = _gate_stub(readiness=_readiness(ci_state="FAILING"))
    assert _call(stub, _entry()) is False
    stub.observe_pr_readiness.assert_not_called()
    assert _ACTIVATIONS not in mem_fs


def test_unaddressed_thread_does_not_fire(mem_fs: dict[str, str]) -> None:
    """An unaddressed AI-finding thread is self-clearable — no fire."""
    stub = _gate_stub(readiness=_readiness(threads=1))
    assert _call(stub, _entry()) is False
    assert _ACTIVATIONS not in mem_fs


def test_unaddressed_overflow_does_not_fire(mem_fs: dict[str, str]) -> None:
    """An unaddressed overflow finding is self-clearable — no fire."""
    stub = _gate_stub(readiness=_readiness(overflow=1))
    assert _call(stub, _entry()) is False
    assert _ACTIVATIONS not in mem_fs


def test_behind_main_does_not_fire(mem_fs: dict[str, str]) -> None:
    """A behind-main branch is not the CI-pending sole-blocker terminal.

    Behind-main is a merge-block the downstream dequeue chain handles; the
    CI-pending gate must not swallow it, so it falls through.
    """
    stub = _gate_stub(readiness=_readiness(behind=True))
    assert _call(stub, _entry()) is False
    assert _ACTIVATIONS not in mem_fs


def test_landed_branch_does_not_fire(mem_fs: dict[str, str]) -> None:
    """An already-landed branch is not open — no fire (the landed gate evicts)."""
    stub = _gate_stub(readiness=_readiness(landed=True))
    assert _call(stub, _entry()) is False
    assert _ACTIVATIONS not in mem_fs


def test_closed_unmerged_pr_does_not_fire(mem_fs: dict[str, str]) -> None:
    """A closed-unmerged PR is settled, not open — no fire (#4051).

    Its CI may still be running, but the host-closed dequeue gate must evict it,
    so the hold must not yield the slot and park it in the queue.
    """
    stub = _gate_stub(readiness=_readiness(landed=False, settled=True))
    assert _call(stub, _entry()) is False
    assert _ACTIVATIONS not in mem_fs


def test_ci_green_does_not_fire(mem_fs: dict[str, str]) -> None:
    """CI green is the observed-ready terminal, not CI-pending-hold — no fire.

    Green short-circuits at the cheap CI read before any readiness observation.
    """
    stub = _gate_stub(readiness=_readiness(ci_state="GREEN"))
    assert _call(stub, _entry()) is False
    stub.observe_pr_readiness.assert_not_called()
    assert _ACTIVATIONS not in mem_fs


def test_non_remediation_goal_skips_observation(mem_fs: dict[str, str]) -> None:
    """A directive without a parseable header never observes readiness."""
    stub = _gate_stub()
    assert _call(stub, _entry(directive="do a thing")) is False
    stub.observe_pr_readiness.assert_not_called()
    assert _ACTIVATIONS not in mem_fs


def test_branchless_request_skips_observation(mem_fs: dict[str, str]) -> None:
    """A branchless request cannot compute branch state — falls through."""
    stub = _gate_stub()
    assert _call(stub, _entry(branch=None)) is False
    stub.observe_pr_readiness.assert_not_called()
    assert _ACTIVATIONS not in mem_fs


# --- Criterion 4: the flag is a hard off switch -------------------------------


def test_flag_off_is_full_noop(mem_fs: dict[str, str]) -> None:
    """With the flag off the gate never fires and never observes readiness."""
    stub = _gate_stub(enabled=False)
    assert _call(stub, _entry()) is False
    stub.observe_pr_readiness.assert_not_called()
    assert _ACTIVATIONS not in mem_fs


def test_flag_off_ignores_a_ci_pending_pr(mem_fs: dict[str, str]) -> None:
    """Even a textbook CI-pending-hold PR is ignored when the flag is off."""
    stub = _gate_stub(enabled=False, readiness=_readiness())
    assert _call(stub, _entry()) is False
    stub.observe_pr_readiness.assert_not_called()
    assert _ACTIVATIONS not in mem_fs


# --- Criterion 1 (integration): no eviction, no session -----------------------


def test_run_task_phase_ci_pending_leaves_pr_queued_no_session(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``_run_task_phase`` yields the slot: PR stays queued and no session opens.

    End-to-end proof of the no-evict / no-session contract: with a single
    CI-pending owned PR queued, the pre-pop gate fires, so ``run`` is never
    awaited (no SDK session) and the entry remains in ``pending_pr_requests``
    (no eviction) for a later pop to re-evaluate once CI concludes.
    """
    agent = make_agent()
    agent.config.ci_pending_hold_dequeue_enabled = True
    # The cheap CI-first read must see a still-running rollup (the fixture
    # default is "success"/GREEN, which would short-circuit the gate).
    agent.evolution._host.get_pr_check_conclusion = MagicMock(  # type: ignore[attr-defined]
        return_value="",
    )
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness(),
    )
    # The mechanical stand-down (a rival zero-SDK disposer) must not consume the
    # head first; keep this test scoped to the CI-pending gate.
    agent._dispose_mechanical_standdown = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )
    agent.run = AsyncMock()  # type: ignore[method-assign]

    model = agent.consciousness.load_self_model()
    entry = PendingPRRequest(
        directive="Remediate PR #7: fix CI", pr_branch="b7",
    )
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)

    import asyncio

    asyncio.run(agent._run_task_phase())

    # No SDK session opened — the slot was yielded before the pop.
    agent.run.assert_not_awaited()
    # The PR is still queued (not evicted); a later pop re-evaluates it.
    reloaded = agent.consciousness.load_self_model()
    assert len(reloaded.pending_pr_requests) == 1
    assert reloaded.pending_pr_requests[0].directive == entry.directive
    # Exactly one activation record, no stand-down verdict file written.
    assert measure_gate_triggers(agent.gate_activations, n=100) == {
        "ci_pending_hold": 1,
    }
    standdown = agent.config.memory_dir / "stand_down_verdicts.jsonl"
    assert not standdown.exists()


def _phase_agent(
    make_agent: Any, conclusions: dict[int, str],
) -> Any:
    """An agent whose CI rollup per PR number comes from *conclusions*.

    ``""`` is a still-running rollup (the hold fires); ``"failure"`` is a red
    head the hold never holds. The mechanical stand-down and the dequeue gates
    are stubbed out so each #4064 test is scoped to slot selection.
    """
    agent = make_agent()
    agent.config.ci_pending_hold_dequeue_enabled = True
    agent.evolution._host.get_pr_check_conclusion = MagicMock(
        side_effect=lambda pr: conclusions[pr],
    )
    agent.observe_pr_readiness = MagicMock(return_value=_readiness())
    agent._dispose_mechanical_standdown = MagicMock(return_value=False)
    agent._pr_remediation.dequeue_gates = AsyncMock(return_value=False)
    agent.run = AsyncMock()
    return agent


def _queue_prs(agent: Any, *numbers: int) -> None:
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(directive=f"Remediate PR #{n}: fix", pr_branch=f"b{n}")
        for n in numbers
    ]
    agent.consciousness.save_self_model(model)


def test_run_task_phase_ci_held_head_yields_slot_to_next_entry(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#4064: a CI-held head passes the slot to the actionable entry behind it."""
    import asyncio

    agent = _phase_agent(make_agent, {7: "", 8: "failure"})
    _queue_prs(agent, 7, 8)

    asyncio.run(agent._run_task_phase())

    agent.run.assert_awaited_once()
    assert agent.run.await_args.kwargs["pr_branch"] == "b8"
    reloaded = agent.consciousness.load_self_model()
    assert [e.pr_branch for e in reloaded.pending_pr_requests] == ["b7"]
    assert measure_gate_triggers(agent.gate_activations, n=100) == {
        "ci_pending_hold": 1,
    }


def test_run_task_phase_all_ci_held_falls_through_to_queue_head(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#4064: every entry held → the self-directed queue head runs this slot."""
    import asyncio

    from maestro.task_queue import QueuedTask

    agent = _phase_agent(make_agent, {7: "", 8: ""})
    _queue_prs(agent, 7, 8)
    agent.task_queue.save([QueuedTask(task_id="q", goal="Do a queued thing")])
    agent._consecutive_remediation = 2

    asyncio.run(agent._run_task_phase())

    agent.run.assert_awaited_once()
    assert agent.run.await_args.args[0] == "Do a queued thing"
    assert agent.run.await_args.kwargs["source"] == "self_directed"
    reloaded = agent.consciousness.load_self_model()
    assert [e.pr_branch for e in reloaded.pending_pr_requests] == ["b7", "b8"]
    assert agent._consecutive_remediation == 0


def test_run_task_phase_ci_held_skips_stalled_entry_behind_it(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#4064: a stalled entry after the held head is skipped too, never run."""
    import asyncio

    agent = _phase_agent(make_agent, {7: "", 8: "failure", 9: "failure"})
    _queue_prs(agent, 7, 8, 9)
    agent._pending_pr_head_stalled = MagicMock(
        side_effect=lambda entry, recent=None: entry.pr_branch == "b8",
    )

    asyncio.run(agent._run_task_phase())

    agent.run.assert_awaited_once()
    assert agent.run.await_args.kwargs["pr_branch"] == "b9"
    reloaded = agent.consciousness.load_self_model()
    assert [e.pr_branch for e in reloaded.pending_pr_requests] == ["b7", "b8"]


def test_run_task_phase_ci_held_skip_does_not_trigger_owned_act_preempt(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#4064: skipping a held (not stalled) head is no owned-act-preempt yield."""
    import asyncio

    agent = _phase_agent(make_agent, {7: "", 8: "failure"})
    _queue_prs(agent, 7, 8)
    agent.config.owned_act_preempt_enabled = True
    agent._owned_act_drought_active = MagicMock(return_value=True)
    agent._preempt_yielded_slot_with_owned_prune = MagicMock()

    asyncio.run(agent._run_task_phase())

    agent._preempt_yielded_slot_with_owned_prune.assert_not_called()
    agent.run.assert_awaited_once()
    assert agent.run.await_args.kwargs["pr_branch"] == "b8"


def test_run_task_phase_ci_held_scan_shares_one_history_read(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#4064: the stall pick and the held-entry scan share one tail read."""
    import asyncio

    agent = _phase_agent(make_agent, {7: "", 8: "failure"})
    _queue_prs(agent, 7, 8)
    agent.memory.get_recent_tasks = MagicMock(return_value=[])
    scan = agent._pr_remediation.next_unheld_pr_index
    reads_at_pick: list[int] = []

    def _spy(*args: Any) -> int | None:
        index = scan(*args)
        reads_at_pick.append(agent.memory.get_recent_tasks.call_count)
        return index

    agent._pr_remediation.next_unheld_pr_index = _spy

    asyncio.run(agent._run_task_phase())

    assert reads_at_pick == [1]
    assert agent.run.await_args.kwargs["pr_branch"] == "b8"


def test_next_unheld_pr_index_reads_history_lazily_without_tail(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#4064: absent a supplied tail, the scan reads it once past *start*."""
    agent = _phase_agent(make_agent, {7: "", 8: "failure"})
    _queue_prs(agent, 7, 8)
    agent.memory.get_recent_tasks = MagicMock(return_value=[])
    entries = agent.consciousness.load_self_model().pending_pr_requests

    index = agent._pr_remediation.next_unheld_pr_index(entries, 0)

    assert index == 1
    agent.memory.get_recent_tasks.assert_called_once_with(30)

# --- #3630: the one-shot green-handoff arm ------------------------------------

_MESSAGES = str(FAKE_DIR / "messages.jsonl")
_STANDDOWNS = str(FAKE_DIR / "stand_down_verdicts.jsonl")
_DEEDS = str(FAKE_DIR / "deed_ledger.jsonl")
_ESCALATIONS = str(FAKE_DIR / "escalation_handoffs.json")


def _gate_runner(*, all_passed: bool) -> MagicMock:
    """An injected ``GateRunner`` seam: exit 0 for every gate, or ruff RED.

    Mocked, so no ruff/mypy subprocess is ever spawned — the assertion that it
    was *not* called is what pins the self-extinguishing ordering.
    """
    if all_passed:
        return MagicMock(return_value=(0, "", ""))
    return MagicMock(return_value=(1, "maestro/agent.py:1:1: E501 too long", ""))


def _handoff_stub(
    *, all_passed: bool = True, handoff_enabled: bool = True,
    runner: MagicMock | None = None, **overrides: Any,
) -> SimpleNamespace:
    """A CI-pending-hold stub wired with the green-handoff arm's collaborators.

    The four ledgers are the real JSONL backends rooted at ``FAKE_DIR`` (so the
    durable artifacts land in ``mem_fs`` and can be counted), and the gate runner
    is injected — the arm's only side-effect seam — so nothing executes.
    """
    gate_runner = runner if runner is not None else _gate_runner(
        all_passed=all_passed,
    )
    stub = _gate_stub(**overrides)
    stub.config.ci_pending_green_handoff_enabled = handoff_enabled
    stub.config.work_target = MagicMock(
        return_value=SimpleNamespace(
            gate_source_path="maestro/", gate_tests_path="tests/",
            gate_lint_extra_paths=("scripts/",),
        ),
    )
    stub._red_root_gate_runner = gate_runner
    # The arm measures the *workspace* tree, so it first confirms the checkout is
    # the PR branch; the stub is on ``b7`` (the entry's branch) by default.
    stub._vcs = MagicMock()
    stub._vcs.get_current_branch = MagicMock(return_value="b7")
    stub._vcs.get_head_hash = MagicMock(return_value="head1")
    stub._vcs.porcelain_status = MagicMock(return_value="")
    # ``origin/<branch>`` tracks the local head unless a test pins it apart.
    stub._vcs.rev_parse = MagicMock(
        side_effect=lambda _ref: stub._vcs.get_head_hash(),
    )
    stub.escalation_standdowns = JsonEscalationLedger(FAKE_DIR)
    stub.stand_down_verdicts = JsonlStandDownVerdictLedger(FAKE_DIR)
    stub.deed_ledger = JsonlDeedLedger(FAKE_DIR)
    stub.message_store = MessageStore(FAKE_DIR)
    return stub


def test_green_handoff_delivers_once_and_still_yields(
    mem_fs: dict[str, str],
) -> None:
    """Locally green + CI pending → one handoff, and the slot is still yielded.

    The arm's three durable artifacts each appear exactly once (message row,
    ``pr_number``-keyed stand-down, ``HANDOFF_DELIVERED`` deed), while the gate's
    contract is unchanged: ``True`` (slot yielded, PR left queued), no eviction
    bookkeeping and no SDK session.
    """
    stub = _handoff_stub()
    assert _call(stub, _entry()) is True
    # Yield semantics unchanged: no eviction record, no stand-down/repetition
    # bookkeeping through the remediation tracker, no close.
    stub.memory.record_task.assert_not_called()
    stub.remediation_tracker.record_outcome.assert_not_called()
    # Exactly one merge-or-close message.
    messages = [
        json.loads(line)
        for line in mem_fs[_MESSAGES].strip().splitlines()
    ]
    assert len(messages) == 1
    assert messages[0]["sender"] == "maestro"
    # Exactly one pr_number-keyed stand-down verdict row.
    standdowns = [
        json.loads(line)
        for line in mem_fs[_STANDDOWNS].strip().splitlines()
    ]
    assert len(standdowns) == 1
    assert standdowns[0]["pr"] == 7
    # The backfilled verdict row stays head-less on purpose: a non-empty head
    # would make it live for ``find_stand_down`` and let ``gate_stood_down``
    # evict the queued entry, which would strand the CI-RED release.
    assert standdowns[0]["head_sha"] == ""
    # The escalation standdown, by contrast, stamps the head the arm measured
    # (the gate target already confirmed the checkout is the PR branch).
    escalation = json.loads(mem_fs[_ESCALATIONS])
    assert escalation["7"]["head_sha"] == "head1"
    # Exactly one HANDOFF_DELIVERED deed.
    deeds = [
        json.loads(line) for line in mem_fs[_DEEDS].strip().splitlines()
    ]
    assert len(deeds) == 1
    assert deeds[0]["deed_type"] == DeedType.HANDOFF_DELIVERED.value
    assert deeds[0]["pr_number"] == 7
    # Both gate names are countable: the arm's own row plus the hold row.
    assert measure_gate_triggers(
        JsonlGateActivationLedger(FAKE_DIR), n=100,
    ) == {"ci_pending_green_handoff": 1, "ci_pending_hold": 1}
    # Delivery-truth bind: ``deliver_handoff`` records its standdown in the
    # *escalation* ledger, so the verdict row above is the bind's own backfill —
    # stamped ``handoff_row_persisted``, never trusting the actuator's return.
    activations = [
        json.loads(line)
        for line in mem_fs[_ACTIVATIONS].strip().splitlines()
    ]
    arm = [r for r in activations if r["gate"] == "ci_pending_green_handoff"]
    assert len(arm) == 1
    assert arm[0]["action"] == "handoff_row_persisted"


def test_green_handoff_runs_both_demanding_gates(
    mem_fs: dict[str, str],
) -> None:
    """The arm runs the demanding pair (ruff → mypy) and nothing wider."""
    runner = _gate_runner(all_passed=True)
    stub = _handoff_stub(runner=runner)
    assert _call(stub, _entry()) is True
    assert [call.args[0] for call in runner.call_args_list] == ["ruff", "mypy"]


def test_failing_local_gate_delivers_nothing_and_still_yields(
    mem_fs: dict[str, str],
) -> None:
    """A RED local gate is not a handoff — nothing delivered, slot still yielded."""
    stub = _handoff_stub(all_passed=False)
    assert _call(stub, _entry()) is True
    assert _MESSAGES not in mem_fs
    assert _STANDDOWNS not in mem_fs
    assert _DEEDS not in mem_fs
    assert measure_gate_triggers(
        JsonlGateActivationLedger(FAKE_DIR), n=100,
    ) == {"ci_pending_hold": 1}


def test_existing_standdown_runs_no_gate_subprocess(
    mem_fs: dict[str, str],
) -> None:
    """A PR already handed up pays for no gate run — the ordering is load-bearing.

    The ``has_standdown`` read is evaluated *before* the gate run, so the
    injected runner is never invoked on any later selection. The arm is
    self-extinguishing: one handoff per PR, and the slot is still yielded.
    """
    runner = _gate_runner(all_passed=True)
    stub = _handoff_stub(runner=runner)
    # Seed the keyed standdown the first delivery would have left.
    stub.escalation_standdowns.mutate(
        lambda standdowns: standdowns.__setitem__(
            7,
            {
                "head_sha": "",
                "delivered_at": "2026-01-01T00:00:00+00:00",
                "serviced_comment_ids": [],
                "claim": "handoff-ready",
            },
        ),
    )
    assert _call(stub, _entry()) is True
    runner.assert_not_called()
    assert _MESSAGES not in mem_fs
    assert _DEEDS not in mem_fs
    assert measure_gate_triggers(
        JsonlGateActivationLedger(FAKE_DIR), n=100,
    ) == {"ci_pending_hold": 1}


def test_handoff_flag_off_runs_no_gate_and_delivers_nothing(
    mem_fs: dict[str, str],
) -> None:
    """With the arm's flag off the hold gate is byte-identical to today."""
    runner = _gate_runner(all_passed=True)
    stub = _handoff_stub(handoff_enabled=False, runner=runner)
    assert _call(stub, _entry()) is True
    runner.assert_not_called()
    assert _MESSAGES not in mem_fs
    assert _STANDDOWNS not in mem_fs
    assert _DEEDS not in mem_fs
    assert measure_gate_triggers(
        JsonlGateActivationLedger(FAKE_DIR), n=100,
    ) == {"ci_pending_hold": 1}


def test_green_handoff_skipped_when_ci_is_not_sole_blocker(
    mem_fs: dict[str, str],
) -> None:
    """A self-clearable blocker falls through before the arm is ever consulted."""
    runner = _gate_runner(all_passed=True)
    stub = _handoff_stub(runner=runner, readiness=_readiness(conflicting=True))
    assert _call(stub, _entry()) is False
    runner.assert_not_called()
    assert _MESSAGES not in mem_fs
    assert _ACTIVATIONS not in mem_fs


# --- The arm measures the workspace tree, so it must be the PR's head ---------


def test_checkout_landing_on_wrong_branch_runs_no_gate_and_delivers_nothing(
    mem_fs: dict[str, str],
) -> None:
    """A checkout that lands on a foreign branch trips the post-positioning guard.

    ``_red_root_gate_runner`` runs ruff/mypy with ``cwd=config.workspace``, so
    the arm positions the PR head first (#4100); if the workspace still reads a
    foreign branch afterwards, measuring it green would deliver a handoff
    premised on a PR nobody measured, so the arm stands down and restores.
    """
    runner = _gate_runner(all_passed=True)
    stub = _handoff_stub(runner=runner)
    stub._vcs.get_current_branch.return_value = "maestro/task/t1"
    assert _call(stub, _entry()) is True
    runner.assert_not_called()
    assert stub._vcs.checkout.call_args_list[0].args == ("b7",)
    assert stub._vcs.checkout.call_args_list[-1].args == ("maestro/task/t1",)
    assert _MESSAGES not in mem_fs
    assert _STANDDOWNS not in mem_fs
    assert _DEEDS not in mem_fs
    assert measure_gate_triggers(
        JsonlGateActivationLedger(FAKE_DIR), n=100,
    ) == {"ci_pending_hold": 1}


class _FakeVCS:
    """A VCS whose ``checkout`` moves the current branch, starting on ``main``."""

    def __init__(
        self, *, fail: frozenset[str] = frozenset(), dirty: bool = False,
    ) -> None:
        self.current = "main"
        self.fail = fail
        self.dirty = dirty
        self.checkouts: list[str] = []

    def get_current_branch(self) -> str:
        return self.current

    def porcelain_status(self) -> str:
        return " M x.py" if self.dirty else ""

    def get_head_hash(self) -> str:
        return "head1"

    def rev_parse(self, _ref: str) -> str:
        return "head1"

    def checkout(self, branch: str) -> None:
        self.checkouts.append(branch)
        if branch in self.fail:
            raise VCSError(f"no {branch}")
        self.current = branch

    def checkout_new_branch(self, branch: str) -> None:
        self.checkout(branch)


def test_green_handoff_positions_pr_head_from_main(
    mem_fs: dict[str, str],
) -> None:
    """From ``main`` the arm checks the PR out, measures it, and restores ``main``."""
    vcs = _FakeVCS()
    seen: list[str] = []
    runner = MagicMock(side_effect=lambda *_a: seen.append(vcs.current) or (0, "", ""))
    stub = _handoff_stub(runner=runner)
    stub._vcs = vcs
    assert _call(stub, _entry()) is True
    assert seen == ["b7", "b7"]
    assert vcs.current == "main"
    assert len(escalation_bridge.JsonEscalationLedger(FAKE_DIR).read()) == 1
    assert _DEEDS in mem_fs
    assert measure_gate_triggers(
        JsonlGateActivationLedger(FAKE_DIR), n=100,
    ) == {"ci_pending_hold": 1, "ci_pending_green_handoff": 1}


def test_green_handoff_falls_back_to_origin_branch(
    mem_fs: dict[str, str],
) -> None:
    """No local PR branch → ``origin/<branch>`` + ``checkout_new_branch``."""
    vcs = _FakeVCS(fail=frozenset({"b7"}))
    stub = _handoff_stub()
    stub._vcs = vcs
    vcs.checkout_new_branch = lambda b: setattr(vcs, "current", b)  # type: ignore[method-assign]
    assert _call(stub, _entry()) is True
    stub._red_root_gate_runner.assert_called()
    assert vcs.checkouts == ["b7", "origin/b7", "main"]
    assert vcs.current == "main"


def test_green_handoff_checkout_failure_skips_and_stays_queued(
    mem_fs: dict[str, str],
) -> None:
    """A failed PR checkout runs no gate, delivers nothing, and yields."""
    vcs = _FakeVCS(fail=frozenset({"b7", "origin/b7"}))
    stub = _handoff_stub()
    stub._vcs = vcs
    assert _call(stub, _entry()) is True
    stub._red_root_gate_runner.assert_not_called()
    assert vcs.current == "main"
    assert _MESSAGES not in mem_fs
    assert _STANDDOWNS not in mem_fs
    assert _DEEDS not in mem_fs


def test_branch_unreadable_after_positioning_skips_and_restores(
    mem_fs: dict[str, str],
) -> None:
    """An unreadable branch *after* the checkout runs no gate and restores."""
    vcs = _FakeVCS()
    reads = iter(["main"])

    def _branch() -> str:
        try:
            return next(reads)
        except StopIteration:
            raise VCSError("HEAD unreadable") from None

    vcs.get_current_branch = _branch  # type: ignore[method-assign]
    stub = _handoff_stub()
    stub._vcs = vcs
    assert _call(stub, _entry()) is True
    stub._red_root_gate_runner.assert_not_called()
    assert vcs.checkouts == ["b7", "main"]
    assert _STANDDOWNS not in mem_fs


def test_green_handoff_dirty_tree_skips_checkout(
    mem_fs: dict[str, str],
) -> None:
    """A dirty workspace is never checked out over — the arm skips instead."""
    vcs = _FakeVCS(dirty=True)
    stub = _handoff_stub()
    stub._vcs = vcs
    assert _call(stub, _entry()) is True
    assert vcs.checkouts == []
    stub._red_root_gate_runner.assert_not_called()
    assert _STANDDOWNS not in mem_fs


def test_unreadable_branch_runs_no_gate_and_still_yields(
    mem_fs: dict[str, str],
) -> None:
    """A detached HEAD (``VCSError``) is not a measurable tree — arm stands down."""
    runner = _gate_runner(all_passed=True)
    stub = _handoff_stub(runner=runner)
    stub._vcs.get_current_branch.side_effect = VCSError("HEAD is detached")
    assert _call(stub, _entry()) is True
    runner.assert_not_called()
    assert _MESSAGES not in mem_fs
    assert measure_gate_triggers(
        JsonlGateActivationLedger(FAKE_DIR), n=100,
    ) == {"ci_pending_hold": 1}


# --- The RED reading is memoized per head (no artifact self-extinguishes it) --


def test_red_reading_memoized_per_head(mem_fs: dict[str, str]) -> None:
    """A RED head pays for the gate subprocess once, not on every selection.

    The green path self-extinguishes through its keyed stand-down; RED leaves no
    durable artifact, so an unchanged head is memoized. A new commit changes the
    sha and the reading is taken again.
    """
    runner = _gate_runner(all_passed=False)
    stub = _handoff_stub(runner=runner)
    controller = PrRemediationController(stub)  # type: ignore[arg-type]
    assert controller.gate_ci_pending_hold(_entry()) is True
    # ruff reads RED, so ``diagnose_red_root`` short-circuits before mypy.
    assert runner.call_count == 1
    # Same head: no second subprocess.
    assert controller.gate_ci_pending_hold(_entry()) is True
    assert runner.call_count == 1
    # A new commit releases the memo.
    stub._vcs.get_head_hash.return_value = "head2"
    assert controller.gate_ci_pending_hold(_entry()) is True
    assert runner.call_count == 2
    assert _MESSAGES not in mem_fs


def test_unreadable_head_hash_skips_the_run(mem_fs: dict[str, str]) -> None:
    """No local head sha → it cannot be matched to origin's, so no gate runs."""
    runner = _gate_runner(all_passed=False)
    stub = _handoff_stub(runner=runner)
    stub._vcs.get_head_hash.return_value = None
    stub._vcs.rev_parse = MagicMock(return_value="head1")
    controller = PrRemediationController(stub)  # type: ignore[arg-type]
    assert controller.gate_ci_pending_hold(_entry()) is True
    runner.assert_not_called()
    assert _MESSAGES not in mem_fs


def test_local_head_diverged_from_origin_skips_the_run(
    mem_fs: dict[str, str],
) -> None:
    """A local branch behind/ahead of ``origin`` is not the tree CI runs."""
    runner = _gate_runner(all_passed=True)
    stub = _handoff_stub(runner=runner)
    stub._vcs.rev_parse = MagicMock(return_value="remote9")
    controller = PrRemediationController(stub)  # type: ignore[arg-type]
    assert controller.gate_ci_pending_hold(_entry()) is True
    runner.assert_not_called()
    assert _MESSAGES not in mem_fs


def test_unresolvable_origin_branch_skips_before_checkout(
    mem_fs: dict[str, str],
) -> None:
    """No ``origin/<branch>`` → no checkout and no gate run."""
    runner = _gate_runner(all_passed=True)
    stub = _handoff_stub(runner=runner)
    stub._vcs.rev_parse = MagicMock(return_value=None)
    controller = PrRemediationController(stub)  # type: ignore[arg-type]
    assert controller.gate_ci_pending_hold(_entry()) is True
    runner.assert_not_called()
    stub._vcs.checkout.assert_not_called()


def test_known_red_origin_head_pays_no_checkout(mem_fs: dict[str, str]) -> None:
    """The RED memo is consulted on origin's sha before positioning."""
    vcs = _FakeVCS()
    runner = _gate_runner(all_passed=False)
    stub = _handoff_stub(runner=runner)
    stub._vcs = vcs
    controller = PrRemediationController(stub)  # type: ignore[arg-type]
    assert controller.gate_ci_pending_hold(_entry()) is True
    assert vcs.checkouts == ["b7", "main"]
    assert controller.gate_ci_pending_hold(_entry()) is True
    assert vcs.checkouts == ["b7", "main"]
    assert runner.call_count == 1


# --- The arm's message names its own premise, not lap exhaustion -------------


def test_green_handoff_message_names_ci_pending_premise(
    mem_fs: dict[str, str],
) -> None:
    """The delivered message states "locally green, CI pending", not spent laps.

    The shared :func:`_handoff_message` asserts "has not landed after N unlanded
    remediation lap(s)", which is false at this fire point — the arm fires
    *before* any lap is spent, on a PR whose CI has not concluded. Asking Thomas
    to merge without naming the pending CI would overclaim.
    """
    stub = _handoff_stub()
    assert _call(stub, _entry()) is True
    body = json.loads(mem_fs[_MESSAGES].strip().splitlines()[0])["content"]
    assert "unlanded remediation lap" not in body
    assert "CI has not concluded yet" in body
    assert "once CI goes green" in body
    assert "PR #7" in body


# --- A RED CI conclusion releases the arm's own stand-down -------------------


def _seed_delivered(stub: SimpleNamespace) -> PrRemediationController:
    """Return a controller that has already delivered the arm's handoff for #7."""
    controller = PrRemediationController(stub)  # type: ignore[arg-type]
    assert controller.gate_ci_pending_hold(_entry()) is True
    return controller


def test_ci_red_after_green_handoff_releases_standdown(
    mem_fs: dict[str, str],
) -> None:
    """CI concluding RED refutes the handoff's premise → stand-down released.

    The local pair is ruff + mypy only, so a CI that later fails on tests would
    otherwise leave the loop muted on exactly the red it can fix: the keyed
    stand-down suppresses Tend re-minting across every head until merge/close.
    """
    stub = _handoff_stub()
    controller = _seed_delivered(stub)
    assert escalation_bridge.has_standdown(stub.escalation_standdowns, 7) is True
    # CI concludes RED: the gate's cheap first read now short-circuits, and the
    # arm's own stand-down is released on the way out.
    stub.evolution.code_host.get_pr_check_conclusion.return_value = "failure"
    assert controller.gate_ci_pending_hold(_entry()) is False
    assert escalation_bridge.has_standdown(stub.escalation_standdowns, 7) is False
    released = [
        json.loads(line)
        for line in mem_fs[_ACTIVATIONS].strip().splitlines()
        if json.loads(line)["action"] == "released"
    ]
    assert len(released) == 1
    assert released[0]["gate"] == "ci_pending_green_handoff"
    # Released exactly once — a second concluded read is a no-op.
    assert controller.gate_ci_pending_hold(_entry()) is False
    assert sum(
        1
        for line in mem_fs[_ACTIVATIONS].strip().splitlines()
        if json.loads(line)["action"] == "released"
    ) == 1


def test_ci_green_after_green_handoff_keeps_standdown(
    mem_fs: dict[str, str],
) -> None:
    """A GREEN conclusion leaves the handoff's premise intact — nothing released."""
    stub = _handoff_stub()
    controller = _seed_delivered(stub)
    stub.evolution.code_host.get_pr_check_conclusion.return_value = "success"
    assert controller.gate_ci_pending_hold(_entry()) is False
    assert escalation_bridge.has_standdown(stub.escalation_standdowns, 7) is True
    assert not [
        line
        for line in mem_fs[_ACTIVATIONS].strip().splitlines()
        if json.loads(line)["action"] == "released"
    ]


def test_release_is_silent_when_the_standdown_is_already_gone(
    mem_fs: dict[str, str],
) -> None:
    """An already-cleared stand-down logs no release row.

    ``clear_standdown`` returning ``False`` means someone else (a merge, a
    close, a drained user message) already cleared the arm's suppression, so
    there is nothing to release and no activation to stamp.
    """
    stub = _handoff_stub()
    controller = _seed_delivered(stub)
    assert escalation_bridge.clear_standdown(stub.escalation_standdowns, 7) is True
    stub.evolution.code_host.get_pr_check_conclusion.return_value = "failure"
    assert controller.gate_ci_pending_hold(_entry()) is False
    assert not [
        line
        for line in mem_fs[_ACTIVATIONS].strip().splitlines()
        if json.loads(line)["action"] == "released"
    ]


def test_foreign_standdown_is_never_released_by_the_arm(
    mem_fs: dict[str, str],
) -> None:
    """A stand-down this arm did not deliver survives a RED conclusion.

    Rule 22 / lap-budget / reality-refutation stand-downs keep their own
    merge-or-close release; only the arm's own deliveries are released here.
    """
    stub = _handoff_stub()
    stub.escalation_standdowns.mutate(
        lambda standdowns: standdowns.__setitem__(
            7,
            {
                "head_sha": "",
                "delivered_at": "2026-01-01T00:00:00+00:00",
                "serviced_comment_ids": [],
                "claim": "handoff-ready",
            },
        ),
    )
    controller = PrRemediationController(stub)  # type: ignore[arg-type]
    stub.evolution.code_host.get_pr_check_conclusion.return_value = "failure"
    assert controller.gate_ci_pending_hold(_entry()) is False
    assert escalation_bridge.has_standdown(stub.escalation_standdowns, 7) is True


def test_foreign_standdown_survives_after_the_arms_own_was_cleared(
    mem_fs: dict[str, str],
) -> None:
    """An external clear + a foreign re-delivery is never released by the arm.

    The arm hands PR #7 up, a drained user message clears *its* stand-down
    (``route_user_pr_message``), and the lap budget then records a *new*,
    foreign stand-down for the still-open PR. A later RED conclusion must not
    clear that one: the arm no longer owns the suppression, and deleting it
    would re-open the remediation treadmill the escalation bridge exists to
    stop.
    """
    stub = _handoff_stub()
    controller = _seed_delivered(stub)
    # (2) a drained user message clears the arm's own stand-down.
    assert escalation_bridge.clear_standdown(stub.escalation_standdowns, 7) is True
    # (3) the lap budget delivers a *new* stand-down for the still-open PR.
    stub.escalation_standdowns.mutate(
        lambda standdowns: standdowns.__setitem__(
            7,
            {
                "head_sha": "head1",
                "delivered_at": "2026-02-02T00:00:00+00:00",
                "serviced_comment_ids": [],
                "claim": "handoff-ready",
            },
        ),
    )
    # (4) the queued entry is re-selected and CI concludes RED.
    stub.evolution.code_host.get_pr_check_conclusion.return_value = "failure"
    assert controller.gate_ci_pending_hold(_entry()) is False
    assert escalation_bridge.has_standdown(stub.escalation_standdowns, 7) is True
    assert not [
        line
        for line in mem_fs[_ACTIVATIONS].strip().splitlines()
        if json.loads(line)["action"] == "released"
    ]


def test_green_handoff_skips_closed_unmerged_pr(mem_fs: dict[str, str]) -> None:
    """A closed-unmerged PR runs no gate and delivers no handoff (#4051)."""
    runner = _gate_runner(all_passed=True)
    stub = _handoff_stub(
        runner=runner, readiness=_readiness(landed=False, settled=True),
    )
    assert _call(stub, _entry()) is False
    runner.assert_not_called()
    assert _MESSAGES not in mem_fs
    assert _STANDDOWNS not in mem_fs
    assert _DEEDS not in mem_fs
    assert _ESCALATIONS not in mem_fs
    assert _ACTIVATIONS not in mem_fs


def test_run_task_phase_green_handoff_measures_pr_head_from_main(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """End to end: from ``main`` the phase's handoff arm gates the PR head (#4100).

    The workspace starts on ``main`` as it does before any pop; the arm must
    check the PR branch out, run the gate there, restore ``main``, deliver one
    handoff, and still leave the PR queued with no SDK session.
    """
    import asyncio

    agent = _phase_agent(make_agent, {7: ""})
    agent.config.ci_pending_green_handoff_enabled = True
    vcs = _FakeVCS()
    agent._vcs = vcs
    seen: list[str] = []
    agent._red_root_gate_runner = MagicMock(
        side_effect=lambda *_a: seen.append(vcs.current) or (0, "", ""),
    )
    _queue_prs(agent, 7)

    asyncio.run(agent._run_task_phase())

    assert seen == ["b7", "b7"]
    assert vcs.current == "main"
    agent.run.assert_not_awaited()
    assert len(agent.consciousness.load_self_model().pending_pr_requests) == 1
    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 7)
