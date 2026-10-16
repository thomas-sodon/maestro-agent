"""Tests for the pure attempt-disposition oracle (proposal 131).

``classify_attempt`` maps a terminal implementation attempt — its convergence
``outcome``, base-vs-diff ``attribution``, current failing-identity set, and the
plan's own counts and budgets — to exactly one ``AttemptDisposition`` from the
closed ``AttemptVerdict`` vocabulary. Like ``maestro.gate_convergence``'s
predicates it is pure (no I/O, no SDK), so these tests need no fixtures and build
every input in memory (``GateRunObservation`` / ``GateObservation`` /
``GateFailure`` constructed directly, ``frozenset({...})`` for the failing sets).
One assertion per branch, in rule order, mirroring
``tests/test_gate_convergence.py``; the ``get_args`` totality check follows
``tests/test_remediation_tracker.py``'s ``TestClassifyLandingAttribution``.
"""

from __future__ import annotations

import dataclasses
from typing import get_args

import pytest

from maestro.attempt_disposition import (
    _MAX_QUESTION_IDENTITIES,
    AttemptDisposition,
    AttemptVerdict,
    _is_environment_red,
    classify_attempt,
)
from maestro.gate_observation import (
    GateFailure,
    GateObservation,
    GateRunObservation,
)

# ---------------------------------------------------------------------------
# In-memory builders (the unit is pure — no fixtures, no mem_fs, no mocking).
# ---------------------------------------------------------------------------


def _fail(identifier, *, tool="tests", kind="FAILED"):
    """One enumerated gate failure with a stable path-qualified identity."""
    return GateFailure(
        tool=tool, kind=kind, location="", identifier=identifier, message="",
    )


def _gate(kind, *, tool="tests", failures=()):
    """One gate observation of the given kind and (optional) failures."""
    return GateObservation(
        tool=tool, kind=kind, failures=tuple(failures), output="",
    )


def _run(*gates):
    """One whole-run observation over the given per-gate observations."""
    return GateRunObservation(gates=tuple(gates))


def _classify(observation, prior_failing, **overrides):
    """Call ``classify_attempt`` with sane defaults, overriding per test."""
    kwargs = {
        "outcome": "stuck",
        "attribution": "ATTRIBUTED",
        "failure_count": 1,
        "inherited_hold_count": 0,
        "resume_count": 0,
        "max_implementation_attempts": 3,
        "max_resume_attempts": 2,
    }
    kwargs.update(overrides)
    return classify_attempt(observation, prior_failing, **kwargs)


# ---------------------------------------------------------------------------
# classify_attempt — one assertion per branch, in rule order.
# ---------------------------------------------------------------------------


def test_green_outcome_is_land() -> None:
    """A green outcome lands — the PR-opening verdict, checked first and needing
    no observation."""
    disposition = _classify(None, frozenset(), outcome="green")
    assert disposition.verdict == "LAND"


def test_land_carries_no_question_or_context() -> None:
    """``LAND`` sets neither a question nor the carry-context flag."""
    disposition = _classify(None, frozenset(), outcome="green")
    assert disposition.question is None
    assert disposition.carry_context is False


def test_inherited_within_budget_is_hold() -> None:
    """A stalled ``INHERITED`` red with the hold budget unspent holds the plan
    (``inherited_hold_count + 1 < max_implementation_attempts``)."""
    obs = _run(_gate("FAILED", failures=(_fail("test_a"),)))
    disposition = _classify(
        obs, frozenset({"tests:test_a"}),
        attribution="INHERITED", inherited_hold_count=0,
    )
    assert disposition.verdict == "HOLD_INHERITED"


def test_inherited_at_last_allowed_hold_is_hold() -> None:
    """The boundary: ``inherited_hold_count + 1 == max - 1`` still holds."""
    obs = _run(_gate("FAILED", failures=(_fail("test_a"),)))
    disposition = _classify(
        obs, frozenset({"tests:test_a"}),
        attribution="INHERITED", inherited_hold_count=1,
        max_implementation_attempts=3,
    )
    assert disposition.verdict == "HOLD_INHERITED"


def test_inherited_over_budget_is_abandon() -> None:
    """An ``INHERITED`` red with the hold budget spent falls through to
    ``ABANDON`` (the failing set is unchanged and code-classed, so neither
    ``RESUME`` nor ``ASK_HUMAN`` intervenes)."""
    obs = _run(_gate("FAILED", failures=(_fail("test_a"),)))
    disposition = _classify(
        obs, frozenset({"tests:test_a"}),
        attribution="INHERITED", inherited_hold_count=2,
        max_implementation_attempts=3,
    )
    assert disposition.verdict == "ABANDON"


