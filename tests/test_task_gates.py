"""Unit tests for the declarative task-gate registry (proposal 069).

Covers :func:`run_gate_chain` in isolation — synthetic gates, a stub
agent, no :class:`MaestroAgent` — plus every shipped gate's ``run``
callable with its predicate patched, and the :data:`GATE_CHAIN` /
:func:`gate_order` precedence contract.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from maestro import audit_checks, task_gates
from maestro.audit_checks import (
    _DIRECTIVE_MAX_CHARS,
    SELF_DIRECTED_WINDOW,
    JsonlGateActivationLedger,
    log_gate_activation,
)
from maestro.ci_diagnosis import CheckRun
from maestro.config import Config
from maestro.consciousness import Consciousness, PendingPRRequest, SelfModel
from maestro.deed_ledger import DeedType, JsonlDeedLedger
from maestro.directive_ledger import JsonlDroppedDirectiveLedger
from maestro.escalation_bridge import JsonEscalationLedger, UnlandedCureHandoff
from maestro.lever_capture import JsonlLeverAttemptsLedger
from maestro.loop.pr_remediation import _compose_pr_remediation_prompt
from maestro.memory import EvolutionPlan, Memory, TaskRecord
from maestro.outcome_authority import (
    _PUSH_OWED_CLAUSE,
    _PUSH_OWED_UNVERIFIED_CLAUSE,
)
from maestro.pr_facts import PRReadiness
from maestro.remediation_tracker import (
    JsonlStandDownVerdictLedger,
    release_stand_downs,
)
from maestro.state_store import SqliteGateActivationLedger, StateStore
from maestro.task_gates import (
    _CONSECUTIVE_PARTIAL_DIRECTIVE,
    _CONSECUTIVE_PARTIAL_GATE,
    _CONSECUTIVE_PARTIAL_RELEASE_STREAK,
    _CONSECUTIVE_PARTIAL_THRESHOLD,
    _COST_RISK_RELEASE_STREAK,
    _FAILED_BRANCH_WINDOW,
    _UNLOSEABLE_STREAK_THRESHOLD,
    GATE_CHAIN,
    GateContext,
    GateOutcome,
    TaskGate,
    _always_enabled,
    _audit_stall_run,
    _branchless_streak_run,
    _build_touched_artifact_ids,
    _build_untouched_candidate_ids,
    _consecutive_partial_breaker_run,
    _cost_risk_run,
    _divert_touched_directive,
    _dropped_directive_replay_run,
    _escalation_bridge_run,
    _failed_branch_redirect_directive,
    _failed_branch_targets,
    _forced_read_run,
    _instrument_consumption_run,
    _integration_cadence_run,
    _investigation_rate_run,
    _investigation_streak_run,
    _no_repeat_failed_branch_target_run,
    _obsolete_plan_touched_ids,
    _partial_construction_run,
    _promotable_backlog_run,
    _quiet_skip_run,
    _readmission_construction_run,
    _referenced_branch_tokens,
    _self_directed_success_run,
    _source_task_id_eligible,
    _stale_obsolete_directive_run,
    _trailing_identical_blocks,
    _unloseable_streak_run,
    _wip_is_code_clearable_red,
    _wip_pr_merge_ready,
    _wip_primacy_code_red_directive_text,
    _wip_primacy_directive_text,
    _wip_primacy_handoff_directive_text,
    _wip_primacy_run,
    check_untouched_first_target,
    classify_task_loseable,
    count_trailing_partial_outcomes,
    gate_order,
    investigation_observable_gate,
    is_investigation_observable_flee,
    run_gate_chain,
)
from maestro.task_queue import QueuedTask
from maestro.types import TaskSource
from maestro.wip_primacy import Discretionary, TendOwnedPR

_DIR = Path("/fake/gates")


def _make_config(**overrides: Any) -> Config:
    return Config(memory_dir=_DIR, workspace=_DIR, **overrides)


class _StubMemory:
    """Recording stand-in for the slice of Memory the gates read."""

    def __init__(
        self,
        recent: list[TaskRecord] | None = None,
        audits: list[Any] | None = None,
        resolutions: list[Any] | None = None,
    ) -> None:
        self.recent = recent or []
        self.audits = audits or []
        self.resolutions = resolutions or []
        self.recent_calls: list[int] = []

    def get_recent_tasks(self, n: int) -> list[TaskRecord]:
        self.recent_calls.append(n)
        return self.recent

    def get_recent_audit_results(self, n: int) -> list[Any]:
        return self.audits

    def get_recent_question_resolutions(self, n: int) -> list[Any]:
        return self.resolutions


class _StubAgent:
    """Just the attribute surface the gate chain touches — no MaestroAgent."""

    _STALL_LINEAGE_RESOLUTION_LOOKBACK = 50

    def __init__(
        self,
        memory: _StubMemory | None = None,
        queue: list[Any] | None = None,
    ) -> None:
        self.memory = memory or _StubMemory()
        self.task_queue = MagicMock()
        self.task_queue.load.return_value = queue or []
        self.journal = MagicMock()
        # ``run_gate_chain`` persists directives through ``_mutate_self_model_persisted``
        # (#4302); the stub applies the transaction to ``live_model`` — which
        # ``_make_ctx`` seeds from the context's model — so tests observe both
        # the call and the persisted slot.
        self.live_model = SelfModel()
        self.consciousness = MagicMock()
        self.consciousness._mutate_self_model_persisted.side_effect = (
            lambda fn: (fn(self.live_model), deepcopy(self.live_model))
        )
        # The escalation-bridge gate forwards this host-first merge-truth oracle
        # to ``detect_unlanded_cure_handoff`` (proposal 080 Phase 1).
        self._host_or_git_pr_merged = MagicMock(name="_host_or_git_pr_merged")
        # The per-iteration readiness memo ``_current_task_focus`` populates and
        # the ``wip_primacy`` gate reads to branch its directive on measured
        # merge-readiness. Empty by default (memo miss → not merge-ready).
        self._pr_readiness_memo: dict[int, PRReadiness] = {}
        # The dropped-directive replay gate reads through this backend seam
        # (proposal 106); the default JSONL adapter over ``_DIR`` reads/writes
        # the same ``dropped_directives.jsonl`` the replay tests seed in mem_fs.
        self.dropped_directives = JsonlDroppedDirectiveLedger(_DIR)
        # The touched-artifact gate reads the stand-down verdict PRs through this
        # backend seam (proposal 106); the default JSONL adapter over ``_DIR``
        # reads the same ``stand_down_verdicts.jsonl`` these tests seed in mem_fs.
        self.stand_down_verdicts = JsonlStandDownVerdictLedger(_DIR)
        # The touched-artifact gate reads ``PR_CLOSE`` deeds through this seam;
        # the JSONL adapter over ``_DIR`` reads ``deed_ledger.jsonl`` in mem_fs.
        self.deed_ledger = JsonlDeedLedger(_DIR)
        # The stale-obsolete-directive guard reads the distinct-target selector
        # through this backend seam (proposal 106); the default JSONL adapter over
        # ``_DIR`` reads the same ``lever_attempts.jsonl`` these tests seed.
        self.lever_attempts = JsonlLeverAttemptsLedger(_DIR)
        # The directive/block gates append their firing rows through this backend
        # seam (proposal 106); the default JSONL adapter over ``_DIR`` writes the
        # same ``gate_activations.jsonl`` ``_gate_records`` reads back.
        self.gate_activations = JsonlGateActivationLedger(_DIR)
        # The escalation-bridge gate reaches the standdown ledger through this
        # backend seam (proposal 106); the default JSON adapter over ``_DIR``
        # reads/writes the same ``escalation_handoffs.json`` these tests seed.
        self.escalation_standdowns = JsonEscalationLedger(_DIR)
        # The escalation-bridge gate passes this owning-plan lookup to
        # ``deliver_handoff`` so the merge-or-close ping can name the capability
        # the merge lands. Defaults to no plan (the generic-message path).
        self._plan_for_pr_number = MagicMock(
            name="_plan_for_pr_number", return_value=None,
        )


def _make_ctx(
    config: Config | None = None,
    model: SelfModel | None = None,
    agent: _StubAgent | None = None,
) -> GateContext:
    agent = agent or _StubAgent()
    model = model or SelfModel()
    agent.live_model = deepcopy(model)
    return GateContext(
        agent=agent,  # type: ignore[arg-type]
        config=config or _make_config(),
        model=model,
    )


def _gate_records(mem_fs: dict[str, str]) -> list[dict[str, Any]]:
    raw = mem_fs.get(str(_DIR / "gate_activations.jsonl"), "")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _ga() -> JsonlGateActivationLedger:
    """The default-off JSONL gate-activation backend over ``_DIR`` (proposal 106).

    ``investigation_observable_gate`` now takes a ``GateActivationBackend``; this
    binds the JSONL adapter over the same dir ``_gate_records`` reads, so the
    tests still assert against ``_DIR/gate_activations.jsonl``.
    """
    return JsonlGateActivationLedger(_DIR)


def _gate(
    name: str,
    kind: str,
    run: Any,
    enabled: Any = _always_enabled,
) -> TaskGate:
    return TaskGate(name=name, kind=kind, enabled=enabled, run=run)  # type: ignore[arg-type]


def _task(
    task_id: str = "t-1",
    goal: str = "a goal",
    branch: str | None = None,
    result_summary: str = "ok",
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id, goal=goal, plan=[], outcome="success",
        result_summary=result_summary, errors=[], duration_secs=1.0,
        timestamp="2026-01-01T00:00:00Z", branch=branch,
    )


# ---------------------------------------------------------------------------
# run_gate_chain — synthetic gates
# ---------------------------------------------------------------------------


def test_empty_chain_returns_false(mem_fs: dict[str, str]) -> None:
    ctx = _make_ctx()
    blocked = run_gate_chain([], ctx)
    assert blocked is False
    assert _gate_records(mem_fs) == []


def test_block_short_circuits_later_gates(mem_fs: dict[str, str]) -> None:
    """The first blocker wins; later gates are never evaluated."""
    later = MagicMock(
        return_value=GateOutcome(detail="d", log_message="m", directive="x"),
    )
    gates = [
        _gate("blocker", "block", lambda ctx: GateOutcome(
            detail="stopped", log_message="blocker engaged",
        )),
        _gate("later_directive", "directive", later),
    ]
    ctx = _make_ctx()

    blocked = run_gate_chain(gates, ctx)

    assert blocked is True
    later.assert_not_called()
    assert ctx.model.next_task_directive is None
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "blocker"
    assert records[0]["action"] == "blocked"
    assert records[0]["detail"] == "stopped"


# --- #4302: runner writes are live-slot transactions, never snapshot saves ---


def _real_consciousness_ctx(
    snapshot_directive: str | None = None,
) -> tuple[_StubAgent, Consciousness, GateContext]:
    """A stub agent over a real ``Consciousness`` plus a stale-snapshot ctx."""
    consciousness = Consciousness(_DIR)
    consciousness.mutate_self_model(
        lambda m: setattr(m, "next_task_directive", snapshot_directive),
    )
    agent = _StubAgent()
    agent.consciousness = consciousness  # type: ignore[assignment]
    ctx = GateContext(
        agent=agent,  # type: ignore[arg-type]
        config=_make_config(),
        model=consciousness.load_self_model(),
    )
    return agent, consciousness, ctx


def _directive_gate(
    directive: str, *, rewrites: bool = False, name: str = "bridge",
) -> TaskGate:
    return TaskGate(
        name=name, kind="directive", enabled=_always_enabled,
        run=lambda ctx: GateOutcome(
            detail="d", log_message="m", directive=directive,
        ),
        rewrites=rewrites,
    )


def test_directive_gate_preserves_concurrent_self_model_fields(
    mem_fs: dict[str, str],
) -> None:
    _agent, consciousness, ctx = _real_consciousness_ctx()
    consciousness.mutate_self_model(
        lambda m: setattr(m, "pending_user_requests", ["concurrent user request"]),
    )

    blocked = run_gate_chain([_directive_gate("do the thing")], ctx)

    assert blocked is False
    persisted = consciousness.load_self_model()
    assert persisted.pending_user_requests == ["concurrent user request"]
    assert persisted.next_task_directive == "do the thing"
    assert ctx.model.pending_user_requests == ["concurrent user request"]


def test_directive_gate_yields_to_concurrently_set_directive(
    mem_fs: dict[str, str],
) -> None:
    _agent, consciousness, ctx = _real_consciousness_ctx()
    consciousness.mutate_self_model(
        lambda m: setattr(m, "next_task_directive", "concurrent directive"),
    )
    later = MagicMock(return_value=GateOutcome(
        detail="d", log_message="m", directive="later",
    ))
    gates = [
        _directive_gate("do the thing"),
        TaskGate(
            name="later", kind="directive", enabled=_always_enabled, run=later,
        ),
    ]

    blocked = run_gate_chain(gates, ctx)

    assert blocked is False
    assert consciousness.load_self_model().next_task_directive == (
        "concurrent directive"
    )
    assert ctx.model.next_task_directive == "concurrent directive"
    later.assert_not_called()
    assert _gate_records(mem_fs) == []


def test_block_gate_directive_preserves_concurrent_fields_and_occupied_slot(
    mem_fs: dict[str, str],
) -> None:
    _agent, consciousness, ctx = _real_consciousness_ctx()

    def _concurrent(m: SelfModel) -> None:
        m.next_task_directive = "live directive"
        m.pending_user_requests = ["concurrent user request"]

    consciousness.mutate_self_model(_concurrent)
    gates = [
        TaskGate(
            name="wip_like", kind="block", enabled=_always_enabled,
            run=lambda ctx: GateOutcome(
                detail="pr=#9", log_message="blocked", directive="finish PR",
            ),
        ),
    ]

    blocked = run_gate_chain(gates, ctx)

    assert blocked is True
    persisted = consciousness.load_self_model()
    assert persisted.next_task_directive == "live directive"
    assert persisted.pending_user_requests == ["concurrent user request"]
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["action"] == "blocked"


def test_rewrite_gate_does_not_replace_newer_directive(
    mem_fs: dict[str, str],
) -> None:
    _agent, consciousness, ctx = _real_consciousness_ctx("old")
    consciousness.mutate_self_model(
        lambda m: setattr(m, "next_task_directive", "newer"),
    )

    blocked = run_gate_chain([_directive_gate("rewritten", rewrites=True)], ctx)

    assert blocked is False
    assert consciousness.load_self_model().next_task_directive == "newer"
    assert _gate_records(mem_fs) == []


def test_rewrite_gate_replaces_unchanged_evaluated_directive(
    mem_fs: dict[str, str],
) -> None:
    _agent, consciousness, ctx = _real_consciousness_ctx("old")

    blocked = run_gate_chain([_directive_gate("rewritten", rewrites=True)], ctx)

    assert blocked is False
    assert consciousness.load_self_model().next_task_directive == "rewritten"
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["action"] == "directive_set"


def test_rewrite_gate_fills_empty_slot_when_evaluated_empty(
    mem_fs: dict[str, str],
) -> None:
    _agent, consciousness, ctx = _real_consciousness_ctx()

    blocked = run_gate_chain([_directive_gate("filled", rewrites=True)], ctx)

    assert blocked is False
    assert consciousness.load_self_model().next_task_directive == "filled"


def test_directive_gate_rebinds_ctx_model_to_persisted_capped_copy(
    mem_fs: dict[str, str],
) -> None:
    _agent, consciousness, ctx = _real_consciousness_ctx()
    overlong = "x" * (Consciousness.MAX_DIRECTIVE_CHARS + 50)

    run_gate_chain([_directive_gate(overlong)], ctx)

    persisted = consciousness.load_self_model().next_task_directive
    assert persisted is not None
    assert persisted != overlong
    assert ctx.model.next_task_directive == persisted


def test_directive_fills_empty_slot(mem_fs: dict[str, str]) -> None:
    agent = _StubAgent()
    ctx = _make_ctx(agent=agent)
    gates = [
        _gate("bridge", "directive", lambda ctx: GateOutcome(
            detail="bridge detail", log_message="bridge fired",
            directive="do the thing",
        )),
    ]

    blocked = run_gate_chain(gates, ctx)

    assert blocked is False
    assert ctx.model.next_task_directive == "do the thing"
    agent.consciousness._mutate_self_model_persisted.assert_called_once()
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "bridge"
    assert records[0]["action"] == "directive_set"
    assert records[0]["detail"] == "bridge detail"


def test_second_directive_skipped_once_slot_full(
    mem_fs: dict[str, str],
) -> None:
    """Once a bridge fills the slot, later bridges are not even called."""
    second = MagicMock(
        return_value=GateOutcome(detail="d2", log_message="m2", directive="b"),
    )
    gates = [
        _gate("first", "directive", lambda ctx: GateOutcome(
            detail="d1", log_message="m1", directive="a",
        )),
        _gate("second", "directive", second),
    ]
    ctx = _make_ctx()

    run_gate_chain(gates, ctx)

    assert ctx.model.next_task_directive == "a"
    second.assert_not_called()
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "first"


def test_preset_directive_skips_all_directive_gates(
    mem_fs: dict[str, str],
) -> None:
    """A directive set before the chain runs is never overwritten."""
    bridge = MagicMock(
        return_value=GateOutcome(detail="d", log_message="m", directive="new"),
    )
    ctx = _make_ctx(model=SelfModel(next_task_directive="pre-set"))

    blocked = run_gate_chain([_gate("bridge", "directive", bridge)], ctx)

    assert blocked is False
    assert ctx.model.next_task_directive == "pre-set"
    bridge.assert_not_called()
    assert _gate_records(mem_fs) == []


def test_disabled_gate_skipped(mem_fs: dict[str, str]) -> None:
    run = MagicMock(
        return_value=GateOutcome(detail="d", log_message="m"),
    )
    gates = [_gate("off", "block", run, enabled=lambda config: False)]
    ctx = _make_ctx()

    blocked = run_gate_chain(gates, ctx)

    assert blocked is False
    run.assert_not_called()
    assert _gate_records(mem_fs) == []


def test_none_outcome_continues_to_next_gate(mem_fs: dict[str, str]) -> None:
    gates = [
        _gate("quiet_blocker", "block", lambda ctx: None),
        _gate("bridge", "directive", lambda ctx: GateOutcome(
            detail="d", log_message="m", directive="fired",
        )),
    ]
    ctx = _make_ctx()

    blocked = run_gate_chain(gates, ctx)

    assert blocked is False
    assert ctx.model.next_task_directive == "fired"
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "bridge"


def test_block_with_journal_records_event(mem_fs: dict[str, str]) -> None:
    agent = _StubAgent()
    model = SelfModel()
    ctx = _make_ctx(agent=agent, model=model)
    gates = [
        _gate("noisy_blocker", "block", lambda ctx: GateOutcome(
            detail="d", log_message="m",
            journal_content="blocked for a reason",
            journal_trigger="noisy_gate",
        )),
    ]

    blocked = run_gate_chain(gates, ctx)

    assert blocked is True
    agent.journal.record_event.assert_called_once_with(
        content="blocked for a reason",
        entry_type="reflection",
        triggered_by="noisy_gate",
    )


def test_directive_with_journal_records_event(mem_fs: dict[str, str]) -> None:
    agent = _StubAgent()
    model = SelfModel()
    ctx = _make_ctx(agent=agent, model=model)
    gates = [
        _gate("noisy_bridge", "directive", lambda ctx: GateOutcome(
            detail="d", log_message="m", directive="steer",
            journal_content="redirecting",
            journal_trigger="noisy_bridge_gate",
        )),
    ]

    run_gate_chain(gates, ctx)

    assert ctx.model.next_task_directive == "steer"
    agent.journal.record_event.assert_called_once_with(
        content="redirecting",
        entry_type="reflection",
        triggered_by="noisy_bridge_gate",
    )


def test_directive_without_journal_skips_thought(
    mem_fs: dict[str, str],
) -> None:
    agent = _StubAgent()
    ctx = _make_ctx(agent=agent)
    gates = [
        _gate("plain_bridge", "directive", lambda ctx: GateOutcome(
            detail="d", log_message="m", directive="steer",
        )),
    ]

    run_gate_chain(gates, ctx)

    agent.journal.record_event.assert_not_called()
    agent.journal.write_thought.assert_not_called()


# ---------------------------------------------------------------------------
# GateContext — lazy history loading
# ---------------------------------------------------------------------------


def test_context_recent_loads_once_and_caches() -> None:
    memory = _StubMemory(recent=[_task()])
    agent = _StubAgent(memory=memory)
    ctx = _make_ctx(agent=agent)

    first = ctx.recent()
    second = ctx.recent()

    assert first is second
    assert memory.recent_calls == [10]


def test_context_recent_dicts_converts_and_caches() -> None:
    memory = _StubMemory(recent=[_task(task_id="t-9", goal="g")])
    agent = _StubAgent(memory=memory)
    ctx = _make_ctx(agent=agent)

    first = ctx.recent_dicts()
    second = ctx.recent_dicts()

    assert first is second
    assert first[0]["task_id"] == "t-9"
    assert memory.recent_calls == [10]


def test_context_does_not_touch_memory_until_asked() -> None:
    memory = _StubMemory()
    agent = _StubAgent(memory=memory)
    _make_ctx(agent=agent)

    assert memory.recent_calls == []


# ---------------------------------------------------------------------------
# Blocking-gate run callables
# ---------------------------------------------------------------------------


def test_quiet_skip_fires_when_queue_has_items() -> None:
    agent = _StubAgent(queue=["item-a", "item-b"])
    ctx = _make_ctx(agent=agent)

    outcome = _quiet_skip_run(ctx)

    assert outcome is not None
    assert outcome.detail == "queue_size=2"
    assert outcome.directive is None
    assert "quiet-skip gate engaged" in outcome.log_message


def test_quiet_skip_silent_when_queue_empty() -> None:
    agent = _StubAgent(queue=[])
    assert _quiet_skip_run(_make_ctx(agent=agent)) is None


def test_quiet_skip_silent_when_user_request_pending() -> None:
    agent = _StubAgent(queue=["item"])
    model = SelfModel(pending_user_requests=["do this"])
    assert _quiet_skip_run(_make_ctx(agent=agent, model=model)) is None


def test_quiet_skip_silent_when_pr_request_pending() -> None:
    agent = _StubAgent(queue=["item"])
    model = SelfModel(
        pending_pr_requests=[PendingPRRequest(directive="Remediate PR #1")],
    )
    assert _quiet_skip_run(_make_ctx(agent=agent, model=model)) is None


def test_audit_stall_fires_at_threshold() -> None:
    config = _make_config(audit_stall_threshold=3)
    ctx = _make_ctx(config=config)
    with patch(
        "maestro.task_gates.audit_checks.compute_audit_stall_counts",
        return_value={"[audit] q1": 3, "[audit] q2": 1},
    ):
        outcome = _audit_stall_run(ctx)

    assert outcome is not None
    assert outcome.detail == "[audit] q1 fired 3 cycles"
    assert "audit-stall gate engaged" in outcome.log_message


def test_audit_stall_silent_below_threshold() -> None:
    config = _make_config(audit_stall_threshold=5)
    ctx = _make_ctx(config=config)
    with patch(
        "maestro.task_gates.audit_checks.compute_audit_stall_counts",
        return_value={"[audit] q1": 4},
    ):
        assert _audit_stall_run(ctx) is None


def test_audit_stall_silent_with_no_counts() -> None:
    ctx = _make_ctx()
    with patch(
        "maestro.task_gates.audit_checks.compute_audit_stall_counts",
        return_value={},
    ):
        assert _audit_stall_run(ctx) is None


def test_audit_stall_reads_lineage_lookback_window() -> None:
    """Resolution lineage is read with the agent's declared lookback."""
    memory = MagicMock()
    agent = _StubAgent()
    agent.memory = memory  # type: ignore[assignment]
    ctx = _make_ctx(agent=agent)
    with patch(
        "maestro.task_gates.audit_checks.compute_audit_stall_counts",
        return_value={},
    ):
        _audit_stall_run(ctx)

    memory.get_recent_question_resolutions.assert_called_once_with(50)


