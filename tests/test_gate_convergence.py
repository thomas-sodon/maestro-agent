"""Tests for the pure gate-convergence predicate + result type (proposal 110).

``plan_next_round`` decides, after each fix round, whether every observed gate is
CLEAN (``green``), the failing set strictly shrank (``continue``), or it stalled
(``stuck``); green binds to the ``all_clean`` per-gate status (proposal 124), not
an empty failing set, and the failure sets carry *failure-identity* signatures
(``GateRunObservation.signature``) when the caller supplies an observation,
``QualityGateResult.failure_signature``'s coarse gate labels otherwise (selected by
``EvolutionEngine._progress_signals``). All are pure — no I/O, no SDK — so these
tests need no fixtures. One assertion per branch, mirroring
``tests/test_no_progress_laps.py``.
"""

from __future__ import annotations

import dataclasses

import pytest

from maestro.evolution import EvolutionEngine, QualityGateResult
from maestro.gate_convergence import (
    ConvergenceResult,
    plan_next_review_round,
    plan_next_round,
)

# ---------------------------------------------------------------------------
# plan_next_round — one assertion per branch, in rule order.
# ---------------------------------------------------------------------------


def test_plan_next_round_all_clean_is_green() -> None:
    """Green binds to per-gate CLEAN status (proposal 124), not to an empty
    failing set: ``all_clean=True`` is green even with a still-non-empty
    ``curr_failures``. The pairing is impossible for a real observation (both
    derive from the same ``gates`` tuple) and is constructed here only to prove the
    predicate keys on ``all_clean`` alone."""
    decision = plan_next_round(
        frozenset({"ruff"}), frozenset({"ruff"}),
        all_clean=True, rounds_used=1, ceiling=20,
    )
    assert decision == "green"


def test_plan_next_round_empty_current_not_clean_is_not_green() -> None:
    """The independent lock (proposal 124): an empty ``curr_failures`` no longer
    reads as green on its own. The reachable pairing is a run that observed *no*
    gates — ``GateRunObservation.all_clean`` is ``False`` (fail-closed) while its
    ``signature()`` is empty — so with ``all_clean=False`` and an empty failing set
    the predicate stalls rather than passing that unverified branch off as green."""
    decision = plan_next_round(
        frozenset(), frozenset(),
        all_clean=False, rounds_used=1, ceiling=20,
    )
    assert decision == "stuck"


def test_plan_next_round_strict_shrink_is_continue() -> None:
    """A strict subset (a gate cleared) means real progress — keep going."""
    decision = plan_next_round(
        frozenset({"ruff", "mypy"}), frozenset({"mypy"}),
        all_clean=False, rounds_used=1, ceiling=20,
    )
    assert decision == "continue"


def test_plan_next_round_shrinking_identity_set_single_label_is_continue() -> None:
    """A strictly shrinking *failure-identity* set drawn from a single gate label
    is real progress (proposal 124) — three failing tests down to one is a strict
    subset, the granularity the coarse three-label signal could not see, so the
    loop continues instead of stalling on round two."""
    decision = plan_next_round(
        frozenset({"tests:a", "tests:b", "tests:c"}), frozenset({"tests:a"}),
        all_clean=False, rounds_used=1, ceiling=20,
    )
    assert decision == "continue"


def test_plan_next_round_no_shrink_equal_is_stuck() -> None:
    """An unchanged failing set (no gate cleared) is a stall."""
    decision = plan_next_round(
        frozenset({"ruff", "mypy"}), frozenset({"ruff", "mypy"}),
        all_clean=False, rounds_used=1, ceiling=20,
    )
    assert decision == "stuck"


def test_plan_next_round_unchanged_identity_set_is_stuck() -> None:
    """An unchanged *failure-identity* set (no individual test cleared) stalls,
    exactly as an unchanged label set does (proposal 124)."""
    decision = plan_next_round(
        frozenset({"tests:a", "tests:b"}), frozenset({"tests:a", "tests:b"}),
        all_clean=False, rounds_used=1, ceiling=20,
    )
    assert decision == "stuck"


def test_plan_next_round_grew_is_stuck() -> None:
    """A superset (a new gate broke) is not progress — stall."""
    decision = plan_next_round(
        frozenset({"ruff"}), frozenset({"ruff", "mypy"}),
        all_clean=False, rounds_used=1, ceiling=20,
    )
    assert decision == "stuck"