def test_inherited_over_budget_with_shrinking_prior_is_abandon_not_resume() -> None:
    """Regression (ledger r2): an ``INHERITED`` red with the hold budget spent must
    not be diverted to ``RESUME`` by a banked ``prior_failing`` it happens to be a
    strict subset of. A base-inherited red is the base's failure, not the diff's, so
    continuing from this branch cannot clear it — the hold-then-abandon track owns
    it. Without excluding ``INHERITED`` from the ``RESUME`` guard, a plan that banked
    a wider failing set on a prior attempt and later inherits a narrower base red
    would resume instead of demoting with the ``held N time(s)`` wording."""
    obs = _run(_gate("FAILED", failures=(_fail("test_a"),)))
    prior = frozenset({"tests:test_a", "tests:test_b"})  # current is a strict subset
    disposition = _classify(
        obs, prior,
        attribution="INHERITED", inherited_hold_count=2,
        max_implementation_attempts=3, resume_count=0, max_resume_attempts=2,
    )
    assert disposition.verdict == "ABANDON"


def test_inherited_over_budget_with_recurring_env_red_is_abandon_not_ask_human() -> None:
    """Regression (ledger r2): an ``INHERITED`` environment red with the hold budget
    spent must not be diverted to ``ASK_HUMAN`` by a banked ``prior_failing`` it
    recurs against. The ``ASK_HUMAN`` guard escalates a recurring environment red the
    diff is not responsible for, but a base-``INHERITED`` red is owned by the
    hold-then-abandon track, so it demotes rather than asking the human."""
    obs = _run(_gate("TIMEOUT"))
    prior = frozenset({"tests:TIMEOUT"})  # equal → recurring-unchanged
    disposition = _classify(
        obs, prior,
        attribution="INHERITED", inherited_hold_count=2,
        max_implementation_attempts=3,
    )
    assert disposition.verdict == "ABANDON"


def test_progressing_stall_within_resume_budget_is_resume() -> None:
    """A strictly shrinking failing set with the resume budget unspent resumes
    from the branch, carrying context."""
    obs = _run(_gate("FAILED", failures=(_fail("test_a"),)))
    prior = frozenset({"tests:test_a", "tests:test_b"})
    disposition = _classify(obs, prior, resume_count=0, max_resume_attempts=2)
    assert disposition.verdict == "RESUME"
    assert disposition.carry_context is True


def test_progressing_stall_across_a_mypy_line_shift_is_resume() -> None:
    """#4309: the prior baseline holds the survivor at line 50; this attempt
    cleared the other error, shifting the survivor to line 51. The position-free
    progress vocabulary reads that as progress, not ABANDON."""
    from maestro.gate_observation import parse_gate_output

    arg_type = "error: Argument 1 has incompatible type  [arg-type]\n"
    prior = _run(parse_gate_output("mypy", (
        'maestro/x.py:12: error: Name "y" is not defined  [name-defined]\n'
        f"maestro/x.py:50: {arg_type}"
    ), "", 1)).progress_signature()
    obs = _run(parse_gate_output("mypy", f"maestro/x.py:51: {arg_type}", "", 1))
    disposition = _classify(obs, prior)
    assert disposition.verdict == "RESUME"


def test_progressing_stall_past_resume_budget_is_abandon() -> None:
    """A strict shrink degrades to ``ABANDON`` once the resume budget is spent."""
    obs = _run(_gate("FAILED", failures=(_fail("test_a"),)))
    prior = frozenset({"tests:test_a", "tests:test_b"})
    disposition = _classify(obs, prior, resume_count=2, max_resume_attempts=2)
    assert disposition.verdict == "ABANDON"


def test_recurring_environment_red_is_ask_human() -> None:
    """A recurring-unchanged, non-code-fixable red asks the human, carrying a
    question that names the offending identities."""
    obs = _run(_gate("TIMEOUT"))
    prior = frozenset({"tests:TIMEOUT"})
    disposition = _classify(obs, prior, attribution="UNATTRIBUTABLE")
    assert disposition.verdict == "ASK_HUMAN"
    assert disposition.question is not None
    assert "tests:TIMEOUT" in disposition.question


