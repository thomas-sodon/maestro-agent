"""Owed-act divergence across decision kinds (proposal 137, step 13).

Covers the disposal-offer and handoff-directive legs the sensor in
:mod:`maestro.remediation_tracker` gained beside its cooldown-skip leg: the
renderer/parser pairs, the per-kind breakdown, the filter that keeps a
non-handoff ``wip_primacy`` row out, and the rendered line. Kept out of
``tests/test_remediation_tracker.py`` so that module does not grow. An in-memory
backend stands in for the gate-activation ledger; no I/O.
"""

from __future__ import annotations

from maestro.audit_checks import GateActivation
from maestro.remediation_tracker import (
    _OWED_ACT_DIVERGENCE_DATA_FLOOR,
    COOLDOWN_SKIP_GATE,
    DISPOSAL_OFFER_GATE,
    HANDOFF_DIRECTIVE_GATE,
    OwedActDivergence,
    format_cooldown_skip_detail,
    format_disposal_offer_detail,
    format_handoff_directive_detail,
    format_owed_act_divergence,
    measure_owed_act_divergence,
)


class _Backend:
    """In-memory ``GateActivationBackend`` over a fixed row list."""

    def __init__(self, rows: list[GateActivation]) -> None:
        self.rows = list(rows)

    def append(self, record: GateActivation) -> None:
        self.rows.append(record)

    def read_all(self) -> list[GateActivation]:
        return list(self.rows)

    def read_tail(self, limit: int) -> list[GateActivation]:
        return list(self.rows)[-limit:] if limit > 0 else []

    def read_gate_tail(self, gate: str, limit: int) -> list[GateActivation]:
        rows = [r for r in self.rows if r.gate == gate]
        return rows[-limit:] if limit > 0 else []


def _row(gate: str, detail: str) -> GateActivation:
    return GateActivation(
        gate=gate,
        action="aligned",
        timestamp="2026-09-23T00:00:00+00:00",
        detail=detail,
    )


def _cooldown(pr: int, owed: str) -> GateActivation:
    return _row(COOLDOWN_SKIP_GATE, format_cooldown_skip_detail(pr, "conflict", owed))


def _disposal(pr: int, owed: str) -> GateActivation:
    return _row(DISPOSAL_OFFER_GATE, format_disposal_offer_detail(pr, owed))


def _handoff(pr: int, owed: str) -> GateActivation:
    return _row(HANDOFF_DIRECTIVE_GATE, format_handoff_directive_detail(pr, owed))


def test_disposal_detail_renders_and_round_trips() -> None:
    assert format_disposal_offer_detail(42, "code-owed") == "pr=#42 owed=code-owed"
    assert format_disposal_offer_detail(42, "") == "pr=#42 owed=-"
    measured = measure_owed_act_divergence(_Backend([_disposal(42, "code-owed")]), 10)
    assert measured.prs == (42,)
    assert measured.diverged_by_kind == (
        ("cooldown", 0), ("disposal", 1), ("handoff", 0),
    )


def test_handoff_detail_renders_and_round_trips() -> None:
    detail = format_handoff_directive_detail(7, "handoff-ready")
    assert detail == "pr=#7 merge_ready=handoff owed=handoff-ready"
    assert format_handoff_directive_detail(7, "") == (
        "pr=#7 merge_ready=handoff owed=-"
    )
    measured = measure_owed_act_divergence(_Backend([_handoff(7, "code-owed")]), 10)
    assert measured.diverged_by_kind == (
        ("cooldown", 0), ("disposal", 0), ("handoff", 1),
    )


def test_mixed_trace_reads_per_kind_counts() -> None:
    backend = _Backend([
        _cooldown(1, "code-owed"),
        _cooldown(2, "wait-ci"),
        _disposal(1, "code-owed"),
        _disposal(3, "-"),
        _handoff(4, "handoff-ready"),
        _handoff(5, "code-owed"),
    ])
    measured = measure_owed_act_divergence(backend, 50)
    assert measured == OwedActDivergence(
        diverged=3,
        aligned=3,
        total=6,
        prs=(1, 5),
        diverged_by_kind=(("cooldown", 1), ("disposal", 1), ("handoff", 1)),
    )


def test_non_handoff_wip_primacy_rows_are_skipped() -> None:
    backend = _Backend([
        _row(HANDOFF_DIRECTIVE_GATE, "pr=#9 blocker=CI_FAILING code_red=first_act"),
        _row(HANDOFF_DIRECTIVE_GATE, "pr=#9 merge_ready=handoff"),
        _row(DISPOSAL_OFFER_GATE, "offered to close #9"),
    ])
    measured = measure_owed_act_divergence(backend, 50)
    assert measured.total == 0


def test_window_is_taken_after_the_filter() -> None:
    backend = _Backend([
        _disposal(1, "code-owed"),
        _row(HANDOFF_DIRECTIVE_GATE, "pr=#2 blocker=CONFLICTING"),
        _handoff(3, "wait-ci"),
    ])
    measured = measure_owed_act_divergence(backend, 1)
    assert measured.total == 1
    assert measured.diverged == 0


def test_stranded_pr_shape_reads_positive() -> None:
    """Disposal offers plus cooldown skips on one code-owed PR surface a line."""
    rows = [_disposal(3971, "code-owed"), _disposal(3971, "code-owed")]
    rows += [_cooldown(3971, "code-owed") for _ in range(3)]
    line = format_owed_act_divergence(_Backend(rows), 50)
    assert line.startswith(
        "OWED-ACT-DIVERGENCE: 5/5 owed-act decisions (cooldown 3, disposal 2)",
    )
    assert "(#3971)" in line


def test_breakdown_omits_zero_kinds() -> None:
    rows = [_handoff(pr, "code-owed") for pr in range(1, 6)]
    line = format_owed_act_divergence(_Backend(rows), 50)
    assert "(handoff 5)" in line
    assert "cooldown" not in line.split(" — ")[0]


def test_below_floor_and_zero_divergence_stay_dark() -> None:
    thin = [_disposal(1, "code-owed")] * (_OWED_ACT_DIVERGENCE_DATA_FLOOR - 1)
    assert format_owed_act_divergence(_Backend(thin), 50) == ""
    clean = [_handoff(1, "handoff-ready")] * (_OWED_ACT_DIVERGENCE_DATA_FLOOR + 1)
    assert format_owed_act_divergence(_Backend(clean), 50) == ""