def test_plan_next_round_disjoint_is_stuck() -> None:
    """A same-size but non-subset set (one cleared, another broke) stalls."""
    decision = plan_next_round(
        frozenset({"ruff"}), frozenset({"mypy"}),
        all_clean=False, rounds_used=1, ceiling=20,
    )
    assert decision == "stuck"


def test_plan_next_round_ceiling_reached_is_stuck_even_when_shrank() -> None:
    """The ceiling backstop wins even when the set strictly shrank."""
    decision = plan_next_round(
        frozenset({"ruff", "mypy"}), frozenset({"mypy"}),
        all_clean=False, rounds_used=20, ceiling=20,
    )
    assert decision == "stuck"


def test_plan_next_round_all_clean_wins_over_ceiling() -> None:
    """A green head wins over the ceiling backstop — ``all_clean`` is checked
    first (proposal 124), so a run that went CLEAN on the ceiling round is green,
    not stuck, even with a still-non-empty ``curr_failures``."""
    decision = plan_next_round(
        frozenset({"ruff"}), frozenset({"ruff"}),
        all_clean=True, rounds_used=20, ceiling=20,
    )
    assert decision == "green"


def test_plan_next_round_over_ceiling_is_stuck() -> None:
    """rounds_used strictly past the ceiling also trips the backstop."""
    decision = plan_next_round(
        frozenset({"ruff", "mypy"}), frozenset({"mypy"}),
        all_clean=False, rounds_used=21, ceiling=20,
    )
    assert decision == "stuck"


# ---------------------------------------------------------------------------
# EvolutionEngine._progress_signals — the flag-selected progress vocabulary
# feeding plan_next_round (proposal 124). A ``@staticmethod``, so callable off
# the class with no instance.
# ---------------------------------------------------------------------------


def test_progress_signals_flag_on_non_answer_keeps_synthetic_identity() -> None:
    """``_progress_signals`` reads a real non-answer observation at
    failure-identity granularity (proposal 124): a ``TIMEOUT`` gate whose parsed
    ``failures`` tuple is empty still contributes its synthetic ``tests:TIMEOUT``
    identity and reports ``all_clean=False``, so the fix loop can never mistake a
    gate that never finished for a clean one. Binds the predicate to the real
    vocabulary rather than a hand-built ``all_clean`` bool."""
    from maestro.gate_observation import GateObservation, GateRunObservation

    review = QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output="timed out",
        observation=GateRunObservation(
            gates=(
                GateObservation(
                    tool="tests", kind="TIMEOUT", failures=(), output="",
                ),
            ),
        ),
    )
    signature, all_clean = EvolutionEngine._progress_signals(review)
    assert signature == frozenset({"tests:TIMEOUT"})
    assert all_clean is False


def test_progress_signals_flag_off_reads_coarse_labels() -> None:
    """With no observation (flag off) ``_progress_signals`` returns the pre-124
    pair — the three coarse gate labels and the aggregate ``passed`` — byte-identical
    to the behaviour before proposal 124 (step 7)."""
    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=False,
        raw_output="",
    )
    signature, all_clean = EvolutionEngine._progress_signals(review)
    assert signature == frozenset({"ruff", "tests"})
    assert all_clean is False


# ---------------------------------------------------------------------------
# plan_next_review_round — one assertion per branch, in rule order,
# mirroring the plan_next_round branch tests above.
# ---------------------------------------------------------------------------


def test_plan_next_review_round_approved_is_green() -> None:
    """An LGTM approval means the review converged — proceed."""
    decision = plan_next_review_round(
        approved=True, made_changes=False, rounds_used=1, ceiling=20,
        gate_clean=False, verify_grace_remaining=0,
    )
    assert decision == "green"


def test_plan_next_review_round_approved_at_ceiling_is_green() -> None:
    """An approval wins over the ceiling backstop — approved is checked first."""
    decision = plan_next_review_round(
        approved=True, made_changes=True, rounds_used=20, ceiling=20,
        gate_clean=False, verify_grace_remaining=0,
    )
    assert decision == "green"


def test_plan_next_review_round_made_changes_under_ceiling_is_continue() -> None:
    """A committed fix under the ceiling is real progress — keep going."""
    decision = plan_next_review_round(
        approved=False, made_changes=True, rounds_used=1, ceiling=20,
        gate_clean=False, verify_grace_remaining=0,
    )
    assert decision == "continue"