def test_ask_human_question_caps_identities_at_the_bound() -> None:
    """The ASK_HUMAN question truncates its identity list to
    ``_MAX_QUESTION_IDENTITIES`` even when far more failures recur, honouring
    CLAUDE.md's context-bloat cap on every string that reaches a prompt."""
    # A non-answer (TIMEOUT) gate can salvage many enumerated rows alongside its
    # synthetic identity, so more than the cap of failing identities recur.
    failures = tuple(
        _fail(f"env_{i:02d}") for i in range(_MAX_QUESTION_IDENTITIES + 2)
    )
    obs = _run(_gate("TIMEOUT", failures=failures))
    prior = obs.signature()  # identical set → recurring-unchanged
    assert len(prior) > _MAX_QUESTION_IDENTITIES
    disposition = _classify(obs, prior, attribution="UNATTRIBUTABLE")
    assert disposition.verdict == "ASK_HUMAN"
    assert disposition.question is not None
    listed = disposition.question.split("fix: ", 1)[1].split(". This needs", 1)[0]
    assert len(listed.split(", ")) == _MAX_QUESTION_IDENTITIES


def test_recurring_code_red_is_not_ask_human() -> None:
    """A recurring-unchanged red that is code-fixable (an ordinary ``FAILED``)
    does not ask the human — it abandons."""
    obs = _run(_gate("FAILED", failures=(_fail("test_a"),)))
    prior = frozenset({"tests:test_a"})
    disposition = _classify(obs, prior)
    assert disposition.verdict == "ABANDON"


def test_recurring_attributed_collection_error_is_not_ask_human() -> None:
    """A recurring-unchanged ``COLLECTION_ERROR`` the diff is provably responsible
    for (``attribution == "ATTRIBUTED"``) is code-fixable — a bad import or broken
    fixture in the diff's own new test module — so it is never escalated as an
    unfixable environment red; it abandons. Were the ``ASK_HUMAN`` guard to fire on
    ``_is_environment_red`` alone (ignoring attribution), this diff-caused red would
    be misrouted to the human with a question claiming no code change can fix it."""
    obs = _run(_gate("COLLECTION_ERROR", failures=(_fail("test_a"),)))
    prior = frozenset({"tests:test_a"})
    disposition = _classify(obs, prior, attribution="ATTRIBUTED")
    assert disposition.verdict == "ABANDON"


def test_non_recurring_environment_red_is_not_ask_human() -> None:
    """An environment red whose failing set does not match the prior attempt's is
    not yet recurring, so it abandons rather than asking."""
    obs = _run(_gate("TIMEOUT"))
    prior = frozenset({"mypy:FAILED"})
    disposition = _classify(obs, prior)
    assert disposition.verdict == "ABANDON"


def test_no_observation_is_abandon() -> None:
    """A terminal with no gate observation (e.g. an unwrapped transport error)
    has an empty current failing set, so it can satisfy neither continuation
    guard and abandons."""
    disposition = _classify(
        None, frozenset({"tests:test_a"}), attribution="UNATTRIBUTABLE",
    )
    assert disposition.verdict == "ABANDON"


def test_first_attempt_no_prior_baseline_is_abandon() -> None:
    """With no prior failing set there is no baseline to progress against, so a
    strict shrink cannot fire and the recurrence guard's non-empty precondition
    fails — the terminal abandons."""
    obs = _run(_gate("FAILED", failures=(_fail("test_a"),)))
    disposition = _classify(obs, frozenset())
    assert disposition.verdict == "ABANDON"


def test_abandon_reason_names_the_attempt_count() -> None:
    """The ``ABANDON`` reason narrates the failure count it was given."""
    obs = _run(_gate("FAILED", failures=(_fail("test_a"),)))
    disposition = _classify(obs, frozenset(), failure_count=3)
    assert "3 failed attempt" in disposition.reason


# ---------------------------------------------------------------------------
# _is_environment_red — the non-code-fixable classifier used by the ASK_HUMAN
# guard. Grouped as a class, mirroring TestClassifyLandingAttribution.
# ---------------------------------------------------------------------------


class TestIsEnvironmentRed:
    """Whether every red gate in a run is a red no code change can clear."""

    def test_all_environment_kinds_is_true(self) -> None:
        obs = _run(
            _gate("TIMEOUT"),
            _gate("COLLECTION_ERROR", tool="tests"),
            _gate("UNAVAILABLE", tool="mypy"),
        )
        assert _is_environment_red(obs) is True

    def test_environment_red_alongside_clean_gate_is_true(self) -> None:
        obs = _run(_gate("CLEAN", tool="ruff"), _gate("TIMEOUT"))
        assert _is_environment_red(obs) is True

    def test_any_code_red_is_false(self) -> None:
        obs = _run(_gate("TIMEOUT"), _gate("FAILED", failures=(_fail("t"),)))
        assert _is_environment_red(obs) is False

    def test_coverage_red_is_false(self) -> None:
        obs = _run(_gate("COVERAGE"))
        assert _is_environment_red(obs) is False

    def test_all_clean_is_false(self) -> None:
        obs = _run(_gate("CLEAN"), _gate("CLEAN", tool="mypy"))
        assert _is_environment_red(obs) is False


