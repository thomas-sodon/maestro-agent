"""Unit tests for the self-calibration owner (proposal 129, phase 1).

The owner is pure and I/O-free — :func:`parse_loss_condition` reads an injected
string and :func:`score_loss_condition` reads an injected
:class:`CalibrationEvidence` bundle — so these build strings and bundles in
memory and assert the probe / verdict plus its provenance. No filesystem,
network, or subprocess is touched; no pytest fixtures and no ``mem_fs`` are
needed (mirroring ``tests/test_plan_grounding.py`` /
``tests/test_frame_discharge.py``).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import get_args

import pytest

from maestro import audit_checks, audit_checks_calibration
from maestro.self_calibration import (
    _CUTOFF_GRACE_CYCLES,
    _GATE_RE,
    _MAX_WINDOW_CYCLES,
    _TASK_TARGET_SHAPES,
    CLAIM_CITATION_GRAMMAR,
    CONSCIOUSNESS_CRITERIA,
    LOSS_CONDITION_GRAMMAR,
    MAX_SCOREABLE_AGE_CYCLES,
    AuthoredVerdict,
    BetReason,
    BetScore,
    CalibrationEvidence,
    CitationKind,
    ClaimCitation,
    ClaimEvidenceSources,
    ConsciousnessCriterion,
    LossProbe,
    ObservationWindow,
    ProbeKind,
    _window_or_none,
    author_reflection_verdict,
    max_scoreable_age,
    parse_claim_citation,
    parse_loss_condition,
    resolve_citation_status,
    resolve_claim_citation,
    score_loss_condition,
    stale_repeat_loss_conditions,
)

_MAESTRO_ROOT = Path(__file__).resolve().parent.parent / "maestro"

# ── parse_loss_condition: one accepted form per family, both polarities ─────────


@pytest.mark.parametrize(
    ("text", "kind", "target", "operand", "polarity"),
    [
        (
            "task remediation outcome is success within 3 cycles",
            "TASK_OUTCOME", "remediation", "success", "OCCURS",
        ),
        (
            "task remediation outcome is not success within 3 cycles",
            "TASK_OUTCOME", "remediation", "success", "DOES_NOT_OCCUR",
        ),
        (
            "gate wip_primacy fires within 5 cycles",
            "GATE_FIRING", "wip_primacy", "", "OCCURS",
        ),
        (
            "gate wip_primacy does not fire within 5 cycles",
            "GATE_FIRING", "wip_primacy", "", "DOES_NOT_OCCUR",
        ),
        (
            "pr #1234 settles within 4 cycles",
            "PR_SETTLEMENT", "1234", "", "OCCURS",
        ),
        (
            "pr #1234 does not settle within 4 cycles",
            "PR_SETTLEMENT", "1234", "", "DOES_NOT_OCCUR",
        ),
        (
            "plan p-42 reaches merged within 6 cycles",
            "PLAN_STATUS", "p-42", "merged", "OCCURS",
        ),
        (
            "plan p-42 does not reach merged within 6 cycles",
            "PLAN_STATUS", "p-42", "merged", "DOES_NOT_OCCUR",
        ),
        (
            "sensor TERMINAL-DEEDS goes above 3 within 2 cycles",
            "SENSOR_THRESHOLD", "TERMINAL-DEEDS", "above 3.0", "OCCURS",
        ),
        (
            "sensor TERMINAL-DEEDS does not go below 1.5 within 2 cycles",
            "SENSOR_THRESHOLD", "TERMINAL-DEEDS", "below 1.5", "DOES_NOT_OCCUR",
        ),
    ],
)
def test_parse_accepts_each_family_in_both_polarities(
    text: str, kind: str, target: str, operand: str, polarity: str
) -> None:
    """Each probe family parses in both polarities to the expected typed probe."""
    probe = parse_loss_condition(text)
    assert probe is not None
    assert probe.kind == kind
    assert probe.target == target
    assert probe.operand == operand
    assert probe.polarity == polarity


def test_parse_builds_two_point_window() -> None:
    """The window is ``matures = N`` and ``cutoff = N + grace``."""
    probe = parse_loss_condition("gate wip_primacy fires within 5 cycles")
    assert probe is not None
    assert probe.window == ObservationWindow(
        matures_after_cycles=5, cutoff_after_cycles=5 + 1 + _CUTOFF_GRACE_CYCLES
    )


def test_parse_is_case_insensitive_whitespace_and_period_tolerant() -> None:
    """Uppercase, padding, extra spaces, and a trailing period all parse."""
    probe = parse_loss_condition(
        "  TASK   REMEDIATION  OUTCOME IS SUCCESS  WITHIN 3 CYCLES.  "
    )
    assert probe is not None
    assert probe.kind == "TASK_OUTCOME"
    assert probe.target == "remediation"
    assert probe.operand == "success"
    assert probe.polarity == "OCCURS"


def test_parse_accepts_singular_cycle_and_bare_pr_number() -> None:
    """``cycle`` (singular) and a PR without ``#`` are lenient accepted forms."""
    probe = parse_loss_condition("pr 77 settles within 1 cycle")
    assert probe is not None
    assert probe.kind == "PR_SETTLEMENT"
    assert probe.target == "77"
    assert probe.window.matures_after_cycles == 1


def test_parse_accepts_negative_sensor_threshold() -> None:
    """A negative sensor threshold is a valid number operand."""
    probe = parse_loss_condition("sensor DRIFT goes below -2.5 within 4 cycles")
    assert probe is not None
    assert probe.operand == "below -2.5"