def test_plan_next_review_round_no_approval_no_change_is_stuck() -> None:
    """No approval and no fix committed is a stall — an identical round can't help."""
    decision = plan_next_review_round(
        approved=False, made_changes=False, rounds_used=1, ceiling=20,
        gate_clean=False, verify_grace_remaining=0,
    )
    assert decision == "stuck"


def test_plan_next_review_round_ceiling_reached_is_stuck_even_when_changed() -> None:
    """The ceiling backstop wins even while the reviewer is still committing fixes."""
    decision = plan_next_review_round(
        approved=False, made_changes=True, rounds_used=20, ceiling=20,
        gate_clean=False, verify_grace_remaining=0,
    )
    assert decision == "stuck"


def test_plan_next_review_round_over_ceiling_is_stuck() -> None:
    """rounds_used strictly past the ceiling also trips the backstop."""
    decision = plan_next_review_round(
        approved=False, made_changes=True, rounds_used=21, ceiling=20,
        gate_clean=False, verify_grace_remaining=0,
    )
    assert decision == "stuck"


def test_plan_next_review_round_gate_clean_with_grace_is_continue() -> None:
    """A verify-only round against a green gate with grace left buys another round.

    Proposal 124: no approval and no committed fix, but every gate is CLEAN and
    the bounded verify-only grace is unspent — a continuation, not a demotion.
    """
    decision = plan_next_review_round(
        approved=False, made_changes=False, rounds_used=1, ceiling=20,
        gate_clean=True, verify_grace_remaining=1,
    )
    assert decision == "continue"


def test_plan_next_review_round_gate_clean_grace_spent_is_stuck() -> None:
    """A green gate cannot buy a round once the verify-only grace is spent."""
    decision = plan_next_review_round(
        approved=False, made_changes=False, rounds_used=2, ceiling=20,
        gate_clean=True, verify_grace_remaining=0,
    )
    assert decision == "stuck"


def test_plan_next_review_round_gate_red_with_grace_is_stuck() -> None:
    """Grace does not rescue a red gate — a would-be stall stays a stall."""
    decision = plan_next_review_round(
        approved=False, made_changes=False, rounds_used=1, ceiling=20,
        gate_clean=False, verify_grace_remaining=1,
    )
    assert decision == "stuck"


def test_plan_next_review_round_approval_outranks_grace() -> None:
    """An approval wins even when a green gate and grace would also continue."""
    decision = plan_next_review_round(
        approved=True, made_changes=False, rounds_used=1, ceiling=20,
        gate_clean=True, verify_grace_remaining=1,
    )
    assert decision == "green"


def test_plan_next_review_round_ceiling_outranks_grace() -> None:
    """The ceiling backstop wins even when a green gate and grace would continue."""
    decision = plan_next_review_round(
        approved=False, made_changes=False, rounds_used=20, ceiling=20,
        gate_clean=True, verify_grace_remaining=1,
    )
    assert decision == "stuck"


# ---------------------------------------------------------------------------
# QualityGateResult.failure_signature
# ---------------------------------------------------------------------------


def _result(*, ruff: bool, mypy: bool, tests: bool) -> QualityGateResult:
    return QualityGateResult(
        passed=ruff and mypy and tests,
        ruff_ok=ruff, mypy_ok=mypy, tests_ok=tests,
        raw_output="",
    )


def test_failure_signature_all_pass_is_empty() -> None:
    """A passing result yields the empty frozenset."""
    sig = _result(ruff=True, mypy=True, tests=True).failure_signature()
    assert sig == frozenset()


def test_failure_signature_names_only_failing_gates() -> None:
    """Only the failing gates appear in the signature."""
    sig = _result(ruff=False, mypy=True, tests=False).failure_signature()
    assert sig == frozenset({"ruff", "tests"})


def test_failure_signature_all_fail() -> None:
    """Every gate failing yields all three identifiers."""
    sig = _result(ruff=False, mypy=False, tests=False).failure_signature()
    assert sig == frozenset({"ruff", "mypy", "tests"})


def test_failure_signature_identical_results_equal() -> None:
    """Identical failing gates yield equal signatures."""
    a = _result(ruff=False, mypy=True, tests=False).failure_signature()
    b = _result(ruff=False, mypy=True, tests=False).failure_signature()
    assert a == b


