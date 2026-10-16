"""Unit tests for the comfort-cluster directive gate.

Two layers are covered:

1. :func:`maestro.audit_checks.check_task_goal_overlap` — the pure
   pairwise-Jaccard overlap detector ported into ``audit_checks``.
2. The ``comfort_cluster`` :class:`~maestro.task_gates.TaskGate` and its
   ``_comfort_cluster_run`` predicate — a *directive-only* (non-blocking)
   gate that fills an empty ``next_task_directive`` when recent task
   goals cluster too tightly, and never overwrites a pre-set directive.

Both layers use ``mem_fs`` and mocked Memory/SelfModel surfaces — no real
I/O, no SDK calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from maestro import audit_checks
from maestro.audit_checks import (
    _COMFORT_CLUSTER_THRESHOLD,
    _DIRECTIVE_MAX_CHARS,
    AuditFinding,
    JsonlGateActivationLedger,
    check_task_goal_overlap,
)
from maestro.config import Config
from maestro.consciousness import SelfModel
from maestro.memory import TaskRecord
from maestro.task_gates import (
    GATE_CHAIN,
    GateContext,
    _comfort_cluster_run,
    run_gate_chain,
)

_DIR = Path("/fake/comfort")


# ---------------------------------------------------------------------------
# check_task_goal_overlap — the pure detector
# ---------------------------------------------------------------------------


def _task(task_id: str = "t-1", goal: str = "a goal") -> TaskRecord:
    return TaskRecord(
        task_id=task_id, goal=goal, plan=[], outcome="success",
        result_summary="ok", errors=[], duration_secs=1.0,
        timestamp="2026-01-01T00:00:00Z",
    )


def test_overlap_fires_on_tight_cluster() -> None:
    """Two near-identical goals → mean Jaccard 1.0 → one finding."""
    tasks = [
        _task("a", "implement audit overlap detection gate module"),
        _task("b", "implement audit overlap detection gate module"),
    ]
    findings = check_task_goal_overlap(SelfModel(), tasks)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "comfort_cluster"
    assert finding.rule == "comfort_cluster"
    assert "overlap" in finding.statement
    assert finding.cites == ["a", "b"]


def test_overlap_silent_on_diverse_goals() -> None:
    """Disjoint goals → mean overlap below threshold → no finding."""
    tasks = [
        _task("a", "measure response latency across loop phases"),
        _task("b", "refactor telegram notification authorization"),
    ]
    findings = check_task_goal_overlap(SelfModel(), tasks)

    assert findings == []


def test_overlap_silent_below_min_tasks() -> None:
    """Fewer than two non-empty goals → no pairwise comparison → []."""
    assert check_task_goal_overlap(SelfModel(), [_task("a", "only one")]) == []
    assert check_task_goal_overlap(SelfModel(), []) == []


def test_overlap_skips_blank_and_stopword_goals() -> None:
    """Blank and stopword-only goals are dropped before pairing.

    Only one real goal survives, so the min-task floor returns ``[]``
    even though three records were supplied.
    """
    tasks = [
        _task("a", "implement overlap detection gate"),
        _task("b", "   "),            # blank after strip → skipped
        _task("c", "the and of with"),  # all stopwords → empty tokens → skipped
    ]
    assert check_task_goal_overlap(SelfModel(), tasks) == []


def test_overlap_threshold_is_strict() -> None:
    """Mean overlap exactly at the threshold does not fire (strict ``>``).

    Two goals sharing 7 of 10 union tokens give Jaccard 0.70 — equal to
    :data:`_COMFORT_CLUSTER_THRESHOLD`, so the rule stays silent.
    """
    assert _COMFORT_CLUSTER_THRESHOLD == 0.70
    tasks = [
        _task("a", "alpha bravo charlie delta echo foxtrot golf"),
        _task("b", "alpha bravo charlie delta echo foxtrot golf hotel india juliet"),
    ]
    # union = 10 tokens, intersection = 7 → Jaccard 0.7 == threshold → silent.
    assert check_task_goal_overlap(SelfModel(), tasks) == []


# ---------------------------------------------------------------------------
# Gate wiring — _comfort_cluster_run + run_gate_chain
# ---------------------------------------------------------------------------


class _StubMemory:
    """Recording stand-in for the Memory slice the gate reads."""

    def __init__(self, recent: list[TaskRecord] | None = None) -> None:
        self._recent = recent or []

    def get_recent_tasks(self, n: int) -> list[TaskRecord]:
        return self._recent


class _StubAgent:
    """Attribute surface the directive gate path touches."""

    def __init__(self, memory: _StubMemory | None = None) -> None:
        self.memory = memory or _StubMemory()
        self.task_queue = MagicMock()
        self.task_queue.load.return_value = []
        self.journal = MagicMock()
        self.consciousness = MagicMock()
        # The directive gate appends its firing row through this backend seam
        # (proposal 106); the default JSONL adapter over ``_DIR`` writes the same
        # ``gate_activations.jsonl`` ``_gate_records`` reads back.
        self.gate_activations = JsonlGateActivationLedger(_DIR)


def _make_ctx(
    recent: list[TaskRecord] | None = None,
    model: SelfModel | None = None,
) -> GateContext:
    model = model or SelfModel()
    agent = _StubAgent(_StubMemory(recent))
    # ``run_gate_chain`` persists through ``_mutate_self_model_persisted`` (#4302); apply
    # the transaction to a live copy of the context's model.
    live = SelfModel(**vars(model))
    agent.consciousness._mutate_self_model_persisted.side_effect = (
        lambda fn: (fn(live), SelfModel(**vars(live)))
    )
    return GateContext(
        agent=agent,  # type: ignore[arg-type]
        config=Config(memory_dir=_DIR, workspace=_DIR),
        model=model,
    )


def _comfort_gate() -> Any:
    return next(g for g in GATE_CHAIN if g.name == "comfort_cluster")


def _gate_records(mem_fs: dict[str, str]) -> list[dict[str, Any]]:
    raw = mem_fs.get(str(_DIR / "gate_activations.jsonl"), "")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


_FINDING = AuditFinding(
    kind="comfort_cluster",
    statement=(
        "Recent task goals cluster tightly (mean pairwise overlap 85%), "
        "suggesting comfort-zone repetition."
    ),
    cites=["a", "b"],
    rule="comfort_cluster",
)


def test_gate_sets_directive_on_findings(mem_fs: dict[str, str]) -> None:
    """Scenario 1: high-overlap findings → directive set, contains 'overlap'."""
    ctx = _make_ctx(recent=[_task("a"), _task("b")])
    with patch(
        "maestro.task_gates.check_task_goal_overlap", return_value=[_FINDING],
    ):
        blocked = run_gate_chain([_comfort_gate()], ctx)

    assert blocked is False
    assert ctx.model.next_task_directive is not None
    assert "overlap" in ctx.model.next_task_directive
    records = _gate_records(mem_fs)
    assert len(records) == 1
    assert records[0]["gate"] == "comfort_cluster"
    assert records[0]["action"] == "directive_set"


def test_gate_leaves_directive_unset_on_no_findings(
    mem_fs: dict[str, str],
) -> None:
    """Scenario 2: empty findings → directive unchanged (None), no record."""
    ctx = _make_ctx(recent=[_task("a"), _task("b")])
    with patch(
        "maestro.task_gates.check_task_goal_overlap", return_value=[],
    ):
        blocked = run_gate_chain([_comfort_gate()], ctx)

    assert blocked is False
    assert ctx.model.next_task_directive is None
    assert _gate_records(mem_fs) == []


def test_gate_never_overwrites_preset_directive(
    mem_fs: dict[str, str],
) -> None:
    """Scenario 3: a pre-set directive is preserved; the check is never run."""
    model = SelfModel()
    model.next_task_directive = "Already set by a higher-precedence gate"
    ctx = _make_ctx(recent=[_task("a"), _task("b")], model=model)
    with patch(
        "maestro.task_gates.check_task_goal_overlap",
    ) as check:
        blocked = run_gate_chain([_comfort_gate()], ctx)

    assert blocked is False
    assert ctx.model.next_task_directive == "Already set by a higher-precedence gate"
    check.assert_not_called()
    assert _gate_records(mem_fs) == []


def test_gate_no_directive_when_history_too_short(
    mem_fs: dict[str, str],
) -> None:
    """Scenario 4: <2 recent tasks → real check returns [] → no directive,
    no exception, no record."""
    ctx = _make_ctx(recent=[_task("a", "only one goal")])
    blocked = run_gate_chain([_comfort_gate()], ctx)

    assert blocked is False
    assert ctx.model.next_task_directive is None
    assert _gate_records(mem_fs) == []


def test_gate_swallows_check_exception() -> None:
    """An unexpected overlap-check failure is logged and yields None.

    Task generation must proceed even if the detector raises, so the gate
    predicate returns ``None`` (no directive) rather than propagating.
    """
    ctx = _make_ctx(recent=[_task("a"), _task("b")])
    with patch(
        "maestro.task_gates.check_task_goal_overlap",
        side_effect=RuntimeError("boom"),
    ):
        outcome = _comfort_cluster_run(ctx)

    assert outcome is None


def _long_cluster_finding() -> AuditFinding:
    """A finding whose statement forces the derived directive over any cap."""
    return AuditFinding(
        kind="comfort_cluster",
        statement="overlap " + "x" * 400,
        cites=[],
        rule="comfort_cluster",
    )


def test_gate_directive_caps_at_directive_max_chars() -> None:
    """A very long finding statement is truncated to fit the directive cap."""
    ctx = _make_ctx(recent=[_task("a"), _task("b")])
    with patch(
        "maestro.task_gates.check_task_goal_overlap",
        return_value=[_long_cluster_finding()],
    ):
        outcome = _comfort_cluster_run(ctx)

    assert outcome is not None
    assert outcome.directive is not None
    assert len(outcome.directive) == _DIRECTIVE_MAX_CHARS
    assert outcome.directive.endswith("…")


def test_comfort_cluster_cap_tracks_shared_constant() -> None:
    """The bridge truncates at the shared constant, not a bare literal.

    This gate used to clip at an inline ``200``. Raising
    ``_DIRECTIVE_MAX_CHARS`` would then have silently dropped the tail of the
    directive — including the "Break the cluster …" instruction that carries
    the whole point of the redirect. Patching the constant down proves the
    bridge now moves with it.
    """
    ctx = _make_ctx(recent=[_task("a"), _task("b")])
    with (
        patch.object(audit_checks, "_DIRECTIVE_MAX_CHARS", 150),
        patch(
            "maestro.task_gates.check_task_goal_overlap",
            return_value=[_long_cluster_finding()],
        ),
    ):
        outcome = _comfort_cluster_run(ctx)

    assert outcome is not None
    assert outcome.directive is not None
    assert len(outcome.directive) == 150
    assert outcome.directive.endswith("…")