# ── parse_loss_condition: rejection paths all return None (never raise) ──────────


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "foobar happens within 3 cycles",  # unknown keyword
        "task nonsense outcome is success within 3 cycles",  # bad shape
        "task remediation outcome is winning within 3 cycles",  # bad outcome
        "plan p-42 reaches shipped within 3 cycles",  # bad status
        "gate wip_primacy fires",  # missing window
        "gate wip_primacy fires within many cycles",  # malformed window
    ],
)
def test_parse_rejects_returns_none(text: str) -> None:
    """Every unparseable / invalid-token bet declines to ``None``."""
    assert parse_loss_condition(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "task remediation outcome is success within 0 cycles",
        "gate wip_primacy fires within 0 cycles",
        "pr #1234 settles within 0 cycles",
        "plan p-42 reaches merged within 0 cycles",
        "sensor FOO goes above 3 within 0 cycles",
    ],
)
def test_parse_rejects_non_positive_window_per_family(text: str) -> None:
    """``N == 0`` is not a bet — every family's window guard returns ``None``."""
    assert parse_loss_condition(text) is None


def test_parse_rejects_window_above_max() -> None:
    """A window above :data:`_MAX_WINDOW_CYCLES` is an unloseable horizon → None."""
    over = _MAX_WINDOW_CYCLES + 1
    assert parse_loss_condition(f"gate g fires within {over} cycles") is None


_OVERSIZED = "9" * (sys.get_int_max_str_digits() + 1)


@pytest.mark.parametrize(
    "template",
    [
        "task remediation outcome is success within {n} cycles",
        "gate wip_primacy fires within {n} cycles",
        "pr #1234 settles within {n} cycles",
        "plan p-42 reaches merged within {n} cycles",
        "sensor FOO goes above 3 within {n} cycles",
    ],
)
def test_parse_declines_oversized_window_per_family(template: str) -> None:
    """A window digit-run past CPython's int limit declines, never raises.

    The conversion in ``_window_or_none`` would raise ``ValueError`` on such a
    token; the width-bounded grammar makes it simply not match, so the bet takes
    the existing decline path and scores ``unfalsifiable``.
    """
    assert parse_loss_condition(template.format(n=_OVERSIZED)) is None


def test_parse_declines_oversized_pr_target() -> None:
    """An oversized PR target declines at the grammar rather than at scoring."""
    assert parse_loss_condition(f"pr #{_OVERSIZED} settles within 5 cycles") is None


def test_window_bound_does_not_steal_the_semantic_range_guard() -> None:
    """The digit bound sits far above the semantic window cap.

    ``_MAX_WINDOW_CYCLES + 1`` must still *match* the regex — so the decline is
    the range check's doing, not the width bound's. The width bound is
    int-safety only, not a second cap.
    """
    text = f"gate g fires within {_MAX_WINDOW_CYCLES + 1} cycles"
    assert _GATE_RE.match(text) is not None
    assert parse_loss_condition(text) is None


def test_window_or_none_declines_oversized_text_directly() -> None:
    """The conversion-site width guard declines rather than letting ``int`` raise.

    The grammar can no longer hand ``_window_or_none`` an oversized token, so the
    restated bound is pinned here directly — it is the backstop that keeps a
    future grammar edit from reopening the ``ValueError`` hole.
    """
    assert _window_or_none(_OVERSIZED) is None


# ── The drift guard: the local shape copy must track the live vocabulary ─────────


def test_task_target_shapes_pins_to_live_taskshape_vocab() -> None:
    """``_TASK_TARGET_SHAPES`` must equal ``get_args(audit_checks.TaskShape)``.

    The module deliberately does not import ``audit_checks`` (to stay cycle-free),
    so the local copy is pinned here instead — a shape added to ``TaskShape``
    without updating the copy fails this test rather than silently narrowing the
    grammar. Only the test imports ``audit_checks``.
    """
    assert get_args(audit_checks.TaskShape) == _TASK_TARGET_SHAPES


# ── score_loss_condition: unfalsifiable and the unobservable / temporal matrix ───


def _probe(
    kind: str,
    target: str,
    operand: str = "",
    polarity: str = "OCCURS",
    matures: int = 2,
    cutoff: int = 4,
) -> LossProbe:
    """Build a probe directly so scoring tests control the window precisely."""
    return LossProbe(
        kind=kind,  # type: ignore[arg-type]
        target=target,
        operand=operand,
        polarity=polarity,  # type: ignore[arg-type]
        window=ObservationWindow(
            matures_after_cycles=matures, cutoff_after_cycles=cutoff
        ),
    )


def test_score_declines_oversized_pr_target_on_hand_built_probe() -> None:
    """A hand-built probe carrying an oversized target scores without raising.

    The grammar can no longer produce one, so this pins the conversion-site guard
    directly: an unreadable target is *unobservable*, not a crash.
    """
    probe = _probe("PR_SETTLEMENT", _OVERSIZED)
    score = score_loss_condition(probe, CalibrationEvidence(), cycles_elapsed=0)
    assert score.observed is None


def test_score_declines_non_ascii_digit_pr_target() -> None:
    """A superscript-digit target is unobservable, not a ``ValueError``.

    ``str.isdigit()`` is True for "²" while ``int()`` rejects it, so the guard
    checks ASCII-ness too.
    """
    probe = _probe("PR_SETTLEMENT", "²")
    score = score_loss_condition(probe, CalibrationEvidence(), cycles_elapsed=0)
    assert score.observed is None