def test_audit_stall_degrades_on_lineage_read_error() -> None:
    """A genuine I/O error reading the lineage log softens to legacy matching.

    The read is best-effort: a JSONL/backend failure must not propagate out
    of the gate (and crash the loop). It degrades to ``resolutions=[]`` so
    ``compute_audit_stall_counts`` folds into legacy single-key matching.
    """
    memory = MagicMock()
    memory.get_recent_question_resolutions.side_effect = OSError("read failed")
    agent = _StubAgent()
    agent.memory = memory  # type: ignore[assignment]
    ctx = _make_ctx(agent=agent)
    with patch(
        "maestro.task_gates.audit_checks.compute_audit_stall_counts",
        return_value={},
    ) as stall_counts:
        # No exception propagates out of the gate.
        assert _audit_stall_run(ctx) is None

    memory.get_recent_question_resolutions.assert_called_once_with(50)
    # The fallback wires legacy matching via an empty resolutions list.
    assert stall_counts.call_args.kwargs["resolutions"] == []


def test_audit_stall_degrades_on_sqlite_lineage_read_error() -> None:
    """The SQLite ledger's failure family degrades exactly like the JSONL one.

    Under ``[paths] sqlite_state_enabled`` the lineage read goes through
    :class:`SqliteQuestionResolutionLedger`, which raises the ``sqlite3.Error``
    family rather than ``OSError``. Both are recoverable storage failures, so
    both soften to legacy single-key matching instead of crashing generation.
    """
    memory = MagicMock()
    memory.get_recent_question_resolutions.side_effect = sqlite3.OperationalError(
        "database is locked",
    )
    agent = _StubAgent()
    agent.memory = memory  # type: ignore[assignment]
    ctx = _make_ctx(agent=agent)
    with patch(
        "maestro.task_gates.audit_checks.compute_audit_stall_counts",
        return_value={},
    ) as stall_counts:
        assert _audit_stall_run(ctx) is None

    memory.get_recent_question_resolutions.assert_called_once_with(50)
    assert stall_counts.call_args.kwargs["resolutions"] == []


def test_audit_stall_propagates_non_storage_lineage_error() -> None:
    """A non-storage exception is a defect and propagates, not a silent degrade.

    Swallowing e.g. a ``TypeError`` from an adapter regression would compute
    stall counts against an empty lineage — zeroing a renamed question's
    accumulated count and skipping a gate that should have fired. Fail loud
    instead.
    """
    memory = MagicMock()
    memory.get_recent_question_resolutions.side_effect = TypeError("bad adapter")
    agent = _StubAgent()
    agent.memory = memory  # type: ignore[assignment]
    ctx = _make_ctx(agent=agent)
    with patch(
        "maestro.task_gates.audit_checks.compute_audit_stall_counts",
        return_value={},
    ) as stall_counts, pytest.raises(TypeError, match="bad adapter"):
        _audit_stall_run(ctx)

    # The gate never reached the counting step with a softened lineage.
    stall_counts.assert_not_called()


def test_cost_risk_fires_with_journal(mem_fs: dict[str, str]) -> None:
    ctx = _make_ctx()
    with patch(
        "maestro.task_gates.task_cost_gate.check_task_cost_risk",
        return_value="3 consecutive expensive failures",
    ):
        outcome = _cost_risk_run(ctx)

    assert outcome is not None
    assert outcome.detail == "3 consecutive expensive failures"
    assert outcome.journal_content == (
        "Cost gate blocked task generation: 3 consecutive expensive failures"
    )
    assert outcome.journal_trigger == "cost_gate"


def test_cost_risk_detail_truncated_to_200(mem_fs: dict[str, str]) -> None:
    ctx = _make_ctx()
    long_verdict = "x" * 300
    with patch(
        "maestro.task_gates.task_cost_gate.check_task_cost_risk",
        return_value=long_verdict,
    ):
        outcome = _cost_risk_run(ctx)

    assert outcome is not None
    assert outcome.detail == "x" * 200


def test_cost_risk_silent_without_verdict() -> None:
    ctx = _make_ctx()
    with patch(
        "maestro.task_gates.task_cost_gate.check_task_cost_risk",
        return_value=None,
    ):
        assert _cost_risk_run(ctx) is None


def test_cost_risk_passes_reference_time_clock(mem_fs: dict[str, str]) -> None:
    """_cost_risk_run hands check_task_cost_risk a tz-aware horizon clock (#2566)."""
    ctx = _make_ctx()
    with patch(
        "maestro.task_gates.task_cost_gate.check_task_cost_risk",
        return_value=None,
    ) as check:
        _cost_risk_run(ctx)

    assert check.call_count == 1
    ref = check.call_args.kwargs["reference_time"]
    assert isinstance(ref, datetime)
    assert ref.tzinfo is not None


def _cost_window(partial_source: TaskSource) -> list[TaskRecord]:
    """3 self-directed successes plus 7 ``partial_source`` partials, all fresh."""
    now = datetime.now(UTC).isoformat()
    ok = [
        replace(
            _task(task_id=f"s-{i}", goal="build a thing"),
            outcome="success", source="self_directed",
            duration_secs=300.0, timestamp=now,
        )
        for i in range(3)
    ]
    partials = [
        replace(
            _task(task_id=f"r-{i}", goal="Remediate PR #4500"),
            outcome="partial", source=partial_source,
            duration_secs=600.0, timestamp=now,
        )
        for i in range(7)
    ]
    return ok + partials


def test_cost_risk_ignores_external_remediation_partials(
    mem_fs: dict[str, str],
) -> None:
    """External remediation partials never feed the cost gate (#4573)."""
    ctx = _make_ctx(agent=_StubAgent(memory=_StubMemory(recent=_cost_window("external"))))
    assert _cost_risk_run(ctx) is None


def test_cost_risk_still_blocks_on_self_directed_partials(
    mem_fs: dict[str, str],
) -> None:
    """The source filter keeps rule 3 live for discretionary partials."""
    ctx = _make_ctx(
        agent=_StubAgent(memory=_StubMemory(recent=_cost_window("self_directed"))),
    )
    outcome = _cost_risk_run(ctx)
    assert outcome is not None
    assert outcome.detail.startswith("82% of recent task duration")


def test_cost_risk_gate_releases_after_repeated_identical_blocks(
    mem_fs: dict[str, str],
) -> None:
    """After N identical blocks the gate releases once and stamps `released`."""
    agent = _StubAgent()
    ctx = _make_ctx(agent=agent)
    verdict = "80% of recent task duration spent on non-success outcomes"
    detail = verdict[:200]
    for _ in range(_COST_RISK_RELEASE_STREAK):
        log_gate_activation(
            agent.gate_activations, gate="cost_risk",
            action="blocked", detail=detail,
        )
    with patch(
        "maestro.task_gates.task_cost_gate.check_task_cost_risk",
        return_value=verdict,
    ):
        outcome = _cost_risk_run(ctx)

    # The gate releases (returns None) rather than emitting another block.
    assert outcome is None
    records = _gate_records(mem_fs)
    released = [r for r in records if r["action"] == "released"]
    assert len(released) == 1
    assert released[0]["gate"] == "cost_risk"
    assert str(_COST_RISK_RELEASE_STREAK) in released[0]["detail"]


def test_cost_risk_below_streak_still_blocks(mem_fs: dict[str, str]) -> None:
    """Below the release streak the gate still blocks and writes no release row."""
    agent = _StubAgent()
    ctx = _make_ctx(agent=agent)
    verdict = "3 consecutive expensive failures"
    detail = verdict[:200]
    for _ in range(_COST_RISK_RELEASE_STREAK - 1):
        log_gate_activation(
            agent.gate_activations, gate="cost_risk",
            action="blocked", detail=detail,
        )
    with patch(
        "maestro.task_gates.task_cost_gate.check_task_cost_risk",
        return_value=verdict,
    ):
        outcome = _cost_risk_run(ctx)

    assert outcome is not None
    assert outcome.detail == detail
    records = _gate_records(mem_fs)
    assert [r for r in records if r["action"] == "released"] == []


def test_trailing_identical_blocks_counts_and_breaks(
    mem_fs: dict[str, str],
) -> None:
    """The counter tallies matching tail rows and stops at the first mismatch."""
    ledger = JsonlGateActivationLedger(_DIR)
    detail = "same detail"
    # An older non-matching row (different gate) must not be counted.
    log_gate_activation(ledger, gate="audit_stall", action="blocked", detail=detail)
    # Two matching cost_risk/blocked rows at the tail.
    log_gate_activation(ledger, gate="cost_risk", action="blocked", detail=detail)
    log_gate_activation(ledger, gate="cost_risk", action="blocked", detail=detail)

    assert _trailing_identical_blocks(ledger, "cost_risk", detail, _COST_RISK_RELEASE_STREAK) == 2
    # A differing detail matches nothing at the tail (detail-mismatch break).
    assert _trailing_identical_blocks(ledger, "cost_risk", "other", _COST_RISK_RELEASE_STREAK) == 0

    # A `released` row at the tail breaks the run (action-mismatch break).
    log_gate_activation(ledger, gate="cost_risk", action="released", detail=detail)
    assert _trailing_identical_blocks(ledger, "cost_risk", detail, _COST_RISK_RELEASE_STREAK) == 0


def test_cost_risk_release_ignores_interleaved_other_gate_rows(
    mem_fs: dict[str, str],
) -> None:
    """Other phases' rows between (and after) the blocks don't wedge the valve (#4071)."""
    agent = _StubAgent()
    ctx = _make_ctx(agent=agent)
    verdict = "80% of recent task duration spent on non-success outcomes"
    detail = verdict[:200]
    backend = agent.gate_activations
    for _ in range(_COST_RISK_RELEASE_STREAK):
        log_gate_activation(backend, gate="cost_risk", action="blocked", detail=detail)
        log_gate_activation(
            backend, gate="tend_cooldown_skip", action="skipped", detail="#1 cooldown",
        )
    log_gate_activation(backend, gate="ci_pending_hold", action="held", detail="#1")
    with patch(
        "maestro.task_gates.task_cost_gate.check_task_cost_risk",
        return_value=verdict,
    ):
        outcome = _cost_risk_run(ctx)

    assert outcome is None
    released = [r for r in _gate_records(mem_fs) if r["action"] == "released"]
    assert len(released) == 1
    assert released[0]["gate"] == "cost_risk"


@pytest.mark.parametrize("substrate", ["jsonl", "sqlite"])
def test_trailing_identical_blocks_skips_other_gates(
    mem_fs: dict[str, str], substrate: str,
) -> None:
    """Other gates' rows neither break nor extend the run; cost_risk rows still do."""
    ledger: JsonlGateActivationLedger | SqliteGateActivationLedger
    if substrate == "jsonl":
        ledger = JsonlGateActivationLedger(_DIR)
    else:
        ledger = SqliteGateActivationLedger(StateStore(":memory:"))
    detail = "same detail"
    log_gate_activation(ledger, gate="cost_risk", action="blocked", detail=detail)
    log_gate_activation(ledger, gate="tend_cooldown_skip", action="skipped", detail="x")
    log_gate_activation(ledger, gate="cost_risk", action="blocked", detail=detail)
    log_gate_activation(ledger, gate="tend_cooldown_skip", action="skipped", detail="x")
    assert _trailing_identical_blocks(ledger, "cost_risk", detail, _COST_RISK_RELEASE_STREAK) == 2

    # A differing-detail cost_risk row breaks the run despite later foreign rows.
    log_gate_activation(ledger, gate="cost_risk", action="blocked", detail="other")
    log_gate_activation(ledger, gate="ci_pending_hold", action="held", detail="x")
    assert _trailing_identical_blocks(ledger, "cost_risk", detail, _COST_RISK_RELEASE_STREAK) == 0

    # So does a released row.
    log_gate_activation(ledger, gate="cost_risk", action="blocked", detail=detail)
    log_gate_activation(ledger, gate="cost_risk", action="released", detail=detail)
    log_gate_activation(ledger, gate="tend_cooldown_skip", action="skipped", detail="x")
    assert _trailing_identical_blocks(ledger, "cost_risk", detail, _COST_RISK_RELEASE_STREAK) == 0