# ---------------------------------------------------------------------------
# AttemptDisposition — the frozen value object.
# ---------------------------------------------------------------------------


def test_attempt_disposition_is_frozen() -> None:
    """AttemptDisposition is an immutable value object."""
    disposition = AttemptDisposition("LAND", None, False, "reason")
    with pytest.raises(dataclasses.FrozenInstanceError):
        disposition.verdict = "ABANDON"  # type: ignore[misc]


def test_attempt_disposition_carries_fields() -> None:
    """The result exposes verdict, question, carry_context, and reason."""
    disposition = AttemptDisposition("RESUME", None, True, "resuming")
    assert disposition.verdict == "RESUME"
    assert disposition.question is None
    assert disposition.carry_context is True
    assert disposition.reason == "resuming"


# ---------------------------------------------------------------------------
# Totality — every input shape maps to exactly one known verdict, and the
# field invariants (carry_context ⇒ RESUME; question iff ASK_HUMAN) hold.
# ---------------------------------------------------------------------------


def test_every_input_shape_maps_to_a_known_verdict() -> None:
    """A spread of input shapes always yields a member of the closed vocabulary,
    with question/carry_context bound to their verdicts."""
    verdicts = set(get_args(AttemptVerdict))
    observations = [
        None,
        _run(_gate("FAILED", failures=(_fail("test_a"),))),
        _run(_gate("TIMEOUT")),
        _run(_gate("CLEAN")),
    ]
    priors = [
        frozenset(),
        frozenset({"tests:test_a"}),
        frozenset({"tests:TIMEOUT"}),
        frozenset({"tests:test_a", "tests:test_b"}),
    ]
    for observation in observations:
        for prior in priors:
            for outcome in ("green", "continue", "stuck"):
                for attribution in (
                    None, "ATTRIBUTED", "INHERITED", "UNATTRIBUTABLE", "ENVIRONMENT",
                ):
                    for hold in (0, 2):
                        for resume in (0, 2):
                            disposition = classify_attempt(
                                observation, prior,
                                outcome=outcome, attribution=attribution,
                                failure_count=1, inherited_hold_count=hold,
                                resume_count=resume,
                                max_implementation_attempts=3,
                                max_resume_attempts=2,
                            )
                            assert disposition.verdict in verdicts
                            if disposition.carry_context:
                                assert disposition.verdict == "RESUME"
                            if disposition.verdict == "ASK_HUMAN":
                                assert disposition.question is not None
                            else:
                                assert disposition.question is None


# ---------------------------------------------------------------------------
# HOLD_ENVIRONMENT (proposal 133) — the unconditional arm.
# ---------------------------------------------------------------------------


def test_environment_attribution_holds_with_no_prior_and_no_failures() -> None:
    """A first, fresh ``ENVIRONMENT`` red holds — no prior baseline needed."""
    disposition = _classify(
        _run(_gate("TIMEOUT")),
        frozenset(),
        attribution="ENVIRONMENT",
        failure_count=0,
    )

    assert disposition.verdict == "HOLD_ENVIRONMENT"
    assert disposition.question is None
    assert disposition.carry_context is False


def test_environment_attribution_holds_with_the_inherited_budget_spent() -> None:
    """No budget of any kind gates the hold — an environment red charges nothing."""
    disposition = _classify(
        _run(_gate("TIMEOUT")),
        frozenset({"tests:TIMEOUT"}),
        attribution="ENVIRONMENT",
        failure_count=2,
        inherited_hold_count=3,
        resume_count=2,
    )

    assert disposition.verdict == "HOLD_ENVIRONMENT"


def test_environment_hold_never_fires_without_the_attribution() -> None:
    """The very same observation with an ordinary attribution still abandons."""
    disposition = _classify(
        _run(_gate("TIMEOUT")),
        frozenset(),
        attribution="UNATTRIBUTABLE",
        failure_count=2,
    )

    assert disposition.verdict == "ABANDON"


def test_green_still_outranks_the_environment_hold() -> None:
    """A green head lands even if the attribution reads ``ENVIRONMENT``."""
    disposition = _classify(
        None, frozenset(), outcome="green", attribution="ENVIRONMENT",
    )

    assert disposition.verdict == "LAND"