def test_score_none_probe_is_unfalsifiable() -> None:
    """A ``None`` probe scores ``unfalsifiable`` with no kind and no observation."""
    score = score_loss_condition(None, CalibrationEvidence(), cycles_elapsed=0)
    assert score.verdict == "unfalsifiable"
    assert score.kind is None
    assert score.observed is None
    assert score.reason == "no-parseable-probe"


def test_score_unobservable_is_pending_then_unresolved() -> None:
    """An unobservable fact is ``pending`` before the cutoff, ``unresolved`` at it."""
    probe = _probe("PLAN_STATUS", "p-1", "merged", matures=2, cutoff=4)
    evidence = CalibrationEvidence()  # nothing supplied → cannot observe
    early = score_loss_condition(probe, evidence, cycles_elapsed=3)
    assert early.verdict == "pending"
    assert early.observed is None
    assert early.reason == "unobservable-pending"
    late = score_loss_condition(probe, evidence, cycles_elapsed=4)
    assert late.verdict == "unresolved"
    assert late.observed is None
    assert late.reason == "unobservable-at-cutoff"


@pytest.mark.parametrize(
    ("cycles", "verdict", "reason"),
    [
        (1, "pending", "awaiting-maturity"),  # before maturity
        (2, "pending", "awaiting-maturity"),  # iteration c+N still inside window
        (3, "correct", "held-through-window"),  # window closed, never occurred
    ],
)
def test_score_occurs_unobserved_matures_to_correct(
    cycles: int, verdict: str, reason: str
) -> None:
    """An ``OCCURS`` loss condition never seen is pending, then correct at maturity."""
    probe = _probe("GATE_FIRING", "g", polarity="OCCURS", matures=2, cutoff=4)
    evidence = CalibrationEvidence(fired_gates=(), gate_window_covered=True)
    score = score_loss_condition(probe, evidence, cycles_elapsed=cycles)
    assert score.verdict == verdict
    assert score.observed is False
    assert score.reason == reason


def test_score_occurs_observed_is_wrong() -> None:
    """An ``OCCURS`` loss condition whose event is seen scores ``wrong``."""
    probe = _probe("GATE_FIRING", "g", polarity="OCCURS")
    evidence = CalibrationEvidence(fired_gates=("g",))
    score = score_loss_condition(probe, evidence, cycles_elapsed=0)
    assert score.verdict == "wrong"
    assert score.observed is True
    assert score.reason == "loss-condition-occurred"


def test_score_does_not_occur_observed_is_correct() -> None:
    """A ``DOES_NOT_OCCUR`` loss condition whose event is seen scores ``correct``."""
    probe = _probe("GATE_FIRING", "g", polarity="DOES_NOT_OCCUR")
    evidence = CalibrationEvidence(fired_gates=("g",))
    score = score_loss_condition(probe, evidence, cycles_elapsed=0)
    assert score.verdict == "correct"
    assert score.observed is True
    assert score.reason == "loss-averted-by-event"


@pytest.mark.parametrize(
    ("cycles", "verdict", "reason"),
    [
        (1, "pending", "awaiting-maturity"),  # before maturity
        (2, "pending", "awaiting-maturity"),  # iteration c+N still inside window
        (3, "wrong", "loss-realized-by-absence"),  # window closed, never happened
    ],
)
def test_score_does_not_occur_unobserved_matures_to_wrong(
    cycles: int, verdict: str, reason: str
) -> None:
    """A ``DOES_NOT_OCCUR`` loss condition never seen is pending, then wrong."""
    probe = _probe("GATE_FIRING", "g", polarity="DOES_NOT_OCCUR", matures=2, cutoff=4)
    evidence = CalibrationEvidence(fired_gates=(), gate_window_covered=True)
    score = score_loss_condition(probe, evidence, cycles_elapsed=cycles)
    assert score.verdict == verdict
    assert score.observed is False
    assert score.reason == reason


# ── The two asymmetries the design turns on ─────────────────────────────────────


def test_pr_absence_is_never_wrong_only_pending_then_unresolved() -> None:
    """A PR absent from ``settled_prs`` is unobservable, never "did not settle".

    Even a ``DOES_NOT_OCCUR`` bet — where an absent event would otherwise mature
    to ``wrong`` — can only reach ``pending`` / ``unresolved`` for an unobserved
    settlement, because settlement is positive evidence only.
    """
    probe = _probe("PR_SETTLEMENT", "999", polarity="DOES_NOT_OCCUR", matures=2, cutoff=4)
    evidence = CalibrationEvidence(settled_prs=(1, 2, 3))  # 999 absent
    pending = score_loss_condition(probe, evidence, cycles_elapsed=3)
    assert pending.verdict == "pending"
    unresolved = score_loss_condition(probe, evidence, cycles_elapsed=5)
    assert unresolved.verdict == "unresolved"


def test_pr_present_in_settled_set_is_observed_true() -> None:
    """A PR in ``settled_prs`` resolves to observed (the only positive PR path)."""
    probe = _probe("PR_SETTLEMENT", "1234", polarity="OCCURS")
    evidence = CalibrationEvidence(settled_prs=(1234,))
    score = score_loss_condition(probe, evidence, cycles_elapsed=0)
    assert score.verdict == "wrong"
    assert score.observed is True


def test_covered_but_empty_window_is_genuine_negative_evidence() -> None:
    """``*_covered`` set with an empty tuple is a real "did not happen" (False).

    Contrasted with an uncovered empty tuple, which is "cannot observe" (None) —
    the two the boolean exists to separate.
    """
    probe = _probe("GATE_FIRING", "g", polarity="OCCURS", matures=2, cutoff=4)
    covered = CalibrationEvidence(fired_gates=(), gate_window_covered=True)
    negative = score_loss_condition(probe, covered, cycles_elapsed=3)
    assert negative.observed is False
    assert negative.verdict == "correct"
    uncovered = CalibrationEvidence(fired_gates=(), gate_window_covered=False)
    unobservable = score_loss_condition(probe, uncovered, cycles_elapsed=3)
    assert unobservable.observed is None
    assert unobservable.verdict == "pending"