def test_failure_signature_resolving_one_gate_is_strict_subset() -> None:
    """Resolving one gate yields a strict subset of the prior signature."""
    before = _result(ruff=False, mypy=False, tests=True).failure_signature()
    after = _result(ruff=False, mypy=True, tests=True).failure_signature()
    assert after < before


# ---------------------------------------------------------------------------
# ConvergenceResult
# ---------------------------------------------------------------------------


def test_convergence_result_is_frozen() -> None:
    """ConvergenceResult is an immutable value object."""
    review = _result(ruff=True, mypy=True, tests=True)
    result = ConvergenceResult("green", review, rounds_used=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.rounds_used = 1  # type: ignore[misc]


def test_convergence_result_carries_fields() -> None:
    """The result exposes the outcome, final review, and round count."""
    review = _result(ruff=False, mypy=True, tests=True)
    result = ConvergenceResult("stuck", review, rounds_used=3)
    assert result.outcome == "stuck"
    assert result.review is review
    assert result.rounds_used == 3


def test_convergence_result_attribution_defaults_to_none() -> None:
    """The trailing ``attribution`` field (proposal 124) defaults to ``None`` so
    every existing positional construction is unaffected and it is ``None``
    whenever ``gate_observation_enabled`` is off."""
    review = _result(ruff=True, mypy=True, tests=True)
    result = ConvergenceResult("green", review, rounds_used=0)
    assert result.attribution is None


def test_convergence_result_carries_attribution() -> None:
    """A ``ConvergenceResult`` constructed with an attribution verdict round-trips
    it (proposal 124)."""
    review = _result(ruff=False, mypy=True, tests=True)
    result = ConvergenceResult(
        "stuck", review, rounds_used=2, attribution="INHERITED",
    )
    assert result.attribution == "INHERITED"


def test_convergence_result_runnability_defaults_to_none() -> None:
    """The trailing ``runnability`` field (proposal 133) defaults to ``None``, so
    every pre-133 construction — positional or keyword — is unaffected."""
    review = _result(ruff=True, mypy=True, tests=True)
    result = ConvergenceResult("green", review, rounds_used=0)
    assert result.runnability is None


def test_convergence_result_carries_the_runnability_verdict() -> None:
    """An ``ENVIRONMENT`` result round-trips the verdict that produced it.

    The hold in ``implement_plan`` needs the verdict's ``fingerprint`` (the stamp),
    ``kind`` and ``detail`` (the repair directive). Carrying it here rather than
    re-reading the cached row is what makes it non-``None`` exactly when the
    attribution is ``ENVIRONMENT``.
    """
    from maestro.gate_environment import GateRunnabilityVerdict

    verdict = GateRunnabilityVerdict(kind="UNFIT", fingerprint="fp", detail="slow")
    review = _result(ruff=True, mypy=True, tests=False)
    result = ConvergenceResult(
        "stuck", review, rounds_used=1,
        attribution="ENVIRONMENT", runnability=verdict,
    )

    assert result.attribution == "ENVIRONMENT"
    assert result.runnability is verdict


# ---------------------------------------------------------------------------
# #4309 — a partial fix that shifts lines, or churns a capped window, is progress.
# ---------------------------------------------------------------------------


def _progress(tool, stdout, *, returncode=1):
    """The run-level progress signature, count and truncation of one parsed gate."""
    from maestro.gate_observation import GateRunObservation, parse_gate_output

    run = GateRunObservation(gates=(parse_gate_output(tool, stdout, "", returncode),))
    return run.progress_signature(), run.enumerated_count(), run.any_truncated()


def test_mypy_line_shift_after_partial_fix_reads_as_progress() -> None:
    """Clearing the error at line 12 shifts the survivor from 50 to 51; the
    line-numbered identity renames it, the progress key does not."""
    prev, _, _ = _progress("mypy", (
        'maestro/x.py:12: error: Name "y" is not defined  [name-defined]\n'
        'maestro/x.py:50: error: Argument 1 has incompatible type  [arg-type]\n'
    ))
    curr, _, _ = _progress("mypy", (
        'maestro/x.py:51: error: Argument 1 has incompatible type  [arg-type]\n'
    ))
    decision = plan_next_round(prev, curr, all_clean=False, rounds_used=1, ceiling=10)
    assert decision == "continue"


def test_ruff_line_shift_after_partial_fix_reads_as_progress() -> None:
    """An ``F401`` removed above a surviving ``E501`` shifts its line and column."""
    prev, _, _ = _progress("ruff", (
        "maestro/x.py:1:8: F401 [*] `os` imported but unused\n"
        "maestro/x.py:40:89: E501 Line too long (120 > 88)\n"
    ))
    curr, _, _ = _progress("ruff", (
        "maestro/x.py:39:90: E501 Line too long (120 > 88)\n"
    ))
    decision = plan_next_round(prev, curr, all_clean=False, rounds_used=1, ceiling=10)
    assert decision == "continue"


def test_mypy_line_reference_in_message_does_not_rename_the_failure() -> None:
    """A ``line N`` inside the message moves with the edit too, and is blanked."""
    prev, _, _ = _progress("mypy", (
        "maestro/x.py:3: error: Unused ignore  [unused-ignore]\n"
        'maestro/x.py:20: error: Name "f" already defined on line 10  [no-redef]\n'
    ))
    curr, _, _ = _progress("mypy", (
        'maestro/x.py:19: error: Name "f" already defined on line 9  [no-redef]\n'
    ))
    decision = plan_next_round(prev, curr, all_clean=False, rounds_used=1, ceiling=10)
    assert decision == "continue"


def test_identical_mypy_errors_count_as_a_multiset() -> None:
    """Two same-code, same-message errors in one file are two members; fixing
    one shrinks the set, and an unchanged pair is stuck."""
    line = "error: Missing return statement  [return]\n"
    prev, _, _ = _progress("mypy", f"maestro/x.py:5: {line}maestro/x.py:9: {line}")
    curr, _, _ = _progress("mypy", f"maestro/x.py:8: {line}")
    same, _, _ = _progress("mypy", f"maestro/x.py:6: {line}maestro/x.py:10: {line}")
    assert len(prev) == 2
    assert plan_next_round(
        prev, curr, all_clean=False, rounds_used=1, ceiling=10,
    ) == "continue"
    assert plan_next_round(
        prev, same, all_clean=False, rounds_used=1, ceiling=10,
    ) == "stuck"


def test_mypy_hidden_error_codes_keep_an_empty_code_progress_key() -> None:
    """Under ``--hide-error-codes`` the key carries an empty code, still stable."""
    prev, _, _ = _progress("mypy", (
        "maestro/x.py:1: error: Bad thing\nmaestro/x.py:7: error: Other thing\n"
    ))
    curr, _, _ = _progress("mypy", "maestro/x.py:6: error: Other thing\n")
    assert curr == frozenset({"mypy:maestro/x.py::Other thing"})
    assert plan_next_round(
        prev, curr, all_clean=False, rounds_used=1, ceiling=10,
    ) == "continue"


def _pytest_rows(ids):
    return "".join(f"FAILED tests/test_m.py::{i} - AssertionError\n" for i in ids)


def test_truncated_pytest_window_churn_is_not_stuck() -> None:
    """60 failures fall to 55; the curr 50-row window holds ids the prev window
    never saw, which the subset compare reads as stuck. The counts decide."""
    prev_sig, prev_count, prev_trunc = _progress("tests", _pytest_rows(
        [f"t{n:02d}" for n in range(60)],
    ))
    curr_sig, curr_count, curr_trunc = _progress("tests", _pytest_rows(
        [f"t{n:02d}" for n in range(5, 60)],
    ))
    assert prev_trunc
    assert curr_trunc
    assert prev_count == 60
    assert curr_count == 55
    assert not curr_sig < prev_sig
    decision = plan_next_round(
        prev_sig, curr_sig, all_clean=False, rounds_used=1, ceiling=10,
        prev_count=prev_count, curr_count=curr_count, truncated=True,
    )
    assert decision == "continue"


def test_truncated_pytest_unchanged_set_reshuffled_is_stuck() -> None:
    """The same 60 failures in a different completion order are not progress."""
    ids = [f"t{n:02d}" for n in range(60)]
    prev_sig, prev_count, _ = _progress("tests", _pytest_rows(ids))
    curr_sig, curr_count, _ = _progress("tests", _pytest_rows(list(reversed(ids))))
    decision = plan_next_round(
        prev_sig, curr_sig, all_clean=False, rounds_used=1, ceiling=10,
        prev_count=prev_count, curr_count=curr_count, truncated=True,
    )
    assert decision == "stuck"


def test_truncated_compare_without_counts_is_refused() -> None:
    """A caller cannot silently fall back to the window compare."""
    with pytest.raises(AssertionError):
        plan_next_round(
            frozenset({"a", "b"}), frozenset({"a"}),
            all_clean=False, rounds_used=1, ceiling=10, truncated=True,
        )


def test_progress_counts_flag_off_is_label_count_untruncated() -> None:
    """No observation: the coarse labels are counted and never truncated."""
    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=False, raw_output="",
    )
    assert EvolutionEngine._progress_counts(review) == (2, False)


