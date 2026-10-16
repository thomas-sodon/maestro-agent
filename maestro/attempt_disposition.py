"""Pure attempt-disposition oracle (proposal 131).

A single owner of the decision *"what happens to an implementation attempt that
did not reach a green head."* Today that decision is a binary cliff — a green
head opens a PR, and every other exit (a scope-gate rejection, a self-review
that never reaches LGTM, the uncalled-symbol gate, a stalled quality-gate
convergence, an unclassified SDK error) funnels into the same demote-to-``failed``
path, with one bespoke inline hold bolted on for a base-inherited red. This
module is the pure decision authority the wiring increments route those scattered
terminals through: given the structured convergence outcome, the base-vs-diff
attribution verdict, and the plan's own attempt counts and budgets,
:func:`classify_attempt` returns exactly one disposition from a closed
vocabulary.

Like :mod:`maestro.gate_convergence`, the classifier is pure — zero I/O, zero
SDK — so it is trivially testable and carries no side effects. This increment
adds *only* the decision authority: nothing calls it in production yet, so the
implementation path stays byte-identical to today. The heavy and type-only
inputs (:class:`~maestro.gate_observation.GateRunObservation`, and the
:data:`~maestro.gate_convergence.ConvergenceDecision` /
:data:`~maestro.gate_observation.Attribution` ``Literal`` aliases) are imported
only under ``TYPE_CHECKING``, keeping this a zero-runtime-import leaf with no
cycle back into :mod:`maestro.evolution`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from maestro.gate_convergence import ConvergenceDecision
    from maestro.gate_observation import Attribution, GateRunObservation

#: The closed disposition vocabulary — exactly what happens to an implementation
#: attempt at its terminal. Each value is a distinct successor to the pre-131
#: binary "PR or demote":
AttemptVerdict = Literal[
    "LAND",            #: every gate is green — open the PR
    "RESUME",          #: stalled but strictly progressing — continue from this branch with memory
    "ASK_HUMAN",       #: blocked on a decision only a human can make — ask one question and pause
    "HOLD_INHERITED",  #: stalled on a base-inherited red — hold, retry once the base moves
    "HOLD_ENVIRONMENT",  #: the environment cannot reach a verdict — hold, charge nothing
    "ABANDON",         #: no progress and no continuation budget — spend the plan
]

#: The non-code-fixable gate kinds: a red no diff can clear because it is the
#: environment, not the code, that failed — a pytest collection/import abort, a
#: runner timeout, or a gate that could not be executed at all. Read only by
#: :func:`_is_environment_red`. Spelled as bare strings (not the
#: ``GateObservationKind`` literal, which is imported only under ``TYPE_CHECKING``)
#: so this module keeps zero runtime import of :mod:`maestro.gate_observation`.
_ENVIRONMENT_KINDS: frozenset[str] = frozenset(
    {"COLLECTION_ERROR", "TIMEOUT", "UNAVAILABLE"},
)

#: Hard cap on the failing identities enumerated into an ``ASK_HUMAN`` question.
#: The question is destined for ``ask_thomas`` — a prompt/message — so it obeys
#: CLAUDE.md's context-bloat invariant that every string reaching a prompt carry
#: a load-bearing cap, mirroring the bounds in :mod:`maestro.gate_observation`.
_MAX_QUESTION_IDENTITIES = 10


@dataclass(frozen=True)
class AttemptDisposition:
    """One terminal disposition of an implementation attempt.

    ``verdict`` is the closed :data:`AttemptVerdict`. ``question`` carries the one
    specific question to put to the human on ``ASK_HUMAN`` and is ``None`` for
    every other verdict. ``carry_context`` is ``True`` only on ``RESUME``, the
    single verdict that continues from the existing branch with the prior
    attempt's memory rather than cold-restarting. ``reason`` is a short
    human-readable justification for the chosen verdict.
    """

    verdict: AttemptVerdict
    question: str | None
    carry_context: bool
    reason: str


def _is_environment_red(observation: GateRunObservation) -> bool:
    """Return whether every red gate in this run is a non-code-fixable one.

    ``True`` iff the run has at least one non-``CLEAN`` gate and *every* such
    gate's ``kind`` is in :data:`_ENVIRONMENT_KINDS` — a red no code change can
    clear (a collection/import abort, a timeout, an unrunnable gate). A run with
    any ordinary ``FAILED`` / ``COVERAGE`` red is code-fixable and returns
    ``False``; an all-``CLEAN`` run returns ``False`` (it is not red at all). Its
    non-test call site is :func:`classify_attempt`'s ``ASK_HUMAN`` guard, which
    uses it to tell a red that warrants asking a human from an ordinary code red.
    Pure.
    """
    red_gates = [gate for gate in observation.gates if gate.kind != "CLEAN"]
    return bool(red_gates) and all(
        gate.kind in _ENVIRONMENT_KINDS for gate in red_gates
    )


def classify_attempt(
    observation: GateRunObservation | None,
    prior_failing: frozenset[str],
    *,
    outcome: ConvergenceDecision,
    attribution: Attribution | None,
    failure_count: int,
    inherited_hold_count: int,
    resume_count: int,
    max_implementation_attempts: int,
    max_resume_attempts: int,
) -> AttemptDisposition:
    """Map a terminal implementation attempt to exactly one :class:`AttemptDisposition`.

    Pure, zero-I/O, zero-SDK. It *consumes* the convergence owner's outputs — it
    does not re-derive the green/continue/stuck decision (``outcome``) or the
    base-vs-diff ``attribution`` — and reads the plan's own counts and budgets. The
    current failing-identity set is read from ``observation.progress_signature()``
    — the position-free vocabulary, so a line shift does not rename a surviving
    failure (#4309); unlike the convergence loop it has no count fallback, so a
    run whose enumeration hit the parser cap still compares capped windows and
    may read a shrinking >50-failure set as not-a-subset (out of scope for #4309:
    the persisted baseline carries no count) — (empty
    when there is no terminal observation, e.g. an unwrapped transport error) and
    compared against ``prior_failing`` — the set the previous attempt carried — to
    tell a strictly-progressing stall from a recurring-unchanged one, mirroring how
    :func:`maestro.gate_convergence.plan_next_round` reads a strict subset as
    progress.

    Rules, applied in order (a flat ordered guard chain, like
    :func:`~maestro.gate_convergence.plan_next_round`):

    - ``outcome == "green"`` → ``LAND`` (every gate is clean — open the PR).
    - an ``ENVIRONMENT`` attribution → ``HOLD_ENVIRONMENT``, *unconditionally*:
      no ``prior_failing``, ``failure_count`` or hold budget is consulted, because
      the red belongs to an environment the oracle measured as unable to reach a
      verdict at all, and no attempt count may be charged against it. Ranked
      immediately after ``green`` for two reasons: nothing below it can be a
      better answer for a red no diff and no base owns, and it keeps
      ``ENVIRONMENT`` out of the ``ASK_HUMAN`` guard below, whose
      ``attribution not in ("ATTRIBUTED", "INHERITED")`` test would otherwise
      admit it.
    - an ``INHERITED`` attribution with the inherited-hold budget unspent
      (``inherited_hold_count + 1 < max_implementation_attempts``) →
      ``HOLD_INHERITED``. This preserves the existing inline-hold semantics
      exactly: hold the plan until the base moves rather than blaming the diff for
      the base's red.
    - a strictly progressing stall the diff owns (the current failing set is a
      strict subset of ``prior_failing``, the ``attribution`` is *not*
      ``INHERITED``) with the resume budget unspent
      (``resume_count < max_resume_attempts``) → ``RESUME`` (``carry_context=True``):
      continue from this branch with memory rather than cold-restarting. A
      base-``INHERITED`` red is excluded — continuing from this branch cannot clear
      a failure that lives in the base, so it is handled solely by the
      ``HOLD_INHERITED`` rule above (while the hold budget is unspent) and otherwise
      abandoned.
    - a recurring-unchanged, non-code-fixable red the diff is *not provably
      responsible for* (the current failing set equals a non-empty ``prior_failing``,
      the ``attribution`` is neither ``ATTRIBUTED`` nor ``INHERITED``, and every red
      gate is environment/collection/timeout-classed, per :func:`_is_environment_red`)
      → ``ASK_HUMAN``, carrying a formulated ``question``: no diff can clear this, so
      ask the human rather than burn attempts. A red the diff *is* provably
      responsible for (``attribution == "ATTRIBUTED"``) is code-fixable — even a
      collection/import/fixture ``COLLECTION_ERROR``, which is frequently the diff's
      own bad import or broken fixture — so it is never escalated as an unfixable
      environment red; and a base-``INHERITED`` red is owned by the hold-then-abandon
      track above rather than surfaced to the human here. Both fall through to
      ``ABANDON``.
    - otherwise → ``ABANDON`` (no continuation condition held — spend the plan).

    ``observation is None`` (no terminal gate run) contributes an empty current
    failing set, so it can satisfy neither the ``RESUME`` progress guard nor the
    ``ASK_HUMAN`` recurrence guard and falls through to ``ABANDON``.
    """
    if outcome == "green":
        return AttemptDisposition(
            "LAND",
            None,
            False,
            "Every quality gate is clean; open the PR.",
        )
    if attribution == "ENVIRONMENT":
        return AttemptDisposition(
            "HOLD_ENVIRONMENT",
            None,
            False,
            "The local quality gate cannot reach a verdict, so this red is the "
            "environment's rather than the diff's; holding the plan and charging "
            "no attempt until the environment reads sound again.",
        )
    if (
        attribution == "INHERITED"
        and inherited_hold_count + 1 < max_implementation_attempts
    ):
        return AttemptDisposition(
            "HOLD_INHERITED",
            None,
            False,
            "Quality-gate red is inherited from the base, not this diff; holding "
            "the plan until origin/main advances "
            f"(hold {inherited_hold_count + 1} of {max_implementation_attempts}).",
        )
    current_failing = (
        observation.progress_signature() if observation is not None else frozenset()
    )
    if (
        observation is not None
        and attribution != "INHERITED"
        and current_failing < prior_failing
        and resume_count < max_resume_attempts
    ):
        return AttemptDisposition(
            "RESUME",
            None,
            True,
            f"Stall is strictly progressing ({len(current_failing)} of "
            f"{len(prior_failing)} prior failures remain); resuming from this "
            f"branch (resume {resume_count + 1} of {max_resume_attempts}).",
        )
    if (
        observation is not None
        and prior_failing
        and current_failing == prior_failing
        and attribution not in ("ATTRIBUTED", "INHERITED")
        and _is_environment_red(observation)
    ):
        failing_list = ", ".join(
            sorted(current_failing)[:_MAX_QUESTION_IDENTITIES],
        )
        question = (
            "Implementation keeps stalling on the same environment/infrastructure "
            f"red that no code change can fix: {failing_list}. This needs an "
            "operational decision — is the environment being repaired, or should "
            "the plan be abandoned?"
        )
        return AttemptDisposition(
            "ASK_HUMAN",
            question,
            False,
            "Recurring environment/infrastructure red no code change can clear; "
            "asking the human rather than burning attempts.",
        )
    return AttemptDisposition(
        "ABANDON",
        None,
        False,
        "No continuation condition held after "
        f"{failure_count} failed attempt(s); abandoning the plan.",
    )