def test_task_outcome_covered_empty_is_negative_and_match_is_true() -> None:
    """A ``TASK_OUTCOME`` match is observed; covered-empty is negative; else None."""
    probe = _probe("TASK_OUTCOME", "remediation", "success", polarity="OCCURS")
    matched = CalibrationEvidence(task_outcomes=(("remediation", "success"),))
    assert score_loss_condition(probe, matched, cycles_elapsed=0).observed is True
    covered = CalibrationEvidence(task_outcomes=(), task_window_covered=True)
    assert score_loss_condition(probe, covered, cycles_elapsed=0).observed is False
    uncovered = CalibrationEvidence(task_outcomes=())
    assert score_loss_condition(probe, uncovered, cycles_elapsed=0).observed is None


def test_plan_status_present_different_status_is_negative() -> None:
    """A plan id present with a *different* status is observed-negative (False).

    Distinct from an absent id (``None``): the caller saw the plan and it is not at
    the target status.
    """
    probe = _probe("PLAN_STATUS", "p-7", "merged", polarity="OCCURS", matures=1, cutoff=3)
    matched = CalibrationEvidence(plan_statuses=(("p-7", "merged"),))
    positive = score_loss_condition(probe, matched, cycles_elapsed=1)
    assert positive.observed is True
    assert positive.verdict == "wrong"
    present_other = CalibrationEvidence(plan_statuses=(("p-7", "rejected"),))
    negative = score_loss_condition(probe, present_other, cycles_elapsed=2)
    assert negative.observed is False
    assert negative.verdict == "correct"
    absent = CalibrationEvidence(plan_statuses=(("p-9", "merged"),))
    assert score_loss_condition(probe, absent, cycles_elapsed=1).observed is None


@pytest.mark.parametrize(
    ("operand", "value", "expected"),
    [
        ("above 3.0", 5.0, True),   # above, crosses
        ("above 3.0", 1.0, False),  # above, does not cross
        ("below 3.0", 1.0, True),   # below, crosses
        ("below 3.0", 5.0, False),  # below, does not cross
    ],
)
def test_sensor_threshold_crossing(
    operand: str, value: float, expected: bool
) -> None:
    """``above`` ⇒ ``>`` and ``below`` ⇒ ``<`` decide the sensor observation."""
    probe = _probe("SENSOR_THRESHOLD", "FOO", operand, polarity="OCCURS")
    evidence = CalibrationEvidence(sensor_readings=(("FOO", value),))
    assert score_loss_condition(probe, evidence, cycles_elapsed=0).observed is expected


def test_sensor_absent_tag_is_unobservable() -> None:
    """A sensor tag not in the readings resolves to ``None`` (cannot observe)."""
    probe = _probe("SENSOR_THRESHOLD", "FOO", "above 3.0", polarity="OCCURS")
    evidence = CalibrationEvidence(sensor_readings=(("BAR", 9.0),))
    assert score_loss_condition(probe, evidence, cycles_elapsed=0).observed is None


def test_sensor_any_matching_reading_crosses_order_independent() -> None:
    """A later crossing reading is not masked by an earlier non-crossing one.

    Regression: the resolver used to ``return`` on the *first* reading whose tag
    matched, so the verdict depended on tuple order. It must now be an any-match
    scan — ``True`` if *any* matching reading crosses, ``False`` only when at least
    one matched and none crossed.
    """
    probe = _probe("SENSOR_THRESHOLD", "DRIFT", "above 3.0", polarity="OCCURS")
    forward = CalibrationEvidence(sensor_readings=(("DRIFT", 1.0), ("DRIFT", 9.0)))
    assert score_loss_condition(probe, forward, cycles_elapsed=0).observed is True
    reversed_ = CalibrationEvidence(sensor_readings=(("DRIFT", 9.0), ("DRIFT", 1.0)))
    assert score_loss_condition(probe, reversed_, cycles_elapsed=0).observed is True
    none_cross = CalibrationEvidence(sensor_readings=(("DRIFT", 1.0), ("DRIFT", 2.0)))
    assert score_loss_condition(probe, none_cross, cycles_elapsed=0).observed is False


def test_gate_and_sensor_targets_match_case_insensitively() -> None:
    """A case-variant gate/sensor target still matches the observed evidence.

    Regression: the family regexes are ``re.IGNORECASE`` but the resolver matched
    the preserved-case target exactly, so a case variant resolved to a confident
    ``False`` (and, once matured, a false ``correct``) for an event that actually
    happened. Both targets must now compare case-insensitively.
    """
    gate = _probe("GATE_FIRING", "WIP_Primacy", polarity="OCCURS")
    fired = CalibrationEvidence(fired_gates=("wip_primacy",))
    assert score_loss_condition(gate, fired, cycles_elapsed=0).observed is True
    sensor = _probe("SENSOR_THRESHOLD", "drift", "above 3.0", polarity="OCCURS")
    readings = CalibrationEvidence(sensor_readings=(("DRIFT", 9.0),))
    assert score_loss_condition(sensor, readings, cycles_elapsed=0).observed is True


