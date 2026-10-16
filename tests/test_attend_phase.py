"""Proposal 130: the observe-only ``attend`` loop phase.

Covers the agent-side wiring and the phase body — registration in
``_LOOP_PHASES`` (and its absence from the budget/focus suppression sets), the
``phase_will_run`` gate matrix for both flag states, and the body's honest
candidate gather → single-moment assembly → single-episode append. The pure
attention policy (``conscious_workspace``) and the candidate adapters
(``workspace_candidates``) are covered in their own suites; here the concern is
that the phase drives them correctly, records exactly one immutable episode per
pass, reaches no host/git/subprocess seam, and stays byte-identical flag-off.

Every record is built in memory over ``mem_fs``; no real filesystem, network, or
subprocess is touched.
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import conscious_workspace as cw
from maestro.agent import _BUDGET_GATED_PHASES, _LOOP_PHASES
from maestro.conscious_workspace import (
    AttentionSchema,
    ConsciousEpisode,
    ConsciousMoment,
    build_workspace_item,
)
from maestro.consciousness import SelfModel
from maestro.loop.lifecycle import _FOCUS_SUPPRESSED_PHASES
from maestro.memory import TaskRecord
from maestro.state_store import SqliteConsciousEpisodeLedger, StateStore

# A hard-block credential — ``secret_scan.has_hard_block`` matches the AWS
# access-key shape, so a directive carrying it must be dropped, never persisted.
_CREDENTIAL = "AKIAIOSFODNN7EXAMPLE"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _self_model(**overrides: Any) -> SelfModel:
    kw: dict[str, Any] = dict(
        pending_user_requests=[],
        pending_pr_requests=[],
        open_questions=[],
        limitations_known=[],
        next_task_directive=None,
    )
    kw.update(overrides)
    return SelfModel(**kw)


def _cited_task(**overrides: Any) -> TaskRecord:
    """A minimal completed ``TaskRecord`` for the recurrence tests.

    ``workspace_item_ids`` is the citation the task-generation producer stamps; an
    override of ``None`` models an external / legacy task that cited no moment.
    """
    kw: dict[str, Any] = dict(
        task_id="t1", goal="do the thing", plan=[], outcome="success",
        result_summary="done", errors=[], duration_secs=1.0,
        timestamp="2026-01-01T00:00:00+00:00",
    )
    kw.update(overrides)
    return TaskRecord(**kw)


def _predecessor_episode(
    *, moment_id: str = "cm-pred", item_id: str = "wi-pred",
) -> ConsciousEpisode:
    """A minimal recorded episode to stand in as the previous pass's moment."""
    item = build_workspace_item(
        item_id=item_id, source_kind="audit_finding", source_ref="ref-1",
        observed_at="2026-01-01T00:00:00+00:00",
        trust="mechanical_observation", confidence=0.5, expires_at=None,
        summary="a bounded predecessor summary",
    )
    schema = AttentionSchema(
        focus="focus", uncertainty=0.5, capacity_used=1, omitted_count=0,
        permission_summary="all", blind_spots=("gap",),
    )
    moment = ConsciousMoment(
        moment_id=moment_id, predecessor_id=None, continuity="genesis",
        assembled_at="2026-01-01T00:00:00+00:00", policy_version="v1",
        items=(item,), excluded=(), schema=schema,
        candidate_hash="ch", selection_hash="sh",
        max_items=cw.MAX_MOMENT_ITEMS, max_chars=cw.MAX_MOMENT_CHARS,
    )
    return ConsciousEpisode(
        moment=moment, receipts=(), cited_task_ids=(), cited_plan_ids=(),
        recorded_at="2026-01-01T00:00:00+00:00",
    )


def _gate(agent, **overrides):  # type: ignore[no-untyped-def]
    """Call ``_phase_will_run`` for the attend phase with defaults."""
    kwargs = dict(
        iteration=1,
        smoke=False,
        first_iteration_after_restart=False,
        task_every=0,
        evolve_every=0,
        dream_every=0,
        audit_self_every=0,
    )
    kwargs.update(overrides)
    return agent._phase_will_run("attend", **kwargs)