def test_truncated_prev_then_timeout_salvage_is_stuck() -> None:
    """60 answered failures, then a round killed at the deadline salvaging two
    rows: the count falls, but the gate stopped judging — not progress."""
    from maestro.gate_observation import GateRunObservation, parse_non_answer

    prev_sig, prev_count, _ = _progress("tests", _pytest_rows(
        [f"t{n:02d}" for n in range(60)],
    ))
    run = GateRunObservation(gates=(
        parse_non_answer("tests", "TIMEOUT", _pytest_rows(["t00", "t01"]), ""),
    ))
    curr_sig, curr_count = run.progress_signature(), run.enumerated_count()
    assert curr_count < prev_count
    decision = plan_next_round(
        prev_sig, curr_sig, all_clean=False, rounds_used=1, ceiling=10,
        prev_count=prev_count, curr_count=curr_count, truncated=True,
    )
    assert decision == "stuck"


def test_truncated_prev_then_collection_error_is_stuck() -> None:
    """A collection error hides the tests it would have run; its lower count
    must not read as progress."""
    prev_sig, prev_count, _ = _progress("tests", _pytest_rows(
        [f"t{n:02d}" for n in range(60)],
    ))
    curr_sig, curr_count, _ = _progress(
        "tests", "ERROR tests/test_m.py - ImportError: boom\n",
    )
    assert "tests:COLLECTION_ERROR" in curr_sig
    assert curr_count < prev_count
    decision = plan_next_round(
        prev_sig, curr_sig, all_clean=False, rounds_used=1, ceiling=10,
        prev_count=prev_count, curr_count=curr_count, truncated=True,
    )
    assert decision == "stuck"