def test_bet_reason_is_the_closed_betreason_set() -> None:
    """Every ``reason`` a scoring branch emits is a member of ``BetReason``.

    Locks the closed slug set to exactly the branches :func:`score_loss_condition`
    can take — a new branch with an unlisted slug (or a stale slug left behind)
    fails here rather than silently widening the "fixed slug" contract.
    """
    allowed = set(get_args(BetReason))
    gate_covered = CalibrationEvidence(fired_gates=(), gate_window_covered=True)
    gate_fired = CalibrationEvidence(fired_gates=("g",))
    occurs = _probe("GATE_FIRING", "g", polarity="OCCURS", matures=2, cutoff=4)
    dno = _probe("GATE_FIRING", "g", polarity="DOES_NOT_OCCUR", matures=2, cutoff=4)
    plan = _probe("PLAN_STATUS", "p-1", "merged", matures=2, cutoff=4)
    emitted = {
        score_loss_condition(None, CalibrationEvidence(), cycles_elapsed=0).reason,
        score_loss_condition(plan, CalibrationEvidence(), cycles_elapsed=1).reason,
        score_loss_condition(plan, CalibrationEvidence(), cycles_elapsed=4).reason,
        score_loss_condition(occurs, gate_covered, cycles_elapsed=1).reason,
        score_loss_condition(occurs, gate_covered, cycles_elapsed=3).reason,
        score_loss_condition(occurs, gate_fired, cycles_elapsed=0).reason,
        score_loss_condition(dno, gate_fired, cycles_elapsed=0).reason,
        score_loss_condition(dno, gate_covered, cycles_elapsed=3).reason,
    }
    assert emitted <= allowed
    # The set is exactly the emitted reasons — no dead slug, none missing.
    assert emitted == allowed


# ── Provenance records are frozen ───────────────────────────────────────────────


def test_value_objects_are_frozen() -> None:
    """The probe, window, evidence, and score are small frozen provenance records."""
    probe = _probe("GATE_FIRING", "g")
    score = score_loss_condition(probe, CalibrationEvidence(), cycles_elapsed=0)
    assert isinstance(score, BetScore)
    with pytest.raises(AttributeError):
        probe.target = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        score.verdict = "wrong"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        probe.window.matures_after_cycles = 9  # type: ignore[misc]


# ── LOSS_CONDITION_GRAMMAR: the prompt's vocabulary must match the parser ───────


def test_loss_condition_grammar_covers_every_probe_kind_in_order() -> None:
    """Every ``LOSS_CONDITION_GRAMMAR`` line parses, and the produced kinds equal
    ``get_args(ProbeKind)`` exactly — one canonical example per kind, in order.

    This is the pin that stops the reflection prompt teaching a shape the parser
    rejects: :func:`format_prediction_requirement` quotes this tuple verbatim, so
    a drifted example would ask the model for an unparseable — hence always
    ``unfalsifiable`` — loss condition, or leave a kind unexemplified.
    """
    probes = [parse_loss_condition(line) for line in LOSS_CONDITION_GRAMMAR]
    assert None not in probes, (
        f"every LOSS_CONDITION_GRAMMAR line must parse; got {probes}"
    )
    kinds = [probe.kind for probe in probes if probe is not None]
    assert kinds == list(get_args(ProbeKind))


def test_loss_condition_grammar_lines_are_bare() -> None:
    """No grammar example carries a ``LOSS CONDITION:`` (or other marker) prefix.

    The lines are quoted mid-prompt; a marker prefix would let an echoed example
    shadow the genuine trailing marker line the parser reads (last occurrence
    wins), so each must be stated bare.
    """
    for line in LOSS_CONDITION_GRAMMAR:
        assert "LOSS CONDITION:" not in line.upper()
        assert "PREDICTION:" not in line.upper()
        assert "CONFIDENCE:" not in line.upper()


# ── author_reflection_verdict: the sole decider of the recorded verdict ─────────


def _score(verdict: str) -> BetScore:
    """Build a :class:`BetScore` with a plausible provenance for *verdict*."""
    provenance: dict[str, tuple[ProbeKind | None, bool | None, BetReason]] = {
        "correct": ("TASK_OUTCOME", False, "held-through-window"),
        "wrong": ("GATE_FIRING", True, "loss-condition-occurred"),
        "pending": ("PLAN_STATUS", None, "unobservable-pending"),
        "unresolved": ("PR_SETTLEMENT", None, "unobservable-at-cutoff"),
        "unfalsifiable": (None, None, "no-parseable-probe"),
    }
    kind, observed, reason = provenance[verdict]
    return BetScore(
        verdict=verdict,  # type: ignore[arg-type]
        kind=kind,
        observed=observed,
        reason=reason,
    )


@pytest.mark.parametrize("verdict", ["correct", "wrong", "pending", "unresolved"])
def test_author_adopts_measured_verdict(verdict: str) -> None:
    """A gradeable score is adopted as ``measured`` with the self-grade retained."""
    authored = author_reflection_verdict(_score(verdict), "correct")
    assert authored is not None
    assert authored.verdict == verdict
    assert authored.source == "measured"
    assert authored.self_verdict == "correct"


def test_author_measured_without_self_grade_carries_none() -> None:
    """A measured verdict for a bet with no self-grade (an older, non-newest bet)
    still records, with ``self_verdict`` left ``None``."""
    authored = author_reflection_verdict(_score("wrong"), None)
    assert authored is not None
    assert authored.verdict == "wrong"
    assert authored.source == "measured"
    assert authored.self_verdict is None


def test_author_unfalsifiable_falls_back_to_narrated() -> None:
    """An unfalsifiable bet keeps the self-grade, stamped ``narrated``."""
    authored = author_reflection_verdict(_score("unfalsifiable"), "wrong")
    assert authored is not None
    assert authored.verdict == "wrong"
    assert authored.source == "narrated"
    assert authored.self_verdict is None