# ---------------------------------------------------------------------------
# Registration + ordering
# ---------------------------------------------------------------------------


def test_attend_registered_after_tend_before_contemplate() -> None:
    assert "attend" in _LOOP_PHASES
    idx = _LOOP_PHASES.index("attend")
    assert idx == _LOOP_PHASES.index("tend_evolution_prs") + 1
    assert idx < _LOOP_PHASES.index("contemplate")


def test_attend_not_budget_gated() -> None:
    """Zero-SDK: the burn-rate throttle must not skip it (matches audit_self_model)."""
    assert "attend" not in _BUDGET_GATED_PHASES


def test_attend_not_focus_suppressed() -> None:
    """Not dropped by the external_delivery focus profile."""
    assert "attend" not in _FOCUS_SUPPRESSED_PHASES


# ---------------------------------------------------------------------------
# phase_will_run gate matrix
# ---------------------------------------------------------------------------


def test_attend_gate_off_by_default(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    assert agent.config.conscious_workspace_observe_enabled is False
    assert _gate(agent) is False


def test_attend_gate_on_when_flag_enabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.conscious_workspace_observe_enabled = True
    assert _gate(agent) is True


def test_attend_gate_skips_under_smoke_even_when_enabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.conscious_workspace_observe_enabled = True
    assert _gate(agent, smoke=True) is False


# ---------------------------------------------------------------------------
# Flag-off byte-identity: the append seam is never reached
# ---------------------------------------------------------------------------


def _neutralize_run_loop(agent) -> None:  # type: ignore[no-untyped-def]
    """Stub the loop's real-IO collaborators so a single iteration is
    deterministic (mirrors the run_loop mock set in test_curate_phase)."""
    from maestro.evolution import MergeCheckResult

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.ensure_main_branch = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="")  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent._process_rejected_plans = AsyncMock()  # type: ignore[method-assign]
    agent._respond_to_messages = AsyncMock()  # type: ignore[method-assign]
    agent._audit_self_model = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )


@pytest.mark.asyncio
async def test_run_loop_never_appends_episode_flag_off(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Flag-off, a full iteration proves the ledger append seam is
    ``assert_not_called`` — the phase body never runs, not merely suppressed."""
    agent = make_agent()
    agent.config.conscious_workspace_observe_enabled = False
    _neutralize_run_loop(agent)
    agent.conscious_episodes.append = MagicMock()  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    agent.conscious_episodes.append.assert_not_called()


@pytest.mark.asyncio
async def test_run_loop_appends_one_episode_flag_on(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Flag-on, a full iteration drives the phase and records exactly one episode."""
    agent = make_agent()
    agent.config.conscious_workspace_observe_enabled = True
    _neutralize_run_loop(agent)

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    assert len(agent.conscious_episodes.read_all()) == 1


# ---------------------------------------------------------------------------
# Body — one valid episode, bounds, and blind spots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attend_records_one_valid_bounded_episode(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    await agent._phase_attend(iteration=1)
    episodes = agent.conscious_episodes.read_all()
    assert len(episodes) == 1
    moment = episodes[0].moment
    assert moment.moment_id.startswith("cm-")
    # Capacity-limited: the admitted set never exceeds the hard cap.
    assert len(moment.items) <= cw.MAX_MOMENT_ITEMS
    # The follow-on decision-citation fields land empty this increment.
    assert episodes[0].cited_task_ids == ()
    assert episodes[0].cited_plan_ids == ()
    assert episodes[0].receipts == ()
    # The full moment is retained in memory for this iteration's broadcast
    # (proposal 130), and its per-moment receipt list is reset empty. ``moment``
    # here is the ledger-reparsed copy, so compare by id, not identity.
    assert agent._active_conscious_moment is not None
    assert agent._active_conscious_moment.moment_id == moment.moment_id
    assert agent._active_broadcast_receipts == []
    # The per-iteration citation accumulators are reset (proposal 130) so a plan
    # or task citing the previous moment never leaks into this episode.
    assert agent._active_cited_plan_ids == []
    assert agent._active_cited_task_ids == []


@pytest.mark.asyncio
async def test_attend_declares_absent_owners_as_blind_spots(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A make_agent has no diagnostic snapshot and no recorded audit, so both
    gaps — plus the always-omitted owned-WIP focus — are declared blind spots."""
    agent = make_agent()
    agent.memory.get_most_recent_audit = MagicMock(return_value=None)  # type: ignore[method-assign]
    assert agent._iter_diag_snapshot is None
    await agent._phase_attend(iteration=1)
    blind = agent.conscious_episodes.read_all()[0].moment.schema.blind_spots
    joined = " | ".join(blind)
    assert "task_focus" in joined
    assert "diagnostic snapshot absent" in joined
    assert "no audit result recorded yet" in joined


@pytest.mark.asyncio
async def test_attend_declares_an_empty_prediction_ledger_as_a_blind_spot(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A fresh agent has scored no reflection bet yet, so the ninth owner's gap is
    declared rather than silently omitted."""
    agent = make_agent()
    await agent._phase_attend(iteration=1)
    blind = agent.conscious_episodes.read_all()[0].moment.schema.blind_spots
    assert "no scored reflection bets in the ledger yet" in " | ".join(blind)


@pytest.mark.asyncio
async def test_attend_admits_a_measured_prediction_verdict(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A bet the calibration oracle scored enters the moment as a mechanical
    prediction_verdict candidate keyed on its prediction id — and the blind spot
    is withdrawn now that the owner has something to say."""
    agent = make_agent()
    agent.predictions.append({
        "source": "reflection", "prediction_id": "reflection-1",
        "prediction": "the gate will stay green", "loss_condition": "a red run",
        "verdict": "unscored", "timestamp": "2026-01-01T00:00:00+00:00",
    })
    agent.predictions.append({
        "source": "reflection_verdict", "prediction_id": "reflection-1",
        "verdict": "wrong", "verdict_source": "measured",
    })

    await agent._phase_attend(iteration=1)

    moment = agent.conscious_episodes.read_all()[0].moment
    scored = [i for i in moment.items if i.source_kind == "prediction_verdict"]
    assert len(scored) == 1
    assert scored[0].source_ref == "reflection-1"
    assert scored[0].trust == "mechanical_observation"
    blind = " | ".join(moment.schema.blind_spots)
    assert "no scored reflection bets" not in blind
    assert "no measured reflection verdicts yet" not in blind


@pytest.mark.asyncio
async def test_attend_skips_a_narrated_prediction_verdict(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A self-graded bet is scored — so the blind spot is withdrawn — but it is
    not a mechanical observation, so it contributes no candidate."""
    agent = make_agent()
    agent.predictions.append({
        "source": "reflection", "prediction_id": "reflection-1",
        "prediction": "the gate will stay green", "loss_condition": "a red run",
        "verdict": "unscored", "timestamp": "2026-01-01T00:00:00+00:00",
    })
    agent.predictions.append({
        "source": "reflection_verdict", "prediction_id": "reflection-1",
        "verdict": "right", "verdict_source": "narrated",
    })

    await agent._phase_attend(iteration=1)

    moment = agent.conscious_episodes.read_all()[0].moment
    assert [i for i in moment.items if i.source_kind == "prediction_verdict"] == []
    blind = " | ".join(moment.schema.blind_spots)
    assert "no scored reflection bets" not in blind
    # The owner still contributed nothing, so the distinct measurement gap stands.
    assert "no measured reflection verdicts yet" in blind


# ---------------------------------------------------------------------------
# Determinism — identical inputs yield an identical moment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attend_identical_inputs_yield_identical_moment(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The same facts and clock produce the same moment_id and both hashes — the
    property the burn-in corpus checks. Two passes over a controlled clock with a
    pinned-empty predecessor read are byte-for-byte identical."""
    agent = make_agent()
    agent.consciousness.load_self_model = MagicMock(  # type: ignore[method-assign]
        return_value=_self_model(open_questions=["what is stable?"]),
    )
    fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    captured: list[ConsciousEpisode] = []
    ledger = MagicMock()
    ledger.read_recent.return_value = []
    ledger.append.side_effect = captured.append
    agent.conscious_episodes = ledger  # type: ignore[assignment]

    with patch("maestro.agent.datetime") as dt:
        dt.now.return_value = fixed
        await agent._phase_attend(iteration=1)
        await agent._phase_attend(iteration=2)

    assert len(captured) == 2
    a, b = captured[0].moment, captured[1].moment
    assert a.moment_id == b.moment_id
    assert a.candidate_hash == b.candidate_hash
    assert a.selection_hash == b.selection_hash
    assert a.candidate_hash != ""


# ---------------------------------------------------------------------------
# Continuity — each of the four ContinuityKind branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continuity_genesis_on_empty_ledger(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    await agent._phase_attend(iteration=1)
    assert agent.conscious_episodes.read_all()[0].moment.continuity == "genesis"


@pytest.mark.asyncio
async def test_continuity_linked_when_predecessor_is_active(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    pred = _predecessor_episode(moment_id="cm-prev")
    ledger = MagicMock()
    ledger.read_recent.return_value = [pred]
    captured: list[ConsciousEpisode] = []
    ledger.append.side_effect = captured.append
    agent.conscious_episodes = ledger  # type: ignore[assignment]
    agent._active_conscious_moment_id = "cm-prev"
    agent._pending_crash_gap = False
    await agent._phase_attend(iteration=2)
    assert captured[0].moment.continuity == "linked"
    assert captured[0].moment.predecessor_id == "cm-prev"


@pytest.mark.asyncio
async def test_continuity_crash_gap_consumes_one_shot(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A crash marker forces crash_gap on the first pass and self-extinguishes:
    the next pass over the same active id is linked."""
    agent = make_agent()
    pred = _predecessor_episode(moment_id="cm-prev")
    ledger = MagicMock()
    ledger.read_recent.return_value = [pred]
    captured: list[ConsciousEpisode] = []
    ledger.append.side_effect = captured.append
    agent.conscious_episodes = ledger  # type: ignore[assignment]
    agent._active_conscious_moment_id = "cm-prev"
    agent._pending_crash_gap = True

    await agent._phase_attend(iteration=1)
    assert captured[0].moment.continuity == "crash_gap"
    assert agent._pending_crash_gap is False

    # Next pass: predecessor is now the just-recorded moment, marker spent.
    ledger.read_recent.return_value = [captured[0]]
    await agent._phase_attend(iteration=2)
    assert captured[1].moment.continuity == "linked"


@pytest.mark.asyncio
async def test_continuity_disabled_interval_when_predecessor_stale(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A predecessor that is not the restored active moment means the loop ran
    without recording — recorded as a disabled_interval, not silently repaired."""
    agent = make_agent()
    pred = _predecessor_episode(moment_id="cm-old")
    ledger = MagicMock()
    ledger.read_recent.return_value = [pred]
    captured: list[ConsciousEpisode] = []
    ledger.append.side_effect = captured.append
    agent.conscious_episodes = ledger  # type: ignore[assignment]
    # Active id does not match the predecessor (or is unset after a flag-off run).
    agent._active_conscious_moment_id = None
    agent._pending_crash_gap = False
    await agent._phase_attend(iteration=2)
    assert captured[0].moment.continuity == "disabled_interval"


# ---------------------------------------------------------------------------
# Credential refusal — a hard-block summary is dropped, never persisted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credential_bearing_candidate_is_dropped(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.consciousness.load_self_model = MagicMock(  # type: ignore[method-assign]
        return_value=_self_model(
            pending_user_requests=[f"use {_CREDENTIAL} to deploy", "ship the fix"],
        ),
    )
    await agent._phase_attend(iteration=1)
    moment = agent.conscious_episodes.read_all()[0].moment
    serialized = " ".join(item.summary for item in moment.items)
    assert _CREDENTIAL not in serialized
    # The clean sibling directive still made it in — one bad summary is dropped,
    # its siblings are not.
    assert any("ship the fix" in item.summary for item in moment.items)


# ---------------------------------------------------------------------------
# Recurrence — a cited task re-enters the moment carrying its lineage (prop 130)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attend_admits_task_outcome_carrying_its_lineage(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A completed task that cited workspace items re-enters the next moment as a
    task_outcome candidate retaining those ids as lineage — the recurrence link.
    The phase already passes ``get_recent_tasks`` into the gather, so stamping the
    adapter is all it takes for the successor candidate to carry lineage."""
    agent = make_agent()
    agent.consciousness.load_self_model = MagicMock(  # type: ignore[method-assign]
        return_value=_self_model(),
    )
    cited = _cited_task(task_id="t-cited", workspace_item_ids=["wi-1", "wi-2"])
    agent.memory.get_recent_tasks = MagicMock(  # type: ignore[method-assign]
        return_value=[cited],
    )

    await agent._phase_attend(iteration=1)

    moment = agent.conscious_episodes.read_all()[0].moment
    outcomes = [i for i in moment.items if i.source_kind == "task_outcome"]
    assert len(outcomes) == 1
    assert outcomes[0].lineage == ("wi-1", "wi-2")


@pytest.mark.asyncio
async def test_attend_uncited_task_yields_lineage_less_item(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Negative control: a recent task with no citation (None) re-enters as a
    lineage-less task_outcome item — the recurrence signal is only present when a
    prior moment actually steered the task."""
    agent = make_agent()
    agent.consciousness.load_self_model = MagicMock(  # type: ignore[method-assign]
        return_value=_self_model(),
    )
    uncited = _cited_task(task_id="t-plain", workspace_item_ids=None)
    agent.memory.get_recent_tasks = MagicMock(  # type: ignore[method-assign]
        return_value=[uncited],
    )

    await agent._phase_attend(iteration=1)

    moment = agent.conscious_episodes.read_all()[0].moment
    outcomes = [i for i in moment.items if i.source_kind == "task_outcome"]
    assert len(outcomes) == 1
    assert outcomes[0].lineage == ()


# ---------------------------------------------------------------------------
# No host / git / subprocess seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attend_reaches_no_host_git_or_subprocess_seam(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Every subprocess seam is patched to raise and the VCS is a tripwire: the
    phase still records an episode, proving it reaches none of them."""
    agent = make_agent()
    tripwire = MagicMock(side_effect=AssertionError("git touched"))
    agent._vcs = MagicMock()  # type: ignore[method-assign]
    agent._vcs.fetch = tripwire
    agent._vcs.run = tripwire

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("subprocess seam reached")

    with (
        patch.object(subprocess, "run", _boom),
        patch.object(subprocess, "Popen", _boom),
        patch.object(subprocess, "check_output", _boom),
        patch.object(asyncio, "create_subprocess_exec", _boom),
        patch.object(asyncio, "create_subprocess_shell", _boom),
    ):
        await agent._phase_attend(iteration=1)

    assert len(agent.conscious_episodes.read_all()) == 1
    agent._vcs.fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Both backends over mem_fs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attend_records_over_jsonl_backend(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The make_agent default is the JSONL adapter; a pass round-trips through it."""
    agent = make_agent()
    from maestro.conscious_workspace import JsonlConsciousEpisodeLedger
    assert isinstance(agent.conscious_episodes, JsonlConsciousEpisodeLedger)
    await agent._phase_attend(iteration=1)
    assert len(agent.conscious_episodes.read_all()) == 1


@pytest.mark.asyncio
async def test_attend_records_over_sqlite_backend(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Swapping in the SQLite adapter exercises the same phase over a DB store."""
    agent = make_agent()
    agent.conscious_episodes = SqliteConsciousEpisodeLedger(  # type: ignore[assignment]
        StateStore(":memory:"),
    )
    await agent._phase_attend(iteration=1)
    episodes = agent.conscious_episodes.read_all()
    assert len(episodes) == 1
    assert episodes[0].moment.moment_id.startswith("cm-")


# ---------------------------------------------------------------------------
# read_active_conscious_moment_id accessor (step 9)
# ---------------------------------------------------------------------------


def test_read_active_moment_id_missing_file_returns_none(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    assert agent._lifecycle.read_active_conscious_moment_id() is None


def test_read_active_moment_id_present(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = (
        '{"active_conscious_moment_id": "cm-abc123"}'
    )
    assert agent._lifecycle.read_active_conscious_moment_id() == "cm-abc123"


def test_read_active_moment_id_absent_key_returns_none(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = '{"iteration": 3}'
    assert agent._lifecycle.read_active_conscious_moment_id() is None


def test_read_active_moment_id_empty_string_returns_none(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = (
        '{"active_conscious_moment_id": ""}'
    )
    assert agent._lifecycle.read_active_conscious_moment_id() is None


def test_read_active_moment_id_non_dict_returns_none(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """A corrupt (non-object) lifecycle.json degrades to None, not a crash."""
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = "[1, 2, 3]"
    assert agent._lifecycle.read_active_conscious_moment_id() is None


# ---------------------------------------------------------------------------
# _finalize_conscious_episode — end-of-iteration receipt persistence (prop 130)
# ---------------------------------------------------------------------------


def _finalizer_moment() -> ConsciousMoment:
    """A real assembled moment for the finalizer tests (one above-floor item)."""
    item = build_workspace_item(
        item_id="wi-fin", source_kind="audit_finding", source_ref="ref-fin",
        observed_at="2026-01-01T00:00:00+00:00", trust="mechanical_observation",
        confidence=0.5, expires_at=None, summary="a bounded finalizer summary",
    )
    return cw.assemble_moment(
        [item], assembled_at="2026-01-01T00:00:00+00:00",
        permission_summary="all", blind_spots=["gap"],
        predecessor_id=None, continuity="genesis",
    )


def _finalizer_receipt(
    moment: ConsciousMoment, consumer: str = "contemplation",
) -> cw.BroadcastReceipt:
    return cw.build_broadcast_receipt(
        moment=moment, consumer=consumer,  # type: ignore[arg-type]
        delivered_at="2026-01-01T00:00:01+00:00", redacted_item_ids=(),
    )


def test_finalize_persists_collected_receipts(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Broadcast on with a receipt collected: one superseding episode is appended
    carrying the active moment and exactly the collected receipts, and the
    per-iteration receipt list is cleared."""
    agent = make_agent()
    agent.config.conscious_workspace_broadcast_enabled = True
    moment = _finalizer_moment()
    agent._active_conscious_moment = moment
    receipt = _finalizer_receipt(moment)
    agent._active_broadcast_receipts = [receipt]
    # Two plans and two tasks cited this moment, one id repeated each (proposal
    # 130): the episode records both lists order-preserved and de-duplicated.
    agent._active_cited_plan_ids = ["plan-a", "plan-b", "plan-a"]
    agent._active_cited_task_ids = ["task-a", "task-b", "task-a"]

    agent._finalize_conscious_episode()

    episodes = agent.conscious_episodes.read_all()
    assert len(episodes) == 1
    assert episodes[0].moment.moment_id == moment.moment_id
    assert episodes[0].receipts == (receipt,)
    # Both citation lists reflect the accumulated ids, order-preserved and
    # de-duplicated.
    assert episodes[0].cited_task_ids == ("task-a", "task-b")
    assert episodes[0].cited_plan_ids == ("plan-a", "plan-b")
    assert agent._active_broadcast_receipts == []
    # Both citation accumulators are drained alongside the receipt list.
    assert agent._active_cited_plan_ids == []
    assert agent._active_cited_task_ids == []


def test_finalize_cited_plan_ids_empty_when_none_cited(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Broadcast on with a receipt but no plan cited the moment (proposal 130):
    the episode's ``cited_plan_ids`` is the empty tuple."""
    agent = make_agent()
    agent.config.conscious_workspace_broadcast_enabled = True
    moment = _finalizer_moment()
    agent._active_conscious_moment = moment
    agent._active_broadcast_receipts = [_finalizer_receipt(moment)]
    agent._active_cited_plan_ids = []

    agent._finalize_conscious_episode()

    episodes = agent.conscious_episodes.read_all()
    assert len(episodes) == 1
    assert episodes[0].cited_plan_ids == ()


def test_finalize_noop_when_no_receipts(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Broadcast on but no receipt collected: nothing is appended."""
    agent = make_agent()
    agent.config.conscious_workspace_broadcast_enabled = True
    agent._active_conscious_moment = _finalizer_moment()
    agent._active_broadcast_receipts = []
    agent.conscious_episodes.append = MagicMock()  # type: ignore[method-assign]

    agent._finalize_conscious_episode()

    agent.conscious_episodes.append.assert_not_called()


def test_finalize_noop_when_broadcast_flag_off(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Broadcast off: nothing is appended and — the flag guard firing first — the
    receipt list is not even cleared, so a flag-off run stays byte-identical."""
    agent = make_agent()
    agent.config.conscious_workspace_broadcast_enabled = False
    moment = _finalizer_moment()
    agent._active_conscious_moment = moment
    receipt = _finalizer_receipt(moment)
    agent._active_broadcast_receipts = [receipt]
    agent._active_cited_plan_ids = ["plan-a"]
    agent._active_cited_task_ids = ["task-a"]
    agent.conscious_episodes.append = MagicMock()  # type: ignore[method-assign]

    agent._finalize_conscious_episode()

    agent.conscious_episodes.append.assert_not_called()
    assert agent._active_broadcast_receipts == [receipt]
    # Both citation accumulators are likewise untouched with the flag off.
    assert agent._active_cited_plan_ids == ["plan-a"]
    assert agent._active_cited_task_ids == ["task-a"]


def test_finalize_resolves_moment_on_cold_resume(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Cold mid-iteration resume: the in-memory moment is gone but its id is
    restored and the bootstrap row is in the ledger, so the finalizer reconstructs
    the moment and appends a superseding episode sharing its moment_id."""
    agent = make_agent()
    agent.config.conscious_workspace_broadcast_enabled = True
    moment = _finalizer_moment()
    bootstrap = ConsciousEpisode(
        moment=moment, receipts=(), cited_task_ids=(), cited_plan_ids=(),
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    agent.conscious_episodes.append(bootstrap)
    agent._active_conscious_moment = None
    agent._active_conscious_moment_id = moment.moment_id
    receipt = _finalizer_receipt(moment, consumer="evolution")
    agent._active_broadcast_receipts = [receipt]

    agent._finalize_conscious_episode()

    episodes = agent.conscious_episodes.read_all()
    assert len(episodes) == 2
    assert episodes[1].moment.moment_id == moment.moment_id
    assert episodes[1].receipts == (receipt,)
    assert agent._active_broadcast_receipts == []


def test_finalize_noop_when_moment_unresolvable(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Broadcast on with receipts but no resolvable moment (in-memory gone, no
    restored id): nothing is appended."""
    agent = make_agent()
    agent.config.conscious_workspace_broadcast_enabled = True
    moment = _finalizer_moment()
    agent._active_conscious_moment = None
    agent._active_conscious_moment_id = None
    agent._active_broadcast_receipts = [_finalizer_receipt(moment)]
    agent.conscious_episodes.append = MagicMock()  # type: ignore[method-assign]

    agent._finalize_conscious_episode()

    agent.conscious_episodes.append.assert_not_called()


@pytest.mark.asyncio
async def test_run_loop_finalizes_episode_with_receipts_flag_on(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Both flags on with one consumer delivery during the iteration: the loop
    ends with a finalized episode carrying the receipt, co-existing with the
    ``attend`` bootstrap row and sharing its moment_id."""
    agent = make_agent()
    agent.config.conscious_workspace_observe_enabled = True
    agent.config.conscious_workspace_broadcast_enabled = True
    _neutralize_run_loop(agent)

    # A consumer delivery during the iteration: the mocked contemplate stands in
    # for a phase that broadcasts the moment, collecting one receipt.
    async def _deliver(*_a: object, **_k: object) -> str:
        agent._broadcast_moment_prefix("contemplation")
        return ""

    agent.contemplate = _deliver  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    episodes = agent.conscious_episodes.read_all()
    assert len(episodes) == 2
    bootstrap, finalized = episodes[0], episodes[1]
    assert bootstrap.receipts == ()
    assert finalized.moment.moment_id == bootstrap.moment.moment_id
    assert len(finalized.receipts) >= 1
    assert any(r.consumer == "contemplation" for r in finalized.receipts)
    assert all(
        r.moment_hash == finalized.moment.selection_hash
        for r in finalized.receipts
    )
    assert agent._active_broadcast_receipts == []


@pytest.mark.asyncio
async def test_run_loop_broadcast_off_writes_only_bootstrap(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Observe on, broadcast off: a full iteration writes exactly the bootstrap
    episode with empty receipts — the finalizer no-ops."""
    agent = make_agent()
    agent.config.conscious_workspace_observe_enabled = True
    agent.config.conscious_workspace_broadcast_enabled = False
    _neutralize_run_loop(agent)

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    episodes = agent.conscious_episodes.read_all()
    assert len(episodes) == 1
    assert episodes[0].receipts == ()


@pytest.mark.asyncio
async def test_run_loop_finalize_oserror_is_swallowed(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A ledger write failure in the finalizer is best-effort: the
    iteration-completion guard swallows the ``OSError`` and the loop completes
    without crashing (mirrors the usage-flush guard below it)."""
    agent = make_agent()
    agent.config.conscious_workspace_observe_enabled = True
    agent.config.conscious_workspace_broadcast_enabled = True
    _neutralize_run_loop(agent)
    agent._finalize_conscious_episode = MagicMock(  # type: ignore[method-assign]
        side_effect=OSError("disk full"),
    )

    # Must complete rather than propagate the ledger write error.
    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    agent._finalize_conscious_episode.assert_called_once()


@pytest.mark.asyncio
async def test_run_loop_finalize_sqlite_error_is_swallowed(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Under ``[paths] sqlite_state_enabled`` the finalizer's ledger append is a
    :class:`SqliteConsciousEpisodeLedger` whose write raises ``sqlite3.Error``
    (not ``OSError``); the iteration-completion guard must swallow it too so the
    loop completes rather than crashing (regression for the ledger finding)."""
    agent = make_agent()
    agent.config.conscious_workspace_observe_enabled = True
    agent.config.conscious_workspace_broadcast_enabled = True
    _neutralize_run_loop(agent)
    agent._finalize_conscious_episode = MagicMock(  # type: ignore[method-assign]
        side_effect=sqlite3.OperationalError("database is locked"),
    )

    # Must complete rather than propagate the sqlite write error.
    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    agent._finalize_conscious_episode.assert_called_once()