def test_truncated_marker_carried_by_both_sides_still_uses_counts() -> None:
    """A marker the previous round already carried is not a new regression."""
    prev = frozenset({"tests:COLLECTION_ERROR", "a", "b"})
    curr = frozenset({"tests:COLLECTION_ERROR", "a"})
    decision = plan_next_round(
        prev, curr, all_clean=False, rounds_used=1, ceiling=10,
        prev_count=3, curr_count=2, truncated=True,
    )
    assert decision == "continue"


def test_regression_markers_ignores_real_failure_keys() -> None:
    from maestro.gate_observation import regression_markers

    prev = frozenset({"ruff:maestro/x.py:E501"})
    curr = frozenset({"ruff:TIMEOUT", "tests:tests/test_m.py::TIMEOUT"})
    assert regression_markers(prev, curr) == frozenset({"ruff:TIMEOUT"})


def test_ruff_measurement_change_in_message_is_still_progress() -> None:
    """A partial E501 fix changes the message's measurement; the key is the code."""
    prev, _, _ = _progress("ruff", (
        "maestro/x.py:1:8: F401 [*] `os` imported but unused\n"
        "maestro/x.py:40:89: E501 Line too long (120 > 88)\n"
    ))
    curr, _, _ = _progress("ruff", (
        "maestro/x.py:39:89: E501 Line too long (100 > 88)\n"
    ))
    assert curr == frozenset({"ruff:maestro/x.py:E501"})
    decision = plan_next_round(prev, curr, all_clean=False, rounds_used=1, ceiling=10)
    assert decision == "continue"


def test_resume_preamble_renders_each_progress_key_once() -> None:
    """Repeat ordinals are a comparison device and never reach the prompt."""
    from maestro.evolution import _resume_preamble

    text = _resume_preamble(["mypy:a.py:return", "mypy:a.py:return#2"])
    assert "#2" not in text
    assert text.count("mypy:a.py:return") == 1