def test_author_flag_off_passes_self_grade_with_no_source() -> None:
    """``score=None`` (the flag-off call) adopts the self-grade with ``source=None``
    and no narration field, so the persisted row stays byte-identical."""
    authored = author_reflection_verdict(None, "correct")
    assert authored is not None
    assert authored.verdict == "correct"
    assert authored.source is None
    assert authored.self_verdict is None


def test_author_returns_none_when_nothing_to_record() -> None:
    """An empty self-grade with no measurable score — flag-off or unfalsifiable —
    has nothing to record."""
    assert author_reflection_verdict(None, "") is None
    assert author_reflection_verdict(None, None) is None
    assert author_reflection_verdict(_score("unfalsifiable"), "") is None
    assert author_reflection_verdict(_score("unfalsifiable"), None) is None


def test_max_scoreable_age_cycles_is_window_plus_grace() -> None:
    """The re-score horizon is the longest window plus the cutoff grace."""
    assert MAX_SCOREABLE_AGE_CYCLES == _MAX_WINDOW_CYCLES + 1 + _CUTOFF_GRACE_CYCLES


@pytest.mark.parametrize(
    ("every", "expected"),
    [
        (0, MAX_SCOREABLE_AGE_CYCLES),
        (1, MAX_SCOREABLE_AGE_CYCLES),
        (5, MAX_SCOREABLE_AGE_CYCLES + 4),
    ],
)
def test_max_scoreable_age_widens_by_contemplate_cadence(
    every: int, expected: int
) -> None:
    """The age cap admits the first contemplate pass at or past the cutoff."""
    assert max_scoreable_age(every) == expected


def test_iteration_start_read_span_reaches_oldest_closed_bet() -> None:
    """The starts read is sized from the oldest closed bet's age, so a slow
    contemplate cadence never under-reads the bet's bound iteration."""
    class _Mem:
        def __init__(self) -> None:
            self.n: list[int] = []

        def get_iteration_starts(self, n: int) -> dict[int, str]:
            self.n.append(n)
            return {}

    class _Gates:
        def read_tail(self, n: int) -> list[object]:
            return []

    mem = _Mem()
    bet = {"prediction_id": "reflection-10",
           "loss_condition": "gate wip_primacy fires within 3 cycles",
           "timestamp": "2026-01-01T00:00:00+00:00", "verdict": "unscored"}
    audit_checks_calibration.build_calibration_evidence(
        [bet], mem, _Gates(), current_iteration=500,  # type: ignore[arg-type]
        current_started_at="",
    )
    assert mem.n == [491]


def test_window_closes_at_elapsed_is_n_plus_one() -> None:
    """Iteration ``c + N`` is inside the window; it closes once ``c + N + 1``
    begins, and the cutoff stays strictly after the close."""
    window = _window_or_none("3")
    assert window is not None
    assert window.closes_at_elapsed == 4
    assert window.cutoff_after_cycles == 4 + _CUTOFF_GRACE_CYCLES


def test_audit_checks_has_no_hand_written_window_bound() -> None:
    """The evidence bound reads ``closes_at_elapsed``; no arithmetic on
    ``matures_after_cycles`` (any spelling) can re-introduce the off-by-one.

    Scans the whole ``audit_checks*`` family: the calibration readers moved to
    ``audit_checks_calibration`` (proposal 147 Step 9)."""
    family = sorted(Path(audit_checks.__file__).parent.glob("audit_checks*.py"))
    assert len(family) >= 4
    tree = ast.Module(
        body=[
            stmt
            for path in family
            for stmt in ast.parse(path.read_text(encoding="utf-8")).body
        ],
        type_ignores=[],
    )
    hand_written = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and any(
            isinstance(side, ast.Attribute) and side.attr == "matures_after_cycles"
            for side in (node.left, node.right)
        )
    ]
    assert hand_written == []
    reads = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        and any(
            isinstance(n, ast.Attribute) and n.attr == "closes_at_elapsed"
            for n in ast.walk(fn)
        )
    }
    assert reads >= {"calibration_window_ends", "build_calibration_evidence"}


def test_authored_verdict_is_frozen() -> None:
    """The authorship record is a small frozen value object."""
    authored = author_reflection_verdict(_score("correct"), "correct")
    assert isinstance(authored, AuthoredVerdict)
    with pytest.raises(AttributeError):
        authored.verdict = "wrong"  # type: ignore[misc]


# ── parse_claim_citation: one accepted form per family (proposal 129) ───────────