def test_escalation_bridge_fires_delivers_then_noop(
    mem_fs: dict[str, str],
) -> None:
    """First firing delivers one message; the second is a no-op."""
    ctx = _make_ctx()
    handoff = UnlandedCureHandoff(
        pr_number=2579, head_sha="abc123def456", stamp_count=4,
    )
    with patch(
        "maestro.task_gates.escalation_bridge.detect_unlanded_cure_handoff",
        return_value=handoff,
    ):
        first = _escalation_bridge_run(ctx)
        second = _escalation_bridge_run(ctx)

    assert first is not None
    assert "2579" in first.detail
    assert "escalation-bridge gate engaged" in first.log_message
    assert first.journal_trigger == "escalation_bridge_gate"
    # Exactly one merge-or-close message reached messages.jsonl.
    raw = mem_fs.get(str(_DIR / "messages.jsonl"), "")
    lines = [line for line in raw.splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["sender"] == "maestro"
    assert "#2579" in record["content"]
    # Delivery recorded → second call suppresses without re-delivering.
    assert second is None


def test_escalation_bridge_silent_when_no_handoff() -> None:
    ctx = _make_ctx()
    with patch(
        "maestro.task_gates.escalation_bridge.detect_unlanded_cure_handoff",
        return_value=None,
    ):
        assert _escalation_bridge_run(ctx) is None


def test_escalation_bridge_progress_aware_flag_off_passes_none() -> None:
    """Both flags explicitly off: the detector receives ``no_progress_laps=None``
    so the gross-count trigger is unchanged. The salvage master flag is pinned off
    too, since it feeds ``progress_aware_lap_budget_active`` and would otherwise
    activate the progress-aware path once its default flipped on (proposal 094)."""
    ctx = _make_ctx(
        config=_make_config(
            progress_aware_lap_budget_enabled=False,
            salvage_before_disengage_enabled=False,
        ),
    )
    with patch(
        "maestro.task_gates.escalation_bridge.detect_unlanded_cure_handoff",
        return_value=None,
    ) as detect, patch(
        "maestro.task_gates.measure_no_progress_laps",
    ) as measure:
        _escalation_bridge_run(ctx)

    measure.assert_not_called()
    assert detect.call_args.kwargs["no_progress_laps"] is None


def test_escalation_bridge_progress_aware_flag_on_passes_no_progress() -> None:
    """Flag on: the gate measures consecutive no-progress laps and threads the
    dict into the detector instead of the gross-count default."""
    ctx = _make_ctx(
        config=_make_config(progress_aware_lap_budget_enabled=True),
    )
    with patch(
        "maestro.task_gates.escalation_bridge.detect_unlanded_cure_handoff",
        return_value=None,
    ) as detect, patch(
        "maestro.task_gates.measure_no_progress_laps",
        return_value={42: 7},
    ) as measure:
        _escalation_bridge_run(ctx)

    measure.assert_called_once()
    assert detect.call_args.kwargs["no_progress_laps"] == {42: 7}


def test_escalation_bridge_salvage_master_flag_on_passes_no_progress() -> None:
    """Proposal 094: the salvage master flag activates the progress-aware gate
    via ``progress_aware_lap_budget_active`` even with the dedicated flag off, so
    the no-progress dict reaches the detector."""
    ctx = _make_ctx(
        config=_make_config(salvage_before_disengage_enabled=True),
    )
    with patch(
        "maestro.task_gates.escalation_bridge.detect_unlanded_cure_handoff",
        return_value=None,
    ) as detect, patch(
        "maestro.task_gates.measure_no_progress_laps",
        return_value={42: 7},
    ) as measure:
        _escalation_bridge_run(ctx)

    measure.assert_called_once()
    assert detect.call_args.kwargs["no_progress_laps"] == {42: 7}


def test_escalation_bridge_forwards_host_first_oracle() -> None:
    """The gate feeds the detector the agent's host-first merge-truth oracle
    (proposal 080 Phase 1), so the bridge stops trusting the git-log substring
    oracle that name-drop-blinded it for #2579."""
    agent = _StubAgent()
    ctx = _make_ctx(agent=agent)
    with patch(
        "maestro.task_gates.escalation_bridge.detect_unlanded_cure_handoff",
        return_value=None,
    ) as detect:
        _escalation_bridge_run(ctx)

    detect.assert_called_once()
    assert detect.call_args.kwargs["is_pr_merged"] is agent._host_or_git_pr_merged


def test_escalation_bridge_forwards_settled_oracle() -> None:
    """The gate feeds the detector a settled predicate reading through the
    agent's ``is_pr_settled`` merge-truth chokepoint (#2967).

    Detection becomes symmetric with ``prune_resolved_standdowns``' release: a PR
    Thomas closes is skipped, not re-handed-off. The predicate returns the
    ``.settled`` fork of the per-iteration-memoized ``MergeTruth`` — never a
    bespoke closed-state check.
    """
    agent = _agent_with_settled(True)
    ctx = _make_ctx(agent=agent)
    with patch(
        "maestro.task_gates.escalation_bridge.detect_unlanded_cure_handoff",
        return_value=None,
    ) as detect:
        _escalation_bridge_run(ctx)

    detect.assert_called_once()
    is_pr_settled = detect.call_args.kwargs["is_pr_settled"]
    assert is_pr_settled(2967) is True
    agent.is_pr_settled.assert_called_once_with(2967)


def test_escalation_bridge_blocks_chain(mem_fs: dict[str, str]) -> None:
    """The registered gate short-circuits generation when it delivers."""
    ctx = _make_ctx()
    handoff = UnlandedCureHandoff(
        pr_number=7, head_sha="deadbeefcafe", stamp_count=5,
    )
    gate = next(g for g in GATE_CHAIN if g.name == "escalation_bridge")
    with patch(
        "maestro.task_gates.escalation_bridge.detect_unlanded_cure_handoff",
        return_value=handoff,
    ):
        blocked = run_gate_chain([gate], ctx)

    assert blocked is True
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "escalation_bridge"
    assert records[0]["action"] == "blocked"


# ---------------------------------------------------------------------------
# no_repeat_failed_branch_target gate
# ---------------------------------------------------------------------------

_FAILED_BRANCH = "maestro/task/16db56580b86"
_FAILED_TASK_ID = "16db56580b86"


def _hist(
    task_id: str,
    outcome: str,
    branch: str | None = None,
    goal: str = "a goal",
) -> TaskRecord:
    """Build a task-history record with a chosen outcome/branch."""
    return TaskRecord(
        task_id=task_id,
        goal=goal,
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary="ok",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00Z",
        branch=branch,
    )


def _no_repeat_gate() -> TaskGate:
    return next(
        g for g in GATE_CHAIN if g.name == "no_repeat_failed_branch_target"
    )


def _seed_failed_branch(records: list[TaskRecord]) -> _StubMemory:
    """Seed both stores the ``no_repeat_failed_branch_target`` gate reads.

    The failed-target scan now walks the shared ``ctx.recent()`` window (the
    agent's memory), while ``_source_task_id_eligible`` still does a separate
    recent-30 read against ``config.memory_dir`` (``_DIR``). Writing the same
    *records* into both keeps a test's history consistent across the two reads;
    returns the stub memory to hand to ``_StubAgent(memory=...)``.
    """
    mem = Memory(_DIR)
    for record in records:
        mem.record_task(record)
    return _StubMemory(recent=records)


def test_no_repeat_gate_blocks_and_redirects(mem_fs: dict[str, str]) -> None:
    """Criteria 1 + 4: a candidate re-attacking a failed branch is blocked, a
    redirect lands in the empty slot, one activation row. A ``failure``-outcome
    branch is *not* ``source_task_id``-eligible, so the redirect steers to a
    fresh-from-HEAD evolution PR (not an id validation would reject)."""
    memory = _seed_failed_branch(
        [_hist(_FAILED_TASK_ID, "failure", branch=_FAILED_BRANCH)],
    )
    agent = _StubAgent(
        memory=memory,
        queue=[QueuedTask(goal=f"Finish {_FAILED_BRANCH} and open a real PR")],
    )
    ctx = _make_ctx(agent=agent, model=SelfModel())

    blocked = run_gate_chain([_no_repeat_gate()], ctx)

    assert blocked is True
    assert ctx.model.next_task_directive == (
        f"Branch {_FAILED_TASK_ID} already failed a recent task attempt; it "
        f"reaches main only via a fresh-from-HEAD evolution PR — propose a plan "
        f"for this work (no source_task_id; the branch is not reusable)"
    )
    assert "source_task_id=" not in ctx.model.next_task_directive
    agent.consciousness._mutate_self_model_persisted.assert_called_once()
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "no_repeat_failed_branch_target"
    assert records[0]["action"] == "blocked"


def test_no_repeat_gate_matches_token_in_rationale(
    mem_fs: dict[str, str],
) -> None:
    """The candidate's rationale text is scanned too, not only its goal. A
    ``partial``-outcome branch *is* ``source_task_id``-eligible, so the
    redirect names it (validation accepts ``partial``)."""
    memory = _seed_failed_branch(
        [_hist(_FAILED_TASK_ID, "partial", branch=_FAILED_BRANCH)],
    )
    agent = _StubAgent(
        memory=memory,
        queue=[
            QueuedTask(
                goal="Promote the stranded branch",
                rationale=f"re-attack {_FAILED_BRANCH}",
            ),
        ],
    )
    ctx = _make_ctx(agent=agent, model=SelfModel())

    blocked = run_gate_chain([_no_repeat_gate()], ctx)

    assert blocked is True
    assert f"source_task_id={_FAILED_TASK_ID}" in ctx.model.next_task_directive


def test_no_repeat_gate_success_only_passes(mem_fs: dict[str, str]) -> None:
    """Criterion 2: a token whose only recent outcomes are success is not
    blocked and the directive slot is untouched."""
    # The referenced branch belongs only to a *successful* entry; a separate
    # failed entry names a different branch, so the failed set excludes it.
    memory = _seed_failed_branch([
        _hist("ok", "success", branch=_FAILED_BRANCH),
        _hist("bad", "failure", branch="maestro/task/deadbeef99aa"),
    ])
    agent = _StubAgent(
        memory=memory,
        queue=[QueuedTask(goal=f"Finish {_FAILED_BRANCH}")],
    )
    ctx = _make_ctx(agent=agent, model=SelfModel())

    blocked = run_gate_chain([_no_repeat_gate()], ctx)

    assert blocked is False
    assert ctx.model.next_task_directive is None
    assert _gate_records(mem_fs) == []


def test_no_repeat_gate_no_branch_token_passes(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 3: a candidate with no branch token passes unchanged."""
    memory = _seed_failed_branch(
        [_hist("orig", "failure", branch=_FAILED_BRANCH)],
    )
    agent = _StubAgent(
        memory=memory,
        queue=[QueuedTask(goal="Add a read-only CLI subcommand")],
    )
    ctx = _make_ctx(agent=agent, model=SelfModel())

    blocked = run_gate_chain([_no_repeat_gate()], ctx)

    assert blocked is False
    assert ctx.model.next_task_directive is None
    assert _gate_records(mem_fs) == []


def test_no_repeat_gate_no_failed_targets_is_noop(
    mem_fs: dict[str, str],
) -> None:
    """No non-success history → the detector is empty and the gate no-ops
    without even reading the queue (self-extinguishing)."""
    memory = _seed_failed_branch(
        [_hist("ok", "success", branch=_FAILED_BRANCH)],
    )
    agent = _StubAgent(
        memory=memory,
        queue=[QueuedTask(goal=f"Finish {_FAILED_BRANCH}")],
    )
    ctx = _make_ctx(agent=agent)

    assert _no_repeat_failed_branch_target_run(ctx) is None
    agent.task_queue.load.assert_not_called()


def test_no_repeat_gate_reuses_shared_recent_window(
    mem_fs: dict[str, str],
) -> None:
    """The gate scans the shared ``ctx.recent()`` window instead of a private
    ``Memory`` read, so the 10-task tail is read once and a downstream
    ``ctx.recent()`` consumer reuses the cache (single-read I/O profile — no
    second history read on the non-blocked path)."""
    memory = _seed_failed_branch(
        [_hist("orig", "failure", branch=_FAILED_BRANCH)],
    )
    agent = _StubAgent(
        memory=memory,
        # No branch token → the gate scans the window but does not block, so the
        # non-blocked path is exercised.
        queue=[QueuedTask(goal="Add a read-only CLI subcommand")],
    )
    ctx = _make_ctx(agent=agent)

    blocked = run_gate_chain([_no_repeat_gate()], ctx)

    assert blocked is False
    # The gate read the tail through the shared cache exactly once (window 10)…
    assert memory.recent_calls == [10]
    # …and a downstream ``ctx.recent()`` consumer reuses it — no second read.
    ctx.recent()
    assert memory.recent_calls == [10]


def test_failed_branch_targets_reads_only_tail_zero_sdk(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 5: the detector reads only the recent task-history tail and
    touches no SDK surface (it takes a data_dir, never an agent/session)."""
    mem = Memory(_DIR)
    # An old failed entry beyond the window must NOT contribute its token.
    mem.record_task(_hist("old", "failure", branch="maestro/task/aaaaaa000000"))
    for i in range(_FAILED_BRANCH_WINDOW):
        mem.record_task(_hist(f"pad{i}", "success", branch=f"maestro/task/pad{i}0000"))
    mem.record_task(_hist("recent", "failure", branch=_FAILED_BRANCH))

    targets = _failed_branch_targets(_DIR, window=_FAILED_BRANCH_WINDOW)

    assert _FAILED_BRANCH in targets
    assert "maestro/task/aaaaaa000000" not in targets


def test_failed_branch_targets_extracts_token_from_narrative(
    mem_fs: dict[str, str],
) -> None:
    """A failed re-attempt whose own branch differs still contributes the
    original target token it named in its goal."""
    mem = Memory(_DIR)
    mem.record_task(
        _hist(
            "reattempt",
            "failure",
            branch="maestro/task/ffffff111111",
            goal=f"wire {_FAILED_BRANCH} to main",
        ),
    )

    targets = _failed_branch_targets(_DIR)

    assert _FAILED_BRANCH in targets
    assert "maestro/task/ffffff111111" in targets


def test_failed_branch_targets_window_guard() -> None:
    """A non-positive window short-circuits to an empty set with no read."""
    assert _failed_branch_targets(_DIR, window=0) == set()


def test_referenced_branch_tokens_extracts_and_tolerates_empty() -> None:
    """The structured extractor finds every token and tolerates empty text."""
    text = f"finish {_FAILED_BRANCH} then maestro/task/abcdef123456"
    assert _referenced_branch_tokens(text) == {
        _FAILED_BRANCH,
        "maestro/task/abcdef123456",
    }
    assert _referenced_branch_tokens("") == set()


def test_failed_branch_redirect_directive_is_bounded() -> None:
    """A pathologically long task id truncates to the shared directive cap,
    in both the eligible and non-eligible wordings."""
    short = _failed_branch_redirect_directive(
        _FAILED_TASK_ID, source_eligible=True,
    )
    assert f"source_task_id={_FAILED_TASK_ID}" in short
    assert len(short) <= _DIRECTIVE_MAX_CHARS

    long_id = "a" * 300
    bounded = _failed_branch_redirect_directive(long_id, source_eligible=True)
    assert len(bounded) == _DIRECTIVE_MAX_CHARS
    assert bounded.endswith("…")

    long_fresh = _failed_branch_redirect_directive(
        long_id, source_eligible=False,
    )
    assert len(long_fresh) == _DIRECTIVE_MAX_CHARS
    assert long_fresh.endswith("…")


def test_failed_branch_redirect_directive_failure_omits_source_task_id() -> None:
    """A non-eligible (``failure``-outcome) branch must not recommend a
    ``source_task_id`` — validation would drop it and basing on a broken branch
    is undesirable — steering to a fresh-from-HEAD evolution PR instead."""
    directive = _failed_branch_redirect_directive(
        _FAILED_TASK_ID, source_eligible=False,
    )
    assert "source_task_id=" not in directive
    assert "fresh-from-HEAD" in directive
    assert _FAILED_TASK_ID in directive


def test_source_task_id_eligible_matches_validation(
    mem_fs: dict[str, str],
) -> None:
    """``_source_task_id_eligible`` mirrors ``_validate_source_task_id``: a
    ``success`` *or* ``partial`` outcome with a non-empty branch is eligible —
    the exact set the validator accepts; ``failure``, branchless, and unknown
    ids are not."""
    mem = Memory(_DIR)
    mem.record_task(_hist("success_id", "success", branch=_FAILED_BRANCH))
    mem.record_task(_hist("partial_id", "partial", branch=_FAILED_BRANCH))
    mem.record_task(_hist("failure_id", "failure", branch=_FAILED_BRANCH))
    mem.record_task(_hist("branchless_id", "partial", branch=None))
    mem.record_task(_hist("branchless_success", "success", branch=None))

    assert _source_task_id_eligible(_DIR, "success_id") is True
    assert _source_task_id_eligible(_DIR, "partial_id") is True
    assert _source_task_id_eligible(_DIR, "failure_id") is False
    assert _source_task_id_eligible(_DIR, "branchless_id") is False
    assert _source_task_id_eligible(_DIR, "branchless_success") is False
    assert _source_task_id_eligible(_DIR, "no_such_id") is False
    assert _source_task_id_eligible(_DIR, "") is False


def test_source_task_id_eligible_latest_retry_wins(
    mem_fs: dict[str, str],
) -> None:
    """When an id has multiple records, the newest (latest retry) decides
    eligibility — matching validation's newest-first resolution."""
    mem = Memory(_DIR)
    mem.record_task(_hist("retry_id", "partial", branch=_FAILED_BRANCH))
    mem.record_task(_hist("retry_id", "failure", branch=_FAILED_BRANCH))

    assert _source_task_id_eligible(_DIR, "retry_id") is False


def _real_validate_source_task_id() -> Any:
    """Build a real ``EvolutionEngine`` over ``_DIR`` and return its bound
    ``_validate_source_task_id`` — the *actual* validator the plan proposer runs.

    Local imports (the pattern used across the suite) keep the heavy engine
    collaborators out of this module's import surface. The engine reads task
    history from the same ``_DIR`` files (backed by ``mem_fs``) the gate reads,
    so the coupling test exercises one shared source of truth end to end.
    """
    from maestro.consciousness import Consciousness
    from maestro.evolution import EvolutionEngine
    from maestro.issues import IssueTracker
    from maestro.journal import Journal
    from maestro.notifications.base import NullNotifier

    mem = Memory(_DIR)
    con = Consciousness(_DIR)
    con.load_self_model()
    jour = Journal(_DIR)
    engine = EvolutionEngine(
        mem, con, jour, NullNotifier(),
        issues=IssueTracker(github_repo=None), config=_make_config(),
    )
    return engine._validate_source_task_id


def _directive_source_task_id(directive: str) -> str | None:
    """Extract the ``source_task_id=<id>`` the directive recommends, or ``None``.

    Matches only the assignment form (``source_task_id=<id>``); the
    fresh-from-HEAD wording mentions ``source_task_id`` without an ``=`` and so
    yields ``None`` — the gate recommended no id.
    """
    match = re.search(r"source_task_id=(\S+)", directive)
    return match.group(1) if match else None


def _assert_gate_directive_coupled_to_validator(
    directive: str, branch_task_id: str,
) -> None:
    """The contradiction-closing invariant: the gate's redirect must never name
    a ``source_task_id`` the *real* validator rejects.

    If the directive names one, the real ``_validate_source_task_id`` must
    accept it verbatim; if it names none (fresh-from-HEAD), the validator must
    indeed reject the branch id — i.e. the gate correctly declined to name an
    unusable id. This is exactly the gate↔validator contradiction the review
    flagged, encoded as a guard.
    """
    validate = _real_validate_source_task_id()
    recommended = _directive_source_task_id(directive)
    if recommended is not None:
        assert validate(recommended) == recommended
    else:
        assert validate(branch_task_id) is None


def test_gate_directive_coupled_to_validator_failure_branch(
    mem_fs: dict[str, str],
) -> None:
    """Confirmed-defect reproduction. For a ``failure``-outcome branch — the
    gate's primary target — the emitted redirect must not name a
    ``source_task_id`` the real validator rejects. RED against the pre-fix
    directive (which always emitted ``source_task_id={hash}`` the validator
    drops); GREEN now that the gate steers a failed branch to fresh-from-HEAD."""
    memory = _seed_failed_branch(
        [_hist(_FAILED_TASK_ID, "failure", branch=_FAILED_BRANCH)],
    )
    agent = _StubAgent(
        memory=memory,
        queue=[QueuedTask(goal=f"Finish {_FAILED_BRANCH} and open a real PR")],
    )
    ctx = _make_ctx(agent=agent, model=SelfModel())

    assert run_gate_chain([_no_repeat_gate()], ctx) is True
    directive = ctx.model.next_task_directive
    assert directive is not None
    # The gate declines to name the unusable id (fresh-from-HEAD path)…
    assert _directive_source_task_id(directive) is None
    # …and the real validator confirms that id would indeed be rejected.
    _assert_gate_directive_coupled_to_validator(directive, _FAILED_TASK_ID)


def test_gate_directive_coupled_to_validator_partial_branch(
    mem_fs: dict[str, str],
) -> None:
    """The eligible arm: a ``partial``-outcome branch yields a
    ``source_task_id`` the real validator accepts, so the coupling holds on the
    recommend path too (not only the fresh-from-HEAD path)."""
    memory = _seed_failed_branch(
        [_hist(_FAILED_TASK_ID, "partial", branch=_FAILED_BRANCH)],
    )
    agent = _StubAgent(
        memory=memory,
        queue=[QueuedTask(goal=f"Finish {_FAILED_BRANCH}")],
    )
    ctx = _make_ctx(agent=agent, model=SelfModel())

    assert run_gate_chain([_no_repeat_gate()], ctx) is True
    directive = ctx.model.next_task_directive
    assert directive is not None
    assert _directive_source_task_id(directive) == _FAILED_TASK_ID
    _assert_gate_directive_coupled_to_validator(directive, _FAILED_TASK_ID)


def test_gate_directive_coupled_to_validator_success_branch(
    mem_fs: dict[str, str],
) -> None:
    """The previously-untested arm the review flagged: a *kept success* branch
    that a later re-attempt named. A ``failure`` re-attempt whose narrative
    references the success branch's token routes that token into the failed set,
    so the gate fires on a candidate re-attacking it; because the real validator
    accepts a ``success`` id, the redirect must *recommend* ``source_task_id``
    (not steer fresh-from-HEAD). RED before the mirror widened to ``success``
    (the directive named no id yet the validator would accept it, breaking the
    coupling); GREEN now that ``_source_task_id_eligible`` accepts ``success``."""
    memory = _seed_failed_branch([
        # The kept, successful base branch.
        _hist(_FAILED_TASK_ID, "success", branch=_FAILED_BRANCH),
        # A separate failed re-attempt (own branch differs) whose narrative names
        # the success branch's token — that is how the success token reaches the
        # failed-target set.
        _hist(
            "reattempt_fail",
            "failure",
            branch="maestro/task/deadbeef99aa",
            goal=f"re-attack {_FAILED_BRANCH} and open a real PR",
        ),
    ])
    agent = _StubAgent(
        memory=memory,
        queue=[QueuedTask(goal=f"Finish {_FAILED_BRANCH}")],
    )
    ctx = _make_ctx(agent=agent, model=SelfModel())

    assert run_gate_chain([_no_repeat_gate()], ctx) is True
    directive = ctx.model.next_task_directive
    assert directive is not None
    # The gate recommends the success branch as base (the validator accepts it)…
    assert _directive_source_task_id(directive) == _FAILED_TASK_ID
    # …and the coupling invariant holds on this success arm.
    _assert_gate_directive_coupled_to_validator(directive, _FAILED_TASK_ID)


# ---------------------------------------------------------------------------
# Directive-bridge run callables
# ---------------------------------------------------------------------------


def test_instrument_consumption_fires_at_threshold() -> None:
    unconsumed = [
        {"task_id": f"inst-{i}", "goal": f"ship instrument {i}"}
        for i in range(3)
    ]
    ctx = _make_ctx()
    with patch(
        "maestro.task_gates.find_unconsumed_instruments",
        return_value=unconsumed,
    ):
        outcome = _instrument_consumption_run(ctx)

    assert outcome is not None
    assert outcome.directive == (
        "Review output of task inst-0: ship instrument 0"
    )
    assert outcome.detail == "3 unconsumed; reviewing inst-0"
    assert outcome.journal_trigger == "instrument_consumption_gate"
    assert outcome.journal_content == (
        "Instrument-consumption redirect: 3 unconsumed instruments "
        "detected; directing review of inst-0"
    )


def test_instrument_consumption_truncates_long_directive() -> None:
    long_id = "i" * 150
    unconsumed = [
        {"task_id": long_id, "goal": "g" * 120},
        {"task_id": "b", "goal": "g"},
        {"task_id": "c", "goal": "g"},
    ]
    ctx = _make_ctx()
    with patch(
        "maestro.task_gates.find_unconsumed_instruments",
        return_value=unconsumed,
    ):
        outcome = _instrument_consumption_run(ctx)

    assert outcome is not None
    assert outcome.directive is not None
    assert len(outcome.directive) == _DIRECTIVE_MAX_CHARS
    assert outcome.directive.endswith("…")


def test_instrument_consumption_cap_tracks_shared_constant() -> None:
    """The bridge truncates at the shared constant, not a bare literal.

    Both directive bridges in this module used to clip at an inline ``200``.
    Raising ``_DIRECTIVE_MAX_CHARS`` would then have left them silently
    dropping the tail of every directive they set, while
    ``consciousness``/``audit_checks`` emitted the new full length. Patching
    the constant down proves the bridge now moves with it.

    The 150-char ``task_id`` is synthetic on purpose: the rendered directive is
    ``24 + len(task_id) + len(goal[:100])`` chars and real task ids are
    ``uuid4().hex[:12]``, so this bridge tops out near 136 and its truncation is
    defensive-only. ``_comfort_cluster_run`` is the production-reachable site
    the shared cap actually protects (``tests/test_comfort_cluster_gate.py``).
    """
    unconsumed = [
        {"task_id": "i" * 150, "goal": "g" * 120},
        {"task_id": "b", "goal": "g"},
        {"task_id": "c", "goal": "g"},
    ]
    ctx = _make_ctx()
    with (
        patch.object(audit_checks, "_DIRECTIVE_MAX_CHARS", 150),
        patch(
            "maestro.task_gates.find_unconsumed_instruments",
            return_value=unconsumed,
        ),
    ):
        outcome = _instrument_consumption_run(ctx)

    assert outcome is not None
    assert outcome.directive is not None
    assert len(outcome.directive) == 150
    assert outcome.directive.endswith("…")


def test_instrument_consumption_silent_below_threshold() -> None:
    ctx = _make_ctx()
    with patch(
        "maestro.task_gates.find_unconsumed_instruments",
        return_value=[{"task_id": "a", "goal": "g"}],
    ):
        assert _instrument_consumption_run(ctx) is None


def test_integration_cadence_fires() -> None:
    config = _make_config(integration_cadence=7)
    ctx = _make_ctx(config=config)
    with patch(
        "maestro.task_gates.check_integration_cadence",
        return_value="Promote a branch",
    ) as check:
        outcome = _integration_cadence_run(ctx)

    assert outcome is not None
    assert outcome.directive == "Promote a branch"
    assert outcome.detail == "Promote a branch"
    check.assert_called_once_with(ctx.recent_dicts(), cadence=7)


def test_integration_cadence_silent() -> None:
    ctx = _make_ctx()
    with patch(
        "maestro.task_gates.check_integration_cadence", return_value=None,
    ):
        assert _integration_cadence_run(ctx) is None


def test_investigation_streak_fires() -> None:
    config = _make_config(investigation_streak_threshold=4)
    ctx = _make_ctx(config=config)
    with patch(
        "maestro.task_gates.check_investigation_streak",
        return_value="Build something",
    ) as check:
        outcome = _investigation_streak_run(ctx)

    assert outcome is not None
    assert outcome.directive == "Build something"
    check.assert_called_once_with(ctx.recent(), threshold=4)


def test_investigation_streak_silent() -> None:
    ctx = _make_ctx()
    with patch(
        "maestro.task_gates.check_investigation_streak", return_value=None,
    ):
        assert _investigation_streak_run(ctx) is None


def test_branchless_streak_fires_at_threshold() -> None:
    config = _make_config(branchless_streak_threshold=3)
    ctx = _make_ctx(config=config)
    with patch(
        "maestro.task_gates.audit_checks.count_consecutive_branchless_tasks",
        return_value=3,
    ) as count:
        outcome = _branchless_streak_run(ctx)

    assert outcome is not None
    assert "target specific code files" in outcome.directive
    assert "No analysis-only tasks" in outcome.directive
    count.assert_called_once_with(ctx.recent_dicts())


def test_branchless_streak_silent_below_threshold() -> None:
    config = _make_config(branchless_streak_threshold=3)
    ctx = _make_ctx(config=config)
    with patch(
        "maestro.task_gates.audit_checks.count_consecutive_branchless_tasks",
        return_value=2,
    ):
        assert _branchless_streak_run(ctx) is None


def test_investigation_rate_fires_above_threshold() -> None:
    config = _make_config(
        investigation_rate_threshold=0.6, investigation_rate_window=10,
    )
    ctx = _make_ctx(config=config)
    with patch(
        "maestro.task_gates.compute_shape_investigation_rate",
        return_value=0.8,
    ):
        outcome = _investigation_rate_run(ctx)

    assert outcome is not None
    assert outcome.directive is not None
    assert outcome.directive.startswith("80% of the last 10 tasks")
    assert "construction-shaped" in outcome.directive


def test_investigation_rate_silent_at_threshold() -> None:
    config = _make_config(investigation_rate_threshold=0.6)
    ctx = _make_ctx(config=config)
    with patch(
        "maestro.task_gates.compute_shape_investigation_rate",
        return_value=0.6,
    ):
        assert _investigation_rate_run(ctx) is None


def test_partial_construction_fires() -> None:
    ctx = _make_ctx()
    with patch(
        "maestro.task_gates.check_partial_construction_gap",
        return_value="Finish what t-3 started",
    ):
        outcome = _partial_construction_run(ctx)

    assert outcome is not None
    assert outcome.directive == "Finish what t-3 started"


def test_partial_construction_silent() -> None:
    ctx = _make_ctx()
    with patch(
        "maestro.task_gates.check_partial_construction_gap",
        return_value=None,
    ):
        assert _partial_construction_run(ctx) is None


def test_promotable_backlog_fires() -> None:
    config = _make_config(promotable_backlog_threshold=4)
    ctx = _make_ctx(config=config)
    with patch(
        "maestro.task_gates.check_promotable_backlog_directive",
        return_value="Promote branch maestro/task/t-1",
    ) as check:
        outcome = _promotable_backlog_run(ctx)

    assert outcome is not None
    assert outcome.directive == "Promote branch maestro/task/t-1"
    check.assert_called_once_with(ctx.recent_dicts(), 4)


def test_promotable_backlog_silent() -> None:
    ctx = _make_ctx()
    with patch(
        "maestro.task_gates.check_promotable_backlog_directive",
        return_value=None,
    ):
        assert _promotable_backlog_run(ctx) is None


def test_integration_cadence_silent_when_promotion_pressure_disabled() -> None:
    """Proposal 116, step 6: with the master switch off the integration-cadence
    bridge returns None without even consulting ``check_integration_cadence``."""
    config = _make_config(branch_promotion_pressure_enabled=False)
    ctx = _make_ctx(config=config)
    with patch(
        "maestro.task_gates.check_integration_cadence",
        return_value="Promote a branch",
    ) as check:
        assert _integration_cadence_run(ctx) is None
    check.assert_not_called()


def test_promotable_backlog_silent_when_promotion_pressure_disabled() -> None:
    """Proposal 116, step 6: with the master switch off the promotable-backlog
    bridge returns None without consulting ``check_promotable_backlog_directive``."""
    config = _make_config(branch_promotion_pressure_enabled=False)
    ctx = _make_ctx(config=config)
    with patch(
        "maestro.task_gates.check_promotable_backlog_directive",
        return_value="Promote branch maestro/task/t-1",
    ) as check:
        assert _promotable_backlog_run(ctx) is None
    check.assert_not_called()


def test_self_directed_success_resolves_via_task_gates_binding() -> None:
    """The predicate is patched on ``maestro.task_gates``, the module that reads it."""
    window_tasks = [_task(task_id=f"w-{i}") for i in range(3)]
    memory = _StubMemory(recent=window_tasks)
    agent = _StubAgent(memory=memory)
    ctx = _make_ctx(agent=agent)
    with patch(
        "maestro.task_gates.check_self_directed_success_rate",
        return_value="Bounded construction next",
    ) as check:
        outcome = _self_directed_success_run(ctx)

    assert outcome is not None
    assert outcome.directive == "Bounded construction next"
    assert memory.recent_calls == [SELF_DIRECTED_WINDOW]
    check.assert_called_once_with(window_tasks)


def test_self_directed_success_silent() -> None:
    ctx = _make_ctx()
    with patch(
        "maestro.task_gates.check_self_directed_success_rate", return_value=None,
    ):
        assert _self_directed_success_run(ctx) is None


def test_forced_read_resolves_via_task_gates_binding() -> None:
    """The predicate is patched on ``maestro.task_gates``, the module that reads it."""
    model = SelfModel()
    ctx = _make_ctx(model=model)
    with patch(
        "maestro.task_gates.check_forced_read_directive",
        return_value="Read PR #7's real blocker",
    ) as check:
        outcome = _forced_read_run(ctx)

    assert outcome is not None
    assert outcome.directive == "Read PR #7's real blocker"
    check.assert_called_once()
    assert check.call_args.args == (ctx.recent_dicts(), model)
    assert "is_pr_merged" in check.call_args.kwargs


def test_forced_read_settled_closed_pr_is_silent() -> None:
    """A closed-unmerged PR reads settled via the chokepoint — no nag (#4311)."""
    goal = "Remediate PR #4311: Fix thing"
    memory = _StubMemory(
        recent=[_task("t-1", goal=goal), _task("t-2", goal=goal)],
    )
    agent = _StubAgent(memory=memory)
    agent.is_pr_settled = MagicMock(  # type: ignore[attr-defined]
        return_value=MagicMock(settled=True, merged=False),
    )
    ctx = _make_ctx(agent=agent)
    with patch(
        "maestro.audit_checks_phantom._git_pr_merged_default", return_value=False,
    ) as git_oracle:
        assert _forced_read_run(ctx) is None
    agent.is_pr_settled.assert_called_once_with(4311)
    git_oracle.assert_not_called()


def test_forced_read_unsettled_pr_fires() -> None:
    """An open PR serviced twice still gets the forced-read directive."""
    goal = "Remediate PR #4311: Fix thing"
    memory = _StubMemory(
        recent=[_task("t-1", goal=goal), _task("t-2", goal=goal)],
    )
    agent = _StubAgent(memory=memory)
    agent.is_pr_settled = MagicMock(  # type: ignore[attr-defined]
        return_value=MagicMock(settled=False),
    )
    outcome = _forced_read_run(_make_ctx(agent=agent))
    assert outcome is not None
    assert "#4311" in outcome.directive


def test_forced_read_silent() -> None:
    ctx = _make_ctx()
    with patch(
        "maestro.task_gates.check_forced_read_directive", return_value=None,
    ):
        assert _forced_read_run(ctx) is None


def test_readmission_construction_resolves_via_task_gates_binding() -> None:
    """The predicate is patched on ``maestro.task_gates``, the module that reads it."""
    agent = _StubAgent()
    agent.config = _make_config()  # type: ignore[attr-defined]
    ctx = _make_ctx(agent=agent)
    with patch(
        "maestro.task_gates.check_readmission_construction_directive",
        return_value="BUILD something new and testable that lands",
    ) as check:
        outcome = _readmission_construction_run(ctx)

    assert outcome is not None
    assert outcome.directive == "BUILD something new and testable that lands"
    check.assert_called_once_with(
        ctx.recent_dicts(),
        ratio_threshold=agent.config.readmission_construction_ratio_threshold,
    )


def test_readmission_construction_silent() -> None:
    # The predicate returns None (below threshold / no readmission), so the
    # gate sets no directive — exercises the ``if not directive: return None``
    # branch.
    agent = _StubAgent()
    agent.config = _make_config()  # type: ignore[attr-defined]
    ctx = _make_ctx(agent=agent)
    with patch(
        "maestro.task_gates.check_readmission_construction_directive",
        return_value=None,
    ):
        assert _readmission_construction_run(ctx) is None


# ---------------------------------------------------------------------------
# unloseable-streak gate (unloseable-streak directive gate proposal)
# ---------------------------------------------------------------------------


def _unloseable_task(task_id: str) -> TaskRecord:
    """A read/analysis-shaped task — classify_task_loseable -> False."""
    return _task(task_id=task_id, goal="analyze the recent journal entries")


def _loseable_task(task_id: str) -> TaskRecord:
    """An implementation-shaped task — classify_task_loseable -> True."""
    return _task(task_id=task_id, goal="implement a new helper in agent.py")


def test_classify_task_loseable_branches() -> None:
    """Implementation/bugfix shapes are loseable; read/analysis are not."""
    assert classify_task_loseable({"goal": "implement a new gate"}) is True
    assert classify_task_loseable({"goal": "fix PR #12"}) is True
    assert classify_task_loseable({"goal": "create a helper function"}) is True
    assert classify_task_loseable({"goal": "analyze the loop module"}) is False
    assert classify_task_loseable({"goal": "measure success rates"}) is False
    assert classify_task_loseable({"goal": ""}) is False


def test_unloseable_streak_fires_with_exit_code_language() -> None:
    """Criterion 1: a 4-unloseable streak sets a ruff/mypy/pytest directive."""
    recent = [
        _unloseable_task(f"u-{i}")
        for i in range(_UNLOSEABLE_STREAK_THRESHOLD)
    ]
    ctx = _make_ctx(agent=_StubAgent(memory=_StubMemory(recent=recent)))

    outcome = _unloseable_streak_run(ctx)

    assert outcome is not None
    assert outcome.directive is not None
    lowered = outcome.directive.lower()
    assert "ruff" in lowered
    assert "mypy" in lowered
    assert "pytest" in lowered
    assert "exit code" in lowered


def test_unloseable_streak_silent_when_loseable_in_window() -> None:
    """Criterion 2: one loseable task inside the trailing window releases it."""
    recent = [
        _unloseable_task(f"u-{i}")
        for i in range(_UNLOSEABLE_STREAK_THRESHOLD)
    ]
    recent[-1] = _loseable_task("builder")
    ctx = _make_ctx(agent=_StubAgent(memory=_StubMemory(recent=recent)))

    assert _unloseable_streak_run(ctx) is None


def test_unloseable_streak_ignores_tasks_before_window() -> None:
    """A loseable task *older* than the trailing window does not release it."""
    recent = [
        _loseable_task("old-builder"),
        *[
            _unloseable_task(f"u-{i}")
            for i in range(_UNLOSEABLE_STREAK_THRESHOLD)
        ],
    ]
    ctx = _make_ctx(agent=_StubAgent(memory=_StubMemory(recent=recent)))

    assert _unloseable_streak_run(ctx) is not None


def test_unloseable_streak_silent_below_threshold() -> None:
    """Threshold boundary: fires at 4, not 3 — three unloseable stays silent."""
    recent = [
        _unloseable_task(f"u-{i}")
        for i in range(_UNLOSEABLE_STREAK_THRESHOLD - 1)
    ]
    ctx = _make_ctx(agent=_StubAgent(memory=_StubMemory(recent=recent)))

    assert _unloseable_streak_run(ctx) is None


def test_unloseable_streak_appends_one_activation_row(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 4: a firing appends exactly one gate-activation row."""
    recent = [
        _unloseable_task(f"u-{i}")
        for i in range(_UNLOSEABLE_STREAK_THRESHOLD)
    ]
    model = SelfModel()
    agent = _StubAgent(memory=_StubMemory(recent=recent))
    ctx = _make_ctx(agent=agent, model=model)
    gate = next(g for g in GATE_CHAIN if g.name == "unloseable_streak")

    blocked = run_gate_chain([gate], ctx)

    assert blocked is False
    assert ctx.model.next_task_directive is not None
    assert "exit code" in ctx.model.next_task_directive.lower()
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "unloseable_streak"
    assert records[0]["action"] == "directive_set"


def test_unloseable_streak_never_overwrites_existing_directive(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 3: a pre-set directive is never overwritten, no row logged."""
    recent = [
        _unloseable_task(f"u-{i}")
        for i in range(_UNLOSEABLE_STREAK_THRESHOLD)
    ]
    model = SelfModel(next_task_directive="already set")
    agent = _StubAgent(memory=_StubMemory(recent=recent))
    ctx = _make_ctx(agent=agent, model=model)
    gate = next(g for g in GATE_CHAIN if g.name == "unloseable_streak")

    blocked = run_gate_chain([gate], ctx)

    assert blocked is False
    assert model.next_task_directive == "already set"
    assert _gate_records(mem_fs) == []


# ---------------------------------------------------------------------------
# consecutive-partial circuit-breaker gate (halt the discretionary treadmill)
# ---------------------------------------------------------------------------


def _outcome_task(
    task_id: str, outcome: str, source: str = "self_directed",
) -> TaskRecord:
    """A task record carrying a specific *outcome* and *source*.

    Defaults to ``source="self_directed"`` because the consecutive-partial
    breaker is scoped to the *discretionary* build-task treadmill; external
    (PR-remediation / user-request) partials must not feed its run.
    """
    rec = _task(task_id=task_id, goal="build a thing")
    return replace(rec, outcome=outcome, source=source)  # type: ignore[arg-type]


def _partials(n: int) -> list[TaskRecord]:
    """A trailing run of *n* ``partial`` self-directed task records."""
    return [_outcome_task(f"p-{i}", "partial") for i in range(n)]


def _ext_partials(n: int) -> list[TaskRecord]:
    """A trailing run of *n* ``partial`` *external* task records.

    Models routine multi-lap PR remediation, which legitimately records
    consecutive ``partial`` outcomes (success iff the PR landed) and must NOT
    trip the discretionary-treadmill breaker.
    """
    return [_outcome_task(f"e-{i}", "partial", source="external") for i in range(n)]


def test_count_trailing_partial_outcomes_branches() -> None:
    """The pure counter: empty→0, full run, trailing/interposed non-partial."""
    # Empty window: no iterations, zero run.
    assert count_trailing_partial_outcomes([]) == 0
    # All partial (loop never breaks within the window).
    assert count_trailing_partial_outcomes(
        [{"outcome": "partial"} for _ in range(4)],
    ) == 4
    # Newest is a non-partial → trailing run resets to zero (break on first).
    assert count_trailing_partial_outcomes(
        [{"outcome": "partial"}, {"outcome": "partial"}, {"outcome": "success"}],
    ) == 0
    # Interposed non-partial: only the tail after it counts.
    assert count_trailing_partial_outcomes(
        [
            {"outcome": "partial"},
            {"outcome": "failure"},
            {"outcome": "partial"},
            {"outcome": "partial"},
        ],
    ) == 2


def test_consecutive_partial_breaker_fires_at_threshold() -> None:
    """Criterion 1: exactly ``_CONSECUTIVE_PARTIAL_THRESHOLD`` partials block."""
    recent = _partials(_CONSECUTIVE_PARTIAL_THRESHOLD)
    ctx = _make_ctx(agent=_StubAgent(memory=_StubMemory(recent=recent)))

    outcome = _consecutive_partial_breaker_run(ctx)

    assert outcome is not None
    assert outcome.directive == _CONSECUTIVE_PARTIAL_DIRECTIVE
    assert str(_CONSECUTIVE_PARTIAL_THRESHOLD) in outcome.detail


def test_consecutive_partial_breaker_fires_above_threshold() -> None:
    """A run longer than the threshold still fires (>=, not ==)."""
    recent = _partials(_CONSECUTIVE_PARTIAL_THRESHOLD + 1)
    ctx = _make_ctx(agent=_StubAgent(memory=_StubMemory(recent=recent)))

    assert _consecutive_partial_breaker_run(ctx) is not None


def test_consecutive_partial_breaker_silent_below_threshold() -> None:
    """Criterion boundary: threshold-1 trailing partials stay silent."""
    recent = _partials(_CONSECUTIVE_PARTIAL_THRESHOLD - 1)
    ctx = _make_ctx(agent=_StubAgent(memory=_StubMemory(recent=recent)))

    assert _consecutive_partial_breaker_run(ctx) is None


def test_consecutive_partial_breaker_resets_on_trailing_success() -> None:
    """Criterion 2: a trailing ``success`` resets the run to zero (self-extinguish)."""
    recent = [*_partials(_CONSECUTIVE_PARTIAL_THRESHOLD), _outcome_task("s", "success")]
    ctx = _make_ctx(agent=_StubAgent(memory=_StubMemory(recent=recent)))

    assert _consecutive_partial_breaker_run(ctx) is None


def test_consecutive_partial_breaker_resets_on_interposed_failure() -> None:
    """Criterion 2: an interposed ``failure`` breaks the run below threshold."""
    # threshold partials, a failure, then threshold-1 partials: trailing run is
    # only threshold-1 (< threshold), so the gate stays silent.
    recent = [
        *_partials(_CONSECUTIVE_PARTIAL_THRESHOLD),
        _outcome_task("f", "failure"),
        *_partials(_CONSECUTIVE_PARTIAL_THRESHOLD - 1),
    ]
    ctx = _make_ctx(agent=_StubAgent(memory=_StubMemory(recent=recent)))

    assert _consecutive_partial_breaker_run(ctx) is None


def test_consecutive_partial_breaker_blocks_and_sets_empty_slot(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 1+3: fires as a block, fills an *empty* directive slot, one row."""
    recent = _partials(_CONSECUTIVE_PARTIAL_THRESHOLD)
    model = SelfModel()
    agent = _StubAgent(memory=_StubMemory(recent=recent))
    ctx = _make_ctx(agent=agent, model=model)
    gate = next(g for g in GATE_CHAIN if g.name == "consecutive_partial_breaker")

    blocked = run_gate_chain([gate], ctx)

    assert blocked is True
    assert ctx.model.next_task_directive == _CONSECUTIVE_PARTIAL_DIRECTIVE
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "consecutive_partial_breaker"
    assert records[0]["action"] == "blocked"


def _log_partial_breaker_blocks(agent: _StubAgent, n: int) -> None:
    """Pre-log *n* breaker ``blocked`` rows carrying the gate's threshold detail."""
    detail = (
        f"{_CONSECUTIVE_PARTIAL_THRESHOLD} consecutive partial task outcomes "
        f"(threshold={_CONSECUTIVE_PARTIAL_THRESHOLD})"
    )
    for _ in range(n):
        log_gate_activation(
            agent.gate_activations,
            gate=_CONSECUTIVE_PARTIAL_GATE,
            action="blocked",
            detail=detail,
        )


def test_consecutive_partial_breaker_releases_after_repeated_identical_blocks(
    mem_fs: dict[str, str],
) -> None:
    """#4108: after the streak of identical blocks, one call is admitted."""
    agent = _StubAgent(memory=_StubMemory(recent=_partials(_CONSECUTIVE_PARTIAL_THRESHOLD)))
    _log_partial_breaker_blocks(agent, _CONSECUTIVE_PARTIAL_RELEASE_STREAK)
    ctx = _make_ctx(agent=agent)

    outcome = _consecutive_partial_breaker_run(ctx)

    assert outcome is None
    released = [r for r in _gate_records(mem_fs) if r["action"] == "released"]
    assert len(released) == 1
    assert released[0]["gate"] == _CONSECUTIVE_PARTIAL_GATE


def test_consecutive_partial_breaker_blocks_below_release_streak(
    mem_fs: dict[str, str],
) -> None:
    """One block short of the streak still blocks and writes no release."""
    agent = _StubAgent(memory=_StubMemory(recent=_partials(_CONSECUTIVE_PARTIAL_THRESHOLD)))
    _log_partial_breaker_blocks(agent, _CONSECUTIVE_PARTIAL_RELEASE_STREAK - 1)
    ctx = _make_ctx(agent=agent)

    outcome = _consecutive_partial_breaker_run(ctx)

    assert outcome is not None
    released = [r for r in _gate_records(mem_fs) if r["action"] == "released"]
    assert released == []


def test_consecutive_partial_breaker_idle_loop_does_not_wedge(
    mem_fs: dict[str, str],
) -> None:
    """#4108: a frozen history blocks the streak, then one call is admitted."""
    agent = _StubAgent(memory=_StubMemory(recent=_partials(_CONSECUTIVE_PARTIAL_THRESHOLD)))
    gate = next(g for g in GATE_CHAIN if g.name == _CONSECUTIVE_PARTIAL_GATE)

    results = [
        run_gate_chain([gate], _make_ctx(agent=agent))
        for _ in range(_CONSECUTIVE_PARTIAL_RELEASE_STREAK + 1)
    ]

    assert results == [True] * _CONSECUTIVE_PARTIAL_RELEASE_STREAK + [False]
    actions = [r["action"] for r in _gate_records(mem_fs)]
    assert actions == ["blocked"] * _CONSECUTIVE_PARTIAL_RELEASE_STREAK + ["released"]
    # The released row breaks the streak: the next call blocks again.
    assert run_gate_chain([gate], _make_ctx(agent=agent)) is True


def test_consecutive_partial_breaker_release_ignores_other_gate_rows(
    mem_fs: dict[str, str],
) -> None:
    """Interleaved rows from other gates neither break nor extend the streak."""
    agent = _StubAgent(memory=_StubMemory(recent=_partials(_CONSECUTIVE_PARTIAL_THRESHOLD)))
    backend = agent.gate_activations
    for _ in range(_CONSECUTIVE_PARTIAL_RELEASE_STREAK - 1):
        _log_partial_breaker_blocks(agent, 1)
        log_gate_activation(backend, gate="cost_risk", action="blocked", detail="x")
        log_gate_activation(
            backend, gate="tend_cooldown_skip", action="skipped", detail="#1 cooldown",
        )
    ctx = _make_ctx(agent=agent)

    # streak - 1 own blocks plus many foreign rows: still blocks.
    assert _consecutive_partial_breaker_run(ctx) is not None
    _log_partial_breaker_blocks(agent, 1)
    log_gate_activation(backend, gate="ci_pending_hold", action="held", detail="#1")

    assert _consecutive_partial_breaker_run(ctx) is None
    released = [r for r in _gate_records(mem_fs) if r["action"] == "released"]
    assert len(released) == 1
    assert released[0]["gate"] == _CONSECUTIVE_PARTIAL_GATE


def test_consecutive_partial_breaker_never_overwrites_directive(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 3: a pre-set directive survives; the gate still blocks the chain."""
    recent = _partials(_CONSECUTIVE_PARTIAL_THRESHOLD)
    model = SelfModel(next_task_directive="already set")
    agent = _StubAgent(memory=_StubMemory(recent=recent))
    ctx = _make_ctx(agent=agent, model=model)
    gate = next(g for g in GATE_CHAIN if g.name == "consecutive_partial_breaker")

    blocked = run_gate_chain([gate], ctx)

    assert blocked is True
    assert model.next_task_directive == "already set"
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["action"] == "blocked"


def test_consecutive_partial_breaker_directive_within_char_cap() -> None:
    """The carried directive is bounded to the shared directive cap.

    Asserted against the constant rather than a literal ``200``: if the
    canonical ``Consciousness.MAX_DIRECTIVE_CHARS`` is ever *lowered*, a
    literal assertion would stay green while ``save_self_model`` silently
    truncated this fixed template on persist.
    """
    assert len(_CONSECUTIVE_PARTIAL_DIRECTIVE) <= _DIRECTIVE_MAX_CHARS


def test_consecutive_partial_breaker_ignores_external_partials() -> None:
    """External (remediation/user) partials never feed the discretionary run.

    Routine multi-lap PR remediation records ``external`` ``partial`` outcomes
    into the same 10-task window the gate reads (outcome_authority: success iff
    the PR landed, else partial). Those are not a discretionary build-task
    treadmill, so a full-threshold run of them must leave the gate silent —
    otherwise the block gate wedges legitimate discretionary generation on
    history it can never advance.
    """
    recent = _ext_partials(_CONSECUTIVE_PARTIAL_THRESHOLD + 2)
    ctx = _make_ctx(agent=_StubAgent(memory=_StubMemory(recent=recent)))

    assert _consecutive_partial_breaker_run(ctx) is None


def test_consecutive_partial_breaker_counts_only_self_directed_in_mixed_window(
) -> None:
    """External partials are excluded, not merely counted — mixed window.

    A window padded with external partials around a sub-threshold run of
    self-directed partials must stay silent: only the ``self_directed`` records
    feed the count, and there are fewer than the threshold of them.
    """
    recent = [
        *_ext_partials(3),
        *_partials(_CONSECUTIVE_PARTIAL_THRESHOLD - 1),
    ]
    ctx = _make_ctx(agent=_StubAgent(memory=_StubMemory(recent=recent)))

    assert _consecutive_partial_breaker_run(ctx) is None


def test_consecutive_partial_breaker_interposed_external_does_not_split_run(
) -> None:
    """An interposed external record neither counts nor breaks the SD run.

    With the external records filtered out first, a discretionary treadmill
    split by an unrelated external partial still fires: the surviving
    self-directed partials form a threshold-length trailing run.
    """
    recent = [
        *_partials(2),
        *_ext_partials(1),
        *_partials(_CONSECUTIVE_PARTIAL_THRESHOLD - 2),
    ]
    ctx = _make_ctx(agent=_StubAgent(memory=_StubMemory(recent=recent)))

    outcome = _consecutive_partial_breaker_run(ctx)

    assert outcome is not None
    assert outcome.directive == _CONSECUTIVE_PARTIAL_DIRECTIVE


def test_consecutive_partial_breaker_zero_sdk_ignores_priority_queues() -> None:
    """Criterion 4: pure history read; priority queues neither read nor gated.

    The gate reads only the trailing task-history window (one
    ``get_recent_tasks`` call, no SDK) and never inspects
    ``pending_user_requests`` / ``pending_pr_requests`` — those are serviced
    upstream in ``_run_task_phase`` and bypass ``generate_task`` entirely, so
    populating them changes neither the block decision nor the read profile.
    """
    recent = _partials(_CONSECUTIVE_PARTIAL_THRESHOLD)
    memory = _StubMemory(recent=recent)
    model = SelfModel(
        pending_user_requests=["please do X"],
        pending_pr_requests=[
            PendingPRRequest(directive="Remediate PR #7"),
        ],
    )
    ctx = _make_ctx(agent=_StubAgent(memory=memory), model=model)

    outcome = _consecutive_partial_breaker_run(ctx)

    assert outcome is not None
    assert outcome.directive == _CONSECUTIVE_PARTIAL_DIRECTIVE
    # Only the 10-task history window was read — the gate is a pure history read.
    assert memory.recent_calls == [10]


# ---------------------------------------------------------------------------
# GATE_CHAIN registry and gate_order()
# ---------------------------------------------------------------------------


def test_gate_order_matches_documented_precedence() -> None:
    assert gate_order() == [
        ("wip_primacy", "block"),
        ("no_repeat_failed_branch_target", "block"),
        ("quiet_skip", "block"),
        ("audit_stall", "block"),
        ("cost_risk", "block"),
        ("escalation_bridge", "block"),
        ("consecutive_partial_breaker", "block"),
        ("stale_obsolete_directive_guard", "directive"),
        ("instrument_consumption", "directive"),
        ("integration_cadence", "directive"),
        ("investigation_streak", "directive"),
        ("investigation_rate", "directive"),
        ("branchless_streak", "directive"),
        ("partial_construction", "directive"),
        ("promotable_backlog_directive", "directive"),
        ("self_directed_success", "directive"),
        ("forced_read", "directive"),
        ("readmission_construction", "directive"),
        ("unloseable_streak", "directive"),
        ("comfort_cluster", "directive"),
        ("untouched_first_target", "directive"),
        ("dropped_directive_replay", "directive"),
    ]


def test_blocking_gates_precede_directive_gates() -> None:
    kinds = [gate.kind for gate in GATE_CHAIN]
    first_directive = kinds.index("directive")
    assert all(kind == "block" for kind in kinds[:first_directive])
    assert all(kind == "directive" for kind in kinds[first_directive:])


# ---------------------------------------------------------------------------
# CONTENT_GATE_ORDER / content_gate_order() — post-call content gates
# ---------------------------------------------------------------------------


def test_content_gate_order_binds_to_vet_goal_content(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """``content_gate_order()`` mirrors the order ``_vet_goal_content`` applies.

    Drives the applier once per content gate with a goal engineered to trip
    that gate and pass every earlier one — reusing the tripping goals the
    per-gate tests in ``test_agent.py`` already exercise — then asserts the
    measured sequence of rejecting-gate names equals the declaration exactly.
    Without this binding the constant would be a seventh copy free to drift
    from the behaviour it claims to describe.
    """
    from maestro.loop.task_generation import _vet_goal_content

    agent = make_agent()
    # The phantom-premise gate is a default-off experiment; enable it so the
    # measured sequence covers the whole declared membership.
    agent.config.phantom_premise_gate_enabled = True
    # Each goal is engineered to trip its own gate while passing every earlier
    # one, so the measured rejection sequence proves each gate fires at its
    # declared position. ``maestro/nonexistent.py`` is deliberately absent from
    # the tree, so the terrain-exhaustion precondition fails for goal #2; the
    # compound goal ("Implement … and connect …") is caught by compound-scope
    # before concreteness is ever reached. The last goal names a concrete file
    # target and an implementation verb (so concreteness admits it) but that
    # target is absent, and its framing claims an existing defect rather than
    # creating the file — the phantom-premise refusal. The sixth frames its
    # absent path as a creation (so phantom-premise admits it) but finishes a
    # branch the seeded history records as ``partial`` — failed-branch-target.
    ordered_trips = [
        ("pr_mutation_goal", "open a PR for the task branch"),
        ("terrain_exhaustion", "Fix bug in maestro/nonexistent.py for #1234"),
        ("compound_scope", "Implement validator and connect to existing gate"),
        ("concreteness", "analyze the recent loop behaviour patterns"),
        (
            "phantom_premise",
            "Fix the broken swallow in maestro/nonexistent.py",
        ),
        (
            "failed_branch_target",
            "Create maestro/newmod.py to finish maestro/task/16db56580b86",
        ),
    ]
    agent.memory.record_task(_failed_branch_history_record())
    measured = [_vet_goal_content(agent, goal) for _, goal in ordered_trips]

    assert measured == [name for name, _ in ordered_trips]
    assert measured == task_gates.content_gate_order()


def test_content_gate_order_matches_applier_precedence(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """The applier resolves overlapping gates in ``content_gate_order()`` order.

    The membership test above drives one *single*-gate goal per gate, which
    proves each gate fires but cannot observe the relative order the ``if``
    blocks run in: because each goal trips exactly one gate, reordering the
    blocks inside ``_vet_goal_content`` without touching ``CONTENT_GATE_ORDER``
    would leave that test green — the exact drift the declaration exists to
    catch. This closes the gap. Each goal below trips *two adjacent* content
    gates at once, so ``_vet_goal_content`` must return the earlier-declared
    one; the goals tile every adjacent boundary, so any transposition of
    the applier's blocks flips a winner and fails here.
    """
    from maestro.loop.task_generation import _vet_goal_content

    agent = make_agent()
    agent.config.phantom_premise_gate_enabled = True
    # Each goal trips exactly two adjacent content gates; the winner must be the
    # earlier-declared one. ``maestro/nonexistent.py`` is absent from the tree,
    # so the terrain-exhaustion precondition fails for the first two goals; the
    # third combines a creation+connection verb pair (compound-scope) with an
    # analysis-only verb and no file target (concreteness); the fourth pairs
    # that same concreteness shape (analysis verb, no concrete target — the
    # ``vendor/`` path is outside the source dirs the concreteness regex
    # accepts) with a defect claim against an absent ``.py`` path
    # (phantom-premise).
    overlapping = [
        (
            "open a PR to fix maestro/nonexistent.py for #1234",
            "pr_mutation_goal",
            "terrain_exhaustion",
        ),
        (
            "Implement a validator in maestro/nonexistent.py and connect it "
            "to the existing gate for #1234",
            "terrain_exhaustion",
            "compound_scope",
        ),
        (
            "build an exploration and integrate the analysis",
            "compound_scope",
            "concreteness",
        ),
        (
            "analyze the broken swallow in vendor/absent.py",
            "concreteness",
            "phantom_premise",
        ),
        (
            "Fix the broken swallow in maestro/nonexistent.py from "
            "maestro/task/16db56580b86",
            "phantom_premise",
            "failed_branch_target",
        ),
    ]
    agent.memory.record_task(_failed_branch_history_record())
    order = task_gates.content_gate_order()
    for goal, earlier, later in overlapping:
        # The pair is genuinely adjacent in the declared order (so the three
        # goals cover every boundary), and the applier picks the earlier gate.
        assert order.index(earlier) + 1 == order.index(later)
        assert _vet_goal_content(agent, goal) == earlier


def _failed_branch_history_record() -> TaskRecord:
    """A ``partial`` task-history row on ``maestro/task/16db56580b86``."""
    return TaskRecord(
        task_id="16db56580b86",
        goal="wire X",
        plan=[],
        outcome="partial",
        result_summary="",
        errors=[],
        duration_secs=0.0,
        timestamp="2026-09-24T00:00:00+00:00",
        source="self_directed",
        branch="maestro/task/16db56580b86",
    )


def test_failed_branch_target_gate_reads_rationale(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """A failed-branch token only in the rationale is refused at admission."""
    from maestro.loop.task_generation import _vet_goal_content

    agent = make_agent()
    agent.memory.record_task(_failed_branch_history_record())
    goal = "Fix the retry loop in maestro/agent.py"

    assert _vet_goal_content(agent, goal) is None
    assert _vet_goal_content(
        agent, goal, rationale="continues maestro/task/16db56580b86",
    ) == "failed_branch_target"


def test_failed_branch_target_named_ignores_success() -> None:
    """The shared predicate names only non-success branches, lowest first."""
    failed = _failed_branch_history_record()
    ok = _failed_branch_history_record()
    ok.outcome = "success"
    ok.branch = "maestro/task/aaaaaa1"
    text = "maestro/task/aaaaaa1 and maestro/task/16db56580b86"

    assert task_gates.failed_branch_target_named(text, [ok]) is None
    assert task_gates.failed_branch_target_named("no token", [failed]) is None
    assert (
        task_gates.failed_branch_target_named(text, [ok, failed])
        == "maestro/task/16db56580b86"
    )


def test_failed_branch_target_named_picks_lowest_sorted() -> None:
    """With two failed tokens named, the lower-sorted one wins."""
    first = _failed_branch_history_record()
    second = _failed_branch_history_record()
    second.branch = "maestro/task/aaaaaa1"
    text = "maestro/task/aaaaaa1 and maestro/task/16db56580b86"

    assert (
        task_gates.failed_branch_target_named(text, [second, first])
        == "maestro/task/16db56580b86"
    )


# ---------------------------------------------------------------------------
# wip_primacy gate (proposal 092)
# ---------------------------------------------------------------------------


def _wip_gate() -> TaskGate:
    """The head-of-chain WIP-primacy gate, built off its real run/enable."""
    return _gate(
        "wip_primacy", "block", _wip_primacy_run,
        enabled=lambda config: config.wip_primacy_enabled,
    )


def _readiness(**overrides: Any) -> PRReadiness:
    """Build a :class:`PRReadiness` for the WIP-primacy merge-ready tests.

    Defaults describe a *green, merge-ready* owned PR (open, 0-behind,
    0-conflict, CI green, no outstanding overflow); each dimension is overridable
    to model a genuine code/gate blocker. No real git/host observation — the
    fact is constructed directly, the "mocked merge-state measurement".
    """
    fields: dict[str, Any] = {
        "pr_number": 42,
        "branch_landed": False,
        "branch_behind": False,
        "branch_conflicting": False,
        "ci_state": "GREEN",
        "unresolved_threads": 0,
        "unaddressed_overflow": 0,
        "awaiting_approval": False,
        "ready_and_waiting": True,
        "source": "host_api",
        "observed_at": 0.0,
    }
    fields.update(overrides)
    return PRReadiness(**fields)


def _agent_with_focus(
    focus: Any,
    readiness_by_pr: dict[int, PRReadiness] | None = None,
    check_runs: Sequence[CheckRun] = (),
) -> _StubAgent:
    agent = _StubAgent()
    agent._current_task_focus = MagicMock(  # type: ignore[attr-defined]
        return_value=focus,
    )
    if readiness_by_pr is not None:
        agent._pr_readiness_memo = readiness_by_pr
    # The code-red directive's failing-check reader (proposal 137, step 11).
    # A MagicMock so a test can assert the flag-off path never reads it.
    agent._observe_pr_check_runs = MagicMock(  # type: ignore[attr-defined]
        return_value=tuple(check_runs),
    )
    return agent


def test_wip_primacy_gate_skipped_when_flag_off(mem_fs: dict[str, str]) -> None:
    """Flag off: the gate is skipped, its owner never consulted, chain unchanged.

    With ``wip_primacy_enabled=False`` the runner never calls ``run`` (so
    ``_current_task_focus`` is never invoked, even though it *would* report owned
    WIP), and a lower directive gate fills the slot exactly as it does today —
    byte-for-byte parity with a chain that lacks the gate.
    """
    agent = _agent_with_focus(TendOwnedPR(pr_number=1, owned_blocker="CONFLICTING"))
    model = SelfModel()
    ctx = _make_ctx(
        config=_make_config(wip_primacy_enabled=False), model=model, agent=agent,
    )
    later = MagicMock(
        return_value=GateOutcome(
            detail="d", log_message="m", directive="later-directive",
        ),
    )
    gates = [_wip_gate(), _gate("later", "directive", later)]

    blocked = run_gate_chain(gates, ctx)

    assert blocked is False
    agent._current_task_focus.assert_not_called()  # type: ignore[attr-defined]
    later.assert_called_once()
    assert ctx.model.next_task_directive == "later-directive"
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "later"


def test_wip_primacy_gate_blocks_and_sets_directive(
    mem_fs: dict[str, str],
) -> None:
    """Flag on + owned WIP: block the chain, set the finish-PR directive.

    A ``TendOwnedPR`` focus makes the gate short-circuit generation *and* write a
    bounded ``Finish PR #N`` directive (persisted via ``save_self_model``); lower
    gates never fire and the single activation row is a ``blocked`` record. With
    no readiness observation in the memo (the default empty ``_pr_readiness_memo``)
    the PR is not measured merge-ready, so the finish variant fires — the
    merge-ready handoff variant is covered separately.
    """
    agent = _agent_with_focus(
        TendOwnedPR(pr_number=42, owned_blocker="THREADS_UNRESOLVED"),
    )
    model = SelfModel()
    ctx = _make_ctx(
        config=_make_config(wip_primacy_enabled=True), model=model, agent=agent,
    )
    later = MagicMock(
        return_value=GateOutcome(detail="d", log_message="m", directive="x"),
    )
    gates = [_wip_gate(), _gate("later", "directive", later)]

    blocked = run_gate_chain(gates, ctx)

    assert blocked is True
    later.assert_not_called()
    assert ctx.model.next_task_directive == _wip_primacy_directive_text(
        42, "THREADS_UNRESOLVED",
    )
    agent.consciousness._mutate_self_model_persisted.assert_called_once()
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "wip_primacy"
    assert records[0]["action"] == "blocked"
    assert records[0]["detail"] == "pr=#42 blocker=THREADS_UNRESOLVED"


def test_wip_primacy_gate_passes_through_when_discretionary(
    mem_fs: dict[str, str],
) -> None:
    """Flag on + no owned WIP: the gate returns ``None`` and the chain proceeds."""
    agent = _agent_with_focus(Discretionary())
    model = SelfModel()
    ctx = _make_ctx(
        config=_make_config(wip_primacy_enabled=True), model=model, agent=agent,
    )
    later = MagicMock(
        return_value=GateOutcome(
            detail="d", log_message="m", directive="later-dir",
        ),
    )
    gates = [_wip_gate(), _gate("later", "directive", later)]

    blocked = run_gate_chain(gates, ctx)

    assert blocked is False
    agent._current_task_focus.assert_called_once()  # type: ignore[attr-defined]
    later.assert_called_once()
    assert ctx.model.next_task_directive == "later-dir"
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "later"
    assert records[0]["action"] == "directive_set"


def test_wip_primacy_gate_hands_off_when_merge_ready(
    mem_fs: dict[str, str],
) -> None:
    """Merge-ready owned PR: the directive hands off to Thomas, not another lap.

    Acceptance criterion 1 — a ``TendOwnedPR`` focus whose PR is *measured*
    merge-ready (green CI, 0-behind, 0-conflict, **no unresolved thread, no
    unaddressed overflow**; only a human merge-or-close remains) makes the gate
    write the handoff-framed directive naming the PR number and instructing the
    merge-or-close message to Thomas, *not* the ``Finish PR #N`` lever the loop
    kept converting into a CI-deferred lap. The merge-state measurement is mocked
    (``_pr_readiness_memo``) — no real git/host call.

    The blocker is ``AWAITING_APPROVAL`` — the residual disposition of a green,
    thread-free PR. It is *not* ``THREADS_UNRESOLVED``: that code always carries
    ``unresolved_threads > 0`` (owned code work → ``Finish``, covered by
    ``test_wip_primacy_gate_finishes_on_open_threads``), so pairing it with a
    thread-free readiness would be the contradictory fixture the earlier version
    used.
    """
    agent = _agent_with_focus(
        TendOwnedPR(pr_number=42, owned_blocker="AWAITING_APPROVAL"),
        readiness_by_pr={42: _readiness()},
    )
    model = SelfModel()
    ctx = _make_ctx(
        config=_make_config(wip_primacy_enabled=True), model=model, agent=agent,
    )
    later = MagicMock(
        return_value=GateOutcome(detail="d", log_message="m", directive="x"),
    )
    gates = [_wip_gate(), _gate("later", "directive", later)]

    blocked = run_gate_chain(gates, ctx)

    assert blocked is True
    later.assert_not_called()
    assert ctx.model.next_task_directive == _wip_primacy_handoff_directive_text(42)
    # Handoff-framed, naming the PR and Thomas — never the Finish-PR lever.
    assert "PR #42" in ctx.model.next_task_directive
    assert "Thomas" in ctx.model.next_task_directive
    assert "Finish PR #" not in ctx.model.next_task_directive
    agent.consciousness._mutate_self_model_persisted.assert_called_once()
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "wip_primacy"
    assert records[0]["action"] == "blocked"
    assert records[0]["detail"] == (
        "pr=#42 merge_ready=handoff owed=handoff-ready"
    )


def test_wip_primacy_gate_finishes_on_code_gate_blocker(
    mem_fs: dict[str, str],
) -> None:
    """Genuine code/gate blocker: the unchanged ``Finish PR #N`` directive fires.

    Acceptance criterion 2 — a ``TendOwnedPR`` focus whose PR carries a
    CONFLICTING (code-clearable git) blocker, measured branch-conflicting, keeps
    the existing ``Finish PR #N`` text: there is real code work to do, so the
    handoff decoy must *not* be substituted.
    """
    agent = _agent_with_focus(
        TendOwnedPR(pr_number=42, owned_blocker="CONFLICTING"),
        readiness_by_pr={
            42: _readiness(branch_conflicting=True, ready_and_waiting=False),
        },
    )
    model = SelfModel()
    ctx = _make_ctx(
        config=_make_config(wip_primacy_enabled=True), model=model, agent=agent,
    )
    gates = [_wip_gate()]

    blocked = run_gate_chain(gates, ctx)

    assert blocked is True
    assert ctx.model.next_task_directive == _wip_primacy_directive_text(
        42, "CONFLICTING",
    )
    assert ctx.model.next_task_directive.startswith("Finish PR #42")
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["detail"] == "pr=#42 blocker=CONFLICTING"


def test_wip_primacy_gate_finishes_when_ci_not_green(
    mem_fs: dict[str, str],
) -> None:
    """A green branch but failing CI is a gate blocker, not merge-ready.

    An owned ``THREADS_UNRESOLVED`` focus whose branch is 0-behind/0-conflict but
    whose CI concluded *failure* is not merge-ready — a failing gate is code work
    — so the gate keeps the ``Finish PR #N`` text rather than handing off.
    """
    agent = _agent_with_focus(
        TendOwnedPR(pr_number=42, owned_blocker="THREADS_UNRESOLVED"),
        readiness_by_pr={
            42: _readiness(ci_state="FAILING", ready_and_waiting=False),
        },
    )
    model = SelfModel()
    ctx = _make_ctx(
        config=_make_config(wip_primacy_enabled=True), model=model, agent=agent,
    )

    blocked = run_gate_chain([_wip_gate()], ctx)

    assert blocked is True
    assert ctx.model.next_task_directive == _wip_primacy_directive_text(
        42, "THREADS_UNRESOLVED",
    )
    assert ctx.model.next_task_directive.startswith("Finish PR #42")


def test_wip_primacy_gate_names_first_act_on_code_clearable_red(
    mem_fs: dict[str, str],
) -> None:
    """Acceptance criterion 1 — a code-clearable red names the Edit+push first act.

    A ``TendOwnedPR`` whose sole remaining blocker is a failing quality gate
    (measured ``ci_state == "FAILING"`` with a clean, thread-free branch, and an
    owned blocker that is *not* a git conflict / review thread) must not get the
    bounded ``Finish PR #N`` verb that opens the act phase with a survey-first
    fork-read (#3264). Instead the directive names the unfakeable first mutating
    act — Edit the failing file/gate, then push — and explicitly forbids a
    fork-read / survey first.
    """
    agent = _agent_with_focus(
        TendOwnedPR(pr_number=3264, owned_blocker="CI_FAILING"),
        readiness_by_pr={
            3264: _readiness(
                pr_number=3264, ci_state="FAILING", ready_and_waiting=False,
            ),
        },
    )
    model = SelfModel()
    ctx = _make_ctx(
        config=_make_config(wip_primacy_enabled=True), model=model, agent=agent,
    )

    blocked = run_gate_chain([_wip_gate()], ctx)

    assert blocked is True
    directive = ctx.model.next_task_directive
    assert directive == _wip_primacy_code_red_directive_text(3264)
    # Names the Edit-then-push first act on the failing file/gate ...
    assert "first act = Edit the failing file/gate" in directive
    assert "then push" in directive
    # ... and forbids the survey-first / fork-read move outright.
    assert "do not fork-read or survey first" in directive.lower()
    # It carries neither WIP marker, so the chokepoint guards stay unambiguous.
    assert "Finish PR #" not in directive
    assert "Hand off PR #" not in directive
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "wip_primacy"
    assert records[0]["action"] == "blocked"
    assert records[0]["detail"] == "pr=#3264 blocker=CI_FAILING code_red=first_act"


def test_wip_primacy_gate_awaiting_push_keeps_finish_text(
    mem_fs: dict[str, str],
) -> None:
    """Acceptance criterion 2 — an AWAITING_PUSH-only blocker keeps ``Finish PR #N``.

    An owned PR whose blocker is a residual owned push (CI not failing — here
    still pending — no conflict, no thread) is *not* a code-clearable red: its
    directive must be byte-identical to the current ``Finish PR #N`` output, not
    the new first-act text.
    """
    agent = _agent_with_focus(
        TendOwnedPR(pr_number=42, owned_blocker="AWAITING_PUSH"),
        readiness_by_pr={
            42: _readiness(ci_state="PENDING", ready_and_waiting=False),
        },
    )
    model = SelfModel()
    ctx = _make_ctx(
        config=_make_config(wip_primacy_enabled=True), model=model, agent=agent,
    )

    blocked = run_gate_chain([_wip_gate()], ctx)

    assert blocked is True
    assert ctx.model.next_task_directive == _wip_primacy_directive_text(
        42, "AWAITING_PUSH",
    )
    assert ctx.model.next_task_directive.startswith("Finish PR #42")


def test_wip_is_code_clearable_red_predicate() -> None:
    """``_wip_is_code_clearable_red`` isolates the bare failing-gate case.

    ``True`` only when a readiness fact is present and shows an open, 0-behind,
    0-conflict, thread-free, overflow-free PR whose CI concluded ``FAILING`` and
    whose owned blocker is not a structural git conflict / review thread. Every
    other measurement (memo miss, landed, CI green/pending, behind, conflicting,
    open thread / overflow) and a ``CONFLICTING`` / ``DIRTY`` /
    ``THREADS_UNRESOLVED`` owned blocker is *not* a bare code-clearable red.
    """
    # Bare failing gate — the only True case.
    assert _wip_is_code_clearable_red(_readiness(ci_state="FAILING"), "CI_FAILING") is True
    # Memo miss — no observation to author a first-act directive.
    assert _wip_is_code_clearable_red(None, "CI_FAILING") is False
    # Landed PR — nothing left to clear.
    assert (
        _wip_is_code_clearable_red(
            _readiness(ci_state="FAILING", branch_landed=True), "CI_FAILING",
        )
        is False
    )
    # Structural owned blockers keep the Finish path even with failing CI.
    assert (
        _wip_is_code_clearable_red(_readiness(ci_state="FAILING"), "CONFLICTING")
        is False
    )
    assert (
        _wip_is_code_clearable_red(_readiness(ci_state="FAILING"), "dirty")
        is False
    )
    assert (
        _wip_is_code_clearable_red(
            _readiness(ci_state="FAILING"), "THREADS_UNRESOLVED",
        )
        is False
    )
    # CI not FAILING (green / pending) — no red to clear.
    assert _wip_is_code_clearable_red(_readiness(), "CI_FAILING") is False
    assert (
        _wip_is_code_clearable_red(_readiness(ci_state="PENDING"), "CI_FAILING")
        is False
    )
    # Behind / conflicting / open thread / overflow are not a *bare* gate red.
    assert (
        _wip_is_code_clearable_red(
            _readiness(ci_state="FAILING", branch_behind=True), "CI_FAILING",
        )
        is False
    )
    assert (
        _wip_is_code_clearable_red(
            _readiness(ci_state="FAILING", branch_conflicting=True), "CI_FAILING",
        )
        is False
    )
    assert (
        _wip_is_code_clearable_red(
            _readiness(ci_state="FAILING", unresolved_threads=1), "CI_FAILING",
        )
        is False
    )
    assert (
        _wip_is_code_clearable_red(
            _readiness(ci_state="FAILING", unaddressed_overflow=1), "CI_FAILING",
        )
        is False
    )


def test_wip_primacy_code_red_directive_text_is_bounded() -> None:
    """The code-red directive names the first act, forbids surveying, ≤200 chars.

    Acceptance criteria 1 + 4 at the text level: the directive carries the
    Edit-then-push first-act phrase and the forbid-survey-first clause, omits
    both WIP markers, and stays within the 200-char cap.
    """
    text = _wip_primacy_code_red_directive_text(123)
    assert text.startswith("PR #123: first act = Edit the failing file/gate")
    assert "then push" in text
    assert "do not fork-read or survey first" in text.lower()
    assert "Finish PR #" not in text
    assert "Hand off PR #" not in text
    assert len(text) <= _DIRECTIVE_MAX_CHARS


def test_wip_primacy_code_red_directive_text_truncates_overlong() -> None:
    """A pathologically large PR number is truncated to the shared cap."""
    text = _wip_primacy_code_red_directive_text(10 ** 250)
    assert len(text) == _DIRECTIVE_MAX_CHARS
    assert text.endswith("…")


def test_wip_pr_merge_ready_predicate() -> None:
    """``_wip_pr_merge_ready`` gates handoff on measurement + owned-blocker class.

    Merge-ready only when a readiness fact is present and shows an open,
    0-behind, 0-conflict, CI-green PR **with no unresolved review thread and no
    unaddressed overflow finding** whose owned blocker is not a code-clearable
    git conflict; every other measurement (memo miss, landed, behind,
    conflicting, CI-not-green, open thread, open overflow) and a CONFLICTING /
    DIRTY owned blocker are code/gate work, not merge-ready.

    The merge-ready case uses ``AWAITING_APPROVAL`` — the genuine residual
    disposition on a green, thread-free PR: only a human merge-or-close remains.
    ``THREADS_UNRESOLVED`` is deliberately *not* used for the ``True`` case: that
    code is only emitted when ``unresolved_threads > 0`` (see
    ``test_wip_pr_merge_ready_open_threads_are_not_merge_ready``), so pairing it
    with a thread-free readiness would be a contradictory fixture.
    """
    # Merge-ready: green, thread-free, overflow-free measurement + a non-git
    # residual blocker (awaiting a human merge-or-close).
    assert _wip_pr_merge_ready(_readiness(), "AWAITING_APPROVAL") is True
    # Memo miss — no observation to author a handoff.
    assert _wip_pr_merge_ready(None, "AWAITING_APPROVAL") is False
    # Landed PR — nothing left to hand off.
    assert (
        _wip_pr_merge_ready(_readiness(branch_landed=True), "AWAITING_APPROVAL")
        is False
    )
    # Owned git-conflict blocker — code work regardless of the measurement.
    assert _wip_pr_merge_ready(_readiness(), "CONFLICTING") is False
    assert _wip_pr_merge_ready(_readiness(), "dirty") is False
    # Behind / conflicting / CI-not-green measurements are code/gate blockers.
    assert (
        _wip_pr_merge_ready(_readiness(branch_behind=True), "AWAITING_APPROVAL")
        is False
    )
    assert (
        _wip_pr_merge_ready(
            _readiness(branch_conflicting=True), "AWAITING_APPROVAL",
        )
        is False
    )
    assert (
        _wip_pr_merge_ready(_readiness(ci_state="PENDING"), "AWAITING_APPROVAL")
        is False
    )
    # No CI configured clears the CI axis, as in ``ready_and_waiting`` (#4133).
    assert (
        _wip_pr_merge_ready(_readiness(ci_state="NONE"), "AWAITING_APPROVAL")
        is True
    )
    # Open review thread / unaddressed overflow — owned code work, not merge-ready.
    assert (
        _wip_pr_merge_ready(_readiness(unresolved_threads=2), "AWAITING_APPROVAL")
        is False
    )
    assert (
        _wip_pr_merge_ready(
            _readiness(unaddressed_overflow=1), "AWAITING_APPROVAL",
        )
        is False
    )


def test_wip_pr_merge_ready_open_threads_are_not_merge_ready() -> None:
    """Reproduces the handoff-on-open-threads defect (PR #3197 thread 3636861604).

    The only reachable runtime pairing for a ``THREADS_UNRESOLVED`` focus is a
    memo ``PRReadiness`` whose ``unresolved_threads > 0``:
    ``classify_owned_blocker`` (the single source ``select_task_focus`` and every
    PR-servicing face use) emits ``THREADS_UNRESOLVED`` *only* when
    ``unresolved_threads > 0``. Those open, un-answered AI review threads are the
    agent's *own* code work to clear, so the PR is **not** merge-ready and the
    gate must keep ``Finish PR #N`` rather than handing off a merge-or-close to
    Thomas. ``_wip_pr_merge_ready`` currently ignores ``unresolved_threads`` and
    so wrongly reports the green-branch state merge-ready — this assertion fails
    against current code, proving the defect. The existing ``threads=0`` fixture
    masks it because that state can never arise at runtime for this blocker.
    """
    assert (
        _wip_pr_merge_ready(
            _readiness(unresolved_threads=3), "THREADS_UNRESOLVED",
        )
        is False
    )


def test_wip_primacy_gate_finishes_on_open_threads(
    mem_fs: dict[str, str],
) -> None:
    """Gate-level reproduction: open threads keep ``Finish PR #N``, not handoff.

    A ``TendOwnedPR(owned_blocker="THREADS_UNRESOLVED")`` focus whose memo
    readiness carries the realistic ``unresolved_threads > 0`` (green CI,
    0-behind, 0-conflict) must write the ``Finish PR #N`` directive — the
    unanswered review threads are owned code work, not a merge-or-close human
    disposition. Against current code the gate hands off instead, so this fails.
    """
    agent = _agent_with_focus(
        TendOwnedPR(pr_number=42, owned_blocker="THREADS_UNRESOLVED"),
        readiness_by_pr={42: _readiness(unresolved_threads=3)},
    )
    model = SelfModel()
    ctx = _make_ctx(
        config=_make_config(wip_primacy_enabled=True), model=model, agent=agent,
    )

    blocked = run_gate_chain([_wip_gate()], ctx)

    assert blocked is True
    assert ctx.model.next_task_directive == _wip_primacy_directive_text(
        42, "THREADS_UNRESOLVED",
    )
    assert ctx.model.next_task_directive.startswith("Finish PR #42")
    assert "Hand off PR #" not in ctx.model.next_task_directive


def test_wip_primacy_handoff_directive_text_is_bounded_and_marked() -> None:
    """The handoff directive names the PR + Thomas, carries its own marker, ≤200."""
    text = _wip_primacy_handoff_directive_text(123)
    assert text.startswith("Hand off PR #123")
    assert "Thomas" in text
    assert "Finish PR #" not in text
    assert len(text) <= _DIRECTIVE_MAX_CHARS


def test_wip_primacy_handoff_directive_text_truncates_overlong() -> None:
    """A pathologically large PR number is truncated to the shared cap."""
    text = _wip_primacy_handoff_directive_text(10 ** 250)
    assert len(text) == _DIRECTIVE_MAX_CHARS
    assert text.endswith("…")


def test_wip_primacy_is_sole_writer_of_both_directive_variants() -> None:
    """Acceptance criterion 3 — no rival mints either WIP directive variant.

    The ``wip_primacy`` gate (in ``task_gates.py``) is the only site that renders
    a WIP directive. The ``Finish PR #`` variant has its own AST chokepoint guard
    (the ``wip_primacy`` entry in ``tests/test_structural_guards.py``); this
    asserts the merge-ready
    ``Hand off PR #`` sibling likewise has no rival writer in the two
    task-generation control-flow modules, and that the two variants keep
    non-overlapping markers so the guards can tell them apart.
    """
    import maestro

    root = Path(maestro.__file__).resolve().parent
    handoff_marker = "Hand off PR #"
    finish_marker = "Finish PR #"
    for rel in ("agent.py", "loop/task_generation.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert handoff_marker not in text, (
            f"rival handoff-directive writer in maestro/{rel}"
        )
    # The three variants are distinguishable: neither marker appears in the
    # other, and the markerless code-red first-act variant carries neither — so
    # the chokepoint guards keep telling all three apart.
    assert finish_marker not in _wip_primacy_handoff_directive_text(7)
    assert handoff_marker not in _wip_primacy_directive_text(7, "CONFLICTING")
    code_red = _wip_primacy_code_red_directive_text(7)
    assert finish_marker not in code_red
    assert handoff_marker not in code_red


def test_block_gate_with_directive_sets_and_persists(
    mem_fs: dict[str, str],
) -> None:
    """A block outcome carrying a directive sets + persists ``next_task_directive``.

    Exercises the ``run_gate_chain`` block-directive branch directly (decoupled
    from the ``wip_primacy`` gate) so the runner behaviour stays covered even if
    the gate is later restructured.
    """
    agent = _StubAgent()
    ctx = _make_ctx(agent=agent)
    gates = [
        _gate(
            "wip_like", "block",
            lambda ctx: GateOutcome(
                detail="pr=#9", log_message="blocked",
                directive="Finish PR #9 yourself.",
            ),
        ),
    ]

    blocked = run_gate_chain(gates, ctx)

    assert blocked is True
    assert ctx.model.next_task_directive == "Finish PR #9 yourself."
    agent.consciousness._mutate_self_model_persisted.assert_called_once()
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["action"] == "blocked"


def test_block_gate_with_directive_preserves_preset_directive(
    mem_fs: dict[str, str],
) -> None:
    """A block gate must not discard a pre-set ``next_task_directive``.

    The block still short-circuits the chain, but its directive is written
    only into an *empty* slot — a pre-set contemplation / fire-or-close
    directive survives (the WIP-primacy gate blocks new work without clobbering
    a decision already made). Guards proposal-092 review finding r1.
    """
    agent = _StubAgent()
    ctx = _make_ctx(
        agent=agent, model=SelfModel(next_task_directive="fire-or-close #7"),
    )
    gates = [
        _gate(
            "wip_like", "block",
            lambda ctx: GateOutcome(
                detail="pr=#9", log_message="blocked",
                directive="Finish PR #9 yourself.",
            ),
        ),
    ]

    blocked = run_gate_chain(gates, ctx)

    assert blocked is True
    # The pre-set directive is preserved, not overwritten by the WIP directive.
    assert ctx.model.next_task_directive == "fire-or-close #7"
    # The live slot keeps the pre-set directive: the block wrote nothing.
    assert agent.live_model.next_task_directive == "fire-or-close #7"
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["action"] == "blocked"


def test_wip_primacy_directive_text_is_bounded() -> None:
    """The rendered directive carries the ``Finish PR #`` marker and is cap-bounded."""
    text = _wip_primacy_directive_text(123, "CONFLICTING")
    assert text.startswith("Finish PR #123")
    assert "CONFLICTING" in text
    assert len(text) <= _DIRECTIVE_MAX_CHARS


def test_wip_primacy_directive_text_truncates_overlong() -> None:
    """A pathologically long blocker is truncated to the shared cap with an ellipsis."""
    text = _wip_primacy_directive_text(1, "X" * 300)
    assert len(text) == _DIRECTIVE_MAX_CHARS
    assert text.endswith("…")


def test_wip_primacy_directive_text_cap_tracks_shared_constant() -> None:
    """The WIP renderers truncate at the shared constant, not a private copy.

    These four renderers used to clip against a module-local
    ``_WIP_PRIMACY_DIRECTIVE_MAX = 200`` whose comment claimed it matched
    ``Consciousness.MAX_DIRECTIVE_CHARS`` while no test bound the two. Patching
    the shared constant down proves each renderer now moves with it.
    """
    with patch.object(audit_checks, "_DIRECTIVE_MAX_CHARS", 120):
        rendered = [
            _wip_primacy_directive_text(1, "X" * 300),
            _wip_primacy_handoff_directive_text(10 ** 250),
            _wip_primacy_code_red_directive_text(10 ** 250),
            _failed_branch_redirect_directive("a" * 300, source_eligible=True),
        ]

    for text in rendered:
        assert len(text) == 120
        assert text.endswith("…")


# ---------------------------------------------------------------------------
# dropped_directive_replay gate (proposal 090)
# ---------------------------------------------------------------------------

_LEDGER = str(_DIR / "dropped_directives.jsonl")

# ``_live_row`` stamps this instant and the two re-offer tests freeze the gate
# clock (``maestro.task_gates.datetime``) to it, so the ``_ENTRY_MAX_AGE`` age
# comparison is deterministic instead of drifting with the wall clock — a fixed
# ``recorded_at`` against the real clock ages the row out exactly 30 days later.
_REPLAY_NOW = datetime(2026, 7, 9, tzinfo=UTC)


def _seed_ledger(mem_fs: dict[str, str], *rows: dict[str, Any]) -> None:
    mem_fs[_LEDGER] = "".join(json.dumps(r) + "\n" for r in rows)


def _live_row(text: str = "fire #123", pr: int = 123) -> dict[str, Any]:
    return {
        "text": text,
        "source": "rule_14_pr_less",
        "pr_numbers": [pr],
        "shas": [],
        "recorded_at": _REPLAY_NOW.isoformat(),
    }


def _agent_with_settled(settled: bool) -> _StubAgent:
    agent = _StubAgent()
    agent.is_pr_settled = MagicMock(  # type: ignore[attr-defined]
        return_value=MagicMock(settled=settled),
    )
    return agent


def test_dropped_directive_replay_reoffers_live_entry(
    mem_fs: dict[str, str],
) -> None:
    """An empty slot + a live entry → the gate re-offers it and consumes it."""
    _seed_ledger(mem_fs, _live_row())
    agent = _agent_with_settled(False)
    ctx = _make_ctx(agent=agent, model=SelfModel())
    with patch("maestro.task_gates.datetime") as mock_dt, patch(
        "maestro.audit_checks.is_branch_merged_into_main", return_value=False,
    ):
        mock_dt.now.return_value = _REPLAY_NOW
        outcome = _dropped_directive_replay_run(ctx)
    assert outcome is not None
    assert outcome.directive == "fire #123"
    # The offered entry was consumed from the ledger.
    assert mem_fs[_LEDGER].strip() == ""


def test_dropped_directive_replay_noop_on_empty_ledger(
    mem_fs: dict[str, str],
) -> None:
    ctx = _make_ctx(agent=_agent_with_settled(False), model=SelfModel())
    assert _dropped_directive_replay_run(ctx) is None


def test_dropped_directive_replay_prunes_settled_entry(
    mem_fs: dict[str, str],
) -> None:
    """A settled-PR entry is pruned, not re-offered."""
    _seed_ledger(mem_fs, _live_row())
    agent = _agent_with_settled(True)
    ctx = _make_ctx(agent=agent, model=SelfModel())
    # Freeze the clock at the stamp so the row is within the age window and the
    # prune is genuinely driven by settlement, not an incidental age-out.
    with patch("maestro.task_gates.datetime") as mock_dt, patch(
        "maestro.audit_checks.is_branch_merged_into_main", return_value=False,
    ):
        mock_dt.now.return_value = _REPLAY_NOW
        outcome = _dropped_directive_replay_run(ctx)
    assert outcome is None
    assert mem_fs[_LEDGER].strip() == ""


def test_dropped_directive_replay_fires_via_chain_when_enabled(
    mem_fs: dict[str, str],
) -> None:
    """Flag on + empty slot: the chain fires the gate, sets the directive, and
    logs exactly one ``directive_set`` activation row."""
    _seed_ledger(mem_fs, _live_row())
    agent = _agent_with_settled(False)
    model = SelfModel()
    config = _make_config(dropped_directive_replay_enabled=True)
    ctx = _make_ctx(config=config, agent=agent, model=model)
    gate = next(g for g in GATE_CHAIN if g.name == "dropped_directive_replay")
    with patch("maestro.task_gates.datetime") as mock_dt, patch(
        "maestro.audit_checks.is_branch_merged_into_main", return_value=False,
    ):
        mock_dt.now.return_value = _REPLAY_NOW
        blocked = run_gate_chain([gate], ctx)
    assert blocked is False
    assert ctx.model.next_task_directive == "fire #123"
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "dropped_directive_replay"
    assert records[0]["action"] == "directive_set"


def test_dropped_directive_replay_disabled_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Flag off: the chain skips the gate — no directive, no ledger read."""
    _seed_ledger(mem_fs, _live_row())
    agent = _agent_with_settled(False)
    model = SelfModel()
    config = _make_config(dropped_directive_replay_enabled=False)
    ctx = _make_ctx(config=config, agent=agent, model=model)
    gate = next(g for g in GATE_CHAIN if g.name == "dropped_directive_replay")
    assert gate.enabled(config) is False
    blocked = run_gate_chain([gate], ctx)
    assert blocked is False
    assert model.next_task_directive is None
    assert _gate_records(mem_fs) == []
    # Ledger untouched — the disabled gate never read or rewrote it.
    assert json.loads(mem_fs[_LEDGER].splitlines()[0])["text"] == "fire #123"


def test_dropped_directive_replay_skipped_on_full_slot(
    mem_fs: dict[str, str],
) -> None:
    """A pre-set directive is never overwritten and the ledger is not touched."""
    _seed_ledger(mem_fs, _live_row())
    agent = _agent_with_settled(False)
    model = SelfModel(next_task_directive="already set")
    config = _make_config(dropped_directive_replay_enabled=True)
    ctx = _make_ctx(config=config, agent=agent, model=model)
    gate = next(g for g in GATE_CHAIN if g.name == "dropped_directive_replay")
    blocked = run_gate_chain([gate], ctx)
    assert blocked is False
    assert model.next_task_directive == "already set"
    assert _gate_records(mem_fs) == []
    assert json.loads(mem_fs[_LEDGER].splitlines()[0])["text"] == "fire #123"


def test_config_toggles_map_to_registry_entries() -> None:
    """Each toggleable gate honours exactly its own config flag."""
    by_name = {gate.name: gate for gate in GATE_CHAIN}
    config = _make_config(
        quiet_skip_enabled=False,
        audit_stall_block_enabled=False,
        investigation_streak_enabled=False,
        investigation_rate_enabled=False,
        promotable_backlog_directive_enabled=False,
        wip_primacy_enabled=False,
    )

    assert by_name["wip_primacy"].enabled(config) is False
    assert by_name["quiet_skip"].enabled(config) is False
    assert by_name["audit_stall"].enabled(config) is False
    assert by_name["investigation_streak"].enabled(config) is False
    assert by_name["investigation_rate"].enabled(config) is False
    assert by_name["promotable_backlog_directive"].enabled(config) is False
    # Gates with no toggle stay enabled regardless.
    assert by_name["cost_risk"].enabled(config) is True
    assert by_name["escalation_bridge"].enabled(config) is True
    assert by_name["instrument_consumption"].enabled(config) is True
    assert by_name["integration_cadence"].enabled(config) is True
    assert by_name["branchless_streak"].enabled(config) is True
    assert by_name["partial_construction"].enabled(config) is True
    assert by_name["self_directed_success"].enabled(config) is True
    assert by_name["forced_read"].enabled(config) is True
    assert by_name["comfort_cluster"].enabled(config) is True


def test_config_toggles_enabled_when_flags_on() -> None:
    by_name = {gate.name: gate for gate in GATE_CHAIN}
    config = _make_config(
        quiet_skip_enabled=True,
        audit_stall_block_enabled=True,
        investigation_streak_enabled=True,
        investigation_rate_enabled=True,
        promotable_backlog_directive_enabled=True,
        wip_primacy_enabled=True,
    )

    assert by_name["wip_primacy"].enabled(config) is True
    assert by_name["quiet_skip"].enabled(config) is True
    assert by_name["audit_stall"].enabled(config) is True
    assert by_name["investigation_streak"].enabled(config) is True
    assert by_name["investigation_rate"].enabled(config) is True
    assert by_name["promotable_backlog_directive"].enabled(config) is True


# ---------------------------------------------------------------------------
# untouched_first_target — steer the opening selection (rule-19 complement)
# ---------------------------------------------------------------------------


def _seed(mem_fs: dict[str, str], name: str, *records: Any) -> None:
    """Write JSONL *records* under ``_DIR / name`` in the in-memory fs."""
    mem_fs[str(_DIR / name)] = "".join(
        json.dumps(record) + "\n" for record in records
    )


# --- pure diversion core -----------------------------------------------------


def test_divert_rewrites_directive_naming_touched_artifact() -> None:
    result = _divert_touched_directive(
        "Remediate PR #1234: reconcile-or-close the branch",
        frozenset({"#1234"}),
        ["#42", "#7"],
    )
    assert result is not None
    # Names the lowest-numbered untouched target, flags the disposed one.
    assert "#7" in result
    assert "#42" not in result
    assert "#1234" in result
    assert len(result) <= _DIRECTIVE_MAX_CHARS


def test_divert_cap_tracks_shared_constant() -> None:
    """The rewrite truncates at the shared constant, with an ellipsis.

    This site used to hard-clip at a bare ``[:200]`` — the only directive
    truncation in the module that dropped its tail with no ellipsis, and
    production-reachable because ``disposed_list`` is a comma-joined issue
    list that grows with the touched set. Patching the constant down proves it
    now moves with the cap.
    """
    disposed = [f"#{n}" for n in range(1000, 1060)]
    directive = "Remediate " + ", ".join(disposed)

    with patch.object(audit_checks, "_DIRECTIVE_MAX_CHARS", 150):
        result = _divert_touched_directive(
            directive, frozenset(disposed), ["#7"],
        )

    assert result is not None
    assert len(result) == 150
    assert result.endswith("…")


def test_divert_noop_when_directive_target_untouched() -> None:
    assert _divert_touched_directive(
        "Build the feature for #99",
        frozenset({"#1234"}),
        ["#7"],
    ) is None


def test_divert_noop_when_no_untouched_target_exists() -> None:
    # Directive names a touched artifact, but every candidate is itself
    # touched (or identical to a named target) — nothing fresh to divert to.
    assert _divert_touched_directive(
        "Remediate PR #1234",
        frozenset({"#1234", "#7"}),
        ["#7", "#1234"],
    ) is None


# --- gate run with a mocked touched set --------------------------------------


def test_gate_rewrites_directive_with_mocked_touched_set(
    mem_fs: dict[str, str],
) -> None:
    model = SelfModel(next_task_directive="Remediate PR #1234 once more")
    ctx = _make_ctx(model=model)
    with patch(
        "maestro.task_gates._build_touched_artifact_ids",
        return_value={"#1234"},
    ), patch(
        "maestro.task_gates._build_untouched_candidate_ids",
        return_value=["#7"],
    ):
        outcome = check_untouched_first_target(ctx)

    assert outcome is not None
    assert outcome.directive is not None
    assert "#7" in outcome.directive
    assert outcome.journal_trigger == "untouched_first_target_gate"
    # The gate itself is pure — the runner, not the gate, mutates the model.
    assert model.next_task_directive == "Remediate PR #1234 once more"


def test_gate_noop_when_directive_target_untouched(
    mem_fs: dict[str, str],
) -> None:
    model = SelfModel(next_task_directive="Build #99")
    ctx = _make_ctx(model=model)
    with patch(
        "maestro.task_gates._build_touched_artifact_ids",
        return_value={"#1234"},
    ), patch(
        "maestro.task_gates._build_untouched_candidate_ids",
        return_value=["#7"],
    ):
        assert check_untouched_first_target(ctx) is None
    assert model.next_task_directive == "Build #99"


def test_gate_noop_when_no_untouched_target(mem_fs: dict[str, str]) -> None:
    model = SelfModel(next_task_directive="Remediate PR #1234")
    ctx = _make_ctx(model=model)
    with patch(
        "maestro.task_gates._build_touched_artifact_ids",
        return_value={"#1234"},
    ), patch(
        "maestro.task_gates._build_untouched_candidate_ids",
        return_value=[],
    ):
        assert check_untouched_first_target(ctx) is None
    assert model.next_task_directive == "Remediate PR #1234"


def test_gate_noop_when_directive_empty() -> None:
    ctx = _make_ctx(model=SelfModel(next_task_directive=None))
    assert check_untouched_first_target(ctx) is None


def test_gate_noop_when_no_touched_artifacts(mem_fs: dict[str, str]) -> None:
    # No ledger files seeded — the touched set is empty, so strict no-op.
    model = SelfModel(next_task_directive="Remediate PR #1234")
    ctx = _make_ctx(model=model)
    assert check_untouched_first_target(ctx) is None
    assert model.next_task_directive == "Remediate PR #1234"


def test_gate_end_to_end_rewrites_from_real_ledgers(
    mem_fs: dict[str, str],
) -> None:
    _seed(
        mem_fs, "stand_down_verdicts.jsonl",
        {"pr": 1234, "head_sha": "abc", "recorded_at": "t"},
    )
    memory = _StubMemory(recent=[_task(goal="Investigate open issue #7")])
    agent = _StubAgent(memory=memory)
    model = SelfModel(
        next_task_directive="Remediate PR #1234: reconcile-or-close",
    )
    ctx = _make_ctx(agent=agent, model=model)

    outcome = check_untouched_first_target(ctx)

    assert outcome is not None
    assert outcome.directive is not None
    assert "#7" in outcome.directive
    assert "#1234" in outcome.directive


# --- touched / untouched builders --------------------------------------------


def test_build_touched_unions_ledgers_and_history(
    mem_fs: dict[str, str],
) -> None:
    _seed(
        mem_fs, "stand_down_verdicts.jsonl",
        {"pr": 1234, "head_sha": "s", "recorded_at": "t"},
        {"pr": 0, "head_sha": "s", "recorded_at": "t"},      # non-positive skip
        {"pr": True, "head_sha": "s", "recorded_at": "t"},   # bool skip
    )
    _seed(
        mem_fs, "evolution_plans.jsonl",
        {"status": "obsolete", "title": "Retire cure for #55"},
        {"status": "pending_review", "title": "Live plan #66"},  # not obsolete
    )
    memory = _StubMemory(recent=[
        # Task prose never disposes a PR, even narrating a close (#4070).
        _task(task_id="c1", result_summary="Closed PR #77 after conflict"),
        _task(task_id="c2", goal="ordinary work on #88"),
    ])
    agent = _StubAgent(memory=memory)
    ctx = _make_ctx(agent=agent)

    touched = _build_touched_artifact_ids(ctx)

    assert touched == {"#1234", "#55"}


def test_build_touched_artifact_ids_excludes_release_only_pr(
    mem_fs: dict[str, str],
) -> None:
    """A release row alone never marks a PR disposed (#4314)."""
    release_stand_downs(4242, JsonlStandDownVerdictLedger(_DIR), now="t")
    ctx = _make_ctx()

    assert "#4242" not in _build_touched_artifact_ids(ctx)


def test_gate_does_not_rewrite_directive_naming_release_only_pr(
    mem_fs: dict[str, str],
) -> None:
    release_stand_downs(4242, JsonlStandDownVerdictLedger(_DIR), now="t")
    memory = _StubMemory(recent=[_task(goal="Investigate open issue #7")])
    agent = _StubAgent(memory=memory)
    model = SelfModel(next_task_directive="Remediate PR #4242")
    ctx = _make_ctx(agent=agent, model=model)

    assert check_untouched_first_target(ctx) is None
    assert model.next_task_directive == "Remediate PR #4242"


def _remediation_history() -> _StubMemory:
    goal = _compose_pr_remediation_prompt(
        pr_number=4001, pr_title="Fix thing", plan=None, plan_id="abc",
        pr_body="Fixes #3990", pr_url="", problems=["merge conflict"],
    )
    return _StubMemory(recent=[
        _task(task_id="r1", goal=goal, result_summary="pushed fix, awaiting CI"),
        _task(task_id="r2", goal="Investigate #3975 flaky test"),
    ])


def test_closed_lineage_ignores_remediation_prompt_goal(
    mem_fs: dict[str, str],
) -> None:
    """#4070: the remediation preamble's close wording never disposes its PR."""
    ctx = _make_ctx(agent=_StubAgent(memory=_remediation_history()))

    touched = _build_touched_artifact_ids(ctx)

    assert touched == set()


def test_closed_lineage_ignores_close_wording_in_goal(
    mem_fs: dict[str, str],
) -> None:
    """A goal narrating a close is instruction, not a disposal outcome."""
    memory = _StubMemory(recent=[_task(goal="Closed PR #77 after conflict")])
    ctx = _make_ctx(agent=_StubAgent(memory=memory))

    assert _build_touched_artifact_ids(ctx) == set()


def test_untouched_gate_noop_for_live_remediation_pr(
    mem_fs: dict[str, str],
) -> None:
    model = SelfModel(
        next_task_directive=(
            "Finish PR #4001 yourself — clear the CONFLICTING state"
        ),
    )
    ctx = _make_ctx(agent=_StubAgent(memory=_remediation_history()), model=model)

    assert check_untouched_first_target(ctx) is None


def test_touched_ignores_push_owed_outcome_clause(
    mem_fs: dict[str, str],
) -> None:
    """The loop-authored ``[push-owed] ... not closed`` clause is no disposal."""
    summary = f"PR #4001; {_PUSH_OWED_CLAUSE}; {_PUSH_OWED_UNVERIFIED_CLAUSE}"
    memory = _StubMemory(recent=[_task(result_summary=summary)])
    ctx = _make_ctx(agent=_StubAgent(memory=memory))

    assert _build_touched_artifact_ids(ctx) == set()


def test_build_touched_includes_pr_close_deeds(mem_fs: dict[str, str]) -> None:
    agent = _StubAgent()
    agent.deed_ledger.record(DeedType.PR_CLOSE, 91, "closed")
    agent.deed_ledger.record(DeedType.PR_CLOSE, None, "no pr")
    agent.deed_ledger.record(DeedType.HANDOFF_DELIVERED, 92, "handoff")
    agent.deed_ledger.record(DeedType.OWNED_PRUNE, 93, "prune")
    ctx = _make_ctx(agent=agent)

    touched = _build_touched_artifact_ids(ctx)

    assert touched == {"#91"}


def test_obsolete_plan_touched_ids_reads_real_plan_fields(
    mem_fs: dict[str, str],
) -> None:
    """Citations in ``proposed_changes`` / ``obsolete_reason`` are collected."""
    _seed(
        mem_fs, "evolution_plans.jsonl",
        # Citation lives only in the proposed-changes list.
        {"status": "obsolete", "proposed_changes": ["Retire the cure for #55"]},
        # Citation lives only in the disposal stamp.
        {"status": "obsolete", "obsolete_reason": "superseded by #66"},
        # A legacy row whose list field is a bare scalar, and a null reason.
        {"status": "obsolete", "proposed_changes": "#77", "obsolete_reason": None},
        # Target issue carried only by the structured linkage field.
        {"status": "obsolete", "issue_number": 99},
        # Non-int / non-positive linkages are ignored rather than stamped.
        {"status": "obsolete", "issue_number": True},
        {"status": "obsolete", "issue_number": 0},
        {"status": "obsolete", "issue_number": "12"},
        # Live plan — never collected.
        {"status": "pending_review", "rationale": "Live plan #88"},
        {"status": "pending_review", "issue_number": 89},
    )

    assert _obsolete_plan_touched_ids(_DIR) == {"#55", "#66", "#77", "#99"}


def test_obsolete_plan_touched_ids_skips_door_refused_mints(
    mem_fs: dict[str, str],
) -> None:
    """#4385 — a mint the submission door refused disposed of nothing."""
    _seed(
        mem_fs, "evolution_plans.jsonl",
        {"status": "obsolete", "refused_at_submission": True,
         "issue_number": 4316, "title": "Fix #4316"},
        {"status": "obsolete", "issue_number": 4317, "title": "Fix #4317"},
    )

    assert _obsolete_plan_touched_ids(_DIR) == {"#4317"}


def test_gate_noop_for_directive_citing_door_refused_mint(
    mem_fs: dict[str, str],
) -> None:
    _seed(
        mem_fs, "evolution_plans.jsonl",
        {"status": "obsolete", "refused_at_submission": True,
         "issue_number": 4316, "title": "Fix #4316"},
    )
    model = SelfModel(next_task_directive="Fix #4316 bug in gate")
    ctx = _make_ctx(model=model)
    with patch(
        "maestro.task_gates._build_untouched_candidate_ids",
        return_value=["#4400"],
    ):
        assert check_untouched_first_target(ctx) is None
    assert model.next_task_directive == "Fix #4316 bug in gate"


def test_obsolete_plan_scan_reads_only_declared_plan_fields() -> None:
    """Every key the helper reads is a real ``EvolutionPlan`` field."""
    source = Path(task_gates.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_obsolete_plan_touched_ids"
    )
    read_keys: set[str] = set()
    for node in ast.walk(func):
        # Direct ``rec.get("field")`` reads.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            read_keys.add(node.args[0].value)
        # Key names iterated as a literal tuple of field names — only the
        # tuple a loop actually iterates, not any string tuple in the body.
        if isinstance(node, ast.For | ast.comprehension) and isinstance(
            node.iter, ast.Tuple
        ):
            read_keys.update(
                elt.value for elt in node.iter.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            )
    declared = {f.name for f in fields(EvolutionPlan)}

    assert read_keys <= declared
    assert {"proposed_changes", "obsolete_reason", "issue_number"} <= read_keys


def test_build_untouched_candidates_excludes_touched(
    mem_fs: dict[str, str],
) -> None:
    memory = _StubMemory(recent=[_task(goal="work on #42 and #1234")])
    agent = _StubAgent(memory=memory)
    model = SelfModel(
        pending_pr_requests=[PendingPRRequest(directive="Remediate PR #99")],
    )
    ctx = _make_ctx(agent=agent, model=model)

    candidates = _build_untouched_candidate_ids(ctx, frozenset({"#1234"}))

    assert candidates == ["#42", "#99"]


# --- runner integration: rewrites flag + one activation row per firing --------


def _untouched_gate() -> TaskGate:
    return next(g for g in GATE_CHAIN if g.name == "untouched_first_target")


def test_rewrite_gate_runs_on_nonempty_directive_via_chain(
    mem_fs: dict[str, str],
) -> None:
    _seed(
        mem_fs, "stand_down_verdicts.jsonl",
        {"pr": 1234, "head_sha": "s", "recorded_at": "t"},
    )
    memory = _StubMemory(recent=[_task(goal="pick up #7")])
    agent = _StubAgent(memory=memory)
    model = SelfModel(next_task_directive="Remediate PR #1234")
    ctx = _make_ctx(agent=agent, model=model)

    blocked = run_gate_chain([_untouched_gate()], ctx)

    assert blocked is False
    assert ctx.model.next_task_directive is not None
    assert "#7" in ctx.model.next_task_directive
    agent.consciousness._mutate_self_model_persisted.assert_called_once()
    agent.journal.record_event.assert_called_once()
    # Exactly one activation row per firing.
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "untouched_first_target"
    assert records[0]["action"] == "directive_set"


def test_rewrite_gate_noop_writes_no_activation_row(
    mem_fs: dict[str, str],
) -> None:
    # No ledgers -> touched empty -> strict no-op even inside the chain.
    memory = _StubMemory(recent=[_task(goal="pick up #7")])
    agent = _StubAgent(memory=memory)
    model = SelfModel(next_task_directive="Remediate PR #1234")
    ctx = _make_ctx(agent=agent, model=model)

    blocked = run_gate_chain([_untouched_gate()], ctx)

    assert blocked is False
    assert model.next_task_directive == "Remediate PR #1234"
    assert _gate_records(mem_fs) == []


def test_rewrites_flag_exempts_gate_from_empty_slot_guard(
    mem_fs: dict[str, str],
) -> None:
    """A ``rewrites=True`` directive gate runs on a non-empty directive."""
    seen: list[str] = []

    def _run(ctx: GateContext) -> GateOutcome:
        seen.append(ctx.model.next_task_directive or "")
        return GateOutcome(detail="d", log_message="m", directive="rewritten")

    gate = TaskGate(
        name="rw", kind="directive", enabled=_always_enabled,
        run=_run, rewrites=True,
    )
    ctx = _make_ctx(model=SelfModel(next_task_directive="pre-set"))

    run_gate_chain([gate], ctx)

    assert seen == ["pre-set"]  # ran despite the slot being full
    assert ctx.model.next_task_directive == "rewritten"


# --- jsonl reader corrupted-tail tolerance -----------------------------------


def test_read_jsonl_records_tolerates_corruption(
    mem_fs: dict[str, str],
) -> None:
    from maestro.task_gates import _read_jsonl_records

    mem_fs[str(_DIR / "x.jsonl")] = (
        '{"a": 1}\n'   # good
        "\n"            # blank -> skipped
        "not json\n"   # malformed -> skipped
        "[1, 2]\n"     # non-dict -> skipped
        '{"b": 2}\n'
    )
    records = _read_jsonl_records(_DIR / "x.jsonl")
    assert records == [{"a": 1}, {"b": 2}]


def test_read_jsonl_records_missing_file() -> None:
    from maestro.task_gates import _read_jsonl_records

    assert _read_jsonl_records(_DIR / "absent.jsonl") == []


def test_read_jsonl_records_oserror(mem_fs: dict[str, str]) -> None:
    from maestro.task_gates import _read_jsonl_records

    mem_fs[str(_DIR / "y.jsonl")] = '{"a": 1}\n'
    with patch("maestro.task_gates.open", side_effect=OSError, create=True):
        assert _read_jsonl_records(_DIR / "y.jsonl") == []


# ---------------------------------------------------------------------------
# stale_obsolete_directive_guard — clear a target-empty obsolete-lever directive
# ---------------------------------------------------------------------------


_OBSOLETE_DIRECTIVE = "Fire mark_plan_obsolete on the oldest stale pending plan"


def _stale_obsolete_gate() -> TaskGate:
    return next(
        g for g in GATE_CHAIN if g.name == "stale_obsolete_directive_guard"
    )


def test_stale_obsolete_guard_clears_directive_when_target_none(
    mem_fs: dict[str, str],
) -> None:
    """Obsolete directive + exhausted selector -> cleared, one activation row."""
    model = SelfModel(next_task_directive=_OBSOLETE_DIRECTIVE)
    ctx = _make_ctx(model=model)
    with patch(
        "maestro.lever_capture.select_distinct_obsolete_target",
        return_value=None,
    ):
        blocked = run_gate_chain([_stale_obsolete_gate()], ctx)

    assert blocked is False
    assert ctx.model.next_task_directive is None
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "stale_obsolete_directive_guard"
    assert records[0]["action"] == "directive_set"


def test_stale_obsolete_guard_preserves_directive_when_target_live(
    mem_fs: dict[str, str],
) -> None:
    """A live selector id leaves the directive unchanged and writes no row."""
    model = SelfModel(next_task_directive=_OBSOLETE_DIRECTIVE)
    ctx = _make_ctx(model=model)
    with patch(
        "maestro.lever_capture.select_distinct_obsolete_target",
        return_value=MagicMock(name="live_plan"),
    ):
        blocked = run_gate_chain([_stale_obsolete_gate()], ctx)

    assert blocked is False
    assert model.next_task_directive == _OBSOLETE_DIRECTIVE
    assert _gate_records(mem_fs) == []


def test_stale_obsolete_guard_ignores_non_obsolete_directive(
    mem_fs: dict[str, str],
) -> None:
    """A directive that does not name the lever is never touched, no selector."""
    directive = "Remediate PR #1234: reconcile-or-close the branch"
    model = SelfModel(next_task_directive=directive)
    ctx = _make_ctx(model=model)
    selector = MagicMock(name="select_distinct_obsolete_target")
    with patch(
        "maestro.lever_capture.select_distinct_obsolete_target", selector,
    ):
        blocked = run_gate_chain([_stale_obsolete_gate()], ctx)

    assert blocked is False
    assert model.next_task_directive == directive
    assert _gate_records(mem_fs) == []
    selector.assert_not_called()


def test_stale_obsolete_guard_noop_on_empty_directive() -> None:
    """An empty slot is a strict no-op — no selector read, no outcome."""
    ctx = _make_ctx(model=SelfModel(next_task_directive=None))
    assert _stale_obsolete_directive_run(ctx) is None


# ---------------------------------------------------------------------------
# investigation_observable_gate — post-call task-selection actuator
# ---------------------------------------------------------------------------

_FLEE_GOAL = (
    "Investigate how the escalation bridge selects a handoff and write a "
    "markdown document describing the mechanism."
)
_OBSERVABLE_GOAL = (
    "Investigate the escalation cadence and write a markdown report of the "
    "gate-activation counts read from gate_activations.jsonl."
)
_NET_DIFF_GOAL = (
    "Investigate how run_gate_chain dispatches gates, then edit "
    "maestro/task_gates.py to reduce nesting."
)
# Unified-frame goals (Step 2): ``is_investigation_observable_flee`` now reads
# the owner's single frame predicate (``classify_task_shape``), so a check/scan-
# framed flee fires while an ``understand``-framed goal (classified ``other``)
# no longer does.
_CHECK_FLEE_GOAL = (
    "Check how the escalation bridge selects a handoff and write a markdown "
    "document describing it."
)
_SCAN_FLEE_GOAL = (
    "Scan the loop dispatch and produce a new report document about it."
)
_UNDERSTAND_GOAL = (
    "Understand how the escalation bridge works and write a markdown document "
    "describing the mechanism."
)
# An investigation-framed goal that, with a kept branch, is an owner-stamped
# COSTUME — the discharge-evidence arm's trigger.
_COSTUME_FRAME_GOAL = "Investigate the throttle stall path"


def test_investigation_observable_classifier_branches() -> None:
    """The pure classifier fires only on investigation + artifact + no observable."""
    # Fire: investigation-framed, commits to a new artifact, no observable.
    assert is_investigation_observable_flee(_FLEE_GOAL) is True
    # Empty goal: strict no-op.
    assert is_investigation_observable_flee("") is False
    # Not investigation-framed (construction): False.
    assert is_investigation_observable_flee(
        "Add a task_id field to IterationUsage in maestro/sdk.py",
    ) is False
    # Investigation but no artifact-creation commitment (net diff): False.
    assert is_investigation_observable_flee(_NET_DIFF_GOAL) is False
    # Investigation + artifact but names an observable signal: False.
    assert is_investigation_observable_flee(_OBSERVABLE_GOAL) is False


def test_investigation_observable_creation_verb_no_overmatch() -> None:
    """Verb suffixes are bounded: lookalike nouns don't trip artifact-creation.

    Regression for the ``\\w*`` over-match nit (PR #3429): "authorization",
    "creative" and "product" are not creation verbs, so an ordinary
    investigation goal that merely mentions them near an artifact noun must not
    be classified as an additive flee — while a genuine creation verb ("write")
    in the same shape still fires.
    """
    # Over-match words paired with an artifact noun, investigation-framed, no
    # observable — the exact conjunction the gate keys on. Must NOT fire.
    assert is_investigation_observable_flee(
        "Investigate the config loader's authorization and note the analysis",
    ) is False
    assert is_investigation_observable_flee(
        "Examine the creative rendering path and summarize the analysis "
        "in a report",
    ) is False
    assert is_investigation_observable_flee(
        "Investigate the product catalog module and record it in a document",
    ) is False
    # Control: same investigation + artifact-noun shape with a genuine creation
    # verb ("write") still fires — the fix does not lose true positives.
    assert is_investigation_observable_flee(
        "Investigate the config loader's auth path and write the analysis "
        "in a report",
    ) is True


def _live_consciousness(model: SelfModel) -> MagicMock:
    """A consciousness whose ``mutate_self_model`` runs on *model* as live."""
    consciousness = MagicMock()
    consciousness.mutate_self_model.side_effect = lambda fn: fn(model)
    return consciousness


def test_investigation_observable_gate_fires(mem_fs: dict[str, str]) -> None:
    """Fire: empty slot + flee goal sets the directive and logs exactly one row."""
    model = SelfModel(next_task_directive=None)
    consciousness = _live_consciousness(model)

    fired = investigation_observable_gate(
        [_FLEE_GOAL], _make_config(), consciousness, [], _ga(),
    )

    assert fired is True
    assert model.next_task_directive is not None
    assert "named observable" in model.next_task_directive
    assert "not a new artifact file" in model.next_task_directive
    consciousness.mutate_self_model.assert_called_once()
    consciousness.save_self_model.assert_not_called()
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "investigation_observable"
    assert records[0]["action"] == "directive_set"
    assert records[0]["detail"] == _FLEE_GOAL


def test_investigation_observable_gate_silent_when_observable_named(
    mem_fs: dict[str, str],
) -> None:
    """No-fire: a goal naming an observable signal leaves slot + ledger untouched."""
    model = SelfModel(next_task_directive=None)
    consciousness = _live_consciousness(model)

    fired = investigation_observable_gate(
        [_OBSERVABLE_GOAL], _make_config(), consciousness, [], _ga(),
    )

    assert fired is False
    assert model.next_task_directive is None
    consciousness.save_self_model.assert_not_called()
    assert _gate_records(mem_fs) == []


def test_investigation_observable_gate_silent_on_net_diff(
    mem_fs: dict[str, str],
) -> None:
    """No-fire: a net-diff-to-existing-file task is not an artifact flee."""
    model = SelfModel(next_task_directive=None)
    consciousness = _live_consciousness(model)

    fired = investigation_observable_gate(
        [_NET_DIFF_GOAL], _make_config(), consciousness, [], _ga(),
    )

    assert fired is False
    assert model.next_task_directive is None
    consciousness.save_self_model.assert_not_called()
    assert _gate_records(mem_fs) == []


def test_investigation_observable_gate_flag_off(mem_fs: dict[str, str]) -> None:
    """Flag off: never fires, never writes the directive or a ledger row."""
    model = SelfModel(next_task_directive=None)
    consciousness = _live_consciousness(model)
    config = _make_config(investigation_observable_gate_enabled=False)

    fired = investigation_observable_gate(
        [_FLEE_GOAL], config, consciousness, [], _ga(),
    )

    assert fired is False
    assert model.next_task_directive is None
    consciousness.save_self_model.assert_not_called()
    assert _gate_records(mem_fs) == []


def test_investigation_observable_gate_preserves_occupied_slot(
    mem_fs: dict[str, str],
) -> None:
    """Occupied slot: a flee goal never overwrites a pre-set directive; no row."""
    model = SelfModel(next_task_directive="Finish PR #42 yourself")
    consciousness = _live_consciousness(model)

    fired = investigation_observable_gate(
        [_FLEE_GOAL], _make_config(), consciousness, [], _ga(),
    )

    assert fired is False
    assert model.next_task_directive == "Finish PR #42 yourself"
    consciousness.save_self_model.assert_not_called()
    assert _gate_records(mem_fs) == []


def test_investigation_observable_flee_uses_owner_frame() -> None:
    """The flee classifier reads the owner's single frame predicate (Step 2).

    The frame set is now exactly ``classify_task_shape``'s: a ``check``/``scan``-
    framed goal that flees to a new artifact fires, while an ``understand``-framed
    goal — dropped from the investigation set — no longer does.
    """
    # check/scan-framed flees now fire (gained from the unified frame set).
    assert is_investigation_observable_flee(_CHECK_FLEE_GOAL) is True
    assert is_investigation_observable_flee(_SCAN_FLEE_GOAL) is True
    # An ``understand``-framed goal is no longer investigation-framed → no fire.
    assert is_investigation_observable_flee(_UNDERSTAND_GOAL) is False


def test_investigation_observable_gate_discharge_arm_fires(
    mem_fs: dict[str, str],
) -> None:
    """Discharge arm: empty slot, no flee goal, latest record a COSTUME → fires.

    Reads the owner verdict via ``measure_frame_discharge_costumes`` over the
    single tail record, fills the slot with the distinct discharge directive, and
    logs exactly one row naming the costume ``task_id``.
    """
    model = SelfModel(next_task_directive=None)
    consciousness = _live_consciousness(model)
    recent = [_task("t-cost", _COSTUME_FRAME_GOAL, branch="maestro/task/x")]

    fired = investigation_observable_gate(
        [], _make_config(), consciousness, recent, _ga(),
    )

    assert fired is True
    assert model.next_task_directive is not None
    assert "investigation costume" in model.next_task_directive
    consciousness.mutate_self_model.assert_called_once()
    consciousness.save_self_model.assert_not_called()
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "investigation_observable"
    assert records[0]["action"] == "directive_set"
    assert records[0]["detail"] == "discharge-costume: t-cost"


def test_investigation_observable_gate_discharge_arm_preserves_occupied_slot(
    mem_fs: dict[str, str],
) -> None:
    """Occupied slot: a COSTUME record never overwrites a pre-set directive."""
    model = SelfModel(next_task_directive="Finish PR #42 yourself")
    consciousness = _live_consciousness(model)
    recent = [_task("t-cost", _COSTUME_FRAME_GOAL, branch="maestro/task/x")]

    fired = investigation_observable_gate(
        [], _make_config(), consciousness, recent, _ga(),
    )

    assert fired is False
    assert model.next_task_directive == "Finish PR #42 yourself"
    consciousness.save_self_model.assert_not_called()
    assert _gate_records(mem_fs) == []


def test_investigation_observable_gate_discharge_arm_self_extinguishes(
    mem_fs: dict[str, str],
) -> None:
    """Self-extinguishing: a clean latest investigation (branch None) silences it.

    The arm reads only the tail record, so an earlier COSTUME followed by an
    honest ``artifacts/``-only investigation (no kept branch) falls silent.
    """
    model = SelfModel(next_task_directive=None)
    consciousness = _live_consciousness(model)
    recent = [
        _task("t-cost", _COSTUME_FRAME_GOAL, branch="maestro/task/x"),
        _task("t-clean", _COSTUME_FRAME_GOAL, branch=None),
    ]

    fired = investigation_observable_gate(
        [], _make_config(), consciousness, recent, _ga(),
    )

    assert fired is False
    assert model.next_task_directive is None
    consciousness.save_self_model.assert_not_called()
    assert _gate_records(mem_fs) == []


def test_investigation_observable_gate_discharge_arm_silent_on_construction(
    mem_fs: dict[str, str],
) -> None:
    """A latest honest construction (construction-framed + branch) is no costume."""
    model = SelfModel(next_task_directive=None)
    consciousness = _live_consciousness(model)
    recent = [
        _task("t-build", "Build a helper to cache results", branch="maestro/task/y"),
    ]

    fired = investigation_observable_gate(
        [], _make_config(), consciousness, recent, _ga(),
    )

    assert fired is False
    assert model.next_task_directive is None
    consciousness.save_self_model.assert_not_called()
    assert _gate_records(mem_fs) == []


# ---------------------------------------------------------------------------
# Structural guard — whole-directive truncation has one owner
# ---------------------------------------------------------------------------


def _literal_slice_target(node: ast.Subscript) -> str | None:
    """Return the sliced identifier for a ``value[:LITERAL]`` subscript.

    ``directive[:200]`` yields ``"directive"`` and
    ``model.next_task_directive[:200]`` yields ``"next_task_directive"``; a
    slice of an unnamed expression — ``str(x)[:200]``, ``f"..."[:200]`` —
    yields ``""``. ``None`` when *node* is not a literal slice at all, so a
    caller can tell "not a truncation" from "truncation of an unnamed value".
    """
    if not isinstance(node.slice, ast.Slice):
        return None
    if not isinstance(node.slice.upper, ast.Constant):
        return None
    target = node.value
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _hand_rolled_directive_truncations(source: str) -> list[str]:
    """Return descriptions of hand-rolled whole-directive truncations in *source*.

    Three shapes, all of which this module carried before the cap was collapsed
    onto :func:`~maestro.audit_checks.truncate_directive`:

    * an ellipsised clip (``text[:CAP - 1] + "…"``) anywhere in the module;
    * a literal slice of a *directive-named* value (``directive[:200]``,
      ``model.next_task_directive[:200]``) anywhere in the module — the shape
      ``_instrument_consumption_run`` and ``_comfort_cluster_run`` would regress
      to, and neither of those carries "directive" in its function name, so the
      renderer-scoped sweep below would miss them;
    * a literal slice of a differently-named local (``rewritten[:200]``) inside
      a function whose name names a directive — the shape
      :func:`_divert_touched_directive` shipped.

    Detail strings written to gate-activation records legitimately clip at a
    literal, so both bare-slice sweeps are keyed to a directive-named value or a
    directive-named renderer rather than every slice in the module.
    """
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Add)
            and isinstance(node.right, ast.Constant)
            and node.right.value == "…"
        ):
            found.append(f"ellipsis-clip at line {node.lineno}")
        if isinstance(node, ast.Subscript):
            name = _literal_slice_target(node)
            if name is not None and "directive" in name.lower():
                found.append(
                    f"literal slice of directive-named value {name!r} "
                    f"at line {node.lineno}",
                )
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if "directive" not in func.name:
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Subscript):
                continue
            name = _literal_slice_target(node)
            # A directive-named value inside a directive-named renderer is
            # already reported by the module-wide sweep above; only the
            # differently-named local needs reporting here.
            if name is not None and "directive" not in name.lower():
                found.append(f"literal slice in {func.name} at line {node.lineno}")
    return found


def test_no_hand_rolled_directive_truncation_survives() -> None:
    """Every whole-directive truncation in ``task_gates`` routes through the helper.

    The invariant :func:`~maestro.audit_checks.truncate_directive`'s docstring
    claims. Without it, a bridge can silently re-acquire a private cap and go
    on clipping at the old length after ``Consciousness.MAX_DIRECTIVE_CHARS``
    is raised — the exact drift this module already shipped twice.
    """
    source = Path(task_gates.__file__).read_text(encoding="utf-8")

    assert _hand_rolled_directive_truncations(source) == []
    # Not vacuous: the helper is the live truncation owner here.
    assert source.count("truncate_directive(") >= 5


def test_hand_rolled_directive_truncation_detector_bites() -> None:
    """The guard flags all three regressed shapes, and clears an accepted one."""
    regressed = _hand_rolled_directive_truncations(
        '''
_CAP = 200


def _render_directive(x: str) -> str:
    if len(x) > _CAP:
        x = x[: _CAP - 1] + "…"
    return x[:200]


def _comfort_cluster_run(model, compose):
    directive = compose()
    model.next_task_directive = model.next_task_directive[:200]
    return directive[:200]


def log_detail(x: str) -> str:
    return x[:200]
''',
    )
    joined = "\n".join(regressed)

    assert len(regressed) == 4
    assert "ellipsis-clip" in joined
    # The renderer-scoped sweep catches a differently-named local.
    assert "literal slice in _render_directive" in joined
    # The module-wide sweep catches both directive-named shapes in a function
    # whose own name says nothing about directives — the #2911 regression.
    assert "directive-named value 'directive'" in joined
    assert "directive-named value 'next_task_directive'" in joined
    # A detail clip of a non-directive value outside a renderer is not swept up.
    assert "log_detail" not in joined

    assert _hand_rolled_directive_truncations(
        "def _render_directive(x: str) -> str:\n"
        "    return truncate_directive(x)\n",
    ) == []


def test_wip_primacy_code_red_directive_names_the_failing_checks(
    mem_fs: dict[str, str],
) -> None:
    """Step 11 — under the binding flag the red directive quotes the failing checks.

    The names are the ones the code host already reports, routed through the
    CI-truth owner: they land in the directive as a bounded ``(failing: …)``
    clause and in the activation row's ``detail`` / log message, so the act
    phase does not have to go looking for which gate is red. Neither WIP marker
    appears, so the chokepoint guards still partition the three variants.
    """
    agent = _agent_with_focus(
        TendOwnedPR(pr_number=3264, owned_blocker="CI_FAILING"),
        readiness_by_pr={
            3264: _readiness(
                pr_number=3264, ci_state="FAILING", ready_and_waiting=False,
            ),
        },
        check_runs=(
            CheckRun(name="quality", conclusion="failure"),
            CheckRun(name="browser", conclusion="failure"),
        ),
    )
    model = SelfModel()
    ctx = _make_ctx(
        config=_make_config(
            wip_primacy_enabled=True, owed_act_binding_enabled=True,
        ),
        model=model,
        agent=agent,
    )

    blocked = run_gate_chain([_wip_gate()], ctx)

    assert blocked is True
    directive = ctx.model.next_task_directive
    assert directive == _wip_primacy_code_red_directive_text(
        3264, ("quality", "browser"),
    )
    assert "(failing: quality, browser)" in directive
    assert "Finish PR #" not in directive
    assert "Hand off PR #" not in directive
    records = _gate_records(mem_fs)
    assert records[0]["detail"] == (
        "pr=#3264 blocker=CI_FAILING code_red=first_act failing=quality,browser"
    )


def test_wip_primacy_code_red_makes_no_check_read_while_binding_off(
    mem_fs: dict[str, str],
) -> None:
    """Flag off: no ``gh`` read is paid for and today's text is byte-identical."""
    agent = _agent_with_focus(
        TendOwnedPR(pr_number=3264, owned_blocker="CI_FAILING"),
        readiness_by_pr={
            3264: _readiness(
                pr_number=3264, ci_state="FAILING", ready_and_waiting=False,
            ),
        },
        check_runs=(CheckRun(name="quality", conclusion="failure"),),
    )
    model = SelfModel()
    ctx = _make_ctx(
        config=_make_config(
            wip_primacy_enabled=True, owed_act_binding_enabled=False,
        ),
        model=model,
        agent=agent,
    )

    run_gate_chain([_wip_gate()], ctx)

    assert ctx.model.next_task_directive == _wip_primacy_code_red_directive_text(3264)
    assert "failing:" not in ctx.model.next_task_directive
    agent._observe_pr_check_runs.assert_not_called()
    records = _gate_records(mem_fs)
    assert records[0]["detail"] == "pr=#3264 blocker=CI_FAILING code_red=first_act"


def test_wip_primacy_code_red_directive_text_falls_back_on_empty_names() -> None:
    """An unreadable / name-less rollup renders today's exact text, capped."""
    assert _wip_primacy_code_red_directive_text(
        123, (),
    ) == _wip_primacy_code_red_directive_text(123)
    named = _wip_primacy_code_red_directive_text(123, ("quality",))
    assert named.endswith("(failing: quality)")
    assert len(named) <= _DIRECTIVE_MAX_CHARS


def test_wip_primacy_code_red_directive_names_respect_the_shared_cap() -> None:
    """A pathological name list is truncated to the shared directive cap."""
    text = _wip_primacy_code_red_directive_text(
        123, tuple(f"check-{n}" for n in range(200)),
    )
    assert len(text) == _DIRECTIVE_MAX_CHARS
    assert text.endswith("…")


def test_investigation_observable_gate_reads_live_slot(
    mem_fs: dict[str, str],
) -> None:
    """A directive committed concurrently blocks the fill (#4040)."""
    consciousness = Consciousness(_DIR)
    consciousness.load_self_model()

    def _concurrent(live: SelfModel) -> None:
        live.next_task_directive = "Finish PR #42 yourself"

    consciousness.mutate_self_model(_concurrent)

    fired = investigation_observable_gate(
        [_FLEE_GOAL], _make_config(), consciousness, [], _ga(),
    )

    assert fired is False
    reloaded = consciousness.load_self_model()
    assert reloaded.next_task_directive == "Finish PR #42 yourself"
    assert _gate_records(mem_fs) == []


def test_investigation_observable_gate_fill_preserves_concurrent_field(
    mem_fs: dict[str, str],
) -> None:
    """A fill writes only the directive; a concurrent field survives (#4040)."""
    consciousness = Consciousness(_DIR)
    consciousness.load_self_model()

    def _concurrent(live: SelfModel) -> None:
        live.pending_user_requests.append("operator-request")

    consciousness.mutate_self_model(_concurrent)

    fired = investigation_observable_gate(
        [_FLEE_GOAL], _make_config(), consciousness, [], _ga(),
    )

    assert fired is True
    reloaded = consciousness.load_self_model()
    assert reloaded.pending_user_requests == ["operator-request"]
    assert reloaded.next_task_directive is not None
    assert "named observable" in reloaded.next_task_directive