@pytest.mark.parametrize(
    ("text", "kind", "target"),
    [
        ("task 3f9a1c2d4e5b", "TASK", "3f9a1c2d4e5b"),
        ("TASK 3F9A1C2D4E5B", "TASK", "3f9a1c2d4e5b"),  # keyword + hex case-folded
        ("task 3f9a1c2d4e5b.", "TASK", "3f9a1c2d4e5b"),  # trailing period tolerated
        ("sha 1a2b3c4d", "SHA", "1a2b3c4d"),
        ("sha 1A2B3C4D5E6F", "SHA", "1a2b3c4d5e6f"),
        ("sensor FORCED-QUIET-RATIO", "SENSOR_TAG", "FORCED-QUIET-RATIO"),
        ("sensor forced-quiet-ratio", "SENSOR_TAG", "FORCED-QUIET-RATIO"),  # upper
        ("sensor PR-READY", "SENSOR_TAG", "PR-READY"),
    ],
)
def test_parse_claim_citation_accepts_each_family(
    text: str, kind: str, target: str
) -> None:
    """Each family's canonical form parses to the right kind and normalised target.

    Task/SHA hex is lowercased, sensor tags upper-cased to the lexicon's canonical
    case, and the leading keyword disambiguates the kind so ``task``/``sha`` never
    cross-match despite the shared hex body.
    """
    citation = parse_claim_citation(text)
    assert citation == ClaimCitation(kind=kind, target=target)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "garbage",
        "task",  # no id
        "task xyz123",  # non-hex body
        "task 3f9",  # too short (< 6 hex)
        "sha",  # no hex
        "sensor",  # no tag
        "sensor a b",  # a tag token cannot contain a space
        "task 3f9a1c2d4e5b extra",  # trailing prose after the anchor
        "pr #123 settles within 3 cycles",  # a loss condition, not a citation
    ],
)
def test_parse_claim_citation_declines_unparseable(text: str) -> None:
    """Any unmatched keyword, malformed body, or empty input declines to ``None``
    — never raises — so an inexpressible citation is recorded ``unresolvable``."""
    assert parse_claim_citation(text) is None


# ── resolve_claim_citation: existence + polarity, never entailment ──────────────


def _sources() -> ClaimEvidenceSources:
    """A populated bundle: one successful, one failed, one partial task, one
    archived SHA, one emitted sensor tag."""
    return ClaimEvidenceSources(
        successful_task_ids=frozenset({"aaaaaaaaaaaa"}),
        failed_task_ids=frozenset({"bbbbbbbbbbbb"}),
        known_task_ids=frozenset(
            {"aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"}
        ),
        archived_shas=("1a2b3c4d5e6f7788990011223344556677889900",),
        sensor_tags=frozenset({"FORCED-QUIET-RATIO"}),
    )


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("task aaaaaaaaaaaa", "resolved"),        # in successful set
        ("task bbbbbbbbbbbb", "wrong_polarity"),  # in failed set
        ("task cccccccccccc", "wrong_polarity"),  # known but non-success (partial)
        ("task dddddddddddd", "absent"),          # unknown id
        ("sha 1a2b3c4d", "resolved"),             # abbreviated prefix of the archived SHA
        ("sha 1A2B3C4D", "resolved"),             # prefix match is case-insensitive
        ("sha ffffffff", "absent"),               # no archived SHA has this prefix
        ("sensor FORCED-QUIET-RATIO", "resolved"),
        ("sensor forced-quiet-ratio", "resolved"),  # tag upper-cased on parse
        ("sensor UNKNOWN-TAG", "absent"),
        ("garbage", "unresolvable"),              # parse declines
    ],
)
def test_resolve_citation_status_maps_each_case(text: str, status: str) -> None:
    """``resolve_citation_status`` composes parse + resolve into the whole
    :data:`ClaimResolution` range, mapping an unparseable citation to
    ``unresolvable`` (the parse-failure sentinel resolve itself never returns)."""
    _citation, resolved = resolve_citation_status(text, _sources())
    assert resolved == status


def test_resolve_claim_citation_empty_sources_is_absent() -> None:
    """An all-empty bundle resolves every parseable citation to ``absent`` —
    never ``wrong_polarity`` (nothing is known) and never ``resolved``."""
    empty = ClaimEvidenceSources()
    for text in ("task aaaaaaaaaaaa", "sha 1a2b3c4d", "sensor FORCED-QUIET-RATIO"):
        citation = parse_claim_citation(text)
        assert citation is not None
        assert resolve_claim_citation(citation, empty) == "absent"


def test_resolve_claim_citation_never_returns_unresolvable() -> None:
    """Given a *parsed* citation, resolve reports existence/polarity only; the
    ``unresolvable`` status is reserved for the parse-failure path."""
    sources = _sources()
    for text in (
        "task aaaaaaaaaaaa", "task bbbbbbbbbbbb", "task dddddddddddd",
        "sha 1a2b3c4d", "sha ffffffff", "sensor FORCED-QUIET-RATIO",
        "sensor UNKNOWN-TAG",
    ):
        citation = parse_claim_citation(text)
        assert citation is not None
        assert resolve_claim_citation(citation, sources) != "unresolvable"


# ── CLAIM_CITATION_GRAMMAR: the prompt's vocabulary must match the parser ───────


def test_claim_citation_grammar_covers_every_kind_in_order() -> None:
    """Every ``CLAIM_CITATION_GRAMMAR`` line parses, and the produced kinds equal
    ``get_args(CitationKind)`` exactly — one canonical example per kind, in order.

    The pin that stops the ``update_self_model`` prompt teaching a citation shape
    the parser rejects, mirroring
    :func:`test_loss_condition_grammar_covers_every_probe_kind_in_order`.
    """
    citations = [parse_claim_citation(line) for line in CLAIM_CITATION_GRAMMAR]
    assert None not in citations, (
        f"every CLAIM_CITATION_GRAMMAR line must parse; got {citations}"
    )
    kinds = [c.kind for c in citations if c is not None]
    assert kinds == list(get_args(CitationKind))


def test_claim_evidence_sources_is_frozen() -> None:
    """The caller-supplied bundle is a frozen value object."""
    sources = ClaimEvidenceSources()
    with pytest.raises(AttributeError):
        sources.known_task_ids = frozenset({"x"})  # type: ignore[misc]


# ── CONSCIOUSNESS_CRITERIA registry (consciousness-report property list) ────────


def test_consciousness_criteria_membership_and_wellformed() -> None:
    """The registry names identity availability, the six now-grounded
    global-workspace indicators, and the three remaining functional properties, in
    report order, with unique well-formed entries."""
    keys = [c.key for c in CONSCIOUSNESS_CRITERIA]
    assert keys == [
        "identity_availability",
        "limited_capacity_selection",
        "receipted_broadcast",
        "recurrence",
        "attention_schema_agreement",
        "temporal_continuity",
        "causal_reach",
        "perceptual_grounding",
        "metacognitive_calibration",
        "narration_actuation_conversion",
    ]
    # Keys are unique.
    assert len(keys) == len(set(keys))
    # No entry is deferred any longer — the six global-workspace indicators now
    # read measured from the episode ledger (proposal 130).
    deferred = {c.key for c in CONSCIOUSNESS_CRITERIA if not c.implemented}
    assert deferred == set()
    implemented = {c.key for c in CONSCIOUSNESS_CRITERIA if c.implemented}
    assert implemented == {
        "identity_availability",
        "limited_capacity_selection",
        "receipted_broadcast",
        "recurrence",
        "attention_schema_agreement",
        "temporal_continuity",
        "causal_reach",
        "perceptual_grounding",
        "metacognitive_calibration",
        "narration_actuation_conversion",
    }
    # Every entry is well-formed: a frozen dataclass with non-empty string label
    # and scope note and a bool implemented flag.
    for criterion in CONSCIOUSNESS_CRITERIA:
        assert isinstance(criterion, ConsciousnessCriterion)
        assert isinstance(criterion.key, str) and criterion.key
        assert isinstance(criterion.label, str) and criterion.label
        assert isinstance(criterion.measures, str) and criterion.measures
        assert isinstance(criterion.implemented, bool)


def test_consciousness_criterion_is_frozen() -> None:
    """Each criterion is a frozen value object (a stable registry row)."""
    criterion = CONSCIOUSNESS_CRITERIA[0]
    with pytest.raises(AttributeError):
        criterion.key = "mutated"  # type: ignore[misc]


def test_self_calibration_module_stays_cycle_free() -> None:
    """The module imports neither ``audit_checks`` nor ``consciousness``.

    The consciousness-report registry lives here so the property list has one
    home without dragging in a measure function; keeping the import list clear of
    ``audit_checks`` / ``consciousness`` preserves the cycle-free invariant the
    module's docstring promises (the CLI consumer binds each criterion to its
    reading). An AST walk over the source is authoritative even if a future edit
    adds an import inside a function body.
    """
    source = (_MAESTRO_ROOT / "self_calibration.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    assert "maestro.audit_checks" not in imported
    assert "maestro.audit_checks_calibration" not in imported
    assert "maestro.consciousness" not in imported


# ── stale_repeat_loss_conditions: the comfort-bet verbatim-repeat guard ─────────


def test_stale_repeat_returns_string_repeated_without_correct() -> None:
    """A loss condition in >= threshold rows, none scoring 'correct', is banned.

    Criterion 1: returned iff it occurs in at least *threshold* prediction rows
    and none of those rows scored ``'correct'``. Two 'wrong' rows for the same
    loss condition are the comfort-bet signature.
    """
    rows = [
        {"loss_condition": "pr #10 settles within 5 cycles", "verdict": "wrong"},
        {"loss_condition": "pr #10 settles within 5 cycles", "verdict": "pending"},
    ]
    assert stale_repeat_loss_conditions(rows) == {
        "pr #10 settles within 5 cycles"
    }


def test_stale_repeat_excludes_string_that_scored_correct_once() -> None:
    """Criterion 2: one 'correct' scoring excludes it regardless of repeat count."""
    rows = [
        {"loss_condition": "gate wip_primacy does not fire", "verdict": "wrong"},
        {"loss_condition": "gate wip_primacy does not fire", "verdict": "correct"},
        {"loss_condition": "gate wip_primacy does not fire", "verdict": "wrong"},
    ]
    assert stale_repeat_loss_conditions(rows) == set()


def test_stale_repeat_excludes_below_threshold() -> None:
    """A loss condition appearing fewer than *threshold* times is not banned."""
    rows = [
        {"loss_condition": "pr #10 settles within 5 cycles", "verdict": "wrong"},
    ]
    assert stale_repeat_loss_conditions(rows) == set()
    # Raising the threshold un-bans a twice-repeated loser.
    twice = [
        {"loss_condition": "pr #10 settles within 5 cycles", "verdict": "wrong"},
        {"loss_condition": "pr #10 settles within 5 cycles", "verdict": "wrong"},
    ]
    assert stale_repeat_loss_conditions(twice, threshold=3) == set()


def test_stale_repeat_normalises_whitespace_and_case() -> None:
    """Whitespace/case variants of one bet collapse to a single banned key.

    The two rows differ only in casing and internal whitespace, so they count as
    the same loss condition and cross the threshold; the returned key is the
    normalised (collapsed, lowercased) form.
    """
    rows = [
        {"loss_condition": "PR #10  settles within 5 cycles", "verdict": "wrong"},
        {"loss_condition": "pr #10 settles within 5 cycles", "verdict": "pending"},
    ]
    assert stale_repeat_loss_conditions(rows) == {
        "pr #10 settles within 5 cycles"
    }


def test_stale_repeat_skips_blank_and_non_string_loss_conditions() -> None:
    """A blank or non-string loss condition is never counted or banned."""
    rows = [
        {"loss_condition": "   ", "verdict": "wrong"},
        {"loss_condition": "   ", "verdict": "wrong"},
        {"loss_condition": None, "verdict": "wrong"},
        {"verdict": "wrong"},
    ]
    assert stale_repeat_loss_conditions(rows) == set()
